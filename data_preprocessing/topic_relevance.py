"""Exact source-supertopic screening for unrelated Weibo records."""
import re
import unicodedata


EXCLUDED_SUPERTOPICS = frozenset({
    'ALCu表白墙',
    '娱乐',
    'sky光遇黑市',
    '教育',
    '荣耀茶话会',
    '蛋仔派对交易',
    '超自然行动组',
})


def normalize_source_topic(value):
    """Normalize only metadata formatting; never use substring matching."""
    topic = unicodedata.normalize('NFKC', value if isinstance(value, str) else '')
    topic = re.sub(r'\s+', '', topic)
    if topic.endswith('超话'):
        topic = topic[:-2]
    return topic


def classify_source_topic(record):
    """Return an auditable exact-match relevance decision from 来源超话."""
    raw = record.get('来源超话', '') if isinstance(record, dict) else ''
    topic = normalize_source_topic(raw)
    excluded = topic in EXCLUDED_SUPERTOPICS
    return {
        'source_field': '来源超话' if isinstance(record, dict) and '来源超话' in record else '',
        'source_topic': raw if isinstance(raw, str) else '',
        'normalized_topic': topic,
        'status': 'clearly_unrelated' if excluded else 'not_matched',
        'excluded': excluded,
        'reason': 'exact_source_supertopic_match' if excluded else '',
    }
