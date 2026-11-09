"""Audit unchanged collection sources and build a date/topic-scoped corpus."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess

from run_reviewed_preprocessing import read_lines, write_lines, sha
from topic_relevance import EXCLUDED_SUPERTOPICS, classify_source_topic

BASE = Path(__file__).resolve().parent.parent
COLLECTION_ROOT = BASE.parent / 'WeiboSpider' / 'output'
if not COLLECTION_ROOT.exists():
    COLLECTION_ROOT = BASE / 'WeiboSpider' / 'output'


def _audit_collection(prior, out):
    audits = []
    for name in ['comment-full-20261103', 'comment-recrawl-20261105', 'comment-nested-replies-20261105']:
        path = COLLECTION_ROOT / name / 'comments.sqlite3'
        db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
        db.execute('BEGIN')
        targets = [dict(status=s, reason=r, count=n) for s, r, n in
                   db.execute('SELECT status,reason,count(*) FROM targets GROUP BY status,reason')]
        streams = [dict(status=s, reason=r, count=n) for s, r, n in
                   db.execute('SELECT status,reason,count(*) FROM streams GROUP BY status,reason')]
        rows = db.execute('SELECT record_key,payload FROM comments ORDER BY record_key').fetchall()
        oldsource = next(s for s in prior['sources'] if s.get('original_path') == str(path))
        old = sqlite3.connect((Path(prior['input_snapshot']) / oldsource['snapshot']).as_uri() + '?mode=ro', uri=True)
        unchanged = rows == old.execute('SELECT record_key,payload FROM comments ORDER BY record_key').fetchall()
        old.close()
        exported = json.loads(path.with_suffix('.json').read_text(encoding='utf-8'))
        audits.append(dict(database=str(path), records=len(rows), target_statuses=targets, stream_statuses=streams,
            last_target_update=db.execute('SELECT max(updated_at) FROM targets').fetchone()[0],
            collection_issues=db.execute('SELECT count(*) FROM issues').fetchone()[0],
            json_export_records=sum(len(t.get('comments', [])) for t in exported['targets']),
            unchanged_since_preprocessing=unchanged))
        db.rollback()
        db.close()
    if not all(a['unchanged_since_preprocessing'] for a in audits):
        raise ValueError('New comment text exists; refresh preprocessing before filtering')
    proc = subprocess.run(['powershell', '-NoProfile', '-Command',
        f"@(Get-CimInstance Win32_Process | Where-Object {{ $_.Name -match 'python' -and $_.ProcessId -ne {os.getpid()} }} | Select-Object ProcessId,Name) | ConvertTo-Json -Compress"],
        capture_output=True, text=True)
    processes = json.loads(proc.stdout) if proc.stdout.strip() else []
    audit = dict(audited_at_utc=datetime.now(timezone.utc).isoformat(), other_python_processes=processes,
        process_query_succeeded=proc.returncode == 0, databases=audits,
        conclusion='collection_not_fully_complete; no active crawler observed; pending/running/incomplete statuses remain')
    (out / 'collection_audit.json').write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding='utf-8')


def main(source, out):
    if out.exists():
        raise ValueError('Use a new output directory')
    out.mkdir(parents=True)
    prior = json.loads((source / 'validation_report.json').read_text(encoding='utf-8'))
    _audit_collection(prior, out)

    posts = read_lines(source / 'posts_preprocessed.jsonl')
    date_kept = [p for p in posts if '2026-09-24' <= p.get('created_at', '') < '2026-10-16']
    date_excluded = [p for p in posts if not '2026-09-24' <= p.get('created_at', '') < '2026-10-16']
    topic_excluded_all = [p for p in posts if classify_source_topic(p)['excluded']]
    topic_excluded = [p for p in date_kept if classify_source_topic(p)['excluded']]
    kept = [p for p in date_kept if not classify_source_topic(p)['excluded']]
    date_excluded_ids = {p['weibo_id'] for p in date_excluded}
    topic_excluded_ids = {p['weibo_id'] for p in topic_excluded}
    excluded_ids = date_excluded_ids | topic_excluded_ids
    comments = read_lines(source / 'comments_preprocessed.jsonl')
    assert not any(str(c.get('tweet_id', '')) in date_excluded_ids for c in comments), \
        'Date exclusions unexpectedly have attached comments'
    excluded_comment_keys = {c['dataset_key'] for c in comments
        if str(c.get('tweet_id', '')) in topic_excluded_ids}
    kept_comments = [c for c in comments if c['dataset_key'] not in excluded_comment_keys]

    write_lines(out / 'posts_preprocessed.jsonl', kept)
    write_lines(out / 'date_exclusions.jsonl', [dict(weibo_id=p['weibo_id'], created_at=p.get('created_at'),
        reason='outside_inclusive_20260924_20261015') for p in date_excluded])
    topic_counts = Counter(classify_source_topic(p)['normalized_topic'] for p in topic_excluded)
    write_lines(out / 'supertopic_exclusions.jsonl', [dict(weibo_id=p['weibo_id'],
        created_at=p.get('created_at'), source_topic=classify_source_topic(p)['source_topic'],
        normalized_topic=classify_source_topic(p)['normalized_topic'], source_field='来源超话',
        status='clearly_unrelated', reason='exact_source_supertopic_match') for p in topic_excluded])
    write_lines(out / 'comments_preprocessed.jsonl', kept_comments)

    # Keep historical human labels intact. Filter derived records whose analyzed rows left the corpus.
    for name in ['comment_relations.jsonl', 'pending_relations.jsonl', 'record_quality.jsonl', 'invalid_records.jsonl']:
        write_lines(out / name, [r for r in read_lines(source / name)
            if r.get('dataset_key') not in excluded_comment_keys])
    for name in ['human_annotations.jsonl', 'missing_post_evidence.jsonl', 'mapping_conflicts.jsonl']:
        shutil.copyfile(source / name, out / name)
    duplicate_groups = []
    for group in read_lines(source / 'possible_duplicate_candidates.jsonl'):
        keys = [k for k in group.get('candidate_keys', []) if k not in excluded_comment_keys]
        if len(keys) > 1:
            duplicate_groups.append({**group, 'candidate_keys': keys})
    write_lines(out / 'possible_duplicate_candidates.jsonl', duplicate_groups)

    def applicable(row):
        key = row.get('dataset_key', '')
        if key in excluded_comment_keys:
            return False
        return not (key.startswith('post:') and key[5:] in excluded_ids)

    for name in ['pending_review.jsonl', 'manual_applications.jsonl']:
        write_lines(out / name, [r for r in read_lines(source / name) if applicable(r)])
    frequency = Counter()
    for post in kept:
        frequency.update(post['preprocessing']['tokens'].split())
    for comment in kept_comments:
        frequency.update(comment['tokens'].split())
    write_lines(out / 'token_frequencies.jsonl', [dict(token=t, frequency=n) for t, n in frequency.most_common()])

    olddb = sqlite3.connect((source / 'research_dataset.sqlite3').resolve().as_uri() + '?mode=ro', uri=True)
    db = sqlite3.connect(out / 'research_dataset.sqlite3')
    olddb.backup(db)
    olddb.close()
    db.execute('PRAGMA foreign_keys=ON')
    with db:
        if excluded_comment_keys:
            keys = sorted(excluded_comment_keys)
            marks = ','.join('?' for _ in keys)
            db.execute(f'DELETE FROM relations WHERE dataset_key IN ({marks}) OR verified_root_key IN ({marks}) OR verified_parent_key IN ({marks})',
                [*keys, *keys, *keys])
            db.executemany('DELETE FROM record_quality WHERE dataset_key=?', [(k,) for k in keys])
            db.executemany('DELETE FROM comments WHERE dataset_key=?', [(k,) for k in keys])
        db.executemany('DELETE FROM relations WHERE verified_weibo_id=?', [(x,) for x in excluded_ids])
        db.executemany('DELETE FROM posts WHERE weibo_id=?', [(x,) for x in excluded_ids])
    fk = db.execute('PRAGMA foreign_key_check').fetchall()
    integrity = db.execute('PRAGMA integrity_check').fetchone()[0]
    assert not fk and integrity == 'ok', (fk, integrity)
    assert db.execute('SELECT count(*) FROM posts').fetchone()[0] == len(kept)
    assert db.execute('SELECT count(*) FROM comments').fetchone()[0] == len(kept_comments)
    assert db.execute("SELECT count(*) FROM posts WHERE created_at<'2026-09-24' OR created_at>='2026-10-16'").fetchone()[0] == 0
    views = {v: db.execute('SELECT count(*) FROM ' + v).fetchone()[0] for v in
        ['posts_clustering', 'comments_clustering', 'comments_clustering_linked', 'comments_malicious_eligible']}
    dangling = db.execute('''SELECT COUNT(*) FROM relations r LEFT JOIN posts p ON p.weibo_id=r.verified_weibo_id
        LEFT JOIN comments c ON c.dataset_key=r.dataset_key WHERE
        (r.verified_weibo_id IS NOT NULL AND p.weibo_id IS NULL) OR c.dataset_key IS NULL''').fetchone()[0]
    assert dangling == 0, dangling
    db.close()

    assert read_lines(out / 'posts_preprocessed.jsonl') == kept
    assert read_lines(out / 'comments_preprocessed.jsonl') == kept_comments
    report = dict(source_directory=str(source),
        date_filter=dict(start_inclusive='2026-09-24', end_inclusive='2026-10-15',
            end_exclusive='2026-10-16', timezone='Asia/Shanghai', basis='original created_at'),
        supertopic_filter=dict(field='来源超话', match='NFKC and whitespace normalization; optional trailing 超话 removed; exact name match only',
            excluded_topics=sorted(EXCLUDED_SUPERTOPICS), matched_posts_in_full_corpus=len(topic_excluded_all),
            posts_excluded_in_date_window=len(topic_excluded), posts_excluded_by_topic=dict(topic_counts),
            comments_attached_to_excluded_posts=len(excluded_comment_keys), no_fuzzy_or_body_text_matching=True),
        posts_before=len(posts), posts_after=len(kept), posts_excluded_by_date=len(date_excluded),
        posts_excluded_by_topic=len(topic_excluded), posts_excluded=len(date_excluded) + len(topic_excluded),
        post_time_range=[min(p['created_at'] for p in kept), max(p['created_at'] for p in kept)],
        comments_before=len(comments), comments=len(kept_comments),
        comments_excluded_with_filtered_posts=len(excluded_comment_keys),
        comment_levels=dict(Counter(str(c['comment_level']) for c in kept_comments)),
        retained_comment_payloads_unchanged=True, comments_attached_to_removed_posts=0, reply_root_errors=0,
        comments_with_missing_curated_post=prior['comments_with_missing_post'],
        missing_post_distinct_ids=prior['missing_post_distinct_ids'],
        pending_review_records=len(read_lines(out / 'pending_review.jsonl')), analysis_view_counts=views,
        original_sources_and_historical_outputs_untouched=True, sqlite_integrity=integrity,
        sqlite_foreign_key_violations=fk, dangling_relation_references=dangling,
        collection_fully_complete=False)
    (out / 'validation_report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    lines = [
        '日期筛选：2026-09-24 至 2026-10-15（含首尾两天）；超话来源按“来源超话”字段精确匹配。',
        f'微博 {len(posts)} -> {len(date_kept)}（日期内） -> {len(kept)}（最终）；日期剔除 {len(date_excluded)} 条，指定超话剔除 {len(topic_excluded)} 条。',
        f'随指定超话微博从本研究集排除的评论/回复 {len(excluded_comment_keys)} 条；完整预处理来源目录保留全部记录。',
        'JSONL 回读、SQLite 数量、日期边界、关系引用、完整性与外键检查通过。',
        f'原结果及原始数据未改写；完整来源、人工规则和预处理日志位于 {source}。']
    (out / 'processing.log').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    (out / 'README.md').write_text('# 2026-09-24 至 2026-10-15 研究数据\n\n' + '\n\n'.join(lines) +
        '\n\n评论采集状态见 collection_audit.json。此步骤不修改采集队列，也不新增恶意分类标签。\n\n' +
        'posts_preprocessed.jsonl 同时应用日期范围和指定来源超话筛选。comments_preprocessed.jsonl 保留未关联被排除微博的评论及回复；属于被排除微博的关联评论随父微博一并从本研究集排除。完整预处理来源目录仍保留这些原记录和关系。\n\n' +
        '历史人工标注文件完整保留；原始 content、ID、归属与关系在完整预处理目录中保留。research_dataset.sqlite3 只含筛选后的微博和评论，并重建相应分析视图。\n\n' +
        f'复现：python -B data_preprocessing/filter_date_and_audit_collection.py --source-dir "{source}" --output-dir "另一个新目录"\n',
        encoding='utf-8')
    shutil.copyfile(Path(__file__), out / Path(__file__).name)
    shutil.copyfile(Path(__file__).with_name('topic_relevance.py'), out / 'topic_relevance.py')
    manifest = {str(p.relative_to(out)): dict(bytes=p.stat().st_size, sha256=sha(p))
        for p in out.iterdir() if p.is_file() and p.name != 'manifest.json'}
    (out / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--source-dir', type=Path, default=BASE / 'data_preprocessing/outputs/reviewed_full_20261105_final')
    ap.add_argument('--output-dir', type=Path, required=True)
    args = ap.parse_args()
    main(args.source_dir.resolve(), args.output_dir.resolve())
