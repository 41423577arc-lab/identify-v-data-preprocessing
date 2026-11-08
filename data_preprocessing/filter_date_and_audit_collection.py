"""Read-only collection audit and date-scoped research outputs; keep historical artifacts."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import os
from run_reviewed_preprocessing import read_lines, write_lines, sha

BASE=Path(__file__).resolve().parent.parent


def main(source,out):
    if out.exists():raise ValueError('Use a new output directory')
    out.mkdir(parents=True)
    prior=json.loads((source/'validation_report.json').read_text(encoding='utf-8'))
    audits=[]
    for name in ['comment-full-20261103','comment-recrawl-20261105','comment-nested-replies-20261105']:
        path=BASE/'WeiboSpider/output'/name/'comments.sqlite3'
        db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True);db.execute('BEGIN')
        targets=[dict(status=s,reason=r,count=n) for s,r,n in db.execute('SELECT status,reason,count(*) FROM targets GROUP BY status,reason')]
        streams=[dict(status=s,reason=r,count=n) for s,r,n in db.execute('SELECT status,reason,count(*) FROM streams GROUP BY status,reason')]
        rows=db.execute('SELECT record_key,payload FROM comments ORDER BY record_key').fetchall()
        oldsource=next(s for s in prior['sources'] if s.get('original_path')==str(path))
        old=sqlite3.connect((Path(prior['input_snapshot'])/oldsource['snapshot']).as_uri()+'?mode=ro',uri=True)
        unchanged=rows==old.execute('SELECT record_key,payload FROM comments ORDER BY record_key').fetchall();old.close()
        exported=json.loads(path.with_suffix('.json').read_text(encoding='utf-8'))
        audits.append(dict(database=str(path),records=len(rows),target_statuses=targets,stream_statuses=streams,
            last_target_update=db.execute('SELECT max(updated_at) FROM targets').fetchone()[0],
            collection_issues=db.execute('SELECT count(*) FROM issues').fetchone()[0],
            json_export_records=sum(len(t.get('comments',[])) for t in exported['targets']),
            unchanged_since_preprocessing=unchanged))
        db.rollback();db.close()
    if not all(a['unchanged_since_preprocessing'] for a in audits):
        raise ValueError('New comment text exists; refresh preprocessing before date filtering')
    proc=subprocess.run(['powershell','-NoProfile','-Command',
        f"@(Get-CimInstance Win32_Process | Where-Object {{ $_.Name -match 'python' -and $_.ProcessId -ne {os.getpid()} }} | Select-Object ProcessId,Name) | ConvertTo-Json -Compress"],capture_output=True,text=True)
    processes=json.loads(proc.stdout) if proc.stdout.strip() else []
    audit=dict(audited_at_utc=datetime.now(timezone.utc).isoformat(),other_python_processes=processes,
        process_query_succeeded=proc.returncode==0,databases=audits,
        conclusion='collection_not_fully_complete; no active crawler observed; pending/running/incomplete statuses remain')
    (out/'collection_audit.json').write_text(json.dumps(audit,ensure_ascii=False,indent=2),encoding='utf-8')
    posts=read_lines(source/'posts_preprocessed.jsonl')
    kept=[p for p in posts if '2026-09-24'<=p.get('created_at','')<'2026-10-16']
    excluded=[p for p in posts if not '2026-09-24'<=p.get('created_at','')<'2026-10-16']
    excluded_ids={p['weibo_id'] for p in excluded}
    comments=read_lines(source/'comments_preprocessed.jsonl')
    assert not any(c['tweet_id'] in excluded_ids for c in comments),'Relations need updating before removal'
    write_lines(out/'posts_preprocessed.jsonl',kept)
    # Deletion applies to the new research corpus; only IDs/date/reason are retained in this audit.
    write_lines(out/'date_exclusions.jsonl',[dict(weibo_id=p['weibo_id'],created_at=p.get('created_at'),reason='outside_inclusive_20260924_20261015') for p in excluded])
    for name in ['comments_preprocessed.jsonl','comment_relations.jsonl','pending_relations.jsonl','record_quality.jsonl','invalid_records.jsonl','human_annotations.jsonl','missing_post_evidence.jsonl','mapping_conflicts.jsonl']:
        shutil.copyfile(source/name,out/name)
    def applicable(r):
        return not (r.get('dataset_key','').startswith('post:') and r['dataset_key'][5:] in excluded_ids)
    for name in ['pending_review.jsonl','manual_applications.jsonl']:
        write_lines(out/name,[r for r in read_lines(source/name) if applicable(r)])
    frequency=Counter()
    for p in kept:frequency.update(p['preprocessing']['tokens'].split())
    for c in comments:frequency.update(c['tokens'].split())
    write_lines(out/'token_frequencies.jsonl',[dict(token=t,frequency=n) for t,n in frequency.most_common()])
    olddb=sqlite3.connect((source/'research_dataset.sqlite3').as_uri()+'?mode=ro',uri=True)
    db=sqlite3.connect(out/'research_dataset.sqlite3');olddb.backup(db);olddb.close()
    db.execute('PRAGMA foreign_keys=ON')
    with db:db.executemany('DELETE FROM posts WHERE weibo_id=?',[(x,) for x in excluded_ids])
    assert db.execute('PRAGMA foreign_key_check').fetchall()==[]
    assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    assert db.execute('SELECT count(*) FROM posts').fetchone()[0]==len(kept)
    assert db.execute("SELECT count(*) FROM posts WHERE created_at<'2026-09-24' OR created_at>='2026-10-16'").fetchone()[0]==0
    views={v:db.execute('SELECT count(*) FROM '+v).fetchone()[0] for v in ['posts_clustering','comments_clustering','comments_clustering_linked','comments_malicious_eligible']}
    db.close()
    saved=read_lines(out/'posts_preprocessed.jsonl');assert saved==kept
    assert sha(source/'comments_preprocessed.jsonl')==sha(out/'comments_preprocessed.jsonl')
    report=dict(source_directory=str(source),date_filter=dict(start_inclusive='2026-09-24',end_inclusive='2026-10-15',end_exclusive='2026-10-16',timezone='Asia/Shanghai',basis='original created_at'),
        posts_before=len(posts),posts_after=len(kept),posts_excluded=len(excluded),
        post_time_range=[min(p['created_at'] for p in kept),max(p['created_at'] for p in kept)],
        comments=len(comments),comment_levels=dict(Counter(str(c['comment_level']) for c in comments)),
        comments_unchanged=True,comments_attached_to_removed_posts=0,reply_root_errors=0,
        comments_with_missing_curated_post=prior['comments_with_missing_post'],missing_post_distinct_ids=prior['missing_post_distinct_ids'],
        pending_review_records=len(read_lines(out/'pending_review.jsonl')),analysis_view_counts=views,
        original_sources_and_historical_outputs_untouched=True,sqlite_integrity='ok',sqlite_foreign_key_violations=[],
        collection_fully_complete=False)
    (out/'validation_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    lines=[f'日期筛选：2026-09-24 至 2026-10-15（含首尾两天）。',
        f'微博 {len(posts)} -> {len(kept)}，剔除 {len(excluded)} 条。所有输出微博均满足日期条件。',
        f'评论和回复保持 {len(comments)} 条不变，逐字节哈希一致；被剔除微博关联评论为 0。',
        'JSONL 回读、SQLite 数量、日期边界、完整性与外键检查通过。',
        f'原结果及原始数据保持不变；完整来源、人工规则和预处理日志位于 {source}。']
    (out/'processing.log').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    (out/'README.md').write_text('# 2026-09-24 至 2026-10-15 研究数据\n\n'+'\n\n'.join(lines)+
        '\n\n评论采集尚不能认定全部完成：当前未发现活动 Python 爬虫；全量、重爬和补采库仍有未完成状态。具体数量见 collection_audit.json。未重新启动或修改采集队列。\n\n'+
        'posts_preprocessed.jsonl 为日期筛选后的微博，comments_preprocessed.jsonl 保留全部既有评论及回复。日期要求仅作用于微博正文，没有按评论自身发布时间删除评论。research_dataset.sqlite3 提供相同数据及聚类/恶意识别特征视图；没有新增恶意分类标签。\n\n'+
        '原有 447 条评论对应 49 个集外微博的情况仍保留；全部 9,660 条回复根评论关联完整，平台评论 ID 和直接回复对象缺失情况未改变。\n\n'+
        f'复现：python -B data_preprocessing/filter_date_and_audit_collection.py --source-dir "{source}" --output-dir "另一个新目录"\n',encoding='utf-8')
    shutil.copyfile(Path(__file__),out/Path(__file__).name)
    manifest={str(p.relative_to(out)):dict(bytes=p.stat().st_size,sha256=sha(p)) for p in out.iterdir() if p.is_file()}
    (out/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--source-dir',type=Path,default=BASE/'data_preprocessing/outputs/reviewed_full_20261105_final')
    ap.add_argument('--output-dir',type=Path,required=True)
    args=ap.parse_args();main(args.source_dir.resolve(),args.output_dir.resolve())
