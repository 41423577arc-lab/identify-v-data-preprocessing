import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
source = ROOT / "data_preprocessing" / "outputs" / "longtext-supplement-20261103" / "longtext_supplement_data.json"
out_dir = ROOT / "WeiboSpider" / "output" / "comment-full-20261103"
out_dir.mkdir(parents=True, exist_ok=True)
targets = []
data = json.loads(source.read_text(encoding="utf-8"))
for row in data["filtered_rows"]:
    url = str(row.get("微博URL") or "").strip().split("?", 1)[0]
    mid = str(row.get("微博ID") or "").strip()
    m = re.match(r"https?://(?:www\.)?weibo\.com/(\d+)/(\w+)$", url)
    if not m or not mid.isdigit():
        raise ValueError(f"invalid target: {mid} {url}")
    targets.append({
        "tweet_id": mid,
        "author_id": m.group(1),
        "url": url,
        "comments_count_at_tweet_crawl": int(row.get("评论数") or 0),
        "published": row.get("发布时间") or "",
    })
if len(targets) != len({t["tweet_id"] for t in targets}):
    raise ValueError("duplicate tweet IDs")
out = out_dir / "targets.json"
out.write_text(json.dumps(targets, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps({"target_count": len(targets), "output": str(out)}, ensure_ascii=False))
