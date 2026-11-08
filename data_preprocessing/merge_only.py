"""仅合并 CSV 并按微博 ID 去重，不执行分类或质量筛除。"""
import argparse
import json
from pathlib import Path
import re

from batch_preprocess import DROP_COLUMNS, normalize_header
from data_io import csv_bytes, read_csv, sha256
from octopus_adapter import weibo_id_from_url
from preprocess import BASE, save_outputs


def merge_only(source_dir, output_dir, recursive=True, source_encoding=None):
    source_dir, output_dir = Path(source_dir).resolve(), Path(output_dir).resolve()
    if not source_dir.is_dir() or source_dir == output_dir or source_dir.is_relative_to(output_dir):
        raise ValueError("请提供有效输入目录，输出目录须与输入分开")
    files = sorted((p for p in (source_dir.rglob('*') if recursive else source_dir.iterdir())
                    if p.is_file() and p.suffix.lower() == '.csv'
                    and not p.resolve().is_relative_to(output_dir)))
    if not files:
        raise ValueError("输入目录没有 CSV")
    metadata = ["合并去重_来源记录", "合并去重_重复条数"]
    fields, groups, sources = [], {}, []
    for path in files:
        header, records, info = read_csv(path, source_encoding)
        names = [normalize_header(n) for n in header]
        if len(names) != len(set(names)) or set(names) & set(metadata):
            raise ValueError(f"表头重名或与输出字段冲突：{path.name}")
        for name in names:
            if name not in DROP_COLUMNS and name not in fields:
                fields.append(name)
        sources.append({**info, "rows": len(records)})
        for rec in records:
            raw = {name: rec['raw'][original] for original, name in zip(header, names)
                   if name not in DROP_COLUMNS}
            identifier = next((raw[n].strip() for n in ('weibo_id', '微博ID', '微博id', '博文ID')
                               if re.fullmatch(r'[0-9]+', raw.get(n, '').strip())), '')
            identifier = identifier or weibo_id_from_url(raw.get('from_链接', ''))
            # 无有效 ID 时只合并字段完全相同的行，不筛掉缺字段、无关或异常记录。
            key = ('id', identifier) if identifier else ('row', tuple(sorted((k, v) for k, v in raw.items() if v)))
            groups.setdefault(key, []).append((raw, str(path), rec['source_csv_row']))
    output = []
    for members in groups.values():
        # 优先正文较完整的副本；并列时按字段丰富度及稳定的输入顺序选择。
        selected = max(members, key=lambda item: (
            max((len(item[0].get(n, '')) for n in ('微博原文', 'content', '正文', '微博正文')), default=0),
            sum(bool(v.strip()) for v in item[0].values())))
        output.append({**selected[0], metadata[0]: json.dumps(
            [{"file": p, "row": number} for _, p, number in members], ensure_ascii=False),
            metadata[1]: len(members)})
    name = '八爪鱼合并数据_仅合并去重.csv'
    summary = {"mode": "merge_only_no_filter", "sources": sources,
               "input_rows": sum(s['rows'] for s in sources), "output_rows": len(output),
               "merged_duplicate_rows": sum(s['rows'] for s in sources) - len(output),
               "output_encoding": "utf-8-sig", "output_file": str(output_dir / name),
               "filters_applied": [], "removed_columns": sorted(DROP_COLUMNS)}
    artifacts = {name: csv_bytes(output, fields + metadata),
                 '仅合并去重_处理记录.json': (json.dumps(summary, ensure_ascii=False, indent=2) + '\n').encode('utf-8')}
    for info in sources:
        if sha256(Path(info['path']).read_bytes()) != info['sha256']:
            raise ValueError('输入文件发生变化，请等待导出完成后重试')
    save_outputs(output_dir, artifacts, {p.resolve() for p in files})
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source_dir', type=Path)
    parser.add_argument('--output-dir', type=Path, default=BASE / 'outputs' / '八爪鱼数据')
    parser.add_argument('--source-encoding')
    args = parser.parse_args()
    result = merge_only(args.source_dir, args.output_dir, source_encoding=args.source_encoding)
    print(json.dumps(result, ensure_ascii=True, indent=2))


if __name__ == '__main__':
    main()
