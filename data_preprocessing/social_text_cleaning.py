"""Conservative Weibo wrapper cleanup and repost-chain provenance helpers.

Only platform structures with clear boundaries are removed. The original input
text remains the caller's source of truth; this module returns derived fields.
"""
from collections import Counter, defaultdict
from datetime import datetime
import html
import re
import unicodedata
from urllib.parse import urlsplit

from relevance import clean_text


ZERO_WIDTH = re.compile(r'[\u180e\u200b-\u200c\u200e-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]')
PRIVATE_ICONS = re.compile(r'[\ue000-\uf8ff\U000f0000-\U000ffffd\U00100000-\U0010fffd]')
URL = re.compile(r'https?://[^\s<>"\'，。！？；、（）【】]+', re.IGNORECASE)
HASHTAG = re.compile(r'[#＃]([^#＃\n\r]*?)[#＃]')
LEADING_HASHTAG = re.compile(r'[#＃][^#＃\n\r]{1,140}[#＃]')
LEADING_BADGE = re.compile(r'[^\s\r\n]{1,100}?超话')
REPOST_MARKER = re.compile(r'//\s*[@＠](?P<author>[^:：\s@＠]{1,80})\s*[:：]\s*')
COLUMN_MARKER = re.compile(r'专栏\s*[·•・‧∙]\s*')
VIDEO_LINK_LABEL = re.compile(r'L[^\s。！？!?，,；;]{1,200}?的微博视频')
URL_STATUS_ID = re.compile(r'(?:weibo\.com/(?:u/)?\d+/|[?&](?:id|mid)=)(\d{10,20})', re.IGNORECASE)
KNOWN_HTML_TAG = re.compile(r'</?(?:a|abbr|b|blockquote|body|br|div|em|html|i|img|li|ol|p|source|span|strong|ul|video)\b[^>]*>', re.IGNORECASE)
EXPLICIT_FALSE = {'0', 'false', 'no', 'n', '否', '不完整', '未完成'}
SUFFIX_SENTENCE_PUNCTUATION = set('。！？?!')


def _text(value):
    return value if isinstance(value, str) else ''


def split_repost_chain(text):
    """Split `own text//@author: quoted text` without discarding delimiters."""
    source = _text(text)
    markers = list(REPOST_MARKER.finditer(source))
    if not markers:
        return {'original_text': source, 'segments': []}
    segments = []
    for index, marker in enumerate(markers):
        end = markers[index + 1].start() if index + 1 < len(markers) else len(source)
        raw_body = source[marker.end():end]
        segments.append({
            'chain_index': index + 1,
            'author': marker.group('author').strip(),
            'raw_text': raw_body,
            'marker': marker.group(0),
            'source_start': marker.start(),
            'source_end': end,
        })
    return {'original_text': source[:markers[0].start()], 'segments': segments}


def _remove_leading_supertopics(text):
    """Remove only explicitly marked supertopics at the start of a segment.

    Ordinary leading hashtags are retained. They are scanned so a mixed run of
    ordinary hashtags and platform supertopic markers can still be recognized.
    """
    source = _text(text)
    cursor = 0
    recognized = []
    ordinary = []
    while cursor < len(source):
        gap = re.match(r'[\s\u200b\u200c\u200e]*', source[cursor:])
        token_start = cursor + (gap.end() if gap else 0)
        if token_start >= len(source):
            break
        badge = LEADING_BADGE.match(source, token_start)
        if badge:
            recognized.append((badge.start(), badge.end(), 'weibo_supertopic_badge'))
            cursor = badge.end()
            continue
        hashtag = LEADING_HASHTAG.match(source, token_start)
        if hashtag:
            inner = hashtag.group(0)[1:-1].strip()
            if re.search(r'(?:\[\s*超话\s*\]|【\s*超话\s*】|（\s*超话\s*）|\(\s*超话\s*\)|超话)$', inner):
                recognized.append((hashtag.start(), hashtag.end(), 'hashtag_supertopic'))
            else:
                ordinary.append((hashtag.start(), hashtag.end()))
            cursor = hashtag.end()
            continue
        break
    if not recognized:
        return source, []
    removed = []
    for start, end, reason in reversed(recognized):
        removed.append({'category': reason, 'reason': 'confirmed_leading_platform_supertopic',
                        'text': source[start:end], 'source_start': start, 'source_end': end})
        source = source[:start] + ' ' + source[end:]
    removed.reverse()
    return source, removed


def _column_reference(text):
    """Return a high-confidence trailing share reference, or a review flag."""
    source = _text(text)
    markers = list(COLUMN_MARKER.finditer(source))
    if not markers:
        return source, None, []
    marker = markers[-1]
    title_tail = source[marker.end():]
    title = title_tail.strip(' \t\r\n\u200b\u200c\u200e\ufeff')
    if not title:
        return source, None, ['possible_column_reference_without_title']
    before = source[:marker.start()]
    boundary = marker.start() == 0 or bool(re.search(r'[\n\r\u200b\u200c]\s*$', before))
    sentence_boundary = bool(before.rstrip().endswith(tuple(SUFFIX_SENTENCE_PUNCTUATION)))
    at_end = not title_tail.strip(' \t\r\n\u200b\u200c\u200e\ufeff') == ''
    plausible_title = len(title) <= 160 and not any(ch in title for ch in '。！？?!')
    if marker.end() <= len(source) and at_end and plausible_title and (boundary or sentence_boundary):
        ref = {'category': 'column_share_reference', 'reason': 'trailing_column_share_title_with_platform_boundary',
               'title': title, 'text': source[marker.start():],
               'source_start': marker.start(), 'source_end': len(source)}
        return source[:marker.start()], ref, []
    if marker.start() == 0 and plausible_title:
        ref = {'category': 'column_share_reference', 'reason': 'column_share_title_only',
               'title': title, 'text': source, 'source_start': marker.start(), 'source_end': len(source)}
        return '', ref, []
    return source, None, ['possible_column_reference_needs_review']


def _clean_segment(raw):
    original = _text(raw)
    # Only hand clear markup to HTMLParser. Weibo text often contains angle-bracket
    # emoticons or names like `<xyt专栏 · ...>`; treating those as tags loses speech.
    source = clean_text(original) if KNOWN_HTML_TAG.search(original) else html.unescape(original)
    source, removed = _remove_leading_supertopics(source)
    source, column_reference, review_flags = _column_reference(source)
    source_urls = URL.findall(source)
    if column_reference:
        source_urls.extend(URL.findall(column_reference.get('text', '')))
    source_ids = []
    for link in source_urls:
        source_ids.extend(URL_STATUS_ID.findall(link))
    source = VIDEO_LINK_LABEL.sub(' ', source)
    source = URL.sub(' ', source)
    source = HASHTAG.sub(r'\1', source)
    source = re.sub(r'(?<!\w)(?:展开全文|展开c|收起全文|查看图片|网页链接)(?!\w)', ' ', source)
    source = re.sub(r'(?:查看全部|查看最新|继续提问)戳\s*[-=]*>\s*(?=微博智搜)', ' ', source)
    source = re.sub(r'(?<!\S)共\s*\d+\s*条回复(?!\S)', ' ', source)
    source = ''.join(ch for ch in source if ch in '\n\t' or unicodedata.category(ch) != 'Cc')
    source = ZERO_WIDTH.sub('', source)
    source = PRIVATE_ICONS.sub('', source)
    cleaned = re.sub(r'\s+', ' ', source).strip()
    if source_urls:
        removed.extend({'category': 'url', 'reason': 'url_removed_from_analysis_body', 'text': link,
                        'source_start': None, 'source_end': None} for link in source_urls)
    return {
        'content_clean': cleaned,
        'removed_platform_fragments': removed,
        'column_reference': column_reference,
        'source_urls': source_urls,
        'source_ids': sorted(set(source_ids)),
        'social_cleaning_flags': review_flags,
    }


def clean_social_text(raw):
    """Clean own text and each repost segment independently, preserving provenance."""
    split = split_repost_chain(raw)
    own = _clean_segment(split['original_text'])
    repost_segments = []
    for segment in split['segments']:
        cleaned = _clean_segment(segment['raw_text'])
        repost_segments.append({**segment, **cleaned,
            'source_permalink': '', 'matched_post_id': '', 'matching_evidence': [],
            'match_status': 'not_checked', 'match_review_reasons': [],
            'already_independently_collected': False,
            'included_in_current_author_body': False})
    flags = list(own['social_cleaning_flags'])
    if repost_segments and not own['content_clean']:
        flags.append('repost_only_record')
    return {**own, 'repost_segments': repost_segments,
            'repost_chain_present': bool(repost_segments),
            'social_cleaning_flags': sorted(set(flags))}


def _normalize_author(value):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', _text(value)).lstrip('@＠')).casefold()


def _body_key(value):
    source = unicodedata.normalize('NFKC', _text(value)).casefold()
    source = ZERO_WIDTH.sub('', source)
    return re.sub(r'\s+', '', source)


def _url_key(value):
    parts = urlsplit(_text(value).strip())
    if not parts.netloc:
        return unicodedata.normalize('NFKC', _text(value)).rstrip('/').casefold()
    host = parts.netloc.lower()
    if host.startswith('www.'):
        host = host[4:]
    return host + parts.path.rstrip('/')


def _parse_time(value):
    text = _text(value).strip()
    if not text:
        return None
    match = re.match(r'^(\d{2,4}-\d{1,2}-\d{1,2}(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?)', text)
    if match:
        text = match.group(1)
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%y-%m-%d %H:%M', '%Y-%m-%d'):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    return None


def _is_complete_text(record, source_text, supplemented=False):
    if supplemented:
        return True, 'verified_longtext_supplement'
    for field in ('content_truncated', 'is_truncated'):
        if record.get(field) is True or str(record.get(field, '')).strip().casefold() in {'1', 'true', 'yes'}:
            return False, 'explicit_truncation_flag'
    marker = _text(record.get('long_text_complete')).strip().casefold()
    if marker in EXPLICIT_FALSE:
        return False, 'long_text_marked_incomplete'
    if re.search(r'(?:\.\.\.|…|查看全部|展开全文|展开c|收起全文)\s*$', _text(source_text)):
        return False, 'truncation_marker_in_source_text'
    if marker in {'1', 'true', 'yes', '是', '完整'}:
        return True, 'explicit_complete_flag'
    return True, 'no_truncation_evidence'


def _candidate_record(record, text=None, supplemented=False):
    body = _text(text if text is not None else record.get('content'))
    cleaned = clean_social_text(body)
    own = cleaned['content_clean']
    key = _body_key(own)
    if not key:
        return None
    source_urls = list(cleaned.get('source_urls', []))
    for field in ('from_链接', 'url', 'source_url', 'weibo_url'):
        value = _text(record.get(field)).strip()
        if value.startswith(('http://', 'https://')):
            source_urls.append(value)
    complete, completeness_reason = _is_complete_text(record, body, supplemented)
    return {
        'weibo_id': _text(record.get('weibo_id') or record.get('tweet_id') or record.get('id')),
        'author': _text(record.get('user_name') or record.get('nick_name') or record.get('author') or record.get('screen_name')),
        'created_at': _text(record.get('created_at') or record.get('created_at_display')),
        'content': own,
        'body_key': key,
        'source_urls': sorted(set(source_urls)),
        'complete': complete,
        'completeness_reason': completeness_reason,
    }


def build_post_match_index(records, text_by_id=None):
    """Index independent post bodies by exact normalized body and source URL."""
    text_by_id = text_by_id or {}
    by_body = defaultdict(list)
    by_url = defaultdict(list)
    by_author = defaultdict(list)
    for record in records:
        post_id = _text(record.get('weibo_id') or record.get('tweet_id') or record.get('id'))
        body = text_by_id.get(post_id, record.get('content', ''))
        candidate = _candidate_record(record, body, supplemented=post_id in text_by_id)
        if not candidate:
            continue
        by_body[candidate['body_key']].append(candidate)
        author = _normalize_author(candidate['author'])
        if author:
            by_author[author].append(candidate)
        for link in candidate['source_urls']:
            by_url[_url_key(link)].append(candidate)
    return {'by_body': dict(by_body), 'by_url': dict(by_url), 'by_author': dict(by_author)}


def _time_relation(source, current):
    source_time = _parse_time(source)
    current_time = _parse_time(current)
    if not source_time or not current_time:
        return 'missing'
    if source_time > current_time:
        return 'source_after_repost'
    return 'valid'


def match_repost_segments(segments, index, current_id='', current_author='', current_time=''):
    """Attach conservative exact-match status; uncertain matches are never auto-removed."""
    if not index:
        return list(segments)
    matched = []
    author_key = _normalize_author(current_author)
    for segment in segments:
        item = dict(segment)
        body = item.get('content_clean', '')
        body_key = _body_key(body)
        segment_author = _normalize_author(item.get('author'))
        reasons, evidence = [], []
        exact = [c for c in index['by_body'].get(body_key, []) if c['weibo_id'] != _text(current_id)] if body_key else []
        # Explicit IDs in embedded source URLs take priority over text-only matching.
        id_candidates = []
        for source_id in item.get('source_ids', []):
            id_candidates.extend(c for candidates in index['by_body'].values() for c in candidates
                                 if c['weibo_id'] == source_id and c['weibo_id'] != _text(current_id))
        url_candidates = []
        for link in item.get('source_urls', []):
            normalized_url = _url_key(link)
            url_candidates.extend(index['by_url'].get(normalized_url, []))
        direct = {c['weibo_id']: c for c in id_candidates + url_candidates}
        candidates = list(direct.values()) if direct else exact
        unique = {c['weibo_id']: c for c in candidates}
        item['match_status'] = 'not_found_in_database'
        item['match_review_reasons'] = []
        item['already_independently_collected'] = False
        item['included_in_current_author_body'] = False
        if not body_key or (len(body_key) < 20 and not direct):
            item['match_status'] = 'needs_manual_review'
            reasons.append('short_repost_fragment')
        elif len(unique) > 1:
            item['match_status'] = 'needs_manual_review'
            reasons.append('non_unique_database_match')
        elif unique:
            candidate = next(iter(unique.values()))
            same_author = bool(segment_author and segment_author == _normalize_author(candidate['author']))
            time_check = _time_relation(candidate['created_at'], current_time)
            evidence.append('source_id_or_permalink' if direct else 'exact_body')
            evidence.append('author_match' if same_author else 'author_mismatch')
            evidence.append('source_time_valid' if time_check == 'valid' else 'source_time_' + time_check)
            evidence.append('source_' + candidate['completeness_reason'])
            if not same_author:
                reasons.append('author_mismatch_or_missing')
            if time_check == 'source_after_repost':
                reasons.append('source_time_after_repost')
            elif time_check == 'missing' and not direct:
                reasons.append('timestamp_missing')
            if not candidate['complete']:
                reasons.append('source_text_incomplete')
            if reasons:
                item['match_status'] = 'needs_manual_review'
            else:
                item['match_status'] = 'confirmed_in_database'
                item['already_independently_collected'] = True
                item['matched_post_id'] = candidate['weibo_id']
                item['source_permalink'] = next(iter(candidate['source_urls']), '')
        else:
            # A same-author prefix match signals truncation; preserve it for review.
            same_author_candidates = index['by_author'].get(segment_author, []) if segment_author else []
            prefix_candidates = [c for c in same_author_candidates if c['weibo_id'] != _text(current_id)
                                 and (c['body_key'].startswith(body_key) or body_key.startswith(c['body_key']))]
            if prefix_candidates:
                item['match_status'] = 'needs_manual_review'
                reasons.append('possible_truncated_source_text')
            elif exact:
                item['match_status'] = 'needs_manual_review'
                reasons.append('author_mismatch_or_missing')
        item['matching_evidence'] = evidence
        item['match_review_reasons'] = reasons
        matched.append(item)
    return matched


def summarize_repost_matches(records):
    counts = Counter()
    for record in records:
        for segment in record:
            counts[segment.get('match_status', 'not_checked')] += 1
            counts['segments_total'] += 1
    return dict(counts)
