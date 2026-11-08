"""CSV 读取及基础检查；未来的数据源适配器应返回同一记录结构。"""
import csv
from datetime import datetime
import hashlib
import io
from pathlib import Path
import re

from convert_encoding import decode_source
from relevance import clean_text


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def read_csv(path, encoding=None):
    path = Path(path).resolve()
    data = path.read_bytes()
    text, detected = decode_source(data, encoding)
    csv.field_size_limit(100_000_000)
    reader = csv.reader(io.StringIO(text.removeprefix("\ufeff"), newline=""), strict=True)
    header = next(reader, [])
    if not header or len(header) != len(set(header)):
        raise ValueError("CSV 缺少表头或表头重复")
    records = []
    digest = sha256(data)
    for number, values in enumerate(reader, 2):
        start = records[-1]["source_physical_line_end"] + 1 if records else 2
        if len(values) != len(header):
            # 不静默跳过坏行，避免列错位产生不可追溯结果。
            raise ValueError(f"CSV 第 {number} 条记录列数不一致")
        records.append({"raw": dict(zip(header, values)), "source_csv_row": number,
                        "source_file": str(path), "source_sha256": digest,
                        "source_physical_line_start": start,
                        "source_physical_line_end": reader.line_num})
    return header, records, {"path": str(path), "sha256": sha256(data), "encoding": detected}


def validate(record):
    raw = record["raw"]
    errors, notes = [], []
    if not re.fullmatch(r"[0-9]+", raw.get("weibo_id", "").strip()):
        errors.append("invalid_weibo_id")
    text = clean_text(raw.get("content", ""))
    if not text:
        errors.append("missing_content")
    try:
        datetime.fromisoformat(raw.get("created_at", "").strip().replace("Z", "+00:00"))
    except ValueError:
        errors.append("invalid_created_at")
    if "\ufffd" in text or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", text):
        errors.append("possible_encoding_damage")
    if re.search(r"(?:锟斤拷|烫烫烫|Ã.|Â.|æ.|å.){2,}", text):
        errors.append("possible_mojibake")
    for name in ("comments_count", "likes_count", "reposts_count"):
        value = raw.get(name, "").strip()
        if value and not re.fullmatch(r"[0-9]+", value):
            errors.append("invalid_" + name)
    complete = raw.get("long_text_complete", "").strip().lower()
    if complete == "false":
        errors.append("incomplete_long_text")
    elif complete == "":
        # 空标记不是已知截断，仍保留备注供复核。
        notes.append("long_text_completeness_unknown")
    elif complete != "true":
        errors.append("invalid_long_text_complete")
    return {**record, "content_clean": text, "errors": errors, "notes": notes}


def csv_bytes(rows, fields):
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction="raise", lineterminator="\r\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8-sig")
