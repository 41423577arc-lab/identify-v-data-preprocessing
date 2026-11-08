"""批量读取 CSV、转为 UTF-8 BOM、去重合并，并沿用原有微博预处理规则。"""
import argparse
import csv
import json
from pathlib import Path
import sys

from convert_encoding import BASE
from data_io import csv_bytes, read_csv, sha256
from preprocess import TABLE_META, AUDIT_FIELDS, load_rules, process_records, save_outputs
from relevance import RuleClassifier
from octopus_adapter import adapt_octopus


DROP_COLUMNS = {"请输入日期", "文本框值", "请输入日期的文本框值", "请输入日期_文本框值"}
ALIASES = {
    "weibo_id": ["微博ID", "微博id", "博文ID"],
    "keyword": ["关键词", "搜索关键词", "检索词"],
    "created_at": ["发布时间", "发表时间", "微博发布时间"],
    "content": ["微博正文", "微博原文", "正文", "微博内容", "博文内容", "内容"],
    "user_name": ["用户名", "用户昵称", "昵称", "作者", "名称"],
    "comments_count": ["评论数", "评论数量"],
    "likes_count": ["点赞数", "赞数", "点赞数量", "喜欢"],
    "reposts_count": ["转发数", "转发数量"],
    "long_text_complete": ["长文完整"],
}
PROVENANCE = ["batch_source_file", "batch_source_row"]


def normalize_header(name):
    return name.strip().replace("\ufeff", "")


def batch_process(source_dir, output_dir=None, recursive=False, source_encoding=None,
                  convert_only=False, column_map=None, rules_path=BASE / "rules.json",
                  progress=True, date_year=None, final_name="final_wps.csv"):
    source_dir = Path(source_dir).resolve()
    output_dir = Path(output_dir or source_dir / "processed").resolve()
    if not source_dir.is_dir():
        raise ValueError(f"输入不是文件夹：{source_dir}")
    if Path(final_name).name != final_name or not final_name.lower().endswith(".csv"):
        raise ValueError("--final-name 必须是 CSV 文件名，不能包含目录")
    if date_year is not None and not 1900 <= date_year <= 9999:
        raise ValueError("--date-year 必须是有效年份")
    if output_dir == source_dir or source_dir.is_relative_to(output_dir):
        raise ValueError("输出目录必须与输入目录分开，不能是输入目录的祖先")
    files = sorted((p for p in (source_dir.rglob("*") if recursive else source_dir.iterdir())
                    if p.is_file() and p.suffix.lower() == ".csv"
                    and not p.resolve().is_relative_to(output_dir)),
                   key=lambda p: p.relative_to(source_dir).as_posix())
    if not files:
        raise ValueError("输入目录没有 CSV 文件")
    column_map = column_map or {}
    unknown = set(column_map) - set(ALIASES)
    if unknown:
        raise ValueError(f"字段映射含未知目标列：{sorted(unknown)}")
    header, records, infos, artifacts = [], [], [], {}
    for source in files:
        names, source_records, info = read_csv(source, source_encoding)
        normalized = [normalize_header(n) for n in names]
        if len(set(normalized)) != len(normalized):
            raise ValueError(f"表头清理后重名：{source.name}")
        kept = [n for n in normalized if n not in DROP_COLUMNS]
        if not kept:
            raise ValueError(f"删除无用列后没有有效字段：{source.name}")
        if set(kept) & (set(TABLE_META) | set(PROVENANCE)):
            raise ValueError(f"输入含输出专用列，请使用原始 CSV：{source.name}")
        mapping = {}
        if not convert_only:
            for target, aliases in ALIASES.items():
                candidates = [column_map[target]] if target in column_map else [target] + aliases
                matches = [name for name in candidates if name in kept]
                if target in column_map and not matches:
                    raise ValueError(f"{source.name} 缺少映射列 {column_map[target]}")
                if target in matches:
                    matches = [target]
                if len(matches) > 1:
                    raise ValueError(f"{source.name} 的 {target} 对应多列，请指定 --column-map")
                if matches:
                    mapping[matches[0]] = target
            if "content" not in mapping.values():
                raise ValueError(f"{source.name} 找不到正文列，请指定 --column-map 或使用 --convert-only")
        local_header = [mapping.get(n, n) for n in kept]
        if len(set(local_header)) != len(local_header):
            raise ValueError(f"字段映射后重名：{source.name}")
        if not convert_only:
            for name in ALIASES:
                if name not in local_header:
                    local_header.append(name)
        for name in local_header:
            if name not in header:
                header.append(name)
        local_rows, seen = [], set()
        is_octopus = "微博原文" in normalized and "from_链接" in normalized
        if is_octopus and not convert_only:
            for name in ("octopus_original_created_at", "octopus_original_comments_count", "octopus_original_likes_count"):
                local_header.append(name)
                if name not in header:
                    header.append(name)
        for rec in source_records:
            raw = {mapping.get(new, new): rec["raw"][old]
                   for old, new in zip(names, normalized) if new in kept}
            raw = {name: raw.get(name, "") for name in local_header}
            if is_octopus and not convert_only:
                raw = adapt_octopus(raw, date_year)
            # 单文件转码副本仅合并完全相同的行；预处理仍保留每个原始行供审计。
            key = tuple(raw.values())
            if key not in seen:
                local_rows.append(raw)
                seen.add(key)
            records.append({**rec, "source_csv_row": len(records) + 2,
                            "raw": {**raw, "batch_source_file": str(source),
                                    "batch_source_row": str(rec["source_csv_row"])}})
        relative = source.relative_to(source_dir).with_suffix("")
        name = (Path("converted") / relative.parent / (relative.name + "_utf8_bom.csv")).as_posix()
        artifacts[name] = csv_bytes(local_rows, local_header)
        infos.append({**info, "input_rows": len(source_records),
                      "converted_rows": len(local_rows), "column_mapping": mapping,
                      "removed_columns": [n for n in normalized if n in DROP_COLUMNS]})
    fields = header + PROVENANCE
    for rec in records:
        rec["raw"] = {name: rec["raw"].get(name, "") for name in fields}
    merged, seen = [], set()
    for rec in records:
        key = tuple(rec["raw"][name] for name in header)
        if key not in seen:
            merged.append(rec["raw"])
            seen.add(key)
    artifacts["merged_utf8_bom.csv"] = csv_bytes(merged, fields)
    summary = {"input_dir": str(source_dir), "sources": infos, "input_rows": len(records),
               "merged_exact_unique_rows": len(merged), "output_encoding": "utf-8-sig",
               "mode": "convert_only" if convert_only else "preprocess",
               "missing_date_year_assumption": date_year}
    if not convert_only:
        config, version, config_hash = load_rules(rules_path)
        tables, audit, prepared, decisions = process_records(
            records, RuleClassifier(config), version, progress)
        for name, decision in (("cleaned_weibo.csv", "keep"), ("review_weibo.csv", "review"),
                               ("unrelated_weibo.csv", "exclude")):
            artifacts[name] = csv_bytes(tables[decision], fields + TABLE_META)
        # 最终主表与旧规则一致：只包含明确相关且质量检查通过的记录。
        artifacts["final_wps.csv"] = artifacts["cleaned_weibo.csv"]
        artifacts["preprocessing_audit.csv"] = csv_bytes(audit, AUDIT_FIELDS)
        artifacts["raw_records.csv"] = csv_bytes([r["raw"] for r in prepared], fields)
        summary.update({"rule_version": version, "config_sha256": config_hash,
                        "unique_groups": len(decisions),
                        "merged_duplicate_rows": len(records) - len(decisions),
                        "decision_counts": {k: len(v) for k, v in tables.items()}})
    else:
        artifacts["final_wps.csv"] = artifacts["merged_utf8_bom.csv"]
    if final_name != "final_wps.csv":
        if final_name in artifacts:
            raise ValueError("最终文件名不能与其他输出文件重名")
        artifacts[final_name] = artifacts.pop("final_wps.csv")
    summary["output_files"] = list(artifacts) + ["batch_manifest.json"]
    artifacts["batch_manifest.json"] = (json.dumps(summary, ensure_ascii=False, indent=2)
                                         + "\n").encode("utf-8")
    for info in infos:
        if sha256(Path(info["path"]).read_bytes()) != info["sha256"]:
            raise ValueError("输入文件在处理期间发生变化，请等导出完成后重试")
    protected = {p.resolve() for p in files} | {Path(rules_path).resolve()}
    # save_outputs 统一预检查，先创建嵌套目录以支持递归输入。
    for name in artifacts:
        (output_dir / name).parent.mkdir(parents=True, exist_ok=True)
    save_outputs(output_dir, artifacts, protected)
    return {**summary, "output_dir": str(output_dir)}


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_dir", type=Path, help="八爪鱼原始 CSV 文件夹")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--recursive", action="store_true", help="包含子文件夹，自动排除输出目录")
    parser.add_argument("--source-encoding", help="无 BOM 时默认 UTF-8；GBK 文件可指定 gb18030")
    parser.add_argument("--convert-only", action="store_true", help="只删除无用列、完全重复行、转码和合并")
    parser.add_argument("--column-map", type=Path, help='UTF-8 JSON：{"content": "原正文列名", ...}')
    parser.add_argument("--rules", type=Path, default=BASE / "rules.json")
    parser.add_argument("--no-progress", action="store_true")
    parser.add_argument("--date-year", type=int, help="明确指定八爪鱼无年份时间的年份；不指定则保持原值并送审")
    parser.add_argument("--final-name", default="final_wps.csv", help="最终合并 CSV 文件名")
    args = parser.parse_args()
    try:
        mapping = json.loads(args.column_map.read_text(encoding="utf-8-sig")) if args.column_map else None
        if mapping is not None and (not isinstance(mapping, dict) or
                                    not all(isinstance(v, str) for v in mapping.values())):
            raise ValueError("--column-map 必须是目标字段名到原列名的 JSON 对象")
        result = batch_process(args.source_dir, args.output_dir, args.recursive,
                               args.source_encoding, args.convert_only, mapping,
                               args.rules, not args.no_progress, args.date_year, args.final_name)
    except (OSError, UnicodeError, ValueError, csv.Error, LookupError) as error:
        parser.exit(1, f"批处理停止：{type(error).__name__}: {error}\n")
    print(json.dumps(result, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
