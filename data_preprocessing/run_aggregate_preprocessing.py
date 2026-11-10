"""Refresh derived cleaning, normalization and token fields from aggregate SQLite."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import sqlite3
import sys

import jieba

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from alias_normalization import AliasNormalizer, annotated_tokens, PROTECTED_SIGNAL_WORDS
from prepare_text import read_wordlist
from social_text_cleaning import MentionIndex, build_post_match_index, clean_social_text, match_repost_segments
from topic_relevance import classify_source_topic

DEFAULT_INPUT = BASE / 'data_preprocessing/outputs/reviewed_full_20261109_cleaning_final/research_dataset.sqlite3'
DEFAULT_LONGTEXT = BASE / 'data_preprocessing/outputs/reviewed_full_20261109_cleaning_final/input_snapshot/longtext.json'
RESOURCES = BASE / 'data_preprocessing/text_resources'
WIKI = RESOURCES / 'IdentityV_BWIKI_Jieba_Lexicon_20261109'

def dump(value): return json.dumps(value, ensure_ascii=False, separators=(',', ':'))
def sha(text): return hashlib.sha256((text or '').encode('utf-8')).hexdigest()

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--input', type=Path, default=DEFAULT_INPUT)
    ap.add_argument('--longtext-snapshot', type=Path, default=DEFAULT_LONGTEXT)
    ap.add_argument('--output', type=Path, required=True, help='新 SQLite 输出路径')
    args = ap.parse_args()
    source, output = args.input.resolve(), args.output.resolve()
    if not source.is_file(): raise FileNotFoundError(source)
    if output.exists(): raise FileExistsError(f'Refusing to overwrite {output}')
    output.parent.mkdir(parents=True, exist_ok=True)

    src = sqlite3.connect(f'file:{source.as_posix()}?mode=ro', uri=True)
    src.row_factory = sqlite3.Row
    dst = sqlite3.connect(output)
    src.backup(dst); src.close()
    dst.row_factory = sqlite3.Row
    dst.execute('PRAGMA foreign_keys=ON')

    entries = json.loads((RESOURCES/'synonyms.json').read_text(encoding='utf-8-sig'))
    manuals = json.loads((RESOURCES/'manual_resolutions.json').read_text(encoding='utf-8-sig'))
    manuals_by_id = {}
    for item in manuals: manuals_by_id.setdefault(str(item['weibo_id']), []).append(item)
    tokenizer = jieba.Tokenizer()
    dictionary = {}
    for path in [RESOURCES/'dwrg_dict.txt', WIKI/'dwrg_dict_wiki_additions.txt']:
        if not path.is_file(): continue
        for line in path.read_text(encoding='utf-8-sig').splitlines():
            if not line.strip() or line.lstrip().startswith('#'): continue
            cols = line.split()
            if len(cols) != 3: continue
            word, weight, category = cols
            if word in dictionary: continue
            tokenizer.add_word(word, int(weight), category); dictionary[word] = category
    normalizer = AliasNormalizer(entries)
    for item in entries: tokenizer.add_word(item['canonical'], int(item['weight']), item['category'])
    stop = {word.casefold() for word in read_wordlist(RESOURCES/'stopwords.txt')}
    keep = read_wordlist(RESOURCES/'keep_single_chars.txt')
    for word in stop | set(PROTECTED_SIGNAL_WORDS):
        if len(word) > 1: tokenizer.add_word(word, 100000, 'signal')

    posts = dst.execute('SELECT weibo_id,content,created_at,payload FROM posts ORDER BY weibo_id').fetchall()
    comments = dst.execute('SELECT dataset_key,weibo_id,comment_level,content,payload FROM comments ORDER BY dataset_key').fetchall()
    post_records, post_payloads = [], {}
    longtext_map = {}
    if args.longtext_snapshot.is_file():
        snapshot = json.loads(args.longtext_snapshot.read_text(encoding='utf-8-sig'))
        for record in snapshot.get('filtered_rows', []):
            if record.get('补充正文状态') in ('supplemented_complete','already_complete_in_main_db') and record.get('补充正文'):
                longtext_map[str(record.get('微博ID') or '')] = str(record['补充正文'])
    for row in posts:
        payload = json.loads(row['payload'] or '{}'); post_payloads[str(row['weibo_id'])] = payload
        processing = longtext_map.get(str(row['weibo_id'])) or ((payload.get('preprocessing') or {}).get('processing_text') or row['content'] or '')
        post_records.append({**payload, 'weibo_id': str(row['weibo_id']), 'content': processing, 'created_at': row['created_at'] or ''})
    comment_records, comment_payloads = [], {}
    for row in comments:
        payload = json.loads(row['payload'] or '{}'); comment_payloads[str(row['dataset_key'])] = payload
        comment_records.append({**payload, 'dataset_key': str(row['dataset_key']), 'tweet_id': str(row['weibo_id'] or ''),
                                'comment_level': row['comment_level'], 'content': row['content'] or ''})
    mention_index = MentionIndex(post_records + comment_records)
    post_index = build_post_match_index(post_records)

    # Reuse occurrence-scoped human decisions already recorded in the aggregate
    # payload. This preserves reviewed labels without transferring by nickname.
    preserved_manual = 0
    def restore_existing_manual(record_id, raw, clean_text, old_matches):
        nonlocal preserved_manual
        hits = Counter()
        for match in sorted(old_matches or [], key=lambda item: item.get('source_start', 0)):
            if match.get('resolution') != 'human_review': continue
            surface = str(match.get('configured_surface') or match.get('surface') or '')
            if not surface: continue
            key = surface.casefold()
            if key not in normalizer.entries: continue
            occurrences = [m.start() for m in re.finditer(re.escape(surface), clean_text, re.I)]
            existing = hits[key]
            if existing >= len(occurrences): continue
            hits[key] += 1
            occurrence = hits[key]
            normalizer.manual_resolutions[(str(record_id),key,occurrence)] = {
                **normalizer.entries[key],
                **{k:match[k] for k in ('canonical','term_type','category','sensitive','toxicity','definition','review_status') if k in match},
                'weibo_id':str(record_id),'surface':surface,'occurrence':occurrence,
                'expected_content_sha256':sha(raw),'source_file':match.get('manual_resolution_source','aggregate_database'),
                'human_note':match.get('human_note','preserved_from_aggregate')}
            preserved_manual += 1

    excluded_ids = set()
    for row in posts:
        payload = post_payloads[str(row['weibo_id'])]
        if classify_source_topic(payload).get('excluded'): excluded_ids.add(str(row['weibo_id']))
    # The aggregate schema does not retain the crawler's isLongText flag. Never
    # infer platform-long status from body length or long_text_complete.
    marker_rows = sum(bool(post_payloads[str(r['weibo_id'])].get('isLongText') is True) for r in posts)

    dst.executescript('''
      CREATE TABLE IF NOT EXISTS preprocessing_run(
        singleton INTEGER PRIMARY KEY CHECK(singleton=1), status TEXT NOT NULL, phase TEXT NOT NULL,
        completed INTEGER NOT NULL, total INTEGER NOT NULL, excluded_posts INTEGER NOT NULL,
        source_longtext_markers INTEGER NOT NULL, updated_at TEXT NOT NULL, detail TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS preprocessing_exclusions(
        record_type TEXT NOT NULL, record_id TEXT NOT NULL, weibo_id TEXT, reason TEXT NOT NULL,
        source_topic TEXT, original_payload TEXT NOT NULL, PRIMARY KEY(record_type,record_id));
    ''')
    total = len(posts) + len(comments)
    def progress(status, phase, completed, detail=''):
        dst.execute('INSERT OR REPLACE INTO preprocessing_run VALUES(1,?,?,?,?,?,?,?,?)',
            (status, phase, completed, total, len(excluded_ids), marker_rows,
             datetime.now(timezone.utc).isoformat(), detail))
        dst.commit()
        print(f'{phase}: {completed}/{total} ({completed/total:.1%}) | {detail}', flush=True)
    progress('running','starting',0,'input snapshot copied; source database is read-only')

    stats = Counter(); todo = 0
    # Existing per-post human decisions are retained only when source hash matches.
    for row in posts:
        pid = str(row['weibo_id']); source_raw = row['content'] or ''; payload = post_payloads[pid]
        if pid in excluded_ids:
            dst.execute('INSERT OR REPLACE INTO preprocessing_exclusions VALUES(?,?,?,?,?,?)',
                ('post',pid,pid,'source_supertopic_exact_match',str(payload.get('来源超话') or ''),dump(payload)))
            continue
        processing = longtext_map.get(pid) or ((payload.get('preprocessing') or {}).get('processing_text') or source_raw)
        cleaning = clean_social_text(processing, mention_index)
        restore_existing_manual(pid,source_raw,cleaning['content_clean'],(payload.get('preprocessing') or {}).get('alias_matches',[]))
        chain = match_repost_segments(cleaning['repost_segments'], post_index, current_id=pid,
            current_author=str(payload.get('user_name') or ''), current_time=str(row['created_at'] or ''))
        for manual in manuals_by_id.get(pid,[]):
            if manual.get('expected_content_sha256') == sha(source_raw):
                normalizer.manual_resolutions[(pid,manual['surface'].casefold(),manual.get('occurrence',1))] = manual
        normalized, matches = normalizer.normalize(cleaning['content_clean'], pid, sha(source_raw))
        tokens, attributed, annotations = annotated_tokens(normalized,matches,tokenizer,stop,keep,dictionary,normalizer.canonical_forms)
        emojis = re.findall(r'\[[^\[\]\s]{1,24}\]|[0-9#*]\ufe0f?\u20e3|[\U0001f000-\U0001faff\u2600-\u27bf](?:[\ufe0f\U0001f3fb-\U0001f3ff]|\u200d[\U0001f000-\U0001faff\u2600-\u27bf])*',cleaning['content_clean'])
        tokens=' '.join([tokens]+emojis).strip()
        flags=sorted(set(cleaning['social_cleaning_flags']+[f for seg in chain for f in seg.get('social_cleaning_flags',[])]))
        if any(seg.get('match_status')=='needs_manual_review' for seg in chain): flags.append('repost_match_review_required')
        prep={'content_clean':cleaning['content_clean'],'content_normalized':normalized,'tokens':tokens,
          'content_classification':cleaning['content_clean'],'tokens_classification_attributed':attributed,
          'alias_matches':matches,'token_annotations':annotations,'emoji_features':emojis,
          'content_sha256':sha(processing),'content_source_sha256':sha(source_raw),
          'processing_text_sha256':sha(processing),'processing_text':processing,
          'processing_text_source':'verified_existing_longtext_supplement' if pid in longtext_map else (payload.get('preprocessing') or {}).get('processing_text_source','original_content'),
          'longtext_metadata':(payload.get('preprocessing') or {}).get('longtext_metadata'),
          'platform_removed_fragments':cleaning['removed_platform_fragments'],
          'column_reference':cleaning['column_reference'],'repost_segments':chain,
          'repost_match_status':dict(Counter(x['match_status'] for x in chain)),
          'social_cleaning_flags':sorted(set(flags)), 'text_preparation_flags':sorted(set(flags)),
          'source_topic_relevance':classify_source_topic(payload)}
        if processing != source_raw:
            payload['content_original_source']=payload.get('content_original_source',source_raw)
            payload['content_original_source_sha256']=payload.get('content_original_source_sha256',sha(source_raw))
        payload['content']=processing
        payload['content_clean']=prep['content_clean']; payload['preprocessing']=prep
        dst.execute('UPDATE posts SET content=?,content_clean=?,content_normalized=?,tokens=?,content_classification=?,alias_matches=?,platform_removed_fragments=?,column_reference=?,repost_segments=?,repost_match_status=?,social_cleaning_flags=?,payload=? WHERE weibo_id=?',
          (processing,prep['content_clean'],normalized,tokens,prep['content_classification'],dump(matches),dump(prep['platform_removed_fragments']),dump(prep['column_reference']),dump(chain),dump(prep['repost_match_status']),dump(prep['social_cleaning_flags']),dump(payload),pid))
        stats['posts_processed']+=1;stats['cleaned_posts']+=prep['content_clean']!=processing
        stats['completed_longtext_posts']+=pid in longtext_map
        todo+=1
        if todo%250==0: progress('running','text_preparation',todo)
    progress('running','comments',todo)
    for row in comments:
        key=str(row['dataset_key']); pid=str(row['weibo_id'] or ''); payload=comment_payloads[key]; raw=row['content'] or ''
        if pid in excluded_ids:
            dst.execute('INSERT OR REPLACE INTO preprocessing_exclusions VALUES(?,?,?,?,?,?)',
                ('comment',key,pid,'parent_post_source_supertopic_exact_match','',dump(payload)))
            continue
        cleaning=clean_social_text(raw,mention_index)
        restore_existing_manual(key,raw,cleaning['content_clean'],payload.get('alias_matches',[]))
        normalized,matches=normalizer.normalize(cleaning['content_clean'],key,sha(raw))
        tokens,attributed,annotations=annotated_tokens(normalized,matches,tokenizer,stop,keep,dictionary,normalizer.canonical_forms)
        emojis=re.findall(r'\[[^\[\]\s]{1,24}\]|[0-9#*]\ufe0f?\u20e3|[\U0001f000-\U0001faff\u2600-\u27bf](?:[\ufe0f\U0001f3fb-\U0001f3ff]|\u200d[\U0001f000-\U0001faff\u2600-\u27bf])*',cleaning['content_clean'])
        tokens=' '.join([tokens]+emojis).strip()
        prep={'content_clean':cleaning['content_clean'],'content_normalized':normalized,'tokens':tokens,
          'content_classification':cleaning['content_clean'],'tokens_classification_attributed':attributed,
          'alias_matches':matches,'token_annotations':annotations,'emoji_features':emojis,
          'content_sha256':sha(raw),'processing_text_sha256':sha(raw),
          'platform_removed_fragments':cleaning['removed_platform_fragments'],
          'column_reference':cleaning['column_reference'],'repost_segments':cleaning['repost_segments'],
          'repost_match_status':{},'social_cleaning_flags':cleaning['social_cleaning_flags'],
          'text_preparation_flags':cleaning['social_cleaning_flags']}
        payload.update(prep)
        dst.execute('UPDATE comments SET content_clean=?,content_normalized=?,tokens=?,content_classification=?,alias_matches=?,platform_removed_fragments=?,column_reference=?,repost_segments=?,repost_match_status=?,social_cleaning_flags=?,payload=? WHERE dataset_key=?',
          (prep['content_clean'],normalized,tokens,prep['content_classification'],dump(matches),dump(prep['platform_removed_fragments']),dump(prep['column_reference']),dump(prep['repost_segments']),dump(prep['repost_match_status']),dump(prep['social_cleaning_flags']),dump(payload),key))
        stats['comments_processed']+=1;stats['cleaned_comments']+=prep['content_clean']!=raw
        todo+=1
        if todo%250==0: progress('running','text_preparation',todo)
    progress('running','apply_topic_filter',todo)
    if excluded_ids:
        keys=[str(r[0]) for r in dst.execute('SELECT dataset_key FROM comments WHERE weibo_id IN (%s)' % ','.join('?'*len(excluded_ids)),tuple(excluded_ids))]
        dst.executemany('DELETE FROM relations WHERE dataset_key=?',[(k,) for k in keys])
        dst.executemany('DELETE FROM record_quality WHERE dataset_key=?',[(k,) for k in keys])
        dst.executemany('DELETE FROM comments WHERE dataset_key=?',[(k,) for k in keys])
        dst.executemany('UPDATE relations SET verified_weibo_id=NULL WHERE verified_weibo_id=?',[(pid,) for pid in excluded_ids])
        dst.executemany('DELETE FROM posts WHERE weibo_id=?',[(pid,) for pid in excluded_ids])
    stats['preserved_human_resolution_occurrences']=preserved_manual
    dst.execute("INSERT OR REPLACE INTO preprocessing_run VALUES(1,'complete','complete',?,?,?,?,?,?)",
        (total,total,len(excluded_ids),marker_rows,datetime.now(timezone.utc).isoformat(),dump(dict(stats))))
    dst.commit()
    check=dst.execute('PRAGMA integrity_check').fetchone()[0]
    fk=dst.execute('PRAGMA foreign_key_check').fetchall()
    if check!='ok' or fk: raise RuntimeError(f'Database validation failed: {check}, foreign key violations={len(fk)}')
    print('COMPLETE',json.dumps({'output':str(output),'posts':dst.execute('select count(*) from posts').fetchone()[0],
      'comments':dst.execute('select count(*) from comments').fetchone()[0],'excluded_posts':len(excluded_ids),
      'excluded_records':dst.execute('select count(*) from preprocessing_exclusions').fetchone()[0],
      'platform_longtext_markers_in_aggregate':marker_rows,'stats':dict(stats),'integrity':check,'foreign_key_violations':len(fk)},ensure_ascii=False),flush=True)
    dst.close()

if __name__=='__main__': main()
