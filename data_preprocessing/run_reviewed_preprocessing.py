"""Lossless reviewed corpus preparation. Offline replay: --snapshot-dir <previous/input_snapshot>."""
import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import unicodedata
import zipfile

from alias_normalization import AliasNormalizer, annotated_tokens, PROTECTED_SIGNAL_WORDS
from prepare_text import read_wordlist
from social_text_cleaning import (build_post_match_index, clean_social_text,
                                  match_repost_segments)

BASE = Path(__file__).resolve().parent.parent
ZIP = BASE/'data_preprocessing/text_resources/reviewed_resources.zip'
POSTS = [BASE/'data_preprocessing/outputs/八爪鱼数据_20261024/八爪鱼合并数据.csv',
         BASE/'data_preprocessing/outputs/post_resume_20261021/cleaned_weibo.csv']
LONG = BASE/'data_preprocessing/outputs/longtext-supplement-20261103/longtext_supplement_data.json'


def dump(x):
    return json.dumps(x, ensure_ascii=False, separators=(',', ':'))


def sha(p):
    h = hashlib.sha256()
    with Path(p).open('rb') as f:
        for b in iter(lambda: f.read(1048576), b''):
            h.update(b)
    return h.hexdigest()


def text_hash(s):
    return hashlib.sha256(s.encode('utf-8')).hexdigest()


def write_lines(p, rows):
    with p.open('w', encoding='utf-8', newline='\n') as f:
        for r in rows:
            f.write(dump(r)+'\n')


def read_lines(p):
    with p.open(encoding='utf-8') as f:
        return [json.loads(x) for x in f]


def read_csv(p):
    with p.open(encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))


def snapshot(out):
    out.mkdir()
    sources = []
    for i,p in enumerate(POSTS+[LONG, ZIP]):
        dest=out/(f'posts_{i}.csv' if i<2 else 'longtext.json' if i==2 else 'reviewed.zip')
        shutil.copyfile(p,dest)
        sources.append(dict(kind='posts' if i<2 else 'longtext' if i==2 else 'resources',
                            original_path=str(p),snapshot=dest.name,sha256=sha(dest),input_unchanged=sha(p)==sha(dest)))
    for i,p in enumerate(sorted((BASE/'WeiboSpider/output').rglob('comments.sqlite3'))):
        dest=out/f'comments_{i:02}.sqlite3'
        src=sqlite3.connect(p.resolve().as_uri()+'?mode=ro',uri=True)
        dst=sqlite3.connect(dest)
        src.backup(dst)
        src.close();dst.close()
        sources.append(dict(kind='comment_db',original_path=str(p),snapshot=dest.name,sha256=sha(dest),
                            snapshot_method='SQLite online backup via read-only source connection'))
    sys.path.insert(0,str(BASE/'WeiboSpider/weibospider'))
    try:
        from workflow_store import connect, read_records, totals
        db=connect()
        try:
            db.cursor().execute('SET TRANSACTION READ ONLY')
            db.cursor().execute('START TRANSACTION WITH CONSISTENT SNAPSHOT')
            records=read_records(db)
        finally:
            db.rollback();db.close()
        write_lines(out/'mysql_crawl_items.jsonl',records)
        sources.append(dict(kind='mysql',original_path='MySQL/weibo_data/crawl_items',snapshot='mysql_crawl_items.jsonl',
                            sha256=sha(out/'mysql_crawl_items.jsonl'),totals=totals(records),
                            sql='SELECT spider,item_id,keyword,payload FROM crawl_items ORDER BY spider,item_id,keyword',
                            transaction='READ ONLY WITH CONSISTENT SNAPSHOT'))
    except Exception as e:
        sources.append(dict(kind='mysql_unavailable',error=type(e).__name__))
    (out/'sources.json').write_text(json.dumps(sources,ensure_ascii=False,indent=2),encoding='utf-8')
    return sources


def conservative_clean(raw):
    # The shared cleaner preserves @targets, emoji, emphasis, negation and ordinary text.
    return clean_social_text(raw)['content_clean']


def emoji_features(s):
    # Preserve exact emoji symbols, ZWJ sequences, modifiers, and Weibo bracket labels.
    return re.findall(r'\[[^\[\]\s]{1,24}\]|[0-9#*]\ufe0f?\u20e3|[\U0001f000-\U0001faff\u2600-\u27bf](?:[\ufe0f\U0001f3fb-\U0001f3ff]|\u200d[\U0001f000-\U0001faff\u2600-\u27bf])*',s)


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--output-dir',type=Path,required=True)
    ap.add_argument('--snapshot-dir',type=Path)
    args=ap.parse_args()
    out=args.output_dir.resolve()
    if out.exists():raise ValueError('Output directory must be new')
    out.mkdir(parents=True)
    snap=args.snapshot_dir.resolve() if args.snapshot_dir else out/'input_snapshot'
    sources=json.loads((snap/'sources.json').read_text(encoding='utf-8')) if args.snapshot_dir else snapshot(snap)
    for s in sources:
        if s.get('snapshot'):assert sha(snap/s['snapshot'])==s['sha256']
    log=[]
    def note(s):
        log.append(s);print(s,flush=True)
        (out/'processing.log').write_text('\n'.join(log)+'\n',encoding='utf-8')
    note('Frozen inputs verified; no source writes. '+datetime.now(timezone.utc).isoformat())
    resources=out/'resource_snapshot';resources.mkdir()
    with zipfile.ZipFile(snap/'reviewed.zip') as z:
        for n in ['README.md','dwrg_dict.txt','glossary_review.json','keep_single_chars.txt','manual_resolutions.json','player_id_mapping.json','stopwords.txt','synonyms.json']:
            (resources/n).write_bytes(z.read(n))
        (resources/'reviewed_annotations.xlsx').write_bytes(z.read(next(n for n in z.namelist() if n.endswith('.xlsx'))))
    entries=json.loads((resources/'synonyms.json').read_text(encoding='utf-8-sig'))
    manuals=json.loads((resources/'manual_resolutions.json').read_text(encoding='utf-8-sig'))
    synonyms={e['surface'].casefold():e for e in entries}
    # These are data annotations. README contents are never interpreted as task instructions.
    import jieba
    import openpyxl
    tokenizer=jieba.Tokenizer()
    dictionary={}
    for line in (resources/'dwrg_dict.txt').read_text(encoding='utf-8-sig').splitlines():
        if not line.strip() or line.lstrip().startswith('#'):continue
        w,f,t=line.split();tokenizer.add_word(w,int(f),t);dictionary[w]=t
    normalizer=AliasNormalizer(entries)
    for e in entries:tokenizer.add_word(e['canonical'],e['weight'],e['category'])
    stop={x.casefold() for x in read_wordlist(resources/'stopwords.txt')}
    keep=read_wordlist(resources/'keep_single_chars.txt')
    for w in stop|set(PROTECTED_SIGNAL_WORDS):
        if len(w)>1:tokenizer.add_word(w,100000,'signal')
    normalizer.canonical_forms.update({x.casefold():x for x in json.loads((resources/'player_id_mapping.json').read_text(encoding='utf-8-sig'))})

    # Preserve both post-source versions separately, select primary curated corpus on ID overlaps.
    post_observations=[];posts={}
    for s in sources:
        if s['kind']!='posts':continue
        for rownum,p in enumerate(read_csv(snap/s['snapshot']),2):
            ref=f"{s['snapshot']}:{rownum}"
            post_observations.append(dict(observation_key=ref,source_path=s['original_path'],source_row=rownum,record=p))
            key=p['weibo_id']
            if key not in posts:posts[key]=dict(original=p,source_refs=[],selected_source=ref)
            posts[key]['source_refs'].append(ref)
    longrows=json.loads((snap/'longtext.json').read_text(encoding='utf-8-sig'))['filtered_rows']
    longmap={p['微博ID']:p for p in longrows if p.get('补充正文状态') in ['supplemented_complete','already_complete_in_main_db'] and p.get('补充正文')}
    post_match_text={pid:lr['补充正文'] for pid,lr in longmap.items()}
    repost_match_index=build_post_match_index([p['original'] for p in posts.values()],post_match_text)
    write_lines(out/'post_source_records.jsonl',post_observations)
    note(f'Curated posts: {len(post_observations)} observations -> {len(posts)} unique IDs; longtext candidates {len(longmap)}')

    observations=[];target_obs=[];source_stats=[];issues=[]
    for s in sources:
        if s['kind']=='comment_db':
            con=sqlite3.connect((snap/s['snapshot']).as_uri()+'?mode=ro',uri=True);con.row_factory=sqlite3.Row
            count=0
            for r in con.execute('SELECT * FROM comments ORDER BY first_seen,record_key'):
                c=json.loads(r['payload']);count+=1
                observations.append(dict(observation_key=f"{s['snapshot']}:{r['record_key']}",source_path=s['original_path'],
                    last_seen=r['last_seen'],first_seen=r['first_seen'],record=c,store_record_key=r['record_key'],store_root_key=r['root_record_key']))
            for r in con.execute('SELECT * FROM targets'):
                target_obs.append(dict(source_path=s['original_path'],fields=dict(r),record=json.loads(r['payload'])))
            if con.execute("select 1 from sqlite_master where name='issues'").fetchone():
                issues.extend(dict(source_path=s['original_path'],fields=dict(r)) for r in con.execute('SELECT * FROM issues'))
            source_stats.append(dict(source=s['original_path'],rows=count,integrity=con.execute('PRAGMA integrity_check').fetchone()[0]))
            con.close()
        elif s['kind']=='mysql':
            for r in read_lines(snap/s['snapshot']):
                if r['spider'] in ['comment','browser_comment']:
                    observations.append(dict(observation_key='mysql:'+r['spider']+':'+r['item_id'],source_path=s['original_path'],
                        last_seen='',first_seen='',record=r['payload'],store_record_key=r['item_id'],store_root_key=None))
    write_lines(out/'comment_source_records.jsonl',observations)
    write_lines(out/'target_source_records.jsonl',target_obs)
    write_lines(out/'collection_issues.jsonl',issues)
    groups=defaultdict(list)
    for o in observations:
        c=o['record'];key=c.get('record_key') or o['store_record_key']
        # Key equality only; different users/times with the same words are never deduplicated.
        groups[key].append(o)
    comments=[];identity_conflicts=[]
    for key,obs in groups.items():
        identity=lambda o:tuple(str(o['record'].get(x,'')) for x in ['tweet_id','comment_level','user_id'])
        variants=defaultdict(list)
        for o in obs:variants[identity(o)].append(o)
        for j,vs in enumerate(variants.values()):
            # A nontruncated newer payload takes priority; every old payload remains in observations.
            chosen=max(vs,key=lambda o:(not o['record'].get('content_truncated',False),o['last_seen'],len(o['record'].get('content',''))))
            dataset_key=key if len(variants)==1 else key+':conflict:'+str(j)
            c=dict(chosen['record'])
            c.update(dataset_key=dataset_key,selected_observation=chosen['observation_key'],source_observation_keys=[v['observation_key'] for v in vs])
            comments.append(c)
            if len(variants)>1:identity_conflicts.append(dict(record_key=key,dataset_key=dataset_key,reason='key_identity_conflict'))
    note(f'Comments: {len(observations)} observations -> {len(comments)} source identities; identity conflicts {len(identity_conflicts)}')
    by_key=defaultdict(list)
    for c in comments:by_key[c.get('record_key') or c['dataset_key']].append(c)
    by_context=defaultdict(list)
    for o in observations:
        c=o['record']
        for nick,txt in {(c.get('nick_name'),c.get('content')),tuple("'"+v if isinstance(v,str) and v.startswith(('=','+','-','@')) else v for v in (c.get('nick_name'),c.get('content')))}:
            by_context[(str(c.get('tweet_id')),c.get('comment_level'),nick,txt)].append(o)
    urls=defaultdict(set)
    for t in target_obs:
        if t['record'].get('url'):urls[str(t['record']['tweet_id'])].add(t['record']['url'])
    annotations=[];overrides=defaultdict(dict);pending=[];annotation_conflicts=[]
    wb=openpyxl.load_workbook(resources/'reviewed_annotations.xlsx',read_only=True,data_only=True)
    for i,row in enumerate(wb['逐条核对清单'].values,1):
        if i==1 or len(row)<3 or not row[0]:continue
        term,ctx,label=row[:3]
        ctx2=ctx.replace('\\u3000','\u3000').replace('\\n','\n')
        m=re.fullmatch(r'微博ID：(\d+)\s+评论层级：(\d+)\s+昵称：(.*?)\n原评论：(.*)\n微博链接：(\S+)\s*',ctx2,re.S)
        matches=by_context.get((m[1],int(m[2]),m[3],m[4]),[]) if m else []
        candidates={o['record'].get('record_key') or o['store_record_key'] for o in matches}
        a=dict(annotation_key=f'review:{i}',term=term,human_label=label,original_context=ctx,source_excel_row=i,
               candidate_keys=sorted(candidates),status='pending_record_match',applied_occurrences=0)
        if len(candidates)==1 and m[5] in urls[m[1]]:
            k=next(iter(candidates));cs=by_key[k]
            if len(cs)==1:
                c=cs[0];a.update(dataset_key=c['dataset_key'],status='linked')
                terms=re.split('[、；]',term);labels=label.split('；')
                if len(terms)==len(labels):
                    for t,l in zip(terms,labels):
                        # Match only the exact annotated text version, never transfer across changed wording.
                        if c.get('content')!=m[4] and ("'"+c.get('content',''))!=m[4]:
                            pending.append(dict(kind='annotation_text_version_changed',**a));continue
                        if '不归一化' in l:canonical=t;typ='literal';category='literal'
                        elif l in ['回忆（普通词义）','帖子编号']:canonical=t;typ='literal';category='literal'
                        else:
                            canonical=l;orig=synonyms.get(t.casefold(),{})
                            typ=orig.get('term_type','neutral_alias');category=orig.get('category','community')
                        value=dict(canonical=canonical,term_type=typ,category=category,sensitive=synonyms.get(t.casefold(),{}).get('sensitive',0),toxicity=None,
                                   review_status='confirmed_for_occurrence',human_note=l,source_file='reviewed_annotations.xlsx',annotation_key=a['annotation_key'])
                        old=overrides[c['dataset_key']].get(t.casefold())
                        if old and old['canonical']!=canonical:
                            annotation_conflicts.append(dict(dataset_key=c['dataset_key'],term=t,first=old,second=value))
                            overrides[c['dataset_key']][t.casefold()]={'conflict':True}
                        else:overrides[c['dataset_key']][t.casefold()]=value
        if a['status']!='linked':pending.append(dict(kind='annotation_record_match',**a))
        annotations.append(a)
    wb.close()
    # Annotated terms absent from the global synonyms retain literal meaning by default.
    # This permits record-scoped 上号 -> 登录 without inventing a global service interpretation.
    added_terms=set()
    for rules in overrides.values():
        for term,value in rules.items():
            if term not in synonyms and term not in added_terms:
                entries.append(dict(surface=term,canonical=term,term_type='literal',weight=10000,
                    category='literal',sensitive=0,toxicity=None,review_status='literal_preserved'))
                added_terms.add(term)
    normalizer=AliasNormalizer(entries)
    for e in entries:tokenizer.add_word(e['canonical'],e['weight'],e['category'])
    # Preserve the full review workbook. It contains source labels, never sentence-level toxicity labels.
    annotation_index={a['annotation_key']:a for a in annotations}
    manual_by_id=defaultdict(list)
    for m in manuals:manual_by_id[m['weibo_id']].append(m)
    original_corpus_ids={o['record']['weibo_id'] for o in post_observations if o['observation_key'].startswith('posts_0.csv:')}
    scoped_entries={k:dict(v) for k,v in normalizer.entries.items() if v.get('scope')=='current_corpus'}
    manual_applied=[];frequencies=Counter();dictionary_hits=Counter();stats=defaultdict(Counter);social_stats=defaultdict(Counter)
    social_audit=[]
    def prepare(raw,processing,key,kind,post_id,manual_list=None,current_author='',current_time='',current_id=''):
        cleaning=clean_social_text(processing)
        surface=cleaning['content_clean']
        repost_segments=match_repost_segments(cleaning['repost_segments'],repost_match_index,
            current_id=current_id,current_author=current_author,current_time=current_time)
        cleaning_flags=list(cleaning['social_cleaning_flags'])
        match_counts=Counter(s['match_status'] for s in repost_segments)
        for segment in repost_segments:
            cleaning_flags.extend(segment.get('social_cleaning_flags', []))
        if any(s['match_status']=='needs_manual_review' for s in repost_segments):
            cleaning_flags.append('repost_match_review_required')
        removed=[*cleaning['removed_platform_fragments']]
        removed.extend(fragment for segment in repost_segments for fragment in segment['removed_platform_fragments'])
        active=[]
        # Post manual annotations have hashes on original bodies; confirm both hash and local term context.
        for m in manual_list or []:
            if m.get('expected_content_sha256')!=text_hash(raw):
                pending.append(dict(kind='manual_hash_mismatch',dataset_key=key,annotation=m));continue
            oldsurface=conservative_clean(raw);term=m['surface'];occ=m.get('occurrence',1)
            oldhits=[h for h in normalizer.pattern.finditer(oldsurface) if h.group().casefold()==term.casefold()]
            newhits=[h for h in normalizer.pattern.finditer(surface) if h.group().casefold()==term.casefold()]
            compact=lambda s:re.sub(r'\s+|\[[^\[\]]{1,24}\]','',s)
            candidates=[]
            if len(oldhits)>=occ:
                h1=oldhits[occ-1]
                left=compact(oldsurface[max(0,h1.start()-48):h1.start()])[-12:]
                right=compact(oldsurface[h1.end():h1.end()+48])[:12]
                for newocc,h2 in enumerate(newhits,1):
                    nl=compact(surface[max(0,h2.start()-48):h2.start()])
                    nr=compact(surface[h2.end():h2.end()+48])
                    if len(left)+len(right)>=10 and nl.endswith(left) and nr.startswith(right):candidates.append(newocc)
            if len(candidates)==1:
                active.append({**m,'weibo_id':key,'occurrence':candidates[0],'original_review_occurrence':occ,
                               'expected_content_sha256':text_hash(raw)})
            elif raw==processing:
                active.append({**m,'weibo_id':key,'expected_content_sha256':text_hash(raw)})
            else:pending.append(dict(kind='manual_context_changed',dataset_key=key,annotation=m,candidate_occurrences=candidates))
        # Reuse compiled global normalizer; replace only its record-scoped manual map.
        for term,entry in scoped_entries.items():
            normalizer.entries[term]={**entry,**({'review_status':'pending','scope_note':'confirmed only for historical curated post corpus'}
                if kind!='posts' or post_id not in original_corpus_ids else {})}
        normalizer.manual_resolutions={(key,m['surface'].casefold(),m.get('occurrence',1)):m for m in active}
        for term,value in overrides.get(key,{}).items():
            if value.get('conflict'):continue
            if term in normalizer.entries:
                hits=list(normalizer.pattern.finditer(surface));occ=0
                for h in hits:
                    if h.group().casefold()==term:
                        occ+=1;normalizer.manual_resolutions[(key,term,occ)]={**value,'weibo_id':key,'surface':h.group(),'occurrence':occ}
        normalized,matches=normalizer.normalize(surface,key,text_hash(raw))
        occurrences=Counter()
        for x in matches:
            term=x['configured_surface'].casefold();occurrences[term]+=1
            rule=normalizer.manual_resolutions.get((key,term,occurrences[term]))
            if rule and rule.get('annotation_key'):x['annotation_key']=rule['annotation_key']
        # Review terms outside synonyms (e.g. 上号/微博智搜) still receive explicit attributes.
        for term,value in overrides.get(key,{}).items():
            if term in normalizer.entries or value.get('conflict'):continue
            for h in re.finditer(re.escape(term),normalized,re.I):
                if any(h.start()<x['clean_end'] and h.end()>x['clean_start'] for x in matches):continue
                matches.append(dict(surface=h.group(),configured_surface=term,canonical=value['canonical'],emitted_canonical=h.group(),
                    source_start=h.start(),source_end=h.end(),clean_start=h.start(),clean_end=h.end(),resolved=True,resolution='human_review_attribute',
                    **{k:v for k,v in value.items() if k!='canonical'}))
        matches.sort(key=lambda x:x['clean_start'])
        tokens,attributed,token_annotations=annotated_tokens(normalized,matches,tokenizer,stop,keep,dictionary,normalizer.canonical_forms)
        # Standard IDs and aliases stay whole; emoji are explicit features rather than discarded punctuation.
        emoji=emoji_features(surface)
        tokens=' '.join([tokens]+emoji).strip()
        # Classification uses original surface wording; do not substitute aliases or drop punctuation/emotion.
        surface_tokens=[w for w in tokenizer.lcut(surface,HMM=True) if w.strip() and
                        (w in PROTECTED_SIGNAL_WORDS or (w.casefold() not in stop and (len(w)>1 or w in keep)))]
        for x in matches:
            if x.get('annotation_key'):
                annotation_index[x['annotation_key']]['applied_occurrences']+=1
            if x['resolution']=='human_review':manual_applied.append(dict(dataset_key=key,kind=kind,match=x))
            if not x['resolved']:
                pending.append(dict(kind='term_disambiguation',dataset_key=key,weibo_id=post_id,term=x['surface'],
                    canonical_candidate=x['canonical'],reason=x['resolution'],content=raw,processing_text=processing,
                    local_context=surface[max(0,x['source_start']-80):x['source_end']+80]))
        flags=list(cleaning_flags)
        if not surface:flags.append('empty_after_cleaning')
        if not tokens:flags.append('empty_tokens')
        if '\ufffd' in raw:flags.append('replacement_character_requires_review')
        if '展开' in processing and processing.endswith(('展开','展开c','展开全文')):flags.append('possibly_truncated_text')
        origin=('repost_only' if repost_segments and not surface else
                'possible_automated_answer' if ('微博智搜' in raw and re.search('(查看全部|查看最新|继续提问)戳',raw)) else 'unclassified')
        st=stats[kind];st['rows']+=1;st['clean_changed']+=surface!=processing;st['empty_raw']+=not raw.strip()
        st['empty_clean']+=not surface;st['empty_tokens']+=not tokens;st['alias_matches']+=len(matches)
        st['normalized_occurrences']+=sum(x['resolved'] and x['surface']!=x['emitted_canonical'] for x in matches)
        st['manual_occurrences']+=sum(x['resolution'].startswith('human_review') for x in matches)
        st['pending_terms']+=sum(not x['resolved'] for x in matches);st['emoji_records']+=bool(emoji)
        social=social_stats[kind]
        social['records_with_supertopic_removal']+=bool(any(x['category'] in {'weibo_supertopic_badge','hashtag_supertopic'} for x in removed))
        social['supertopic_markers_removed']+=sum(x['category'] in {'weibo_supertopic_badge','hashtag_supertopic'} for x in removed)
        column_references=[cleaning['column_reference']]+[s.get('column_reference') for s in repost_segments]
        social['records_with_column_reference_removed']+=any(column_references)
        social['column_references_removed']+=sum(bool(x) for x in column_references)
        social['records_with_column_reference_review']+=any(flag.startswith('possible_column_reference') for group in [cleaning['social_cleaning_flags']]+[s.get('social_cleaning_flags',[]) for s in repost_segments] for flag in group)
        social['records_with_repost_chain']+=bool(repost_segments)
        social['repost_segments']+=len(repost_segments)
        social['repost_confirmed_in_database']+=match_counts['confirmed_in_database']
        social['repost_needs_manual_review']+=match_counts['needs_manual_review']
        social['repost_not_found_in_database']+=match_counts['not_found_in_database']
        social['repost_only_records']+=('repost_only_record' in cleaning_flags)
        frequencies.update(tokens.split());dictionary_hits.update(t for t in tokens.split() if t in dictionary)
        prepared=dict(content_clean=surface,content_normalized=normalized,tokens=tokens,tokens_clustering=tokens,
            content_classification=surface,tokens_classification=' '.join(surface_tokens),tokens_classification_attributed=attributed,
            alias_matches=matches,token_annotations=token_annotations,emoji_features=emoji,content_sha256=text_hash(raw),
            processing_text_sha256=text_hash(processing),text_preparation_flags=sorted(set(flags)),content_origin=origin,
            platform_removed_fragments=removed,column_reference=cleaning['column_reference'],
            repost_segments=repost_segments,repost_match_status=dict(match_counts),
            social_cleaning_flags=sorted(set(cleaning_flags)))
        if removed or repost_segments or cleaning_flags:
            social_audit.append(dict(kind=kind,dataset_key=key,weibo_id=post_id or '',current_record_id=current_id or '',
                author=current_author or '',created_at=current_time or '',original_content=raw,processing_text=processing,
                content_clean=surface,removed_platform_fragments=removed,column_reference=cleaning['column_reference'],
                repost_segments=repost_segments,repost_match_status=dict(match_counts),
                social_cleaning_flags=sorted(set(cleaning_flags)),content_sha256=text_hash(raw),
                processing_text_sha256=text_hash(processing)))
        return prepared

    post_out=[]
    for pid,p in posts.items():
        raw=p['original']['content'];lr=longmap.get(pid)
        processing=lr['补充正文'] if lr else raw
        prepared=prepare(raw,processing,'post:'+pid,'posts',pid,manual_by_id[pid],
            current_author=p['original'].get('user_name',''),current_time=p['original'].get('created_at',''),current_id=pid)
        # Preserve existing content_clean field unchanged; all derived fields live in preprocessing.
        post_out.append({**p['original'],'preprocessing':{**prepared,'processing_text':processing,
            'processing_text_source':'verified_existing_longtext_supplement' if lr else 'original_content',
            'longtext_metadata':lr if lr else None,'source_refs':p['source_refs'],'selected_source':p['selected_source']}})
    note('Post text preparation complete')
    relations=[];comment_out=[]
    for c in comments:
        pid=str(c.get('tweet_id',''));key=c['dataset_key'];level=c.get('comment_level')
        problems=[];root=c.get('root_record_key') or c.get('root_comment_key')
        if pid not in posts:problems.append('post_not_in_curated_corpus')
        if level==1:
            root=key;root_status='self_level1_record'
        elif level==2:
            candidates=by_key.get(root,[])
            if len(candidates)==1 and candidates[0].get('tweet_id')==c.get('tweet_id') and candidates[0].get('comment_level')==1:
                root=candidates[0]['dataset_key'];root_status='verified_collection_root'
            else:root=None;root_status='pending_verification';problems.append('root_missing_conflicting_or_wrong_post')
        else:root_status='pending_verification';problems.append('unknown_comment_level')
        parent=c.get('parent_comment_key');parent_status='not_applicable' if level==1 else 'pending_verification'
        if parent:
            candidates=by_key.get(parent,[])
            if len(candidates)==1 and candidates[0].get('tweet_id')==c.get('tweet_id') and candidates[0]['dataset_key']!=key:
                parent=candidates[0]['dataset_key'];parent_status='verified_collection_parent'
            else:parent=None;problems.append('parent_missing_conflicting_or_wrong_post')
        rel=dict(dataset_key=key,weibo_id=pid,comment_level=level,root_dataset_key=root,direct_parent_dataset_key=parent,
            root_relation_status=root_status,direct_parent_status=parent_status,post_relation_status='verified_id' if pid in posts else 'pending_verification',
            platform_comment_id_status='available' if c.get('platform_comment_id') or c.get('comment_id') else 'missing',issues=problems)
        relations.append(rel)
        if problems or parent_status=='pending_verification' or rel['platform_comment_id_status']=='missing':
            pending.append(dict(kind='relation_verification',**rel))
        prepared=prepare(c.get('content',''),c.get('content',''),key,'level1' if level==1 else 'level2',pid,
            current_author=c.get('nick_name',''),current_time=c.get('created_at_display',''))
        # Derived names must not overwrite original fields from any crawler version.
        if set(prepared)&set(c):raise ValueError('Original comment field collision')
        comment_out.append({**c,**prepared,'relation_validation':rel})
    write_lines(out/'posts_preprocessed.jsonl',post_out)
    write_lines(out/'comments_preprocessed.jsonl',comment_out)
    write_lines(out/'comment_relations.jsonl',relations)
    write_lines(out/'social_cleaning_audit.jsonl',social_audit)
    write_lines(out/'human_annotations.jsonl',annotations)
    write_lines(out/'pending_review.jsonl',[p for p in pending if p['kind']!='relation_verification'])
    write_lines(out/'pending_relations.jsonl',[p for p in pending if p['kind']=='relation_verification'])
    write_lines(out/'mapping_conflicts.jsonl',identity_conflicts+annotation_conflicts)
    write_lines(out/'manual_applications.jsonl',manual_applied)
    write_lines(out/'token_frequencies.jsonl',[dict(token=t,frequency=n,in_game_dictionary=t in dictionary) for t,n in frequencies.most_common()])
    write_lines(out/'tokenization_samples.jsonl',[dict(kind='post',weibo_id=p['weibo_id'],raw=p['content'],preprocessing=p['preprocessing']) for p in post_out[:8]]+
        [dict(kind='comment',dataset_key=c['dataset_key'],raw=c['content'],content_normalized=c['content_normalized'],tokens=c['tokens'],alias_matches=c['alias_matches'])
         for c in comment_out if c['alias_matches']][:30])
    note('All text preparation complete; building SQLite and analysis views')
    db=sqlite3.connect(out/'research_dataset.sqlite3')
    db.executescript('''
      PRAGMA foreign_keys=ON;
      CREATE TABLE posts(weibo_id TEXT PRIMARY KEY,content TEXT,created_at TEXT,content_clean TEXT,content_normalized TEXT,tokens TEXT,content_classification TEXT,alias_matches TEXT,platform_removed_fragments TEXT,column_reference TEXT,repost_segments TEXT,repost_match_status TEXT,social_cleaning_flags TEXT,payload TEXT);
      CREATE TABLE comments(dataset_key TEXT PRIMARY KEY,record_key TEXT,weibo_id TEXT,comment_level INTEGER,root_dataset_key TEXT,direct_parent_dataset_key TEXT,content TEXT,content_clean TEXT,content_normalized TEXT,tokens TEXT,content_classification TEXT,alias_matches TEXT,platform_removed_fragments TEXT,column_reference TEXT,repost_segments TEXT,repost_match_status TEXT,social_cleaning_flags TEXT,payload TEXT);
      CREATE TABLE relations(dataset_key TEXT PRIMARY KEY REFERENCES comments(dataset_key),verified_weibo_id TEXT REFERENCES posts(weibo_id),verified_root_key TEXT REFERENCES comments(dataset_key),verified_parent_key TEXT REFERENCES comments(dataset_key),payload TEXT);
      CREATE INDEX comment_post ON comments(weibo_id); CREATE INDEX comment_root ON comments(root_dataset_key);
      CREATE VIEW posts_clustering AS SELECT weibo_id,tokens,content_normalized FROM posts WHERE tokens!='';
      CREATE VIEW comments_clustering AS SELECT dataset_key,weibo_id,comment_level,tokens,content_normalized FROM comments WHERE tokens!='';
      CREATE VIEW posts_malicious_features AS SELECT weibo_id,content,content_classification,alias_matches,payload FROM posts;
      CREATE VIEW comments_malicious_features AS SELECT dataset_key,weibo_id,comment_level,content,content_classification,alias_matches,payload FROM comments;
    ''')
    with db:
        db.executemany('INSERT INTO posts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',[(p['weibo_id'],p['content'],p.get('created_at'),p['preprocessing']['content_clean'],p['preprocessing']['content_normalized'],p['preprocessing']['tokens'],p['preprocessing']['content_classification'],dump(p['preprocessing']['alias_matches']),dump(p['preprocessing']['platform_removed_fragments']),dump(p['preprocessing']['column_reference']),dump(p['preprocessing']['repost_segments']),dump(p['preprocessing']['repost_match_status']),dump(p['preprocessing']['social_cleaning_flags']),dump(p)) for p in post_out])
        db.executemany('INSERT INTO comments VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',[(c['dataset_key'],c.get('record_key'),c.get('tweet_id'),c.get('comment_level'),c['relation_validation']['root_dataset_key'],c['relation_validation']['direct_parent_dataset_key'],c.get('content'),c['content_clean'],c['content_normalized'],c['tokens'],c['content_classification'],dump(c['alias_matches']),dump(c['platform_removed_fragments']),dump(c['column_reference']),dump(c['repost_segments']),dump(c['repost_match_status']),dump(c['social_cleaning_flags']),dump(c)) for c in comment_out])
        db.executemany('INSERT INTO relations VALUES (?,?,?,?,?)',[(r['dataset_key'],r['weibo_id'] if r['post_relation_status']=='verified_id' else None,r['root_dataset_key'],r['direct_parent_dataset_key'],dump(r)) for r in relations])
    fk=db.execute('PRAGMA foreign_key_check').fetchall();integrity=db.execute('PRAGMA integrity_check').fetchone()[0]
    view_counts={v:db.execute('select count(*) from '+v).fetchone()[0] for v in ['posts_clustering','comments_clustering','posts_malicious_features','comments_malicious_features']}
    db.close()
    # Full readback, original values and types, not only output counts.
    pc=read_lines(out/'posts_preprocessed.jsonl');cc=read_lines(out/'comments_preprocessed.jsonl')
    exact=lambda a,b:all(k in b and type(v) is type(b[k]) and v==b[k] for k,v in a.items())
    oi={o['observation_key']:o for o in observations}
    originals_preserved=all(exact(posts[p['weibo_id']]['original'],p) for p in pc) and all(exact(oi[c['selected_observation']]['record'],c) for c in cc)
    assert originals_preserved and len(pc)==len(posts) and len(cc)==len(comments) and not fk and integrity=='ok'
    # All source observations must remain recoverable, even when canonical selection used newer fields.
    assert read_lines(out/'comment_source_records.jsonl')==observations
    assert read_lines(out/'post_source_records.jsonl')==post_observations
    for s in sources:
        if s.get('snapshot'):assert sha(snap/s['snapshot'])==s['sha256']
    dates=sorted(p['created_at'] for p in post_out if p.get('created_at'))
    cdates=[];unknown_dates=0
    for c in comment_out:
        m=re.match(r'(\d{2})-(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})',c.get('created_at_display') or '')
        if m:cdates.append(f'20{m[1]}-{int(m[2]):02}-{int(m[3]):02} {int(m[4]):02}:{m[5]}')
        else:unknown_dates+=1
    results=dict(executed_at_utc=datetime.now(timezone.utc).isoformat(),input_snapshot=str(snap),sources=sources,
        post_input_observations=len(post_observations),posts=len(posts),post_duplicate_observations=len(post_observations)-len(posts),
        post_time_range=[dates[0],dates[-1]],post_date_month_counts=dict(Counter(d[:7] for d in dates)),
        longtext_used=sum(p['preprocessing']['processing_text_source']=='verified_existing_longtext_supplement' for p in post_out),
        comment_input_observations=len(observations),comments=len(comments),comment_duplicate_observations=len(observations)-len(comments),
        comment_levels=dict(Counter(str(c.get('comment_level')) for c in comments)),comment_time_range=[min(cdates,default=None),max(cdates,default=None)],comment_dates_unparsed=unknown_dates,
        comments_with_missing_post=sum(r['post_relation_status']!='verified_id' for r in relations),
        verified_reply_root_links=sum(r['root_relation_status']=='verified_collection_root' for r in relations),
        reply_root_errors=sum(r['comment_level']==2 and r['root_relation_status']!='verified_collection_root' for r in relations),
        missing_platform_comment_ids=sum(r['platform_comment_id_status']=='missing' for r in relations),
        direct_parent_pending=sum(r['direct_parent_status']=='pending_verification' for r in relations),
        posts_with_comments=len({c.get('tweet_id') for c in comments}&set(posts)),
        posts_without_comments=len(set(posts)-{c.get('tweet_id') for c in comments}),
        source_database_checks=source_stats,text_processing={k:dict(v) for k,v in stats.items()},
        social_cleaning={k:dict(v) for k,v in social_stats.items()},
        social_cleaning_audit_records=len(social_audit),
        repost_match_status_counts=dict(Counter(segment['match_status'] for row in social_audit for segment in row['repost_segments'])),
        annotations=len(annotations),annotations_linked=sum(a['status']=='linked' for a in annotations),
        annotations_with_applied_occurrences=sum(a['applied_occurrences']>0 for a in annotations),
        manual_post_occurrences_applied=sum(m['kind']=='posts' for m in manual_applied),
        mapping_conflicts=len(identity_conflicts)+len(annotation_conflicts),
        pending_review_records=sum(p['kind']!='relation_verification' for p in pending),
        pending_review_kind_counts=dict(Counter(p['kind'] for p in pending if p['kind']!='relation_verification')),
        pending_relation_records=sum(p['kind']=='relation_verification' for p in pending),
        original_fields_values_types_preserved=originals_preserved,all_source_observations_roundtrip_equal=True,
        sqlite_foreign_key_violations=fk,sqlite_integrity=integrity,analysis_view_counts=view_counts,
        dictionary_effective_words=len(dictionary),dictionary_token_hits=sum(dictionary_hits.values()),dictionary_top_hits=dictionary_hits.most_common(25),
        bot_marker_records=sum(c['content_origin']=='possible_automated_answer' for c in cc),
        same_text_different_identity_records=sum(n-1 for n in Counter(c.get('content') for c in cc).values() if n>1),
        no_sentence_malicious_labels_generated=True,coverage_caveat='browser-visible collection, not proof of complete historical comments; orphan records retained')
    (out/'validation_report.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
    for name in ['run_reviewed_preprocessing.py','alias_normalization.py','prepare_text.py','social_text_cleaning.py','relevance.py',
                 'data_io.py','preprocess.py','convert_encoding.py','semantic.py','test_reviewed_preprocessing.py']:
        shutil.copyfile(Path(__file__).parent/name,out/name)
    manifest={str(p.relative_to(out)):dict(bytes=p.stat().st_size,sha256=sha(p)) for p in out.rglob('*') if p.is_file() and p.name not in ['manifest.json','processing.log']}
    (out/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    note(f'PASS: {len(pc)} posts, {len(cc)} comments/replies; field readback and SQLite integrity verified')
    print(json.dumps({k:v for k,v in results.items() if k not in ['sources','source_database_checks','dictionary_top_hits']},ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
