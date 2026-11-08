import json
import csv
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPIDER = ROOT / "WeiboSpider" / "weibospider" / "run_browser_comments.py"
PYTHON = ROOT / "WeiboSpider" / ".browser-venv" / "Scripts" / "python.exe"
OUT = ROOT / "WeiboSpider" / "output" / "comment-full-20261103"
PROFILE = ROOT / "WeiboSpider" / "output" / "browser-comments" / "browser-profile"
MASTER = OUT / "targets.json"
QUEUE = OUT / "resume_targets.json"
QUEUE_CSV = OUT / "comment-remaining-queue.csv"
EVIDENCE = OUT / "manual_skips.jsonl"

STOP_PREFIXES = (
    "manual_", "navigation_access_limited", "unexpected_destination", "post_redirected",
    "browser_error_page", "browser_navigation_",
)
MISSING_REASONS = (
    "comments_unknown_timeout", "post_redirected_login_or_removed", "browser_error_page",
    "navigation_access_limited_404", "comments_loading_timeout",
)


def load_json(path, fallback):
    if not path.exists():
        return fallback
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError):
        return fallback


def current_statuses():
    payload = load_json(OUT / "comments.json", {})
    return {str(t.get("tweet_id")): t for t in payload.get("targets", [])}


def append_evidence(target, reason):
    item = {
        "at": datetime.now(timezone(timedelta(hours=8))).isoformat(),
        "tweet_id": target.get("tweet_id"),
        "url": target.get("url"),
        "action": "skip_once_after_runner_stop",
        "reason": reason,
    }
    with EVIDENCE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(item, ensure_ascii=False) + "\n")


def write_remaining_csv(queue, statuses):
    with QUEUE_CSV.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["tweet_id", "url", "published", "status", "reason"])
        writer.writeheader()
        for target in queue:
            row = statuses.get(str(target["tweet_id"]), {})
            writer.writerow({
                "tweet_id": target.get("tweet_id", ""),
                "url": target.get("url", ""),
                "published": target.get("published", ""),
                "status": row.get("status", "pending"),
                "reason": row.get("reason", ""),
            })


def mark_missing_or_deleted(statuses):
    updates = []
    for mid, row in statuses.items():
        reason = str(row.get("reason") or "")
        if row.get("status") in ("needs_attention", "incomplete") and any(reason.startswith(prefix) for prefix in MISSING_REASONS):
            updates.append((mid, reason))
    if not updates:
        return statuses
    conn = sqlite3.connect(OUT / "comments.sqlite3")
    now = datetime.now(timezone(timedelta(hours=8))).isoformat()
    with conn:
        for mid, reason in updates:
            updated_reason = f"微博不存在或疑似已删除；原始原因={reason}"
            conn.execute("UPDATE targets SET status=?, reason=?, updated_at=? WHERE tweet_id=?",
                         ("skipped_missing_or_deleted", updated_reason, now, mid))
            statuses[mid]["status"] = "skipped_missing_or_deleted"
            statuses[mid]["reason"] = updated_reason
    conn.close()
    for mid, reason in updates:
        append_evidence({"tweet_id": mid, "url": statuses[mid].get("url")}, f"微博不存在或疑似已删除；原始原因={reason}")
    return statuses


all_targets = load_json(MASTER, [])
statuses = current_statuses()
statuses = mark_missing_or_deleted(statuses)
queue = [t for t in all_targets if statuses.get(str(t["tweet_id"]), {}).get("status") in (None, "pending", "running")]
print(f"resume_start total={len(all_targets)} queue={len(queue)}", flush=True)
write_remaining_csv(queue, statuses)

while queue:
    QUEUE.write_text(json.dumps(queue, ensure_ascii=False, indent=2), encoding="utf-8")
    cmd = [str(PYTHON), str(SPIDER), "--targets", str(QUEUE), "--output", str(OUT), "--profile", str(PROFILE),
           "--interval", "2", "--max-passes", "500", "--idle-passes", "4", "--max-reply-threads", "1000",
           "--export-every", "20", "--timeout", "30"]
    proc = subprocess.run(cmd, cwd=str(SPIDER.parent), text=True, capture_output=True)
    statuses = current_statuses()
    statuses = mark_missing_or_deleted(statuses)
    processed = []
    blocked = []
    for target in queue:
        row = statuses.get(str(target["tweet_id"]), {})
        status = row.get("status")
        if status in ("visible_exhausted", "incomplete", "needs_attention", "skipped_missing_or_deleted"):
            processed.append(target)
            reason = str(row.get("reason") or "")
            if any(reason.startswith(prefix) for prefix in STOP_PREFIXES):
                blocked.append((target, reason or status))
    print(f"runner_exit={proc.returncode} processed={len(processed)} queue_before={len(queue)}", flush=True)
    if proc.stdout:
        print(proc.stdout[-2000:], flush=True)
    if proc.stderr:
        print(proc.stderr[-2000:], file=sys.stderr, flush=True)
    if blocked:
        target, reason = blocked[0]
        write_remaining_csv(queue, statuses)
        print(f"stop_blocked tweet_id={target['tweet_id']} reason={reason}", flush=True)
        break
    if not processed:
        write_remaining_csv(queue, statuses)
        print("stop_no_progress", flush=True)
        break
    for target in processed:
        row = statuses.get(str(target["tweet_id"]), {})
        if row.get("reason") == "PermissionError" or row.get("status") == "needs_attention":
            append_evidence(target, row.get("reason") or row.get("status"))
    done_ids = {str(t["tweet_id"]) for t in processed}
    queue = [t for t in queue if str(t["tweet_id"]) not in done_ids]
    write_remaining_csv(queue, statuses)
    print(f"resume_queue={len(queue)}", flush=True)

print(f"resume_finished remaining={len(queue)}", flush=True)
