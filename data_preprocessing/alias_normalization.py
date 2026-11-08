"""用户提供的同义词及用语属性：最长匹配、单次替换、保留原始称呼。"""
import re

PROTECTED_SIGNAL_WORDS = frozenset('不 没 别 太 好 丑 恶心 失望 敷衍 催 求 骂 举报 优化 加强 削弱 返场 能不能 敢不敢'.split())


class AliasNormalizer:
    def __init__(self, entries, manual_resolutions=None):
        self.manual_resolutions = {}
        self.applied_manual_resolutions = set()
        for item in manual_resolutions or []:
            key = (str(item['weibo_id']), item['surface'].casefold(), item.get('occurrence', 1))
            if key in self.manual_resolutions:
                raise ValueError('人工语境标注存在重复键')
            self.manual_resolutions[key] = dict(item)
        if isinstance(entries, dict):
            entries = [dict(surface=k, canonical=v, term_type='neutral_alias',
                            weight=10000, category='game', sensitive=0, toxicity=0)
                       for k, v in entries.items()]
        if not isinstance(entries, list):
            raise ValueError('同义词配置必须是词条数组或旧版映射对象')
        self.entries = {}
        for entry in entries:
            required = {'surface', 'canonical', 'term_type', 'weight', 'category', 'sensitive', 'toxicity'}
            if not isinstance(entry, dict) or not required.issubset(entry):
                raise ValueError('同义词条缺少所需属性')
            if not all(isinstance(entry[k], str) and entry[k] for k in ('surface', 'canonical', 'term_type', 'category')):
                raise ValueError('同义词名称与类型必须是非空字符串')
            if entry['sensitive'] not in (0, 1) or (entry['toxicity'] is not None and not 0 <= entry['toxicity'] <= 1):
                raise ValueError('敏感标记或 toxicity 超出范围')
            key = entry['surface'].casefold()
            if key in self.entries:
                raise ValueError('同义词 surface 重复')
            self.entries[key] = dict(entry)
        # 标准名称也加入匹配，保护“加页手记”“周上限”等免受内部短简称替换。
        self.canonical_forms = {entry['canonical'].casefold(): entry['canonical'] for entry in self.entries.values()}
        terms = dict(self.canonical_forms)
        terms.update({key: entry['surface'] for key, entry in self.entries.items()})
        self.keys = list(sorted(terms, key=lambda key: (-len(terms[key]), key)))
        alternatives = []
        for index, key in enumerate(self.keys):
            surface = terms[key]
            pattern = r'兆\s*[√✓]' if surface == '兆√' else re.escape(surface)
            if surface.isascii():
                pattern = r'(?<![A-Za-z0-9_])' + pattern + r'(?![A-Za-z0-9_])'
            alternatives.append(f'(?P<a{index}>{pattern})')
        self.pattern = re.compile('|'.join(alternatives), re.IGNORECASE) if alternatives else None

    @staticmethod
    def resolve(entry, text, start, end):
        status = entry.get('review_status', 'user_supplied')
        if status == 'pending':
            return False, 'pending_human_review', []
        if status == 'context_required':
            window = entry.get('context_window', 48)
            local = text[max(0, start-window):end+window].casefold()
            # p5 的图号用法优先排除，不能因附近碰巧有“联动”就强行解释。
            before, after = text[max(0, start-16):start], text[end:end+16]
            if any(before.casefold().endswith(word.casefold()) for word in entry.get('exclude_before', [])) or any(
                    after.casefold().startswith(word.casefold()) for word in entry.get('exclude_after', [])):
                return False, 'non_player_expression', []
            if entry.get('exclude_image_number') and (
                    re.search(r'[0-9]\ufe0f?\u20e3\s*$', before) or
                    re.search(r'(?i)p\d+\s*[-～~—至]\s*$', before) or
                    re.search(r'(?:阅读|看图|详见|见图|请看)\s*$', before) or
                    re.search(r'(?i)^\s*[-～~—至]\s*p?\d+', after) or
                    (re.search(r'(?i)p[1-46-9](?![0-9])', local) and
                     not any(word in local for word in ['女神异闻录', '雨宫莲', '明智吾郎', 'joker', '觉醒']))):
                return False, 'possible_image_number', []
            cues = [word for word in entry.get('context_any', []) if word.casefold() in local]
            return bool(cues), 'resolved_by_context' if cues else 'insufficient_context', cues
        return True, 'user_confirmed' if status == 'confirmed' else 'user_supplied', []

    def normalize(self, text, record_id=None, raw_content_hash=None):
        if self.pattern is None:
            return text, []
        chunks, matches, end, length = [], [], 0, 0
        occurrences = {}
        for match in self.pattern.finditer(text):
            gap = text[end:match.start()]
            chunks.append(gap)
            length += len(gap)
            key = self.keys[int(match.lastgroup[1:])]
            entry = self.entries.get(key)
            if entry:
                occurrences[key] = occurrences.get(key, 0) + 1
                manual_key = (str(record_id), key, occurrences[key])
                manual = self.manual_resolutions.get(manual_key)
                if manual:
                    expected = manual.get('expected_content_sha256')
                    if expected and expected != raw_content_hash:
                        raise ValueError('人工标注对应的原文已变化，请重新核对')
                    entry = {**entry, **{k: manual[k] for k in ('canonical', 'term_type', 'category',
                        'sensitive', 'toxicity', 'definition', 'review_status', 'segmentation',
                        'segmentation_definitions') if k in manual},
                        'manual_resolution_source': manual.get('source_file', ''),
                        'human_note': manual.get('human_note', '')}
                    resolved, resolution, cues = True, 'human_review', []
                    self.applied_manual_resolutions.add(manual_key)
                else:
                    resolved, resolution, cues = self.resolve(entry, text, match.start(), match.end())
                canonical = entry['canonical'] if resolved else match.group()
                matches.append({**entry, 'surface': match.group(), 'configured_surface': entry['surface'],
                                'review_status': entry.get('review_status', 'user_supplied'),
                                'resolved': resolved, 'resolution': resolution, 'context_cues': cues,
                                'emitted_canonical': canonical,
                                'source_start': match.start(), 'source_end': match.end(),
                                'clean_start': length, 'clean_end': length + len(canonical)})
            else:
                canonical = self.canonical_forms[key]
            chunks.append(canonical)
            length += len(canonical)
            end = match.end()
        chunks.append(text[end:])
        return ''.join(chunks), matches


def annotated_tokens(text, matches, tokenizer, stopwords, keep_single, dictionary, canonical_forms=None):
    """已识别的别名整词保留，其余片段正常切词；两版词序一一对应。"""
    annotations = []

    def emit(word, match=None):
        word = word.strip()
        if not word or (not match and not re.search(r'[\u3400-\u9fffA-Za-z0-9]', word)):
            return
        if word.casefold() in (canonical_forms or {}):
            word = canonical_forms[word.casefold()]
        elif word.isascii():
            word = word.lower()
        if word not in PROTECTED_SIGNAL_WORDS and (
                word.casefold() in stopwords or (not match and len(word) <= 1 and word not in keep_single)):
            return
        pending = bool(match and not match.get('resolved', True))
        derogatory = bool(match and not pending and match['term_type'] == 'derogatory_alias')
        suffix = '_待核实' if pending else ({'derogatory_alias': '_黑称', 'pejorative_alias': '_贬称',
                    'ironic_curse': '_反讽', 'euphemistic_curse': '_谐音',
                    'homophonic_alias': '_谐音'}.get(match['term_type'], '') if match else '')
        annotations.append({'token': word + suffix, 'canonical': word,
                            'surface': match['surface'] if match else word,
                            'term_type': match['term_type'] if match else 'literal',
                            'category': match['category'] if match else dictionary.get(word, ''),
                            'sensitive': match['sensitive'] if match else 0,
                            'toxicity': match['toxicity'] if match else 0,
                            'is_derogatory_alias': int(derogatory),
                            'review_status': match.get('review_status', 'user_supplied') if match else 'unreviewed',
                            'resolution': match.get('resolution', 'user_supplied') if match else 'literal',
                            'needs_review': int(pending),
                            'definition': match.get('definition', '') if match else '',
                            'related_entity': match.get('related_entity', '') if match else '',
                            'is_manual_resolution': int(bool(match and match.get('resolution') == 'human_review')),
                            'human_note': match.get('human_note', '') if match else '',
                            'segmentation_parent': match.get('segmentation_parent', '') if match else '',
                            'is_alias_match': int(match is not None)})

    end = 0
    for match in matches:
        for word in tokenizer.lcut(text[end:match['clean_start']], HMM=True):
            emit(word)
        if match.get('segmentation') and match.get('resolved', True):
            for part in match['segmentation']:
                emit(part, {**match, 'surface': part, 'segmentation_parent': match['surface'],
                            'definition': match.get('segmentation_definitions', {}).get(part, match.get('definition', ''))})
        else:
            emit(text[match['clean_start']:match['clean_end']], match)
        end = match['clean_end']
    for word in tokenizer.lcut(text[end:], HMM=True):
        emit(word)
    return (' '.join(item['canonical'] for item in annotations),
            ' '.join(item['token'] for item in annotations), annotations)
