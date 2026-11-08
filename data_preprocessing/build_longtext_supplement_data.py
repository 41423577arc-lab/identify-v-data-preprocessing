import csv
import json
import os
import re
from datetime import datetime
from pathlib import Path

from workflow_store import connect


ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "data_preprocessing" / "outputs" / "八爪鱼数据_20261024" / "cleaned_weibo.csv"
OUT_DIR = ROOT / "data_preprocessing" / "outputs" / "longtext-supplement-20261103"
OUT_DIR.mkdir(parents=True, exist_ok=True)
OUT_JSON = OUT_DIR / "longtext_supplement_data.json"
STAGING_DB = "weibo_stage_longtext_20261102"
STAGING_KEYWORD = "octopus_longtext_20261103"
START = datetime(2026, 9, 24)
END = datetime(2026, 10, 16)


def parse_dt(value):
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    return None


def clean_id(value):
    text = str(value or "").strip()
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    return text


def strip_tracking(url):
    return str(url or "").split("?", 1)[0]


def row_view(row, index):
    text = str(row.get("content") or "")
    clean = str(row.get("content_clean") or "")
    marker = "展开c" if "展开c" in text or "展开c" in clean else ("展开" if "展开" in text or "展开" in clean else "")
    mid = clean_id(row.get("weibo_id"))
    return {
        "序号": index,
        "微博ID": mid,
        "发布时间": row.get("created_at", ""),
        "作者": row.get("user_name", ""),
        "微博URL": strip_tracking(row.get("from_链接", "")),
        "原始正文": text,
        "原始正文清洗": clean,
        "原始长文完整": row.get("long_text_complete", ""),
        "正文标记": marker,
        "相关性标签": row.get("relevance_label", ""),
        "决策": row.get("decision", ""),
        "关键词": row.get("keyword", ""),
        "评论数": row.get("comments_count", ""),
        "点赞数": row.get("likes_count", ""),
        "转发数": row.get("reposts_count", ""),
        "来源超话": row.get("来源超话", ""),
        "合并关键词": row.get("merged_keywords", ""),
        "来源文件": row.get("source_files", "") or row.get("batch_source_file", ""),
    }


def main():
    with SOURCE.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        filtered = []
        outside = 0
        unparsable = 0
        for raw in reader:
            dt = parse_dt(raw.get("created_at"))
            if dt is None:
                unparsable += 1
                continue
            if START <= dt < END:
                filtered.append(raw)
            else:
                outside += 1

    marked = [r for r in filtered if "展开" in str(r.get("content") or "") or "展开" in str(r.get("content_clean") or "")]
    ids = [clean_id(r.get("weibo_id")) for r in marked if clean_id(r.get("weibo_id"))]

    staging = {}
    db = connect(STAGING_DB)
    try:
        with db.cursor() as cur:
            cur.execute("SELECT item_id,payload FROM crawl_items WHERE spider=%s AND keyword=%s", ("tweet_spider_by_keyword", STAGING_KEYWORD))
            for item_id, payload in cur.fetchall():
                obj = json.loads(payload) if isinstance(payload, str) else payload
                staging[clean_id(item_id)] = obj
    finally:
        db.close()

    main_complete = {}
    if ids:
        db = connect("weibo_data")
        try:
            with db.cursor() as cur:
                placeholders = ",".join(["%s"] * len(ids))
                cur.execute(
                    f"SELECT item_id,payload FROM crawl_items WHERE spider=%s AND item_id IN ({placeholders})",
                    ["tweet_spider_by_keyword", *ids],
                )
                for item_id, payload in cur.fetchall():
                    obj = json.loads(payload) if isinstance(payload, str) else payload
                    if obj.get("long_text_complete") is True:
                        main_complete[clean_id(item_id)] = obj
        finally:
            db.close()

    skipped_path = ROOT / "WeiboSpider" / "output" / "longtext-supplement-20261103" / "skipped_post_body_not_loaded.jsonl"
    skipped = {}
    if skipped_path.exists():
        for line in skipped_path.read_text(encoding="utf-8-sig").splitlines():
            if line.strip():
                obj = json.loads(line)
                skipped[clean_id(obj.get("item_id") or obj.get("tweet_id"))] = obj
    skipped_single_path = skipped_path.with_suffix(".json")
    if skipped_single_path.exists():
        try:
            single = json.loads(skipped_single_path.read_text(encoding="utf-8-sig"))
            if isinstance(single, list):
                for obj in single:
                    skipped[clean_id(obj.get("item_id") or obj.get("tweet_id"))] = obj
            elif isinstance(single, dict):
                skipped[clean_id(single.get("item_id") or single.get("tweet_id"))] = single
        except (ValueError, UnicodeError):
            pass

    def augment(view):
        mid = view["微博ID"]
        stage = staging.get(mid)
        main = main_complete.get(mid)
        if main:
            body = str(main.get("content") or "")
            view.update({
                "补充正文": body,
                "补充正文状态": "already_complete_in_main_db",
                "补充正文来源": main.get("long_text_source") or "main_db",
                "补采长文完整": True,
                "补采原因": main.get("long_text_completeness_reason") or "已有正式库完整正文",
                "补采输出关键词": "",
                "补采URL": main.get("url") or view["微博URL"],
                "补采备注": "原正式库已存在完整正文，本次不重复请求",
            })
        elif stage:
            complete = stage.get("long_text_complete") is True
            view.update({
                "补充正文": str(stage.get("content") or ""),
                "补充正文状态": "supplemented_complete" if complete else "supplemented_incomplete",
                "补充正文来源": stage.get("long_text_source") or "",
                "补采长文完整": complete,
                "补采原因": stage.get("long_text_completeness_reason") or "",
                "补采输出关键词": stage.get("keyword") or STAGING_KEYWORD,
                "补采URL": stage.get("url") or view["微博URL"],
                "补采备注": "使用现有 run_browser_tweets.py 浏览器正文采集器",
            })
        elif mid in skipped:
            view.update({
                "补充正文": "",
                "补充正文状态": "post_body_not_loaded",
                "补充正文来源": "",
                "补采长文完整": False,
                "补采原因": "正文未加载，采集器安全停止后记录并跳过",
                "补采输出关键词": STAGING_KEYWORD,
                "补采URL": view["微博URL"],
                "补采备注": "未重复请求；详见 skipped_post_body_not_loaded.jsonl",
            })
        else:
            view.update({
                "补充正文": "",
                "补充正文状态": "not_found_in_supplement_db",
                "补充正文来源": "",
                "补采长文完整": False,
                "补采原因": "未找到补采记录",
                "补采输出关键词": STAGING_KEYWORD,
                "补采URL": view["微博URL"],
                "补采备注": "",
            })
        return view

    filtered_views = [augment(row_view(row, i)) for i, row in enumerate(filtered, 1)]
    marked_views = [augment(row_view(row, i)) for i, row in enumerate(marked, 1)]
    status_counts = {}
    for row in marked_views:
        status = row["补充正文状态"]
        status_counts[status] = status_counts.get(status, 0) + 1

    out = {
        "source": str(SOURCE),
        "filter_start": START.isoformat(sep=" "),
        "filter_end_exclusive": END.isoformat(sep=" "),
        "date_filter_inclusive_end": "2026-10-15",
        "filtered_count": len(filtered),
        "outside_count": outside,
        "unparsable_count": unparsable,
        "marked_count": len(marked),
        "staging_keyword": STAGING_KEYWORD,
        "staging_db": STAGING_DB,
        "main_complete_count": len(main_complete),
        "staging_row_count": len(staging),
        "skipped_count": len(skipped),
        "status_counts": status_counts,
        "filtered_rows": filtered_views,
        "marked_rows": marked_views,
    }
    OUT_JSON.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({k: out[k] for k in ("filtered_count", "outside_count", "unparsable_count", "marked_count", "main_complete_count", "staging_row_count", "skipped_count", "status_counts")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
