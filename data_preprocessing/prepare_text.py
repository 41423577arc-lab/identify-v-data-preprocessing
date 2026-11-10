"""最终数据的正则清洗、jieba 分词及词表审核材料；保留全部输入行和原始正文。"""
import argparse
from collections import Counter
import json
from pathlib import Path
import re

from data_io import csv_bytes, read_csv, sha256
from preprocess import BASE, save_outputs
from alias_normalization import AliasNormalizer, annotated_tokens, PROTECTED_SIGNAL_WORDS
from social_text_cleaning import (MentionIndex, build_post_match_index, clean_social_text,
                                  match_repost_segments)


RESOURCES = BASE / 'text_resources'
DEFAULT_SOURCE = BASE / 'outputs/八爪鱼数据_20261024/八爪鱼合并数据.csv'
DEFAULT_OUTPUT = BASE / 'outputs/八爪鱼文本准备_v7'
TOKEN_CONTENT = re.compile(r'[\u3400-\u9fffA-Za-z0-9]')


def read_wordlist(path):
    return {line.strip() for line in Path(path).read_text(encoding='utf-8-sig').splitlines()
            if line.strip() and not line.lstrip().startswith('#')}


def real_text_cleaning(text, synonyms=None):
    text = clean_social_text(text)['content_clean']
    if synonyms:
        text, _ = AliasNormalizer(synonyms).normalize(text)
    return text


def segment_text(text, tokenizer, stopwords, keep_single):
    candidates = [word.strip() for word in tokenizer.lcut(text, cut_all=False, HMM=True)]
    words = []
    for word in candidates:
        if not word or not TOKEN_CONTENT.search(word):
            continue
        if word.isascii():
            word = word.lower()
        if word not in stopwords and (len(word) > 1 or word in keep_single):
            words.append(word)
    return ' '.join(words)


def prepare_text(source=DEFAULT_SOURCE, output_dir=DEFAULT_OUTPUT, resources=RESOURCES):
    import jieba
    source, resources = Path(source).resolve(), Path(resources).resolve()
    header, records, info = read_csv(source)
    if 'content' not in header:
        raise ValueError('缺少 content 原始正文列')
    if {'content_clean_previous', 'tokens', 'text_preparation_flags', 'repost_segments'} & set(header):
        raise ValueError('输入已经过文本准备，请使用原始最终数据')
    repost_match_index = build_post_match_index([record['raw'] for record in records])
    mention_index = MentionIndex(record['raw'] for record in records)
    synonyms = json.loads((resources / 'synonyms.json').read_text(encoding='utf-8-sig'))
    manual_path = resources / 'manual_resolutions.json'
    manual_resolutions = json.loads(manual_path.read_text(encoding='utf-8-sig')) if manual_path.is_file() else []
    # 原始最终数据集才使用本批人工语境；临时测试或其他输入不套用已有微博标注。
    input_ids = {r['raw'].get('weibo_id', '') for r in records}
    manual_resolutions = [item for item in manual_resolutions if item['weibo_id'] in input_ids]
    normalizer = AliasNormalizer(synonyms, manual_resolutions)
    stopwords = {w.casefold() for w in read_wordlist(resources / 'stopwords.txt')}
    keep_single = read_wordlist(resources / 'keep_single_chars.txt')
    if stopwords & keep_single:
        raise ValueError('单字保留词与停用词冲突')
    dictionary_lines = [line.split() for line in (resources / 'dwrg_dict.txt').read_text(encoding='utf-8-sig').splitlines()
                        if line.strip() and not line.lstrip().startswith('#')]
    if any(len(parts) != 3 or not parts[1].isdigit() or not re.fullmatch('[a-z_]+', parts[2])
           for parts in dictionary_lines):
        raise ValueError('游戏词典格式须为：词 权重 小写类别')
    # 追加文件保留全部原行；同词重复时，以最后一条用户更新为有效配置。
    dictionary = {parts[0]: parts[2] for parts in dictionary_lines}
    tokenizer = jieba.Tokenizer()
    for word, weight, category in dictionary_lines:
        tokenizer.add_word(word, int(weight), category)
    for entry in normalizer.entries.values():
        tokenizer.add_word(entry['canonical'], entry['weight'], entry['category'])
    # 平台噪声必须先成为完整分词才能由停用词表过滤。
    for word in sorted(stopwords):
        if len(word) > 1:
            tokenizer.add_word(word, 100000, 'noise')
    for word in sorted(PROTECTED_SIGNAL_WORDS):
        if len(word) > 1:
            tokenizer.add_word(word, 100000, 'signal')
    output, comparisons, pending_contexts = [], [], []
    frequencies, document_frequencies = Counter(), Counter()
    empty_clean, empty_tokens, changed_count = 0, 0, 0
    for record in records:
        row = dict(record['raw'])
        social_cleaning = clean_social_text(row['content'], mention_index)
        repost_segments = match_repost_segments(social_cleaning['repost_segments'], repost_match_index,
            current_id=row.get('weibo_id', ''), current_author=row.get('user_name', ''),
            current_time=row.get('created_at', ''))
        surface_text = social_cleaning['content_clean']
        text, alias_matches = normalizer.normalize(surface_text, row.get('weibo_id', ''), sha256(row['content'].encode('utf-8')))
        tokens, classification_tokens, annotations = annotated_tokens(
            text, alias_matches, tokenizer, stopwords, keep_single, dictionary, normalizer.canonical_forms)
        flags = []
        if not text:
            empty_clean += 1
            flags.append('empty_after_cleaning')
        if not tokens:
            empty_tokens += 1
            flags.append('empty_tokens')
        flags.extend(social_cleaning['social_cleaning_flags'])
        if any(segment['match_status'] == 'needs_manual_review' for segment in repost_segments):
            flags.append('repost_match_review_required')
        changed_count += text != row['content']
        if 'content_clean' in row:
            row['content_clean_previous'] = row['content_clean']
        row.update(content_clean=text, content_clean_surface=surface_text,
                   platform_removed_fragments=json.dumps(social_cleaning['removed_platform_fragments'], ensure_ascii=False, separators=(',', ':')),
                   column_reference=json.dumps(social_cleaning['column_reference'], ensure_ascii=False, separators=(',', ':')),
                   repost_segments=json.dumps(repost_segments, ensure_ascii=False, separators=(',', ':')),
                   repost_match_status=json.dumps(dict(Counter(s['match_status'] for s in repost_segments)), ensure_ascii=False, separators=(',', ':')),
                   social_cleaning_flags=';'.join(sorted(set(social_cleaning['social_cleaning_flags']))),
                   tokens=tokens, tokens_clustering=tokens, tokens_classification=classification_tokens,
                   token_annotations=json.dumps(annotations, ensure_ascii=False, separators=(',', ':')),
                   alias_matches=json.dumps(alias_matches, ensure_ascii=False, separators=(',', ':')),
                   is_derogatory_alias=str(int(any(m['term_type'] == 'derogatory_alias' for m in alias_matches))),
                   has_pending_terms=str(int(any(not m['resolved'] for m in alias_matches))),
                   text_preparation_flags=';'.join(flags))
        for match in alias_matches:
            if not match['resolved']:
                pending_contexts.append({'weibo_id': row.get('weibo_id', ''), 'term': match['configured_surface'],
                    'surface': match['surface'], 'proposed_canonical': match['canonical'],
                    'status': match['review_status'], 'resolution': match['resolution'],
                    'candidates': json.dumps(match.get('candidates', []), ensure_ascii=False),
                    'definition': match.get('definition', ''), 'content': row['content'],
                    'local_context': surface_text[max(0, match['source_start']-80):match['source_end']+80],
                    'human_resolution': '', 'human_note': ''})
        output.append(row)
        words = tokens.split()
        frequencies.update(words)
        document_frequencies.update(set(words))
        comparisons.append({'weibo_id': row.get('weibo_id', ''), 'content': row['content'],
                            'content_clean_surface': surface_text, 'content_clean': text,
                            'platform_removed_fragments': row['platform_removed_fragments'],
                            'repost_match_status': row['repost_match_status'],
                            'tokens_clustering': tokens, 'tokens_classification': classification_tokens,
                            'is_derogatory_alias': row['is_derogatory_alias'],
                            'text_preparation_flags': row['text_preparation_flags']})
    if len(normalizer.applied_manual_resolutions) != len(manual_resolutions):
        raise ValueError('存在未匹配到原文的人工语境标注，请核对词条或次数')
    fields = list(header)
    if 'content_clean' in header:
        fields.append('content_clean_previous')
    else:
        fields.append('content_clean')
    fields += ['content_clean_surface', 'platform_removed_fragments', 'column_reference', 'repost_segments',
               'repost_match_status', 'social_cleaning_flags', 'tokens', 'tokens_clustering', 'tokens_classification',
               'token_annotations', 'alias_matches', 'is_derogatory_alias', 'has_pending_terms', 'text_preparation_flags']
    vocabulary = [{'word': w, 'frequency': count, 'document_frequency': document_frequencies[w],
                   'in_game_dictionary': str(w in dictionary).lower(),
                   'category': (dictionary.get(w, '') if dictionary.get(w) != 'sensitive_alias' else 'sensitive_alias_only'),
                   'review_action': '', 'review_note': ''}
                  for w, count in sorted(frequencies.items(), key=lambda item: (-item[1], item[0]))]
    resource_hashes = {name: sha256((resources / name).read_bytes()) for name in
                       ('dwrg_dict.txt', 'stopwords.txt', 'synonyms.json', 'keep_single_chars.txt')}
    glossary = []
    if (resources / 'glossary_review.json').is_file():
        resource_hashes['glossary_review.json'] = sha256((resources / 'glossary_review.json').read_bytes())
        glossary = json.loads((resources / 'glossary_review.json').read_text(encoding='utf-8-sig'))
    if manual_path.is_file():
        resource_hashes['manual_resolutions.json'] = sha256(manual_path.read_bytes())
    player_mapping_path = resources / 'player_id_mapping.json'
    if player_mapping_path.is_file():
        resource_hashes['player_id_mapping.json'] = sha256(player_mapping_path.read_bytes())
    manifest = {'source': info, 'input_rows': len(records), 'output_rows': len(output),
                'rows_deleted': 0, 'changed_text_rows': changed_count,
                'empty_clean_rows': empty_clean, 'empty_token_rows': empty_tokens,
                'game_dictionary_entries': len(dictionary), 'stopwords': len(stopwords),
                'dictionary_appended_duplicate_entries': len(dictionary_lines) - len(dictionary),
                'synonym_entries': len(normalizer.entries),
                'review_status_counts': dict(Counter(e['review_status'] for e in glossary)),
                'pending_occurrences': len(pending_contexts),
                'pending_rows': sum(r['has_pending_terms'] == '1' for r in output),
                'manual_resolutions_applied': len(normalizer.applied_manual_resolutions),
                'derogatory_alias_rows': sum(r['is_derogatory_alias'] == '1' for r in output),
                'alias_metadata_source': 'user_supplied_2026-10-26; toxicity is a configured annotation, not a model probability',
                'keep_single_char_entries': len(keep_single), 'token_vocabulary_size': len(vocabulary),
                'explicit_signal_words_protected': sorted(PROTECTED_SIGNAL_WORDS),
                'jieba_version': jieba.__version__, 'jieba_HMM': True,
                'resources_sha256': resource_hashes, 'script_sha256': sha256(Path(__file__).read_bytes()),
                'alias_normalization_sha256': sha256((BASE / 'alias_normalization.py').read_bytes()),
                'dictionary_status': 'draft_pending_human_review',
                'ambiguous_alias_fifth_normalized': False}
    artifacts = {
        '八爪鱼最终数据_文本清洗与分词.csv': csv_bytes(output, fields),
        '八爪鱼最终数据_分类版.csv': csv_bytes([{**row, 'tokens': row['tokens_classification']} for row in output], fields),
        '八爪鱼最终数据_聚类版.csv': csv_bytes(output, fields),
        '清洗前后对照.csv': csv_bytes(comparisons, list(comparisons[0]) if comparisons else
                                  ['weibo_id', 'content', 'content_clean', 'tokens', 'text_preparation_flags']),
        '分词词频_待审核.csv': csv_bytes(vocabulary, ['word', 'frequency', 'document_frequency',
                                     'in_game_dictionary', 'category', 'review_action', 'review_note']),
        '待核实词语境.csv': csv_bytes(pending_contexts, ['weibo_id', 'term', 'surface', 'proposed_canonical',
            'status', 'resolution', 'candidates', 'definition', 'content', 'local_context', 'human_resolution', 'human_note']),
        '人工语境标注应用记录.csv': csv_bytes([{'weibo_id': item['weibo_id'], 'surface': item['surface'],
            'occurrence': item.get('occurrence', 1), 'canonical': item['canonical'],
            'human_resolution': item.get('human_resolution', ''),
            'original_review_surface': item.get('original_review_surface', item['surface']),
            'original_review_occurrence': item.get('original_review_occurrence', item.get('occurrence', 1)),
            'human_note': item.get('human_note', ''), 'original_human_note': item.get('original_human_note', ''),
            'clarification_source': item.get('clarification_source', ''),
            'source_file': item.get('source_file', ''), 'source_encoding': item.get('source_encoding', '')}
            for item in manual_resolutions], ['weibo_id', 'surface', 'occurrence', 'canonical', 'human_resolution',
            'original_review_surface', 'original_review_occurrence', 'human_note',
            'original_human_note', 'clarification_source', 'source_file', 'source_encoding']),
        '人工标注词表_确定与待核实.csv': csv_bytes([{'term': e['surface'], 'canonical': e['canonical'],
            'status': e['review_status'], 'definition': e['definition'], 'term_type': e['term_type'],
            'related_entity': e.get('related_entity', ''),
            'candidates': json.dumps(e.get('candidates', []), ensure_ascii=False)} for e in glossary],
            ['term', 'canonical', 'status', 'definition', 'term_type', 'related_entity', 'candidates']),
        'text_preparation_manifest.json': (json.dumps(manifest, ensure_ascii=False, indent=2) + '\n').encode('utf-8'),
    }
    for name in resource_hashes:
        artifacts[name] = (resources / name).read_bytes()
    if sha256(source.read_bytes()) != info['sha256']:
        raise ValueError('输入在处理期间发生变化')
    save_outputs(output_dir, artifacts, {source} | {resources / name for name in resource_hashes})
    return {**manifest, 'output_dir': str(Path(output_dir).resolve())}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', nargs='?', type=Path, default=DEFAULT_SOURCE)
    parser.add_argument('--output-dir', type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument('--resources', type=Path, default=RESOURCES)
    args = parser.parse_args()
    result = prepare_text(args.source, args.output_dir, args.resources)
    print(json.dumps(result, ensure_ascii=True, indent=2))


if __name__ == '__main__':
    main()
