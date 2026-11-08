import csv
import json
import sqlite3
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "WeiboSpider" / "output" / "comment-full-20261103"
DATABASE = OUT / "comments.sqlite3"
STATUS_JSON = OUT / "comment-crawl-status.json"
HISTORY_CSV = OUT / "comment-crawl-status-history.csv"


def snapshot():
    conn = sqlite3.connect(f"file:{DATABASE.as_posix()}?mode=ro", uri=True)
    counts = dict(conn.execute("SELECT status, COUNT(*) FROM targets GROUP BY status"))
    total = sum(counts.values())
    pending = counts.get("pending", 0) + counts.get("running", 0)
    comments = conn.execute("SELECT COUNT(*) FROM comments").fetchone()[0]
    conn.close()
    return {
        "at": datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds"),
        "total": total,
        "processed": total - pending,
        "pending": pending,
        "comments": comments,
        "statuses": counts,
        "crawler_output": str(OUT),
    }


def write_status(value):
    STATUS_JSON.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    new_file = not HISTORY_CSV.exists()
    with HISTORY_CSV.open("a", encoding="utf-8-sig", newline="") as f:
        fields = ["at", "total", "processed", "pending", "comments", "visible_exhausted", "incomplete", "skipped_missing_or_deleted", "needs_attention", "running"]
        writer = csv.DictWriter(f, fieldnames=fields)
        if new_file:
            writer.writeheader()
        row = {field: value.get(field, "") for field in fields}
        for status, count in value.get("statuses", {}).items():
            if status not in row:
                continue
            row[status] = count
        writer.writerow(row)


def main():
    while True:
        value = snapshot()
        write_status(value)
        print(json.dumps(value, ensure_ascii=False), flush=True)
        if value["total"] and value["pending"] == 0:
            return 0
        time.sleep(10)


if __name__ == "__main__":
    raise SystemExit(main())
