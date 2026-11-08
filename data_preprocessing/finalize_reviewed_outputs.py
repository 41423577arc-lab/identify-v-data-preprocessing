"""Finalize generated artifacts only: flag exact UI-only observations and audit analysis readiness."""
import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import re
import shutil
import sqlite3
from run_reviewed_preprocessing import read_lines,write_lines,sha,dump


def finalize(out):
    report=json.loads((out/'validation_report.json').read_text(encoding='utf-8'))
    posts=read_lines(out/'posts_preprocessed.jsonl');comments=read_lines(out/'comments_preprocessed.jsonl')
    controls=re.compile(r'(?:等人\s*)?共\s*\d+\s*条回复|网页链接|查看图片|收起|展开全文')
    invalid=[];quality=[];candidates=defaultdict(list);span_errors=[]
    for c in comments:
        key=c['dataset_key'];raw=c.get('content','')
        if controls.fullmatch(raw.strip()):
            for field in ['content_clean','content_normalized','tokens','tokens_clustering','content_classification','tokens_classification','tokens_classification_attributed']:
                c[field]=''
            c['text_preparation_flags']=sorted(set(c['text_preparation_flags'])|{'platform_control_only','empty_after_cleaning','empty_tokens'})
            invalid.append(dict(dataset_key=key,weibo_id=c.get('tweet_id'),raw_content=raw,reason='exact_platform_control_only'))
        q=dict(dataset_key=key,weibo_id=c.get('tweet_id'),flags=c['text_preparation_flags'],
            clustering_eligible=bool(c['tokens']),malicious_features_eligible=bool(c['content_classification']),
            post_join_eligible=c['relation_validation']['post_relation_status']=='verified_id')
        quality.append(q)
        if c['content_clean']:
            sig=tuple(c.get(x) for x in ['tweet_id','comment_level','user_id','created_at_display'])+(c['relation_validation']['root_dataset_key'],c['content_clean'])
            candidates[sig].append(key)
        for m in c['alias_matches']:
            if c['content_normalized'][m['clean_start']:m['clean_end']]!=m['emitted_canonical']:
                span_errors.append(dict(dataset_key=key,surface=m['surface']))
    duplicates=[dict(candidate_keys=ks,reason='same_post_user_time_root_clean_text_distinct_source_keys_retained') for ks in candidates.values() if len(ks)>1]
    # Parent evidence is explicitly distinct from a curated post body or an invented relationship.
    missing={c['tweet_id'] for c in comments if c['relation_validation']['post_relation_status']!='verified_id'}
    evidence={mid:dict(weibo_id=mid,source_targets=[],mysql_records=[],status='outside_curated_post_corpus') for mid in missing}
    for t in read_lines(out/'target_source_records.jsonl'):
        mid=str(t['record'].get('tweet_id'))
        if mid in missing:evidence[mid]['source_targets'].append(t)
    snap=Path(report['input_snapshot'])
    if (snap/'mysql_crawl_items.jsonl').exists():
        for r in read_lines(snap/'mysql_crawl_items.jsonl'):
            if r['item_id'] in missing and r['spider'] not in ['comment','browser_comment','history_search_hit']:
                evidence[r['item_id']]['mysql_records'].append(r)
    write_lines(out/'comments_preprocessed.jsonl',comments)
    write_lines(out/'invalid_records.jsonl',invalid)
    write_lines(out/'record_quality.jsonl',quality)
    write_lines(out/'possible_duplicate_candidates.jsonl',duplicates)
    write_lines(out/'missing_post_evidence.jsonl',evidence.values())
    con=sqlite3.connect(out/'research_dataset.sqlite3')
    with con:
        con.execute('PRAGMA foreign_keys=ON')
        con.executemany('UPDATE comments SET content_clean=?,content_normalized=?,tokens=?,content_classification=?,payload=? WHERE dataset_key=?',
            [(c['content_clean'],c['content_normalized'],c['tokens'],c['content_classification'],dump(c),c['dataset_key']) for c in comments])
        con.execute('CREATE TABLE IF NOT EXISTS record_quality(dataset_key TEXT PRIMARY KEY REFERENCES comments(dataset_key),clustering_eligible INTEGER,malicious_features_eligible INTEGER,post_join_eligible INTEGER,payload TEXT)')
        con.executemany('INSERT OR REPLACE INTO record_quality VALUES (?,?,?,?,?)',[(q['dataset_key'],int(q['clustering_eligible']),int(q['malicious_features_eligible']),int(q['post_join_eligible']),dump(q)) for q in quality])
        con.execute('CREATE VIEW IF NOT EXISTS comments_malicious_eligible AS SELECT c.* FROM comments c JOIN record_quality q USING(dataset_key) WHERE q.malicious_features_eligible=1')
        con.execute('CREATE VIEW IF NOT EXISTS comments_clustering_linked AS SELECT c.* FROM comments_clustering c JOIN record_quality q USING(dataset_key) WHERE q.post_join_eligible=1')
    assert not span_errors,span_errors[:5]
    assert con.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    assert con.execute('PRAGMA foreign_key_check').fetchall()==[]
    assert all(json.loads(r[1])==c for r,c in zip(con.execute('SELECT dataset_key,payload FROM comments ORDER BY rowid'),comments))
    for kind,rows in [('posts',[p['preprocessing'] for p in posts]),('level1',[c for c in comments if c.get('comment_level')==1]),('level2',[c for c in comments if c.get('comment_level')==2])]:
        st=report['text_processing'][kind]
        st['empty_clean']=sum(not r['content_clean'] for r in rows)
        st['empty_tokens']=sum(not r['tokens'] for r in rows)
    report['invalid_platform_control_only_records']=len(invalid)
    report['missing_post_distinct_ids']=len(missing)
    report['possible_duplicate_groups_retained']=len(duplicates)
    report['alias_normalized_span_errors']=len(span_errors)
    report['final_jsonl_sqlite_payloads_equal']=True
    report['finalization_script']='finalize_reviewed_outputs.py'
    report['analysis_view_counts']={v:con.execute('SELECT COUNT(*) FROM '+v).fetchone()[0] for v in ['posts_clustering','comments_clustering','posts_malicious_features','comments_malicious_features','comments_malicious_eligible','comments_clustering_linked']}
    con.close()
    (out/'validation_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    shutil.copyfile(Path(__file__),out/Path(__file__).name)
    with (out/'processing.log').open('a',encoding='utf-8') as f:f.write(f'Final QA PASS: {len(invalid)} UI-only rows flagged, no alias span errors; JSONL/SQLite equal.\n')
    print(json.dumps({k:v for k,v in report.items() if k not in ['sources','source_database_checks','dictionary_top_hits']},ensure_ascii=False,indent=2))
    return report


if __name__=='__main__':
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--output-dir',type=Path,required=True)
    finalize(ap.parse_args().output_dir.resolve())
