"""只读抽取研究库样本，并在独立子进程中比较中文分词器。"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import sqlite3
import statistics
import subprocess
import sys
import threading
import time

import psutil


HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
PROJECT = REPO.parent
DEFAULT_DB = PROJECT / "database" / "identify_v.sqlite3"
DEFAULT_LEXICON = PROJECT / "data_preprocessing" / "text_resources" / "dwrg_dict.txt"
DEFAULT_GLOSSARY = PROJECT / "data_preprocessing" / "text_resources" / "glossary_review.json"
CACHE = HERE / ".cache"
ARTIFACT = CACHE / "benchmark_private.json"
SEED = "identityv-weibo-segmentation-20261119-v1"
SAMPLE_LIMITS = {"微博": 25, "一级评论": 45, "楼中楼": 40}
REPEATS = 5


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_domain_words(path: Path) -> list[str]:
    words = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        fields = line.strip().split()
        if fields and not fields[0].startswith("#"):
            words.append(fields[0])
    return sorted(set(words), key=lambda word: (-len(word), word))


def load_sensitive_words(path: Path) -> list[str]:
    if not path.is_file():
        return []
    items = json.loads(path.read_text(encoding="utf-8-sig"))
    found = set()
    for item in items if isinstance(items, list) else []:
        if item.get("term_type") == "derogatory_alias" or item.get("toxicity"):
            term = str(item.get("surface") or item.get("term") or "").strip()
            if len(term) > 1:
                found.add(term)
    return sorted(found, key=lambda word: (-len(word), word))


def terms_in(text: str, dictionary: list[str]) -> list[str]:
    hits, occupied = [], [False] * len(text)
    for term in dictionary:
        start = 0
        while True:
            start = text.find(term, start)
            if start < 0:
                break
            end = start + len(term)
            if not any(occupied[start:end]):
                hits.append(term)
                occupied[start:end] = [True] * len(term)
            start = end
    return hits


def sample_records(db_path: Path, entities: list[str], sensitive: list[str]) -> list[dict]:
    conn = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    sql = """
        SELECT '微博' AS record_type, p.weibo_id AS record_id, p.original_text AS text
        FROM posts p JOIN text_preprocessing t ON t.weibo_id=p.weibo_id
        WHERE t.record_type='微博' AND p.original_text IS NOT NULL
        UNION ALL
        SELECT CASE c.comment_type WHEN '一级评论' THEN '一级评论' ELSE '楼中楼' END,
               c.dataset_key, c.original_text
        FROM comments c JOIN text_preprocessing t ON t.comment_id=c.dataset_key
        WHERE c.original_text IS NOT NULL
    """
    rows = [dict(row) for row in conn.execute(sql)]
    conn.close()
    for row in rows:
        text = row["text"]
        row["sha"] = hashlib.sha256((SEED + row["record_id"]).encode("utf-8")).hexdigest()
        row["entities"] = terms_in(text, entities)
        row["sensitive"] = terms_in(text, sensitive)
        row["tags"] = []
        if row["entities"]:
            row["tags"].append("领域实体")
        if row["sensitive"]:
            row["tags"].append("词典标记的攻击性表达")
        if re.search(r"[A-Za-z]{2,}|[0-9]{2,}", text):
            row["tags"].append("中英/数字混写")
        if re.search(r"([\u3400-\u9fffA-Za-z])\1{2,}|([!?！？。])\2{1,}|[\U0001F300-\U0001FAFF]", text):
            row["tags"].append("重复字符/标点/Emoji")
        if not row["tags"]:
            row["tags"].append("普通表达")
    selected = []
    for record_type, limit in SAMPLE_LIMITS.items():
        group = [r for r in rows if r["record_type"] == record_type]
        group.sort(key=lambda r: r["sha"])
        chosen = []
        chosen_ids = set()
        # 先纳入包含领域实体及词典标记攻击性表达的真实案例，再以固定散列补足。
        for tag in ("领域实体", "词典标记的攻击性表达", "中英/数字混写", "重复字符/标点/Emoji"):
            for row in group:
                if tag in row["tags"] and row["record_id"] not in chosen_ids:
                    chosen.append(row)
                    chosen_ids.add(row["record_id"])
                    if sum(tag in item["tags"] for item in chosen) >= max(2, limit // 8):
                        break
        for row in group:
            if len(chosen) >= limit:
                break
            if row["record_id"] not in chosen_ids:
                chosen.append(row)
                chosen_ids.add(row["record_id"])
        selected.extend(chosen[:limit])
    for i, row in enumerate(selected, 1):
        row["sample_id"] = f"样本{i:03d}"
        row.pop("sha", None)
    return selected


def package_size(module: str) -> int | None:
    names = {"jieba": "jieba", "pkuseg": "pkuseg", "jiojio": "jiojio", "thulac": "thulac", "cutword": "cutword"}
    try:
        dist = importlib.metadata.distribution(names[module])
        return sum((dist.locate_file(file)).stat().st_size for file in (dist.files or []) if dist.locate_file(file).is_file())
    except Exception:
        return None


def runtime_dependency_size(module: str) -> int | None:
    package = {"jieba": "jieba", "pkuseg": "pkuseg", "jiojio": "jiojio", "thulac": "thulac", "cutword": "cutword"}[module]
    try:
        importlib.metadata.distribution(package)
    except importlib.metadata.PackageNotFoundError:
        return None
    visited = set()

    def visit(name: str):
        try:
            dist = importlib.metadata.distribution(name)
        except importlib.metadata.PackageNotFoundError:
            return 0
        normalized = re.sub(r"[-_.]+", "-", dist.metadata["Name"]).lower()
        if normalized in visited:
            return 0
        visited.add(normalized)
        own = sum(dist.locate_file(item).stat().st_size for item in (dist.files or []) if dist.locate_file(item).is_file())
        total = own
        for requirement in dist.requires or []:
            match = re.match(r"([A-Za-z0-9_.-]+)", requirement)
            if match:
                total += visit(match.group(1))
        return total

    try:
        return visit(package)
    except Exception:
        return None


def initialize(name: str, custom: bool, dictionary: Path | None):
    user_dictionary = None
    if custom and dictionary:
        words = load_domain_words(dictionary)
        user_dictionary = CACHE / f"{name}_user_dict.txt"
        user_dictionary.write_text("\n".join(words) + "\n", encoding="utf-8")
    if name == "jieba":
        import jieba
        tok = jieba.Tokenizer()
        if custom and dictionary:
            for line in dictionary.read_text(encoding="utf-8-sig").splitlines():
                fields = line.split()
                if fields and not fields[0].startswith("#"):
                    freq = int(fields[1]) if len(fields) > 1 and fields[1].isdigit() else None
                    tok.add_word(fields[0], freq)
        tok.initialize()
        return tok, tok.lcut
    if name == "pkuseg":
        import pkuseg
        kwargs = {"model_name": "web"}
        if user_dictionary:
            kwargs["user_dict"] = str(user_dictionary)
        tok = pkuseg.pkuseg(**kwargs)
        return tok, tok.cut
    if name == "jiojio":
        import jiojio
        if custom and dictionary:
            jiojio.init(cws_user_dict=str(user_dictionary))
        else:
            jiojio.init()
        return jiojio, jiojio.cut
    if name == "thulac":
        import thulac
        tok = thulac.thulac(user_dict=str(user_dictionary) if user_dictionary else None,
                            seg_only=True, filt=False)
        return tok, lambda text: [w for w in tok.cut(text, text=False)]
    if name == "cutword":
        # 包入口会无条件载入可选 NER 与 torch；这里直接使用其公开的 Cutter 模块。
        spec = importlib.util.find_spec("cutword")
        source = Path(spec.origin).parent / "cutword.py"
        module_spec = importlib.util.spec_from_file_location("cutword_segmenter", source)
        module = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(module)
        custom_path = None
        if custom and dictionary:
            custom_path = CACHE / "cutword_domain_dict.txt"
            lines = []
            for line in dictionary.read_text(encoding="utf-8-sig").splitlines():
                fields = line.split()
                if fields and not fields[0].startswith("#"):
                    frequency = fields[1] if len(fields) > 1 and fields[1].isdigit() else "100"
                    lines.append(f"{fields[0]} {frequency}")
            custom_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        tok = module.Cutter(custom_dict_path=str(custom_path) if custom_path else None)
        return tok, tok.cutword
    raise ValueError(name)


def measure_child(name: str, custom: bool, texts: list[str], dictionary: Path | None, entities: list[str], sensitive: list[str]):
    process = psutil.Process()
    baseline = process.memory_info().rss
    peak = [baseline]
    stop = threading.Event()

    def sample_memory():
        while not stop.wait(0.005):
            try:
                peak[0] = max(peak[0], process.memory_info().rss)
            except psutil.Error:
                return

    watcher = threading.Thread(target=sample_memory, daemon=True)
    watcher.start()
    # 分开记录首次初始化和预热后的缓存初始化；首次调用包含可能的模型下载。
    try:
        first_start = time.perf_counter()
        tok, cut = initialize(name, custom, dictionary)
        first_init = time.perf_counter() - first_start
        for text in texts[:3]:
            cut(text)
        init_times = []
        for _ in range(2):
            start = time.perf_counter()
            tok, cut = initialize(name, custom, dictionary)
            init_times.append(time.perf_counter() - start)
        loaded = process.memory_info().rss
        timings, all_tokens = [], None
        for _ in range(REPEATS):
            start = time.perf_counter()
            all_tokens = [cut(text) for text in texts]
            timings.append(time.perf_counter() - start)
        stop.set(); watcher.join()
        results = []
        for text, tokens in zip(texts, all_tokens):
            normalized = []
            for token in tokens:
                if isinstance(token, (list, tuple)):
                    token = token[0]
                token = str(token).strip()
                if token:
                    normalized.append(token)
            results.append(normalized)
        entity_hits = entity_total = hostile_hits = hostile_total = 0
        for text, tokens in zip(texts, results):
            for phrase in terms_in(text, entities):
                entity_total += 1
                entity_hits += phrase in tokens
            for phrase in terms_in(text, sensitive):
                hostile_total += 1
                hostile_hits += phrase in tokens
        print(json.dumps({"ok": True, "first_init_s": first_init,
                          "init_median_s": statistics.median(init_times),
                          "elapsed_median_s": statistics.median(timings),
                          "elapsed_runs_s": timings, "rss_baseline_bytes": baseline,
                          "rss_loaded_bytes": loaded, "rss_peak_bytes": peak[0],
                          "tokens": results, "entity_hits": entity_hits, "entity_total": entity_total,
                          "hostile_hits": hostile_hits, "hostile_total": hostile_total,
                          "package_bytes": package_size(name),
                          "package_plus_dependencies_bytes": runtime_dependency_size(name)}, ensure_ascii=False))
    except Exception as exc:
        stop.set(); watcher.join()
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}",
                          "package_bytes": package_size(name),
                          "package_plus_dependencies_bytes": runtime_dependency_size(name)}, ensure_ascii=False))


def child_run(args):
    cache_home = Path(sys.prefix) / "pkuseg-model-cache"
    cache_home.mkdir(parents=True, exist_ok=True)
    os.environ["PKUSEG_HOME"] = str(cache_home)
    sample = json.loads(Path(args.samples).read_text(encoding="utf-8"))
    measure_child(args.child, args.custom, [r["text"] for r in sample],
                  Path(args.dictionary) if args.dictionary else None,
                  json.loads(Path(args.entities).read_text(encoding="utf-8")),
                  json.loads(Path(args.sensitive).read_text(encoding="utf-8")))


def run_parent(db: Path, dictionary: Path, glossary: Path):
    CACHE.mkdir(parents=True, exist_ok=True)
    entities = load_domain_words(dictionary)
    sensitive = load_sensitive_words(glossary)
    samples = sample_records(db, entities, sensitive)
    (CACHE / "samples_private.json").write_text(json.dumps(samples, ensure_ascii=False), encoding="utf-8")
    (CACHE / "entities.json").write_text(json.dumps(entities, ensure_ascii=False), encoding="utf-8")
    (CACHE / "sensitive.json").write_text(json.dumps(sensitive, ensure_ascii=False), encoding="utf-8")
    text_files = {"samples": CACHE / "samples_private.json", "entities": CACHE / "entities.json",
                  "sensitive": CACHE / "sensitive.json"}
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["PKUSEG_HOME"] = str(Path(sys.prefix) / "pkuseg-model-cache")
    configurations = [(n, custom) for n in ("jieba", "pkuseg", "jiojio", "thulac", "cutword") for custom in (False, True)]
    outcomes, token_rows = [], {}
    for name, custom in configurations:
        label = name + ("（领域词典）" if custom else "（默认）")
        command = [sys.executable, str(Path(__file__).resolve()), "--child", name,
                   "--custom" if custom else "--default", "--samples", str(text_files["samples"]),
                   "--entities", str(text_files["entities"]), "--sensitive", str(text_files["sensitive"]),
                   "--dictionary", str(dictionary)]
        start = time.perf_counter()
        proc = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", env=env, cwd=HERE)
        elapsed_wall = time.perf_counter() - start
        try:
            data = json.loads(proc.stdout.strip().splitlines()[-1])
        except Exception:
            data = {"ok": False, "error": (proc.stderr or proc.stdout or f"子进程退出码 {proc.returncode}")[-1200:]}
        data.update({"tool": name, "custom": custom, "label": label,
                     "wall_elapsed_s": elapsed_wall, "python": platform.python_version()})
        if name == "pkuseg":
            data["install_reason"] = "Python 3.11 / Windows x64 源码包需 Microsoft Visual C++ 14.0+ 编译；隔离构建、补装 numpy / wheel 后仍失败；PyPI Windows 预编译轮仅至 CPython 3.8。"
        if name == "cutword" and not data.get("ok"):
            data["install_reason"] = "包入口导入失败；已使用公开 Cutter 分词模块绕过可选 NER 模块对 torch 的无条件导入。"
        outcomes.append(data)
        if data.get("ok"):
            token_rows[label] = data.pop("tokens")
        print(label, "成功" if data.get("ok") else "失败", round(elapsed_wall, 2), flush=True)
    package_sizes = {name: package_size(name) for name in ("jieba", "pkuseg", "jiojio", "thulac", "cutword")}
    summary = {"meta": {"date": time.strftime("%Y-%m-%d %H:%M:%S"), "python": platform.python_version(),
                        "platform": platform.platform(), "processor": platform.processor(),
                        "logical_cpus": os.cpu_count(), "sample_count": len(samples),
                        "sample_by_type": {k: sum(r["record_type"] == k for r in samples) for k in SAMPLE_LIMITS},
                        "source_db_name": db.name, "lexicon_entries": len(entities),
                        "source_db_sha256": sha256_file(db),
                        "dictionary_sha256": sha256_file(dictionary),
                        "sensitive_lexicon_entries": len(sensitive), "repeats": REPEATS,
                        "sample_seed": SEED, "package_sizes": package_sizes,
                        "method": "SQLite只读连接；固定 SHA-256 顺序分层抽样；每条件独立子进程；预热后重复5次；报告中位数；RSS为进程基线、加载后和峰值；无人工金标准，不计算准确率。"},
               "samples": samples, "outcomes": outcomes, "tokens": token_rows}
    ARTIFACT.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    for path in text_files.values():
        path.unlink(missing_ok=True)
    print(f"私有原始样本与结果已写入临时缓存：{ARTIFACT}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--dictionary", type=Path, default=DEFAULT_LEXICON)
    parser.add_argument("--glossary", type=Path, default=DEFAULT_GLOSSARY)
    parser.add_argument("--child", choices=("jieba", "pkuseg", "jiojio", "thulac", "cutword"))
    parser.add_argument("--custom", action="store_true")
    parser.add_argument("--default", action="store_true")
    parser.add_argument("--samples")
    parser.add_argument("--entities")
    parser.add_argument("--sensitive")
    args = parser.parse_args()
    if args.child:
        child_run(args)
    else:
        run_parent(args.db, args.dictionary, args.glossary)


if __name__ == "__main__":
    main()
