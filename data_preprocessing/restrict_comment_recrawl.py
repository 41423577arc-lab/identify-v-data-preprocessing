"""Build a fresh, recoverable recrawl queue containing only reviewed speech-related posts."""
from collections import defaultdict
import argparse
import csv
import hashlib
import json
from pathlib import Path
import shutil
import sys

BASE=Path(__file__).resolve().parent.parent
SOURCE_DIR=BASE/'data_preprocessing/outputs/reviewed_20260924_20261015_20261106'
REVIEW=SOURCE_DIR/'unfinished_crawl_review_20261106/unfinished_crawl_posts_review.csv'
SOURCE_QUEUE=BASE/'WeiboSpider/output/comment-recrawl-20261105/targets.json'
SOURCE_DB=BASE/'WeiboSpider/output/comment-recrawl-20261105/comments.sqlite3'
COMMENTS=BASE/'data_preprocessing/outputs/reviewed_full_20261105_final/comment_source_records.jsonl'
WEIBO_DIR=BASE/'WeiboSpider'


def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
    return h.hexdigest()


def main(out):
    partial_files={'targets.json','source_targets_backup.json','archived_excluded_targets.json','selection.json'}
    if out.exists():
        existing={p.name for p in out.iterdir()}
        if existing!=partial_files:raise ValueError(f'Output path already exists with unexpected contents: {out}')
    else:out.mkdir(parents=True)
    with REVIEW.open(encoding='utf-8-sig',newline='') as f:
        reviewed=list(csv.DictReader(f))
    chosen={r['weibo_id']:r for r in reviewed if r['speech_related'].casefold()=='true'}
    if len(chosen)!=11:raise ValueError(f'Expected the 11 manually reviewed related posts, got {len(chosen)}')
    source_targets=json.loads(SOURCE_QUEUE.read_text(encoding='utf-8'))
    source_by_id={str(x['tweet_id']):x for x in source_targets}
    if len(source_by_id)!=len(source_targets) or not set(chosen)<=set(source_by_id):
        raise ValueError('Reviewed IDs do not exactly resolve against the prior task queue')
    selected=[source_by_id[mid] for mid in sorted(chosen)]
    excluded=[t for t in source_targets if str(t['tweet_id']) not in chosen]
    (out/'targets.json').write_text(json.dumps(selected,ensure_ascii=False,indent=2),encoding='utf-8')
    (out/'source_targets_backup.json').write_text(json.dumps(source_targets,ensure_ascii=False,indent=2),encoding='utf-8')
    (out/'archived_excluded_targets.json').write_text(json.dumps(excluded,ensure_ascii=False,indent=2),encoding='utf-8')
    selection=[]
    for t in selected:
        r=chosen[str(t['tweet_id'])]
        selection.append(dict(tweet_id=t['tweet_id'],url=t['url'],review_screen=r['speech_screen'],
            review_note=r['review_note'],prior_recrawl_reason=r.get('recrawl_reason','')))
    (out/'selection.json').write_text(json.dumps(selection,ensure_ascii=False,indent=2),encoding='utf-8')

    # Start from prior raw comment observations for the 11 selected posts so that refresh
    # adds to their existing comments. Unselected IDs are never inserted as active targets.
    records=defaultdict(list)
    with COMMENTS.open(encoding='utf-8') as f:
        for line in f:
            observation=json.loads(line);c=observation['record'];mid=str(c.get('tweet_id',''))
            if mid in chosen:
                key=c.get('record_key') or observation.get('store_record_key')
                if key:records[(mid,key)].append(observation)
    if not all(records):raise ValueError('No existing comment observations found for selected posts')
    sys.path.insert(0,str(WEIBO_DIR/'weibospider'))
    from browser_comment_store import BrowserCommentStore
    store=BrowserCommentStore(out/'comments.sqlite3')
    try:
        store.register(selected)
        with store.conn:
            for (mid,key),observations in records.items():
                obs=max(observations,key=lambda x:(not x['record'].get('content_truncated',False),
                    x.get('last_seen',''),len(x['record'].get('content',''))))
                payload=dict(obs['record'])
                root=payload.get('root_record_key') or payload.get('root_comment_key')
                first=min((x.get('first_seen','') for x in observations if x.get('first_seen')),default='')
                last=max((x.get('last_seen','') for x in observations if x.get('last_seen')),default='')
                store.conn.execute('INSERT OR IGNORE INTO comments VALUES (?,?,?,?,?,?)',
                    (key,mid,root,json.dumps(payload,ensure_ascii=False),first,last))
        # Store initialization and refresh never mark old data as newly complete.
        store.export(out/'comments.json')
    finally:store.close()
    summary=dict(selected_targets=len(selected),excluded_from_active_queue=len(excluded),
        selected_existing_comment_records=sum(len({k for m,k in records if m==mid}) for mid in chosen),
        selected_ids=sorted(chosen),source_queue=str(SOURCE_QUEUE),source_queue_sha256=sha(SOURCE_QUEUE),
        source_comments_db=str(SOURCE_DB),source_comments_db_sha256=sha(SOURCE_DB),
        source_processed_comments=str(COMMENTS),source_processed_comments_sha256=sha(COMMENTS),
        active_queue_contains_only_reviewed_speech_related_ids=True,
        archived_source_queue_preserved=True)
    (out/'queue_manifest.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    (out/'README.md').write_text(
        '# 11 篇微博评论定向补采\n\n'
        '活动目标文件 targets.json 仅含人工复核为与辱骂/粉圈敌意语境相关的 11 个微博。旧队列中其余 142 个目标移入 archived_excluded_targets.json，不会传给本轮采集器；source_targets_backup.json 保存原始 153 条队列。原始数据库不修改。\n\n'
        '新 SQLite 保留这 11 篇此前已采到的原始评论，并将目标状态设为待补采；collector 使用 --refresh 重新访问这 11 篇、保留旧评论并补入新增观测。不会向 MySQL 镜像写入。\n\n'
        '手工审核语境及 ID 选择见 selection.json。history comments.json 是启动前的旧记录快照，实际采集后由采集器更新。\n',encoding='utf-8')
    (out/'queue_builder.py').write_text(Path(__file__).read_text(encoding='utf-8'),encoding='utf-8')
    print(json.dumps(summary,ensure_ascii=False,indent=2))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output-dir',type=Path,default=WEIBO_DIR/'output/comment-hate-related-20261106')
    main(p.parse_args().output_dir.resolve())
