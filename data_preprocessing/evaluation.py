"""参考结果对照和报告。此模块不参与主题判定，参考标签不会回流分类器。"""
from collections import Counter
import random

from data_io import csv_bytes, sha256
from relevance import matching_text


def packed(value):
    import json
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def old_keyword_match(raw):
    terms = raw.get("keyword", "").split()
    return bool(terms) and all(t in raw.get("content", "") for t in terms)


def build_evaluation(header, prepared, audit, decisions, references, version, source_info):
    artifacts = {}
    by_row = {r["source_csv_row"]: r for r in prepared}
    audit_by_row = {r["source_csv_row"]: r for r in audit}
    reference_ids = set()
    comparisons = []
    for reference in references:
        for rec in reference["records"]:
            raw = rec["raw"]
            reference_ids.add(raw["weibo_id"])
            # 原CSV行号按逻辑记录计数，含表头；另外保留真正的物理行号。
            suggested = raw.get("原CSV行号", "")
            candidates = [r for r in prepared if all(r["raw"].get(k) == raw.get(k)
                          for k in ("weibo_id", "keyword", "content"))]
            preferred = [r for r in candidates if str(r["source_csv_row"]) == suggested]
            matches = preferred or candidates
            found = matches[0] if matches else None
            actual = audit_by_row[found["source_csv_row"]] if found else None
            matched = bool(actual and actual["relevance_label"] == reference["label"])
            comparisons.append({
                "weibo_id": raw["weibo_id"], "reference_file": reference["info"]["path"],
                "reference_record_row": rec["source_csv_row"], "reference_source_row": suggested,
                "matched_source_row": found["source_csv_row"] if found else "",
                "reference_source_row_verified": bool(preferred),
                "original_keyword": raw.get("keyword", ""), "expected_label": reference["label"],
                "actual_label": actual["relevance_label"] if actual else "not_found",
                "agreement": matched, "reference_reason": raw.get("筛选原因", ""),
                "actual_reason": actual["reason"] if actual else "参考内容在原始文件中未找到",
                "matched_rules": actual["matched_rules"] if actual else "[]",
                "difference_explanation": "一致" if matched else (
                    "规则结果与参考不同：" + actual["reason"] if actual else "原始数据与参考不一致，无法评价"),
                "merged_keywords": actual["merged_keywords"] if actual else "[]"})
    comparison_fields = ["weibo_id", "reference_file", "reference_record_row", "reference_source_row",
        "matched_source_row", "reference_source_row_verified", "original_keyword", "expected_label",
        "actual_label", "agreement", "reference_reason", "actual_reason", "matched_rules",
        "difference_explanation", "merged_keywords"]
    artifacts["reference_comparison.csv"] = csv_bytes(comparisons, comparison_fields)

    old_seen, changes = set(), []
    for r in prepared:
        raw = r["raw"]
        old_match = old_keyword_match(raw)
        key = raw["weibo_id"].strip()
        old_keep = old_match and (not key or key not in old_seen)
        if old_match and key:
            old_seen.add(key)
        current = audit_by_row[r["source_csv_row"]]
        changes.append({"source_csv_row": r["source_csv_row"], "weibo_id": raw["weibo_id"],
            "keyword": raw.get("keyword", ""), "old_keyword_match": old_match,
            "old_decision": "keep" if old_keep else "keyword_miss" if not old_match else "duplicate",
            "new_label": current["relevance_label"], "new_decision": current["decision"],
            "new_dedup_action": current["dedup_action"], "new_reason": current["reason"]})
    artifacts["before_after_comparison.csv"] = csv_bytes(changes, ["source_csv_row", "weibo_id",
        "keyword", "old_keyword_match", "old_decision", "new_label", "new_decision",
        "new_dedup_action", "new_reason"])

    # 固定种子按ID和相同正文分组抽样，标注列留空，不能伪称已有人工金标。
    candidates, seen_text = [], set()
    reference_texts = {sha256(matching_text(r["raw"].get("content", "")).encode())
                       for ref in references for r in ref["records"]}
    for d in decisions:
        m = d["master"]
        digest = sha256(matching_text(m["content_clean"]).encode())
        if m["weibo_id"] not in reference_ids and digest not in seen_text and digest not in reference_texts:
            seen_text.add(digest)
            candidates.append(d)
    random.Random(20261020).shuffle(candidates)
    samples = []
    for index, d in enumerate(candidates[:60]):
        m = d["master"]
        samples.append({"sample_split": "development_random" if index < 20 else "heldout_for_human",
            "weibo_id": m["weibo_id"], "source_rows": m["source_rows"],
            "content": m["content"], "content_clean": m["content_clean"],
            "merged_keywords": m["merged_keywords"], "rule_label": m["relevance_label"],
            "rule_reason": m["reason"], "human_label": "", "human_reason": ""})
    artifacts["annotation_sample.csv"] = csv_bytes(samples, ["sample_split", "weibo_id", "source_rows",
        "content", "content_clean", "merged_keywords", "rule_label", "rule_reason", "human_label", "human_reason"])

    labels = Counter(d["result"]["relevance_label"] for d in decisions)
    decisions_count = Counter(d["decision"] for d in decisions)
    quality = Counter(e for r in prepared for e in r["errors"] + r["notes"])
    rule_counts = Counter(rule for d in decisions for rule in d["result"]["matched_rules"])
    row_rule_counts = Counter(rule for r in prepared for rule in r["classification"]["matched_rules"])
    agreements = {}
    for ref in references:
        subset = [r for r in comparisons if r["expected_label"] == ref["label"]]
        agreements[ref["label"]] = {"reference_rows": len(subset),
            "matched_rows": sum(r["actual_label"] != "not_found" for r in subset),
            "agree_rows": sum(r["agreement"] for r in subset),
            "unique_ids": len({r["weibo_id"] for r in subset}),
            "agree_unique_ids": len({r["weibo_id"] for r in subset if r["agreement"]})}
    rescued_rows = [r for r in changes if not r["old_keyword_match"] and
                    r["new_label"] in {"clearly_related", "likely_related"}]
    summary = {"input_rows": len(prepared), "unique_groups": len(decisions),
        "valid_unique_weibo_ids": len({r["raw"]["weibo_id"].strip() for r in prepared
                                       if r["raw"]["weibo_id"].strip().isascii() and r["raw"]["weibo_id"].strip().isdigit()}),
        "merged_duplicate_rows": len(prepared) - len(decisions),
        "duplicate_groups": sum(len(d["members"]) > 1 for d in decisions),
        "label_counts": dict(labels), "decision_counts": dict(decisions_count),
        "quality_counts_rows": dict(quality), "rule_counts_groups": dict(rule_counts),
        "reference_agreement": agreements,
        "reference_sources": [ref["info"] for ref in references],
        "old_kept_unique": sum(r["old_decision"] == "keep" for r in changes),
        "old_keyword_miss_rows": sum(not r["old_keyword_match"] for r in changes),
        "keyword_miss_now_related_rows": len(rescued_rows),
        "keyword_miss_now_related_ids": len({r["weibo_id"] for r in rescued_rows}),
        "human_annotation_pending": len(samples), "heldout_labels_available": False}

    report = ["# 微博预处理离线报告", "", f"规则版本：`{version}`。", "",
        f"输入：`{source_info['path']}`；SHA-256：`{source_info['sha256']}`。",
        "原文件和参考文件不覆盖，数据库与爬虫不修改；CSV 均为 UTF-8-SIG。", "",
        "## 流程与数量", "",
        f"- 原始记录 {len(prepared)} 行，合并后 {len(decisions)} 个 ID/独立异常记录组。",
        f"- 重复组 {summary['duplicate_groups']} 组，合并重复行 {summary['merged_duplicate_rows']} 行。",
        "- 先检查原始字段并去除 HTML，保留原正文和清理文本；不删除表情、口语、缩写或攻击性词。",
        "- 同 ID 优先基础字段有效、完整标记为 true、正文更长、字段更齐全的记录，最后按原行号稳定选择。",
        "- 同 ID 非前缀式正文差异或相关性冲突进入人工复核。不同 ID 即使同文也不自动合并。",
        "- 检索词只用于合并来源和旧规则对照，不参与新主题分类。", "",
        "| 标签 | 去重后数量 | 去向 |", "|---|---:|---|",
        *[f"| {label} | {labels[label]} | {'cleaned_weibo.csv' if label == 'clearly_related' else 'unrelated_weibo.csv' if label == 'clearly_unrelated' else 'review_weibo.csv'} |"
          for label in ["clearly_related", "likely_related", "clearly_unrelated", "uncertain"]], "",
        "likely_related 保留在 review 中，不属于排除；明确相关且基础检查通过才进入 cleaned。", "",
        "## 基础异常与备注（原始行计数，可重叠）", "",
        "| 检查项 | 行数 |", "|---|---:|", *[f"| {k} | {v} |" for k, v in sorted(quality.items())], "",
        "false 表示已知长文不完整，进入复核；空完整性标记仅作未知备注，不直接认定截断。",
        "乱码检测为启发式，无法识别所有已损坏但可解码的中文。", "",
        "## 各规则命中", "", "| 规则 | 原始行 | 最终记录组 |", "|---|---:|---:|",
        *[f"| {k} | {row_rule_counts[k]} | {rule_counts[k]} |" for k in sorted(set(row_rule_counts) | set(rule_counts))], "",
        "## 人工辅助参考对照", "",
        "参考标签只在分类完成后读入对照，不通过微博 ID 硬编码分类。参考已用于开发回归，不是独立测试集。",
        "两个参考集只有无关和边界标签，没有随机正样本金标，不能由一致率推断总体准确率、相关召回率或误删率。", "",
        *[f"- {label}：逐行一致 {v['agree_rows']}/{v['reference_rows']}；不同 ID 一致 {v['agree_unique_ids']}/{v['unique_ids']}。"
          for label, v in agreements.items()],
        *([] if references else ["未提供参考文件，本次不计算参考一致性。"]), "",
        "逐条结果如下，原CSV行号按逻辑记录（含表头）计，真正的多行文本物理行号另存审计。", "",
        "| ID | 参考原行号 | 期望 | 实际 | 对照与解释 |", "|---|---:|---|---|---|",
        *[f"| {r['weibo_id']} | {r['reference_source_row']} | {r['expected_label']} | {r['actual_label']} | {r['difference_explanation']}；{r['actual_reason']} |"
          for r in comparisons], "",
        "完整参考原因、来源哈希、行号校验与合并关键词见 reference_comparison.csv、run_manifest.json。", "",
        "## 开发回归中的误判与修正", "",
        "这是本次开发观察记录，不是独立测试集上的效果提升。", "",
        "| 阶段 | 无关参考一致 | 边界参考一致 | 问题/修正 |",
        "|---|---:|---:|---|",
        "| 初版分层规则 | 22/27 | 3/4 | 第五次、赛车排位、约瑟夫同名被当成线索；体育词不全；生活日志分句不足 |",
        "| 上下文和体育词分组修正 | 27/27 | 3/4 | 第五及人物需游戏语境，体育人物需专项表达；但规范化后的省略号仍未断句 |",
        "| 省略号与短混合报道修正 | 见本报告实际对照 | 见本报告实际对照 | NFKC 会把省略号转成英文点号，现同时处理；混合主题只进复核 |", "",
        "以上历史数字仅对应本次 936 行及两份已提供参考，不作为新输入的准确率。后续配置可能改变实际结果，以 reference_comparison.csv 为准。", "",
        "## 修改前后", "",
        f"- 旧版严格关键词匹配：未命中 {summary['old_keyword_miss_rows']} 行，去重后仅保留 {summary['old_kept_unique']} 条。",
        f"- 新版：此前未命中但现判断明确/可能相关的有 {len(rescued_rows)} 行，涉及 {summary['keyword_miss_now_related_ids']} 个 ID；这是保留变化量，不是已人工验证的召回率。",
        "- 对照片段不复制正文到报告，逐行旧/新决策见 before_after_comparison.csv；原文可按行号在 raw_records.csv 查回。", "",
        "## 独立复核与误判风险", "",
        "- annotation_sample.csv 固定种子抽取最多 60 个 ID：前 20 个为随机开发复核，后 40 个留作独立人工标注。",
        "- 抽样排除参考 ID、相同正文，并避免同 ID 跨组；近似模板仍可能泄漏，正式评估需人工合并模板分组。",
        "- human_label/human_reason 留空，不冒充人工标注。完成独立标注前，无法给出可靠总体误删率。",
        "- 建议人工标注时先隐藏 rule_label/rule_reason，允许 clearly_related、likely_related、clearly_unrelated、uncertain 四类。",
        "- 开发中另阅读了随机正例（游戏角色/共号/选手评价/短话题），只作辅助检查，不当作独立人工金标。",
        "- 主要风险：词典覆盖不足、同名人物、蹭标签、隐喻、跨游戏比较、转发依赖上下文、生活日志。相关短帖不因短而剔除。",
        "- 新发现无关样式优先进入复核，不根据单个广告信号排除；扩大排除规则前需补充正例和独立标注验证。", "",
        "## 语义模型与工程约束", "",
        "本批先用规则版；尚无已标注的随机正例测试集，不下载 BGE、不使用未经验证的相似度阈值。",
        "semantic.py 保留本地模型接口，对 likely_related/uncertain 可分别输出游戏和赛事参考相似度，但只辅助复核，不改变决策。",
        "game_related 与 event_related 分开：yes/likely/mentioned/no/unknown 是规则证据状态，不是真值；confidence 留空。",
        "规则中的人名/战队来自种子词典或本批候选，未建立未经外部核实的真人别名映射。",
        "进度和基于试跑的估时写终端，实际耗时不写入结果，确保同输入/规则路径生成相同字节。",
        "原始每行保存在 raw_records.csv；审计每原始行一条，记录选择/合并动作及最终去向。",
        "同一输出目录只接受完全相同结果，不覆盖已有不同内容；修改配置或参考后请换输出目录。", ""]
    artifacts["preprocessing_report.md"] = "\n".join(report).encode("utf-8")
    return artifacts, summary
