"""只读导出微博快照，和旧 CSV 比较新增 ID；不修改任何数据库表。"""
import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
import sys


BASE = Path(__file__).resolve().parent
FIELDS = ["weibo_id", "keyword", "created_at", "user_name", "content", "ip_location",
          "comments_count", "likes_count", "reposts_count", "weibo_url", "long_text_complete"]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def encode_csv(rows):
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=FIELDS, lineterminator="\r\n")
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8-sig")


def text(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value).lower()
    return str(value)


def export_snapshot(baseline, output, new_output, manifest_path):
    baseline, output, new_output, manifest_path = [Path(p).resolve() for p in
                                                 (baseline, output, new_output, manifest_path)]
    if len({baseline, output, new_output, manifest_path}) != 4:
        raise ValueError("输入和各输出路径必须不同")
    baseline_hash = digest(baseline)
    csv.field_size_limit(100_000_000)
    with baseline.open(encoding="utf-8-sig", newline="") as stream:
        before = list(csv.DictReader(stream))
    if before and not {"weibo_id", "keyword"}.issubset(before[0]):
        raise ValueError("旧 CSV 缺少微博 ID 或检索词")
    old_ids = {r["weibo_id"] for r in before}
    old_pairs = {(r["weibo_id"], r["keyword"]) for r in before}
    sys.path.insert(0, str(BASE.parent / "WeiboSpider" / "weibospider"))
    from workflow_store import connect, read_records, totals
    from extract_topic_keywords import TWEET_SPIDERS
    db = connect()
    try:
        # 在只读一致性快照内查询，禁止本事务更新原始表。
        with db.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT")
        records = read_records(db)
        counts = totals(records)
    finally:
        db.rollback()
        db.close()
    rows = []
    for record in records:
        payload = record["payload"]
        if record["spider"] not in TWEET_SPIDERS or not any(payload.get(k) for k in ("content", "text_raw", "text")):
            continue
        row = {
            "weibo_id": record["item_id"], "keyword": record["keyword"],
            "created_at": payload.get("created_at"),
            "user_name": payload.get("user", {}).get("nick_name", payload.get("user", {}).get("screen_name", "")),
            "content": payload.get("content") or payload.get("text_raw") or payload.get("text") or "",
            "ip_location": payload.get("ip_location"),
            "comments_count": payload.get("comments_count"),
            "likes_count": payload.get("attitudes_count", payload.get("likes_count")),
            "reposts_count": payload.get("reposts_count"), "weibo_url": payload.get("url"),
            "long_text_complete": payload.get("long_text_complete"),
        }
        rows.append({k: text(row[k]) for k in FIELDS})
    current_ids = {r["weibo_id"] for r in rows}
    current_pairs = {(r["weibo_id"], r["keyword"]) for r in rows}
    incremental = [r for r in rows if r["weibo_id"] not in old_ids]
    manifest = {
        "baseline_file": str(baseline), "baseline_sha256": baseline_hash,
        "snapshot_file": str(output), "new_ids_file": str(new_output),
        "baseline_rows": len(before), "baseline_unique_ids": len(old_ids),
        "snapshot_rows": len(rows), "snapshot_unique_ids": len(current_ids),
        "new_unique_ids": len(current_ids - old_ids),
        "new_keyword_pairs": len(current_pairs - old_pairs),
        "new_keyword_pairs_on_existing_ids": sum(mid in old_ids for mid, _ in current_pairs - old_pairs),
        "missing_baseline_ids": sorted(old_ids - current_ids),
        "missing_baseline_pairs": sorted(old_pairs - current_pairs),
        "new_id_rows": len(incremental), "database_totals": counts,
        "sql": "SELECT spider,item_id,keyword,payload FROM crawl_items ORDER BY spider,item_id,keyword",
        "transaction": "READ ONLY WITH CONSISTENT SNAPSHOT",
        "field_mapping": {"likes_count": "payload.attitudes_count", "user_name": "payload.user.nick_name",
                          "weibo_url": "payload.url", "content": "payload.content (fallback text_raw/text)"},
    }
    payloads = {output: encode_csv(rows), new_output: encode_csv(incremental)}
    manifest["snapshot_sha256"] = hashlib.sha256(payloads[output]).hexdigest()
    manifest["new_ids_sha256"] = hashlib.sha256(payloads[new_output]).hexdigest()
    payloads[manifest_path] = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if digest(baseline) != baseline_hash:
        raise ValueError("旧 CSV 在处理过程中发生变化")
    for path, data in payloads.items():
        if path.is_symlink() or path.exists() and path.read_bytes() != data:
            raise FileExistsError("输出已存在且不同，或路径为符号链接")
    for path, data in payloads.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            with path.open("xb") as stream:
                stream.write(data)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--new-output", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = export_snapshot(args.baseline, args.output, args.new_output, args.manifest)
    except Exception as error:
        # 数据库异常可能携带连接信息，不直接打印异常内容。
        parser.exit(1, f"只读导出失败：{type(error).__name__}；没有更新原始数据库。\n")
    print(json.dumps(result, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
