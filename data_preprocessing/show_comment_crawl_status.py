import json
import time
from datetime import datetime
from pathlib import Path

import sys

status_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / "WeiboSpider/output/comment-full-20261103/comment-crawl-status.json"

while True:
    try:
        data = json.loads(status_path.read_text(encoding="utf-8"))
        statuses = data.get("statuses", {})
        print("\033[2J\033[H", end="")
        print("微博评论采集实时状态")
        print("更新时间:", data.get("at", ""))
        print("总微博数:", data.get("total", ""))
        print("已处理:", data.get("processed", ""))
        print("待处理:", data.get("pending", ""))
        print("评论记录:", data.get("comments", ""))
        print("可见结束:", statuses.get("visible_exhausted", 0))
        print("未完成线程:", statuses.get("incomplete", 0))
        print("疑似删除跳过:", statuses.get("skipped_missing_or_deleted", 0))
        print("待关注:", statuses.get("needs_attention", 0))
        print("\n窗口可关闭，后台采集不会停止。")
    except Exception as exc:
        print("状态暂不可读:", type(exc).__name__)
    time.sleep(10)
