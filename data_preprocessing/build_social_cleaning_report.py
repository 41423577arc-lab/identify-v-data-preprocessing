"""Build a local-only Chinese report and review queue from a reviewed run."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys

from social_text_cleaning import COLUMN_MARKER, REPOST_MARKER, clean_social_text, split_repost_chain


BADGE = re.compile(r'[^\s\r\n]{1,100}?超话')
BRACKET_SUPERTOPIC = re.compile(r'[#＃][^#＃\r\n]{1,140}?(?:\[\s*超话\s*\]|【\s*超话\s*】|（\s*超话\s*）|\(\s*超话\s*\))[#＃]')
NORMAL_HASHTAG = re.compile(r'[#＃][^#＃\r\n]{1,140}[#＃]')
EMOJI = re.compile(r'[\U0001f000-\U0001faff\u2600-\u27bf]')


def read_lines(path):
    with Path(path).open(encoding='utf-8') as handle:
        return [json.loads(line) for line in handle]


def write_lines(path, rows):
    with Path(path).open('w', encoding='utf-8', newline='\n') as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(',', ':')) + '\n')


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def excerpt(value, limit=135):
    text = re.sub(r'\s+', ' ', str(value or '')).strip().replace('|', '\\|')
    return text if len(text) <= limit else text[:limit - 1] + '…'


def source_text_for(record):
    if record['kind'] == 'posts':
        prep = record['value']['preprocessing']
        return prep.get('processing_text', record['value'].get('content', ''))
    return record['value'].get('content', '')


def output_text_for(record):
    value = record['value']
    return value['preprocessing'].get('content_clean', '') if record['kind'] == 'posts' else value.get('content_clean', '')


def make_record(kind, value):
    if kind == 'posts':
        prep = value['preprocessing']
        return {'kind': kind, 'record_type': '微博', 'id': str(value.get('weibo_id', '')),
                'author': value.get('user_name', ''), 'created_at': value.get('created_at', ''),
                'value': value, 'preprocessing': prep}
    level = value.get('comment_level')
    return {'kind': 'level1' if level == 1 else 'level2',
            'record_type': '一级评论' if level == 1 else '楼中楼回复',
            'id': str(value.get('dataset_key', '')), 'author': value.get('nick_name', ''),
            'created_at': value.get('created_at_display', ''), 'value': value,
            'preprocessing': value}


def hit_stats(records):
    stats = {kind: Counter() for kind in ('posts', 'level1', 'level2')}
    for record in records:
        raw = source_text_for(record)
        count = stats[record['kind']]
        for name, pattern in (('badge', BADGE), ('bracket_supertopic', BRACKET_SUPERTOPIC),
                              ('column_dot', COLUMN_MARKER), ('repost_marker', REPOST_MARKER)):
            found = list(pattern.finditer(raw))
            count[name + '_records'] += bool(found)
            count[name + '_occurrences'] += len(found)
        count['records_total'] += 1
        count['repost_chain_records'] += bool(split_repost_chain(raw)['segments'])
    return {kind: dict(value) for kind, value in stats.items()}


def _all_segments(record):
    return record['preprocessing'].get('repost_segments', [])


def _removed_categories(record):
    data = record['preprocessing']
    if record['kind'] == 'posts':
        return [item.get('category') for item in data.get('platform_removed_fragments', [])]
    return [item.get('category') for item in data.get('platform_removed_fragments', [])]


def choose_examples(records):
    selected = []
    used = set()

    def add(label, predicate, note, segment_predicate=None):
        for record in records:
            if record['id'] in used or not predicate(record):
                continue
            segment = None
            if segment_predicate:
                segment = next((s for s in _all_segments(record) if segment_predicate(s)), None)
                if segment is None:
                    continue
            used.add(record['id'])
            before = source_text_for(record)
            after = output_text_for(record)
            if segment:
                note_text = (f"转发作者={segment.get('author')}; 状态={segment.get('match_status')}; "
                             f"原因={','.join(segment.get('match_review_reasons', [])) or '无'}; "
                             f"原微博ID={segment.get('matched_post_id') or '未确认'}; 引文={excerpt(segment.get('raw_text'), 90)}")
            else:
                categories = ','.join(x for x in _removed_categories(record) if x)
                flags = ','.join(record['preprocessing'].get('social_cleaning_flags', []))
                note_text = '; '.join(x for x in (note, f'删除={categories}' if categories else '', f'标记={flags}' if flags else '') if x)
            selected.append({'label': label, 'record_type': record['record_type'], 'id': record['id'],
                             'author': record['author'], 'before': before, 'after': after,
                             'note': note_text})
            return True
        return False

    add('徽标超话前缀清除', lambda r: 'weibo_supertopic_badge' in _removed_categories(r), '正文保留，平台徽标单独记录')
    add('方括号超话前缀清除', lambda r: 'hashtag_supertopic' in _removed_categories(r), '仅删除明确的[超话]包装')
    add('普通话题与超话混排', lambda r: {'weibo_supertopic_badge', 'hashtag_supertopic'} <= set(_removed_categories(r)), '普通话题名保留')
    add('连续多个超话标记', lambda r: sum(x in {'weibo_supertopic_badge', 'hashtag_supertopic'} for x in _removed_categories(r)) >= 2, '移除多个确认的前缀')
    add('普通话题标签保留', lambda r: bool(NORMAL_HASHTAG.search(source_text_for(r))) and not BRACKET_SUPERTOPIC.search(source_text_for(r)), '话题文字仍进入清洗正文')
    add('正文内超话文字保留', lambda r: ('超话' in source_text_for(r) and not any(x in _removed_categories(r) for x in ('weibo_supertopic_badge', 'hashtag_supertopic'))), '不在片段开头的文字不按平台前缀删除')
    add('@提及、表情和语气保留', lambda r: '@' in source_text_for(r) and bool(EMOJI.search(source_text_for(r))) and '@' in output_text_for(r), '原表达不做停用词或情绪过滤')
    add('明确边界的专栏尾注', lambda r: bool(r['preprocessing'].get('column_reference')) and r['preprocessing']['column_reference'].get('reason') == 'trailing_column_share_title_with_platform_boundary', '尾注标题另存，前面的评论保留')
    add('仅专栏分享卡片', lambda r: (r['preprocessing'].get('column_reference') or {}).get('reason') == 'column_share_title_only', '标题另存为引用信息')
    add('专栏格式歧义待复核', lambda r: any(flag.startswith('possible_column_reference') for flag in r['preprocessing'].get('social_cleaning_flags', [])), '原句保留，未自动截断')
    add('普通提及专栏保留', lambda r: '专栏' in source_text_for(r) and not COLUMN_MARKER.search(source_text_for(r)), '没有分享标记的普通句子保持原样')
    add('已收录转发片段去重', lambda r: True, '匹配依据含作者、完整正文和时间', lambda s: s.get('match_status') == 'confirmed_in_database')
    add('库内未匹配转发片段保留', lambda r: True, '片段与原作者仍单独保存', lambda s: s.get('match_status') == 'not_found_in_database')
    add('短转发片段待复核', lambda r: True, '短句不自动删除', lambda s: 'short_repost_fragment' in s.get('match_review_reasons', []))
    add('转发作者不一致待复核', lambda r: True, '不依据相同正文自动删除', lambda s: 'author_mismatch_or_missing' in s.get('match_review_reasons', []))
    add('重复候选待复核', lambda r: True, '候选不唯一时保留原片段', lambda s: 'non_unique_database_match' in s.get('match_review_reasons', []))
    add('疑似截断原文待复核', lambda r: True, '疑似前缀匹配不作为去重证据', lambda s: 'possible_truncated_source_text' in s.get('match_review_reasons', []))
    add('只有转发内容的微博', lambda r: 'repost_only_record' in r['preprocessing'].get('social_cleaning_flags', []), '微博记录保留，原创正文为空，引用及作者另存')
    add('评论中的超话标签', lambda r: r['kind'] == 'level1' and 'hashtag_supertopic' in _removed_categories(r), '评论ID及所属微博关系保留')
    add('楼中楼专栏格式待复核', lambda r: r['kind'] == 'level2' and any(flag.startswith('possible_column_reference') for flag in r['preprocessing'].get('social_cleaning_flags', [])), '回复原文保留')
    add('补全长文后先清洗', lambda r: r['kind'] == 'posts' and r['preprocessing'].get('processing_text_source') == 'verified_existing_longtext_supplement' and bool(_removed_categories(r)), '原文哈希对应原始content，补全文本单独哈希')
    add('人工实体标注与原文哈希', lambda r: r['kind'] == 'posts' and any(x.get('resolution') == 'human_review' for x in r['preprocessing'].get('alias_matches', [])), '保留人工标注，归一化跨度由最终检查确认')
    return selected


def build_report(output_dir, compare_output_dir=None):
    out = Path(output_dir).resolve()
    validation = json.loads((out / 'validation_report.json').read_text(encoding='utf-8'))
    posts = [make_record('posts', value) for value in read_lines(out / 'posts_preprocessed.jsonl')]
    comments = [make_record('comments', value) for value in read_lines(out / 'comments_preprocessed.jsonl')]
    records = posts + comments
    audit = read_lines(out / 'social_cleaning_audit.jsonl')
    pre_hits = hit_stats(records)
    social = validation['social_cleaning']
    repost_counts = validation.get('repost_match_status_counts', {})
    review_rows = []
    for row in audit:
        flags = row.get('social_cleaning_flags', [])
        segments = row.get('repost_segments', [])
        review_segments = [s for s in segments if s.get('match_status') == 'needs_manual_review']
        if any(flag.startswith('possible_column_reference') for flag in flags) or review_segments:
            review_rows.append({**row, 'repost_segments_needing_review': review_segments})
    examples = choose_examples(records)
    deterministic_files = ['posts_preprocessed.jsonl', 'comments_preprocessed.jsonl',
        'comment_relations.jsonl', 'social_cleaning_audit.jsonl', 'post_source_records.jsonl',
        'comment_source_records.jsonl', 'human_annotations.jsonl', 'pending_review.jsonl',
        'pending_relations.jsonl', 'record_quality.jsonl', 'invalid_records.jsonl',
        'research_dataset.sqlite3']
    determinism = None
    if compare_output_dir:
        repeat = Path(compare_output_dir).resolve()
        comparisons = []
        for name in deterministic_files:
            left, right = sha256(out / name), sha256(repeat / name)
            comparisons.append({'file': name, 'identical': left == right, 'sha256': left,
                                'repeat_sha256': right})
        determinism = {'repeat_run_identical': all(x['identical'] for x in comparisons),
                       'files_compared': len(comparisons), 'files_identical': sum(x['identical'] for x in comparisons),
                       'files': comparisons}
    report_path = out / 'social_cleaning_report.md'
    report = [
        '# 微博文本清洗报告',
        '',
        '运行日期：2026-11-09。清洗发生在实体归一化和 jieba 分词之前。源库和历史输出只读；本报告、复核队列与新研究库均写入本输出目录。',
        '',
        '## 输入命中量',
        '',
        '以下为原始处理文本的扫描量，按记录类型拆分；同一条可命中多类。徽标/方括号匹配统计片段中出现的标记，可能位于正文或转发片段内，实际删除只针对片段开头的确认平台标记。',
        '',
        '|记录类型|记录数|徽标超话记录/标记|方括号超话记录/标记|“专栏 ·”记录/标记|转发链记录/片段|',
        '|---|---:|---:|---:|---:|---:|',
    ]
    for kind, title in [('posts', '微博'), ('level1', '一级评论'), ('level2', '楼中楼回复')]:
        h = pre_hits[kind]
        segments = sum(len(split_repost_chain(source_text_for(r))['segments']) for r in records if r['kind'] == kind)
        report.append(f"|{title}|{h.get('records_total', 0)}|{h.get('badge_records', 0)}/{h.get('badge_occurrences', 0)}|{h.get('bracket_supertopic_records', 0)}/{h.get('bracket_supertopic_occurrences', 0)}|{h.get('column_dot_records', 0)}/{h.get('column_dot_occurrences', 0)}|{h.get('repost_chain_records', 0)}/{segments}|")
    report += [
        '',
        '## 处理结果',
        '',
        '|问题|命中或片段量|成功自动处理|保留/待复核|处理口径|',
        '|---|---:|---:|---:|---|',
    ]
    topic_rows = sum(social[k].get('records_with_supertopic_removal', 0) for k in social)
    topic_markers = sum(social[k].get('supertopic_markers_removed', 0) for k in social)
    column_hits = sum(pre_hits[k].get('column_dot_records', 0) for k in pre_hits)
    column_removed = sum(social[k].get('column_references_removed', 0) for k in social)
    column_review = sum(social[k].get('records_with_column_reference_review', 0) for k in social)
    repost_records = sum(social[k].get('records_with_repost_chain', 0) for k in social)
    repost_segments = sum(social[k].get('repost_segments', 0) for k in social)
    repost_only = sum(social[k].get('repost_only_records', 0) for k in social)
    report.append(f'|超话前缀|{topic_rows} 条记录|{topic_markers} 个标记已删除|仅正文内提及保留|只清除明确徽标或[超话]标签，不删除普通话题名|')
    report.append(f'|专栏引用|{column_hits} 条记录出现“专栏 ·”|{column_removed} 个有明确边界的引用已移出正文|{column_review} 条原文保留并待复核|不满足边界证据时不截断正文|')
    report.append(f'|微博转发链|{repost_records} 条记录，{repost_segments} 个片段|{repost_counts.get("confirmed_in_database", 0)} 个确认已收录片段标记为重复来源|{repost_counts.get("needs_manual_review", 0)} 个待复核；{repost_counts.get("not_found_in_database", 0)} 个未匹配片段保留|只把原创段落送入当前作者的归一化/分词；引用片段另存作者、正文、匹配证据|')
    report += [
        '',
        f'纯转发记录保留 {repost_only} 条，设置 `repost_only_record`；其他用户的引用内容没有并入当前博主的恶意/实体句级特征。待复核队列共 {len(review_rows)} 条记录（可能同时含专栏歧义和转发匹配歧义），见 `social_cleaning_review.jsonl`。',
        '',
        '## 字段与完整性核验',
        '',
        f"- 原始记录保留：微博 {validation['posts']} 条，评论/回复 {validation['comments']} 条；原始字段及类型回读一致：`{validation['original_fields_values_types_preserved']}`；所有来源观测回读一致：`{validation['all_source_observations_roundtrip_equal']}`。",
        f"- 长文：使用已有补全文本 {validation['longtext_used']} 条；`content` 原文不覆盖，另存 `processing_text` 和 SHA-256。",
        f"- 人工标注：保留 {validation['annotations']} 条标注；人工实体归一化位置核验错误 {validation.get('alias_normalized_span_errors', 0)}；微博人工消歧应用 {validation['manual_post_occurrences_applied']} 处。",
        f"- SQLite：`{validation['sqlite_integrity']}`；外键违规 {len(validation['sqlite_foreign_key_violations'])}；微博和评论ID、微博归属、回复根/父关系及采集记录继续保留。",
        '- 未改实体词典或人工判断；没有增加停用词过滤或自动恶意标签。',
        '',
        '## 重复执行一致性',
        '',
        (f"以相同冻结输入完整重跑后，{determinism['files_identical']}/{determinism['files_compared']} 个关键数据文件（含 SQLite、原始来源观测、人工标注和清洗审计）SHA-256 完全一致。"
         if determinism else '本次报告生成时未提供第二次完整运行结果。'),
        '',
        '## 清洗前后样本',
        '',
        '样本取自本地完整数据；转发引用在“结果”列单独显示，避免把引用者的话归到当前作者。正文在 135 字符处截短展示，完整内容保留在 SQLite/JSONL。',
        '',
        '|#|问题/风险|记录|清洗前|清洗后当前作者正文|结果与复核信息|',
        '|---:|---|---|---|---|---|',
    ]
    for number, item in enumerate(examples, 1):
        report.append(f"|{number}|{item['label']}|{item['record_type']} / `{item['id']}` / {excerpt(item['author'], 32)}|{excerpt(item['before'])}|{excerpt(item['after'])}|{excerpt(item['note'], 140)}|")
    report += [
        '',
        '## 输出',
        '',
        f'- 研究数据库：`{out / "research_dataset.sqlite3"}`',
        f'- 微博：`{out / "posts_preprocessed.jsonl"}`',
        f'- 评论与楼中楼回复：`{out / "comments_preprocessed.jsonl"}`',
        f'- 清洗审计：`{out / "social_cleaning_audit.jsonl"}`',
        f'- 人工复核队列：`{out / "social_cleaning_review.jsonl"}`',
        f'- 本报告：`{report_path}`',
        '',
        '输入快照由既有完整流水线生成，路径记录在 `validation_report.json`。所有真实语料与人工标注仅保存在本机输出目录，没有推送到 GitHub。',
        '',
    ]
    report_path.write_text('\n'.join(report), encoding='utf-8')
    write_lines(out / 'social_cleaning_review.jsonl', review_rows)
    metrics = {'pre_cleaning_hits': pre_hits, 'social_cleaning': social,
               'repost_match_status_counts': repost_counts,
               'determinism_check': determinism,
               'review_records': len(review_rows), 'example_count': len(examples),
               'original_content_hashes_valid': sum(
                   hashlib.sha256(r['value'].get('content', '').encode('utf-8')).hexdigest() == r['preprocessing'].get('content_sha256')
                   for r in records),
               'original_record_count': len(records), 'sqlite_integrity': validation['sqlite_integrity'],
               'alias_normalized_span_errors': validation.get('alias_normalized_span_errors', 0)}
    (out / 'social_cleaning_metrics.json').write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    shutil.copyfile(Path(__file__).resolve(), out / Path(__file__).name)
    print(json.dumps({'report': str(report_path), 'review_records': len(review_rows),
                      'example_count': len(examples), 'metrics': metrics}, ensure_ascii=False, indent=2))
    return metrics


def main():
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--compare-output-dir', type=Path,
                        help='Optional second complete run to compare primary data output hashes')
    args = parser.parse_args()
    build_report(args.output_dir, args.compare_output_dir)


if __name__ == '__main__':
    main()
