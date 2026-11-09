"""Create a lossless, offline post/comment/thread dataset without inventing IDs."""
import argparse
from collections import Counter, defaultdict
import csv
import hashlib
import io
import json
from pathlib import Path
import re
import shutil
import sqlite3
import zipfile

BASE = Path(__file__).resolve().parent.parent


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def write_jsonl(path, rows):
    with path.open('w', encoding='utf-8', newline='\n') as f:
        for row in rows:
            f.write(dumps(row) + '\n')


def excel_export_text(value):
    """Known workbook formula-protection transform; never alter the raw payload."""
    return "'" + value if isinstance(value, str) and value.startswith(('=', '+', '-', '@')) else value


def check_relations(targets, comments, post_ids):
    """Root-thread membership is distinct from an immediate reply-to relation."""
    index = {}
    for c in comments:
        key = c.get('record_key')
        if not key or key in index:
            raise ValueError('Missing or duplicate local comment record_key')
        index[key] = c
    tids = [t['tweet_id'] for t in targets]
    if len(tids) != len(set(tids)):
        raise ValueError('Duplicate tweet targets')
    links, pending = [], []
    for c in comments:
        mid, key, level = c['tweet_id'], c['record_key'], c['comment_level']
        problems = []
        if mid not in set(tids) or mid not in post_ids:
            problems.append('post_not_found')
        root_key = c.get('root_record_key') or c.get('root_comment_key')
        if c.get('root_record_key') and c.get('root_comment_key') and c['root_record_key'] != c['root_comment_key']:
            problems.append('conflicting_root_keys')
        root_status = 'not_applicable'
        if level == 2:
            root = index.get(root_key)
            if not root:
                problems.append('root_not_found')
            elif root['tweet_id'] != mid or root['comment_level'] != 1:
                problems.append('root_wrong_post_or_level')
            else:
                root_status = 'verified_from_collection_metadata'
        elif level == 1:
            if root_key != key:
                problems.append('level1_root_key_not_self')
        else:
            problems.append('unknown_comment_level')
        # Never interpret a root key, DOM index, nickname or mention as a parent ID.
        parent_key = c.get('parent_comment_key')
        parent_status = 'not_applicable' if level == 1 else 'pending_verification'
        if parent_key:
            parent = index.get(parent_key)
            if not parent or parent['tweet_id'] != mid:
                problems.append('direct_parent_not_found_or_wrong_post')
            elif parent_key == key:
                problems.append('direct_parent_is_self')
            elif level == 2 and parent.get('root_record_key') != root_key:
                problems.append('direct_parent_wrong_thread')
            else:
                parent_status = 'verified_from_collection_metadata'
        row = dict(record_key=key, weibo_id=mid, comment_level=level,
                   root_record_key=root_key, direct_parent_record_key=parent_key,
                   thread_relation_status=root_status,
                   direct_parent_status=parent_status,
                   platform_id_status='available' if c.get('platform_comment_id') else 'pending_verification',
                   relationship_issues=problems)
        links.append(row)
        if problems or not c.get('platform_comment_id') or parent_status == 'pending_verification':
            pending.append(row)
    return links, pending


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--resources-zip', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--comments', type=Path, default=BASE / 'WeiboSpider/output/comment-full-20261103/comments.json')
    parser.add_argument('--workbook', type=Path, default=BASE / 'outputs/01a1a1eb-2ffa-7ad3-85cc-9ea83f93f3c7/weibo-comment-crawl-summary.xlsx')
    parser.add_argument('--posts', type=Path, default=BASE / 'data_preprocessing/outputs/八爪鱼数据_20261024/八爪鱼合并数据.csv')
    args = parser.parse_args()
    import jieba
    import openpyxl
    from prepare_text import read_wordlist
    from alias_normalization import annotated_tokens, PROTECTED_SIGNAL_WORDS
    from social_text_cleaning import build_post_match_index, clean_social_text, match_repost_segments

    sources = {name: p.resolve() for name, p in vars(args).items() if name != 'output_dir'}
    hashes = {name: digest(p) for name, p in sources.items()}
    out = args.output_dir.resolve()
    if out.exists():
        raise ValueError('Use a new output directory; existing outputs are never overwritten')
    data = json.loads(sources['comments'].read_text(encoding='utf-8'))
    targets = data['targets']
    comments = []
    for t in targets:
        for c in t['comments']:
            if c['tweet_id'] != t['tweet_id']:
                raise ValueError('Nested target/comment tweet_id conflict')
            comments.append(c)
    csv.field_size_limit(100_000_000)
    with sources['posts'].open(encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        post_fields, posts = reader.fieldnames, list(reader)
    post_ids = {p['weibo_id'] for p in posts}
    if len(post_ids) != len(posts) or '' in post_ids:
        raise ValueError('Post IDs must be nonempty and unique')
    repost_match_index = build_post_match_index(posts)
    links, pending = check_relations(targets, comments, post_ids)
    if any(x['relationship_issues'] for x in links):
        raise ValueError('Broken relations: inspect source data before generating outputs')

    # Reconcile the frozen Excel export against full collection metadata by exact values.
    wb = openpyxl.load_workbook(sources['workbook'], read_only=True, data_only=True)
    records = iter(wb['评论明细'].values)
    headers = list(next(records))
    excel_rows = [dict(zip(headers, row)) for row in records if row and any(v is not None for v in row)]
    def signature(c):
        return (c['tweet_id'], c['comment_level'], c['user_id'], c['nick_name'], c['content'], c['created_at_display'])
    def exported_signature(c):
        return tuple(excel_export_text(v) for v in signature(c))
    def excel_signature(c):
        return tuple(c[k] for k in ['微博ID', '层级', '评论用户ID', '评论昵称', '评论正文', '评论时间'])
    by_sig = defaultdict(list)
    for c in comments:
        by_sig[exported_signature(c)].append(c)
    if Counter(map(exported_signature, comments)) != Counter(map(excel_signature, excel_rows)):
        raise ValueError('Excel and collection JSON populations differ')
    excel_map = defaultdict(list)
    excel_output = []
    for i, r in enumerate(excel_rows, 2):
        candidates = by_sig[excel_signature(r)]
        excel_output.append(dict(source_excel_row=i, fields=r,
            record_key=candidates[0]['record_key'] if len(candidates) == 1 else None,
            candidate_record_keys=[c['record_key'] for c in candidates],
            match_status='verified_exact_fields' if len(candidates) == 1 else 'pending_verification'))
        for candidate in candidates:
            excel_map[candidate['record_key']].append((i, r))
    wb.close()

    # These are data annotations, not executable instructions or global synonym rules.
    review_rows, review_pending = [], []
    with zipfile.ZipFile(sources['resources_zip']) as z:
        names = z.namelist()
        xlsx_name = next(n for n in names if n.endswith('.xlsx'))
        review_wb = openpyxl.load_workbook(io.BytesIO(z.read(xlsx_name)), read_only=True, data_only=True)
        review_index = defaultdict(list)
        for c in comments:
            # Review contexts may derive from either the raw JSON or protected Excel.
            for key in {(c['tweet_id'], c['comment_level'], c['nick_name'], c['content']),
                        (c['tweet_id'], c['comment_level'], excel_export_text(c['nick_name']), excel_export_text(c['content']))}:
                review_index[key].append(c)
        for excel_row, row in enumerate(review_wb['逐条核对清单'].values, 1):
            if excel_row == 1 or len(row) < 3 or not row[0]:
                continue
            term, context, label = row[:3]
            # The workbook stores literal backslash-u3000 and backslash-n separators.
            text = context.replace('\\u3000', '\u3000').replace('\\n', '\n')
            m = re.fullmatch(r'微博ID：(\d+)\s+评论层级：(\d+)\s+昵称：(.*?)\n原评论：(.*)\n微博链接：(\S+)\s*', text, re.S)
            candidates = review_index.get((m[1], int(m[2]), m[3], m[4]), []) if m else []
            status = 'verified_exact_context' if len(candidates) == 1 else 'pending_verification'
            item = dict(annotation_key=f'review:{excel_row}', source_sheet='逐条核对清单', source_excel_row=excel_row,
                        term=term, original_context=context, human_label=label, match_status=status,
                        record_key=candidates[0]['record_key'] if len(candidates) == 1 else None,
                        candidate_count=len(candidates), candidate_record_keys=[c['record_key'] for c in candidates])
            if len(candidates) == 1 and m[5] != next(t['url'] for t in targets if t['tweet_id'] == m[1]):
                item.update(match_status='pending_verification', record_key=None, match_reason='url_conflict')
            review_rows.append(item)
            if item['match_status'] != 'verified_exact_context':
                review_pending.append(item)
        review_wb.close()
        out.mkdir(parents=True)
        resources = out / 'resource_snapshot'
        resources.mkdir()
        allowed = {'README.md', 'dwrg_dict.txt', 'glossary_review.json', 'keep_single_chars.txt',
                   'manual_resolutions.json', 'player_id_mapping.json', 'stopwords.txt', 'synonyms.json'}
        for name in sorted(allowed):
            (resources / name).write_bytes(z.read(name))
        # Rename only the copied workbook; retain the archive member name in the manifest.
        (resources / 'reviewed_annotations.xlsx').write_bytes(z.read(xlsx_name))

    tokenizer = jieba.Tokenizer()
    dictionary = {}
    for line in (resources / 'dwrg_dict.txt').read_text(encoding='utf-8-sig').splitlines():
        if line.strip() and not line.lstrip().startswith('#'):
            word, weight, tag = line.split()
            tokenizer.add_word(word, int(weight), tag)
            dictionary[word] = tag
    stopwords = {w.casefold() for w in read_wordlist(resources / 'stopwords.txt')}
    keep = read_wordlist(resources / 'keep_single_chars.txt')
    for word in stopwords | set(PROTECTED_SIGNAL_WORDS):
        if len(word) > 1:
            tokenizer.add_word(word, 100000, 'signal')
    def prepare(text, record=None):
        record = record or {}
        social = clean_social_text(text)
        clean = social['content_clean']
        repost_segments = match_repost_segments(social['repost_segments'], repost_match_index,
            current_id=record.get('weibo_id', ''),
            current_author=record.get('user_name') or record.get('nick_name', ''),
            current_time=record.get('created_at') or record.get('created_at_display', ''))
        tokens, _, _ = annotated_tokens(clean, [], tokenizer, stopwords, keep, dictionary)
        match_counts=dict(Counter(s['match_status'] for s in repost_segments))
        flags=list(social['social_cleaning_flags'])
        if any(s['match_status']=='needs_manual_review' for s in repost_segments):
            flags.append('repost_match_review_required')
        flags.extend(segment_flag for segment in repost_segments for segment_flag in segment.get('social_cleaning_flags', []))
        return dict(content_clean=clean, tokens=tokens,
                    content_sha256=hashlib.sha256(text.encode('utf-8')).hexdigest(),
                    text_preparation_flags=sorted(set(flags+[k for k, value in [('empty_after_cleaning', clean), ('empty_tokens', tokens)] if not value])),
                    platform_removed_fragments=social['removed_platform_fragments'],
                    column_reference=social['column_reference'],repost_segments=repost_segments,
                    repost_match_status=match_counts,social_cleaning_flags=sorted(set(flags)))
    annotations_by_key = defaultdict(list)
    for a in review_rows:
        if a['record_key']:
            annotations_by_key[a['record_key']].append(a['annotation_key'])
    enriched = []
    for c, link in zip(comments, links):
        excel_matches = excel_map[c['record_key']]
        added = {**prepare(c['content'], c), 'relation_validation': link,
                 'annotation_keys': annotations_by_key[c['record_key']],
                 'source_excel_row': excel_matches[0][0] if len(excel_matches) == 1 else None,
                 'source_excel_candidate_rows': [r[0] for r in excel_matches],
                 'source_excel_fields': excel_matches[0][1] if len(excel_matches) == 1 else None}
        if set(c) & set(added):
            raise ValueError('Extension field collides with original schema')
        enriched.append({**c, **added})
    post_out = []
    for p in posts:
        if 'preprocessing' in p:
            raise ValueError('Post preprocessing field already exists')
        post_out.append({**p, 'preprocessing': prepare(p['content'], p)})
    target_out = [{k: v for k, v in t.items() if k != 'comments'} for t in targets]
    write_jsonl(out / 'posts.jsonl', post_out)
    write_jsonl(out / 'targets.jsonl', target_out)
    write_jsonl(out / 'comments.jsonl', enriched)
    write_jsonl(out / 'comment_relations.jsonl', links)
    write_jsonl(out / 'comment_annotations.jsonl', review_rows)
    write_jsonl(out / 'source_excel_comments.jsonl', excel_output)
    write_jsonl(out / 'pending_verification.jsonl', pending)
    write_jsonl(out / 'pending_annotations.jsonl', review_pending)
    write_jsonl(out / 'collection_issues.jsonl', data.get('issues', []))

    db = sqlite3.connect(out / 'linked_dataset.sqlite3')
    db.execute('PRAGMA foreign_keys=ON')
    db.executescript('''
      CREATE TABLE posts(weibo_id TEXT PRIMARY KEY, payload TEXT NOT NULL);
      CREATE TABLE targets(tweet_id TEXT PRIMARY KEY REFERENCES posts(weibo_id), payload TEXT NOT NULL);
      CREATE TABLE comments(record_key TEXT PRIMARY KEY, tweet_id TEXT NOT NULL REFERENCES targets(tweet_id),
        comment_level INTEGER NOT NULL CHECK(comment_level IN (1,2)),
        comment_id TEXT, platform_comment_id TEXT, parent_comment_id TEXT,
        root_record_key TEXT REFERENCES comments(record_key) DEFERRABLE INITIALLY DEFERRED,
        parent_comment_key TEXT REFERENCES comments(record_key) DEFERRABLE INITIALLY DEFERRED,
        content TEXT NOT NULL, content_clean TEXT NOT NULL, tokens TEXT NOT NULL, payload TEXT NOT NULL);
      CREATE TABLE comment_relations(record_key TEXT PRIMARY KEY REFERENCES comments(record_key),
        weibo_id TEXT NOT NULL REFERENCES posts(weibo_id), root_record_key TEXT REFERENCES comments(record_key),
        direct_parent_record_key TEXT REFERENCES comments(record_key), thread_relation_status TEXT,
        direct_parent_status TEXT, platform_id_status TEXT, payload TEXT NOT NULL);
      CREATE TABLE comment_annotations(annotation_key TEXT PRIMARY KEY,
        record_key TEXT REFERENCES comments(record_key), source_excel_row INTEGER, term TEXT,
        human_label TEXT, match_status TEXT, payload TEXT NOT NULL);
      CREATE TABLE collection_issues(issue_id INTEGER PRIMARY KEY, tweet_id TEXT, payload TEXT NOT NULL);
      CREATE TABLE source_excel_comments(source_excel_row INTEGER PRIMARY KEY,
        record_key TEXT REFERENCES comments(record_key), match_status TEXT, payload TEXT NOT NULL);
      CREATE INDEX comment_tweet ON comments(tweet_id);
      CREATE INDEX comment_root ON comments(root_record_key);
    ''')
    with db:
        db.executemany('INSERT INTO posts VALUES (?,?)', [(p['weibo_id'], dumps(p)) for p in post_out])
        db.executemany('INSERT INTO targets VALUES (?,?)', [(t['tweet_id'], dumps(t)) for t in target_out])
        db.executemany('INSERT INTO comments VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', [
            (c['record_key'], c['tweet_id'], c['comment_level'], c['comment_id'], c['platform_comment_id'],
             c['parent_comment_id'], c['root_record_key'], c['parent_comment_key'], c['content'],
             c['content_clean'], c['tokens'], dumps(c)) for c in enriched])
        db.executemany('INSERT INTO comment_relations VALUES (?,?,?,?,?,?,?,?)', [
            (r['record_key'], r['weibo_id'], r['root_record_key'], r['direct_parent_record_key'],
             r['thread_relation_status'], r['direct_parent_status'], r['platform_id_status'], dumps(r)) for r in links])
        db.executemany('INSERT INTO comment_annotations VALUES (?,?,?,?,?,?,?)', [
            (a['annotation_key'], a['record_key'], a['source_excel_row'], a['term'], a['human_label'], a['match_status'], dumps(a)) for a in review_rows])
        db.executemany('INSERT INTO collection_issues VALUES (?,?,?)', [
            (i['issue_id'], i['tweet_id'], dumps(i)) for i in data.get('issues', [])])
        db.executemany('INSERT INTO source_excel_comments VALUES (?,?,?,?)', [
            (e['source_excel_row'], e['record_key'], e['match_status'], dumps(e)) for e in excel_output])
    foreign_keys = db.execute('PRAGMA foreign_key_check').fetchall()
    integrity = db.execute('PRAGMA integrity_check').fetchone()[0]
    db.close()

    # Read serialized outputs back and compare EVERY original field, including value types.
    def load_lines(name):
        return [json.loads(line) for line in (out / name).read_text(encoding='utf-8').splitlines()]
    saved_comments = load_lines('comments.jsonl')
    saved_posts = load_lines('posts.jsonl')
    def exact_fields(old, new):
        return all(k in new and type(new[k]) is type(v) and new[k] == v for k, v in old.items())
    original_fields_preserved = (len(comments) == len(saved_comments) and
        all(exact_fields(a, b) for a, b in zip(comments, saved_comments)) and
        len(posts) == len(saved_posts) and all(exact_fields(a, b) for a, b in zip(posts, saved_posts)))
    restored_targets = []
    saved_by_mid = defaultdict(list)
    for c in saved_comments:
        original = next(x for x in by_sig[exported_signature(c)] if x['record_key'] == c['record_key'])
        saved_by_mid[c['tweet_id']].append({k: c[k] for k in original})
    for t in load_lines('targets.jsonl'):
        restored_targets.append({**t, 'comments': saved_by_mid[t['tweet_id']]})
    target_roundtrip = restored_targets == targets
    inputs_unchanged = all(digest(p) == hashes[name] for name, p in sources.items())
    result = dict(posts=len(posts), collection_targets=len(targets),
        posts_without_collection_target=len(post_ids - set(t['tweet_id'] for t in targets)),
        comments=len(comments), level1_comments=sum(c['comment_level'] == 1 for c in comments),
        level2_replies=sum(c['comment_level'] == 2 for c in comments),
        verified_reply_root_links=sum(r['thread_relation_status'] == 'verified_from_collection_metadata' for r in links),
        direct_parent_pending=sum(r['direct_parent_status'] == 'pending_verification' for r in links),
        missing_platform_comment_ids=sum(not c['platform_comment_id'] for c in comments),
        relationship_errors=sum(bool(r['relationship_issues']) for r in links),
        annotations=len(review_rows), uniquely_linked_annotations=len(review_rows)-len(review_pending),
        pending_annotations=len(review_pending), original_field_names_values_types_preserved=original_fields_preserved,
        collection_hierarchy_roundtrip_equal=target_roundtrip,
        excel_comment_population_match_after_known_formula_protection=True,
        excel_formula_protected_rows=sum(signature(c) != exported_signature(c) for c in comments),
        excel_rows_with_ambiguous_record_match=sum(e['match_status'] == 'pending_verification' for e in excel_output),
        sqlite_foreign_key_violations=foreign_keys,
        sqlite_integrity_check=integrity, inputs_unchanged=inputs_unchanged,
        collection_status_counts=dict(Counter(t['status'] for t in targets)),
        collection_coverage=data.get('coverage'), exported_at=data.get('exported_at'),
        historical_completeness='not_established_by_browser_visible_collection')
    if not (original_fields_preserved and target_roundtrip and inputs_unchanged and not foreign_keys and integrity == 'ok'):
        raise ValueError('Output verification failed')
    (out / 'validation_report.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    manifest = dict(schema_version=1, sources={k: dict(path=str(p), sha256=hashes[k]) for k, p in sources.items()},
        collection_metadata={k: v for k, v in data.items() if k not in ('targets', 'issues')},
        original_post_columns=post_fields, source_excel_columns=headers,
        archive_review_member=xlsx_name, script_sha256=digest(Path(__file__)),
        processing=dict(cleaning='existing real_text_cleaning; no alias replacement', tokenizer='jieba '+jieba.__version__,
                        annotations='exact tweet_id + level + nickname + full raw content + URL; ambiguous matches pending',
                        global_manual_resolution_application=False),
        output_sha256={p.name: digest(p) for p in out.iterdir() if p.is_file()},
        resources_sha256={p.name: digest(p) for p in resources.iterdir()})
    (out / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    shutil.copyfile(Path(__file__), out / 'prepare_linked_dataset.py')
    print(json.dumps(result, ensure_ascii=True, indent=2))


if __name__ == '__main__':
    main()
