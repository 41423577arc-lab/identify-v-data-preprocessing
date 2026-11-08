import json
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYTHON = ROOT / "WeiboSpider" / ".browser-venv" / "Scripts" / "python.exe"
RESUME = ROOT / "data_preprocessing" / "resume_full_comment_collection.py"
OUT = ROOT / "WeiboSpider" / "output" / "comment-full-20261103"
MONITOR = ROOT / "data_preprocessing" / "monitor_comment_crawl.py"
SUPERVISOR_LOG = OUT / "comment-crawl-supervisor.log"
STATUS_JSON = OUT / "comment-crawl-status.json"


def log(message):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}"
    with SUPERVISOR_LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line, flush=True)


def monitor_loop():
    while True:
        try:
            subprocess.run([str(PYTHON), str(MONITOR)], cwd=str(ROOT), check=False)
            return
        except Exception as exc:
            log(f"monitor_error={type(exc).__name__}")
            time.sleep(5)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    monitor = threading.Thread(target=monitor_loop, daemon=True)
    monitor.start()
    log("supervisor_started")
    while True:
        proc = subprocess.Popen([str(PYTHON), str(RESUME)], cwd=str(ROOT), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
        log(f"resume_started pid={proc.pid}")
        for line in proc.stdout:
            text = line.rstrip()
            if text:
                with SUPERVISOR_LOG.open("a", encoding="utf-8") as f:
                    f.write(text + "\n")
        code = proc.wait()
        log(f"resume_exit code={code}")
        try:
            status = json.loads(STATUS_JSON.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError, UnicodeError):
            status = {}
        if status.get("total") and status.get("pending") == 0:
            log("supervisor_finished")
            return 0
        # The resume script stops when it encounters a global access/login block.
        # Do not spin on a blocked queue; leave the status and CSV for inspection.
        if code != 0:
            log("supervisor_stopped_nonzero")
            return code
        time.sleep(3)


if __name__ == "__main__":
    raise SystemExit(main())
