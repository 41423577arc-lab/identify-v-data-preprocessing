"""离线微博预处理：分层分类、ID 合并、逐行审计；不连接数据库。"""
import argparse
from collections import defaultdict
from copy import deepcopy
import csv
import json
from pathlib import Path
import re
import sys
import time

from convert_encoding import BASE
from data_io import csv_bytes, read_csv, sha256, validate
from relevance import RuleClassifier, matching_text
from semantic import add_advice

REQUIRED = {"weibo_id", "keyword", "created_at", "content"}
TABLE_META = ["content_clean", "merged_keywords", "source_rows", "source_files",
              "selected_source_row", "record_count", "content_variant_count",
              "game_related", "event_related", "spam_type", "relevance_label",
              "decision", "reason", "matched_rules", "confidence", "quality_flags",
              "semantic_advice", "rule_version"]
AUDIT_FIELDS = ["weibo_id", "source_csv_row", "source_physical_line_start",
               "source_physical_line_end", "source_file", "source_sha256",
               "original_keyword", "merged_keywords", "selected_source_row",
               "is_selected", "dedup_action", "input_relevance_label",
               "input_reason", "input_matched_rules", "game_related", "event_related",
               "spam_type", "relevance_label", "decision", "reason", "matched_rules",
               "confidence", "quality_flags", "group_quality_flags", "signals",
               "related_sentence_share", "semantic_advice", "rule_version"]


def j(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


class Progress:
    """先用小批量试跑估时，再按实测速率更新剩余时间。"""
    def __init__(self, total, enabled=True):
        self.total, self.enabled = total, enabled
        self.started = time.perf_counter()
        self.last = 0.0

    def update(self, done):
        if not self.enabled:
            return
        elapsed = time.perf_counter() - self.started
        if done != self.total and elapsed - self.last < 0.15:
            return
        self.last = elapsed
        fraction = done / max(1, self.total)
        remaining = elapsed / max(1, done) * (self.total - done)
        bar = "#" * int(fraction * 24) + "-" * (24 - int(fraction * 24))
        print(f"\r主题判定 [{bar}] {done}/{self.total} {fraction:6.1%} "
              f"已用 {elapsed:.2f}s 预计剩余 {remaining:.2f}s",
              end="\n" if done == self.total else "", file=sys.stderr, flush=True)


def load_rules(path):
    data = Path(path).read_bytes()
    config = json.loads(data.decode("utf-8-sig"))
    if not config.get("version") or not isinstance(config.get("thresholds"), dict):
        raise ValueError("规则配置缺少 version 或 thresholds")
    if config.get("semantic", {}).get("enabled"):
        raise ValueError("尚无已校准语义模型；请保持 semantic.enabled=false")
    # 同时记录代码和配置指纹，避免同版本号下悄悄改规则。
    code = b"".join((BASE / name).read_bytes() for name in (
        "relevance.py", "data_io.py", "semantic.py", "preprocess.py", "evaluation.py", "convert_encoding.py"))
    return config, f"{config['version']}+{sha256(data)[:10]}.{sha256(code)[:10]}", sha256(data)


def rank(record):
    raw = record["raw"]
    complete = {"true": 2, "": 1, "false": 0}.get(raw.get("long_text_complete", "").lower(), 0)
    other_errors = [e for e in record["errors"] if e != "incomplete_long_text"]
    return (-len(other_errors), complete, len(record["content_clean"]),
            sum(bool(v.strip()) for v in raw.values()), -record["source_csv_row"])


def incompatible_texts(records):
    texts = set()
    for r in records:
        value = matching_text(r["content_clean"]).strip()
        value = re.sub(r"(?:…+|\.{3,}|展开全文)\s*$", "", value).strip()
        if value:
            texts.add(value)
    if len(texts) < 2:
        return False
    longest = max(texts, key=len)
    return any(not longest.startswith(value) for value in texts)


def process_records(records, classifier, version, progress=True, advisor=None):
    """数据源扩展入口：MySQL 只读适配器可传入同一结构，不需改分类器。"""
    prepared = []
    bar = Progress(len(records), progress)
    sample_size = min(32, len(records))
    for index, record in enumerate(records, 1):
        value = validate(record)
        value["classification"] = add_advice(
            classifier.classify(value["content_clean"]), value["content_clean"], classifier.config, advisor)
        prepared.append(value)
        if index == sample_size and progress:
            elapsed = time.perf_counter() - bar.started
            print(f"试跑 {sample_size} 条，主题判定预计总耗时约 "
                  f"{elapsed / sample_size * len(records):.2f}s；另需读写和生成报告，估时随运行更新。",
                  file=sys.stderr)
        bar.update(index)
    groups = defaultdict(list)
    for r in prepared:
        value = r["raw"]["weibo_id"].strip()
        key = value if re.fullmatch(r"[0-9]+", value) else f"invalid-row-{r['source_csv_row']}"
        groups[key].append(r)

    tables = {"keep": [], "exclude": [], "review": []}
    audit, decisions = [], []
    for members in groups.values():
        selected = max(members, key=rank)
        result = deepcopy(selected["classification"])
        flags = list(selected["errors"])
        if incompatible_texts(members):
            flags.append("content_conflict")
        labels = {m["classification"]["relevance_label"] for m in members}
        if {"clearly_related", "clearly_unrelated"}.issubset(labels):
            flags.append("classification_conflict")
        if flags:
            result["relevance_label"] = "uncertain"
            result["reason"] = "基础字段异常、长文不完整或同 ID 冲突，需要人工复核：" + ",".join(flags)
            result["matched_rules"] += ["QUALITY_OR_CONFLICT_REVIEW"]
        decision = {"clearly_related": "keep", "clearly_unrelated": "exclude",
                    "likely_related": "review", "uncertain": "review"}[result["relevance_label"]]
        keywords = list(dict.fromkeys(m["raw"].get("keyword", "") for m in members))
        notes = sorted(set(flags + [n for m in members for n in m["notes"]]))
        common = {key: result[key] for key in ("game_related", "event_related", "spam_type",
                  "relevance_label", "reason", "confidence")}
        common.update({"decision": decision, "matched_rules": j(result["matched_rules"]),
                       "merged_keywords": j(keywords), "rule_version": version,
                       "selected_source_row": selected["source_csv_row"],
                       "semantic_advice": j(result["semantic_advice"])})
        master = {**selected["raw"], **common, "content_clean": selected["content_clean"],
                  "source_rows": j([m["source_csv_row"] for m in members]),
                  "source_files": j(list(dict.fromkeys(m["source_file"] for m in members))),
                  "record_count": len(members),
                  "content_variant_count": len({m["raw"].get("content", "") for m in members}),
                  "quality_flags": j(notes)}
        tables[decision].append(master)
        decisions.append({"members": members, "selected": selected, "result": result,
                          "decision": decision, "master": master})
        for m in members:
            original = m["classification"]
            audit.append({**common, "weibo_id": m["raw"]["weibo_id"],
                **{k: m[k] for k in ("source_csv_row", "source_physical_line_start",
                    "source_physical_line_end", "source_file", "source_sha256")},
                "original_keyword": m["raw"].get("keyword", ""),
                "is_selected": str(m is selected).lower(),
                "dedup_action": "selected" if m is selected else "merged",
                "input_relevance_label": original["relevance_label"],
                "input_reason": original["reason"],
                "input_matched_rules": j(original["matched_rules"]),
                "quality_flags": j(m["errors"] + m["notes"]),
                "group_quality_flags": j(notes), "signals": j(original["signals"]),
                "related_sentence_share": original["related_sentence_share"]})
    audit.sort(key=lambda a: a["source_csv_row"])
    return tables, audit, prepared, decisions


def save_outputs(directory, artifacts, protected):
    """相同结果可重复执行；不同结果不覆盖，要求换输出目录。"""
    directory = Path(directory).resolve()
    for name, data in artifacts.items():
        target = directory / name
        if target.resolve() in protected or target.is_symlink():
            raise ValueError("输出路径与原始/参考文件冲突，或为符号链接")
        if target.exists() and target.read_bytes() != data:
            raise FileExistsError(f"输出已存在且内容不同，请换 --output-dir：{target.name}")
    directory.mkdir(parents=True, exist_ok=True)
    for name, data in artifacts.items():
        target = directory / name
        if not target.exists():
            with target.open("xb") as stream:
                stream.write(data)


def preprocess(source, output_dir, rules_path=BASE / "rules.json", source_encoding=None,
               unrelated_reference=None, review_reference=None, progress=True):
    from evaluation import build_evaluation
    header, records, source_info = read_csv(source, source_encoding)
    if not REQUIRED.issubset(header):
        raise ValueError("输入缺少 weibo_id、keyword、created_at 或 content")
    if set(header).intersection(TABLE_META):
        raise ValueError("输入含预处理输出列，请使用原始 CSV")
    config, version, config_hash = load_rules(rules_path)
    references = []
    for path, label in ((unrelated_reference, "clearly_unrelated"), (review_reference, "uncertain")):
        if path:
            _, rows, info = read_csv(path)
            if rows and not {"weibo_id", "keyword", "content"}.issubset(rows[0]["raw"]):
                raise ValueError("参考文件缺少 weibo_id、keyword 或 content")
            references.append({"label": label, "records": rows, "info": info})
    # 分类器只接触正文和配置，绝不把参考标签或 ID 传入。
    tables, audit, prepared, decisions = process_records(records, RuleClassifier(config), version, progress)
    artifacts = {}
    for name, decision in (("cleaned_weibo.csv", "keep"), ("unrelated_weibo.csv", "exclude"),
                           ("review_weibo.csv", "review")):
        artifacts[name] = csv_bytes(tables[decision], header + TABLE_META)
    artifacts["preprocessing_audit.csv"] = csv_bytes(audit, AUDIT_FIELDS)
    raw_fields = header + ["content_clean", "source_csv_row", "source_physical_line_start",
                          "source_physical_line_end", "source_file", "source_sha256", "quality_flags"]
    raw_rows = [{**r["raw"], "content_clean": r["content_clean"],
                 **{k: r[k] for k in raw_fields if k.startswith("source_")},
                 "quality_flags": j(r["errors"] + r["notes"])} for r in prepared]
    artifacts["raw_records.csv"] = csv_bytes(raw_rows, raw_fields)
    extra, summary = build_evaluation(header, prepared, audit, decisions, references, version, source_info)
    artifacts.update(extra)
    summary.update({"source": source_info, "rule_version": version, "config_sha256": config_hash})
    artifacts["run_manifest.json"] = (json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    protected = {Path(source).resolve(), Path(rules_path).resolve()}
    protected.update(Path(r["info"]["path"]) for r in references)
    # 运行前后核对源文件和参考文件哈希，避免并发修改导致不可追溯。
    for info in [source_info] + [r["info"] for r in references]:
        if sha256(Path(info["path"]).read_bytes()) != info["sha256"]:
            raise ValueError("原始或参考文件在处理期间发生变化，请重试")
    save_outputs(output_dir, artifacts, protected)
    return {**summary, "output_dir": str(Path(output_dir).resolve()), "output_files": list(artifacts)}


def main():
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", nargs="?", type=Path, default=BASE.parent / "raw_data" / "无标题.csv")
    parser.add_argument("--output-dir", type=Path, default=BASE / "outputs" / "rules_v2")
    parser.add_argument("--rules", type=Path, default=BASE / "rules.json")
    parser.add_argument("--source-encoding")
    parser.add_argument("--unrelated-reference", type=Path)
    parser.add_argument("--review-reference", type=Path)
    parser.add_argument("--no-progress", action="store_true")
    args = parser.parse_args()
    started = time.perf_counter()
    try:
        result = preprocess(args.source, args.output_dir, args.rules, args.source_encoding,
                            args.unrelated_reference, args.review_reference, not args.no_progress)
    except (OSError, UnicodeError, ValueError, csv.Error, LookupError, KeyError) as error:
        parser.exit(1, f"预处理停止：{type(error).__name__}。请检查路径、字段、规则及输出目录；原文件不会覆盖。\n")
    print(json.dumps(result, ensure_ascii=True, indent=2))
    print(f"处理完成，实际总耗时 {time.perf_counter() - started:.2f}s。", file=sys.stderr)


if __name__ == "__main__":
    main()
