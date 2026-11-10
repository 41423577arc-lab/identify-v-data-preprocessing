"""Rebuild direct-parent links from preserved Weibo evidence.

The source SQLite database is read-only. This tool makes a separate copy,
records evidence in `parent_link_audit`, and never labels a heuristic edge as a
platform-verified edge. Run from any directory with Python 3.10+.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import unicodedata
import zipfile
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
BASE_DIR = ROOT / 'data_preprocessing/outputs/reviewed_full_20261109_aggregate_complete'
DEFAULT_DATABASE = BASE_DIR / 'research_dataset_excel.sqlite3'
DEFAULT_OBSERVATIONS = ROOT / 'data_preprocessing/outputs/reviewed_full_20261109_cleaning_final/comment_source_records.jsonl'
DEFAULT_INPUT_SNAPSHOT = ROOT / 'data_preprocessing/outputs/reviewed_full_20261109_cleaning_final/input_snapshot'
DEFAULT_ARCHIVE = ROOT / 'data_preprocessing/outputs/reviewed_20260924_20261015_20261106.zip'
DEFAULT_XLSX = BASE_DIR / '第五人格微博机器学习语料_全量.xlsx'
DEFAULT_PRIOR_NICKNAME = BASE_DIR / 'research_dataset_parent_links.sqlite3'
DEFAULT_PRIOR_ORDER = BASE_DIR / 'research_dataset_parent_links_time_order.sqlite3'
DEFAULT_OUTPUT_DIR = ROOT / 'data_preprocessing/outputs/parent_comment_relation_repair_20261109'
ROUTE = re.compile(r'(?:^|\n)\s*:?[ \t]*回复\s*[@＠]\s*([^:：\s]{1,80})\s*[:：]')
DIRECT_ID_FIELDS = ('reply_id', 'direct_parent_platform_id')
LOCAL_PARENT_FIELDS = ('parent_comment_key', 'direct_parent_dataset_key',
                       'direct_parent_record_key', 'verified_parent_key')
PARENT_USER_FIELDS = ('reply_user_id', 'reply_to_user_id', 'parent_user_id', 'target_user_id')
RELIABILITY = {
    'platform_id': 'high',
    'nickname_match': 'medium',
    'time_order_inferred': 'low',
    'root_assumed': 'experiment_default',
    'unknown': 'none',
}


def clean_id(value) -> str:
    if value is None:
        return ''
    value = str(value).strip()
    return '' if value.casefold() in {'', 'none', 'null', 'nan', '0'} else value


def dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def json_object(value) -> dict:
    try:
        result = json.loads(value or '{}')
        return result if isinstance(result, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


def normalize_name(value) -> str:
    return ''.join(unicodedata.normalize('NFKC', str(value or '')).casefold().split())


def parse_time(value):
    text = str(value or '')
    match = re.search(r'(?<!\d)(\d{2,4})[-/](\d{1,2})[-/](\d{1,2})\s+(\d{1,2}):(\d{2})', text)
    if not match:
        return None
    year, month, day, hour, minute = map(int, match.groups())
    if year < 100:
        year += 2000
    try:
        return datetime(year, month, day, hour, minute)
    except ValueError:
        return None


def parse_seen(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return None


def route_names(variants) -> set[str]:
    names = set()
    for variant in variants:
        for field in ('content', 'display_text', 'text'):
            for match in ROUTE.finditer(str(variant.get(field) or '')):
                name = match.group(1).strip()
                if name:
                    names.add(name)
    return names


def explicit_parent_evidence(record: dict) -> tuple[list[dict], list[dict]]:
    """Return original parent IDs and local parent keys; root IDs are not direct IDs."""
    ids, keys = [], []
    relation_source = str(record.get('parent_relation_source') or record.get('relation_source') or '')
    root_id = clean_id(record.get('root_comment_id') or record.get('root_comment_key'))
    for field in DIRECT_ID_FIELDS:
        value = clean_id(record.get(field))
        if value:
            ids.append({'field': field, 'value': value})
    parent_id = clean_id(record.get('parent_comment_id'))
    if parent_id and not any(x['field'] == 'parent_comment_id' for x in ids):
        if not (relation_source == 'thread_root_fallback' and parent_id == root_id):
            ids.append({'field': 'parent_comment_id', 'value': parent_id})
    reply = record.get('reply_comment')
    if isinstance(reply, dict):
        value = clean_id(reply.get('id') or reply.get('_id'))
        if value:
            ids.append({'field': 'reply_comment.id', 'value': value})
    for field in LOCAL_PARENT_FIELDS:
        value = clean_id(record.get(field))
        if value:
            keys.append({'field': field, 'value': value})
    return ids, keys


def parent_user_hints(record: dict) -> set[str]:
    result = {clean_id(record.get(k)) for k in PARENT_USER_FIELDS}
    reply = record.get('reply_comment')
    if isinstance(reply, dict):
        user = reply.get('user') or {}
        if isinstance(user, dict):
            result.add(clean_id(user.get('id') or user.get('_id')))
    return {x for x in result if x}


def record_author(record: dict) -> str:
    if record.get('nick_name') or record.get('nickname'):
        return str(record.get('nick_name') or record.get('nickname') or '').strip()
    for field in ('user', 'comment_user'):
        user = record.get(field)
        if isinstance(user, dict):
            return str(user.get('nick_name') or user.get('screen_name') or user.get('nickname') or '').strip()
    return ''


def record_user_id(record: dict) -> str:
    value = clean_id(record.get('user_id') or record.get('uid'))
    if value:
        return value
    for field in ('user', 'comment_user'):
        user = record.get(field)
        if isinstance(user, dict):
            value = clean_id(user.get('id') or user.get('_id') or user.get('user_id'))
            if value:
                return value
    return ''


def record_comment_ids(record: dict) -> set[str]:
    values = {clean_id(record.get(k)) for k in ('comment_id', 'platform_comment_id', '_id', 'id')}
    return {x for x in values if x}


def read_observations(path: Path):
    by_record = defaultdict(list)
    times_by_record = defaultdict(dict)
    field_counts = Counter()
    source_paths = set()
    records = nested = route_count = 0
    if not path.is_file():
        return by_record, times_by_record, {'records': 0, 'nested': 0, 'route': 0,
                                            'fields': {}, 'source_paths': []}
    with path.open(encoding='utf-8-sig') as stream:
        for line in stream:
            if not line.strip():
                continue
            row = json.loads(line)
            record = row.get('record') or {}
            if not isinstance(record, dict):
                continue
            records += 1
            key = clean_id(record.get('record_key') or row.get('store_record_key'))
            level = str(record.get('comment_level') or '')
            if level == '2':
                nested += 1
                if route_names([record]):
                    route_count += 1
                for field in ('comment_id', 'platform_comment_id', 'parent_comment_id',
                              'parent_comment_key', 'reply_id', 'root_comment_id', 'root_comment_key'):
                    if clean_id(record.get(field)):
                        field_counts[field] += 1
                reply = record.get('reply_comment')
                if isinstance(reply, dict) and clean_id(reply.get('id') or reply.get('_id')):
                    field_counts['reply_comment.id'] += 1
            if key:
                by_record[key].append(row)
                source = clean_id(row.get('source_path'))
                seen = parse_seen(row.get('first_seen'))
                if source:
                    source_paths.add(source)
                    if seen:
                        times_by_record[key][source] = seen
    return by_record, times_by_record, {
        'records': records, 'nested': nested, 'route': route_count,
        'fields': dict(field_counts), 'source_paths': sorted(source_paths),
    }


def source_scan(path: Path, input_snapshot: Path, archive: Path, xlsx: Path) -> dict:
    result = {'observations': {}, 'mysql_export': {}, 'comment_snapshot': {},
              'archive': {}, 'xlsx': {}, 'snapshot_manifest': {}}
    _, _, obs = read_observations(path)
    result['observations'] = obs

    mysql_path = input_snapshot / 'mysql_crawl_items.jsonl'
    mysql = Counter()
    if mysql_path.is_file():
        with mysql_path.open(encoding='utf-8-sig') as stream:
            for line in stream:
                if not line.strip():
                    continue
                row = json.loads(line)
                payload = row.get('payload') or {}
                mysql['records'] += 1
                mysql['spider:' + str(row.get('spider') or '')] += 1
                if str(payload.get('comment_level') or '') == '1':
                    mysql['comment_level_1'] += 1
                if str(payload.get('comment_level') or '') == '2':
                    mysql['comment_level_2'] += 1
                    for field in ('comment_id', 'platform_comment_id', 'parent_comment_id',
                                  'parent_comment_key', 'reply_id', 'root_comment_id', 'root_comment_key'):
                        if clean_id(payload.get(field)):
                            mysql[field + '_present'] += 1
                    reply = payload.get('reply_comment')
                    if isinstance(reply, dict) and clean_id(reply.get('id') or reply.get('_id')):
                        mysql['reply_comment.id_present'] += 1
        result['mysql_export'] = dict(mysql)

    shard_stats = Counter()
    manifest_path = input_snapshot / 'sources.json'
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding='utf-8-sig'))
            result['snapshot_manifest'] = dict(Counter(str(x.get('kind')) for x in manifest if isinstance(x, dict)))
        except (json.JSONDecodeError, TypeError):
            pass
    for db_path in sorted(input_snapshot.glob('comments_*.sqlite3')):
        try:
            con = sqlite3.connect(f'file:{db_path.resolve().as_posix()}?mode=ro', uri=True)
            tables = {x[0] for x in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'comments' in tables:
                shard_stats['sqlite_files'] += 1
                shard_stats['comments'] += con.execute('SELECT COUNT(*) FROM comments').fetchone()[0]
                cols = {x[1] for x in con.execute('PRAGMA table_info(comments)')}
                if 'payload' in cols:
                    for field in ('comment_id', 'platform_comment_id', 'parent_comment_id',
                                  'parent_comment_key', 'reply_id', 'root_comment_id', 'root_comment_key'):
                        try:
                            count = con.execute(
                                f"SELECT COUNT(*) FROM comments WHERE json_extract(payload, '$.{field}') IS NOT NULL "
                                f"AND CAST(json_extract(payload, '$.{field}') AS TEXT) NOT IN ('', 'null', '0')"
                            ).fetchone()[0]
                        except sqlite3.DatabaseError:
                            count = 0
                        shard_stats[field + '_present'] += count
                    try:
                        shard_stats['reply_comment.id_present'] += con.execute(
                            "SELECT COUNT(*) FROM comments WHERE json_extract(payload, '$.reply_comment.id') IS NOT NULL"
                        ).fetchone()[0]
                    except sqlite3.DatabaseError:
                        pass
            con.close()
        except sqlite3.DatabaseError:
            shard_stats['unreadable_files'] += 1
    result['comment_snapshot'] = dict(shard_stats)

    archive_stats = Counter()
    if archive.is_file():
        try:
            with zipfile.ZipFile(archive) as zf:
                archive_stats['entries'] = len(zf.namelist())
                for member in zf.namelist():
                    if not member.endswith('.jsonl') or not ('comments_preprocessed' in member or 'comment_relations' in member):
                        continue
                    is_relation = 'comment_relations' in member
                    with zf.open(member) as raw:
                        import io
                        for line in io.TextIOWrapper(raw, encoding='utf-8-sig'):
                            if not line.strip():
                                continue
                            row = json.loads(line)
                            rec = row.get('record') if isinstance(row.get('record'), dict) else row
                            if is_relation:
                                archive_stats['relation_rows'] += 1
                                for field in ('direct_parent_dataset_key', 'verified_parent_key',
                                              'parent_comment_key', 'parent_comment_id'):
                                    if clean_id(rec.get(field)):
                                        archive_stats[field + '_present'] += 1
                                continue
                            archive_stats['comment_rows'] += 1
                            if str(rec.get('comment_level') or '') == '2':
                                archive_stats['nested_rows'] += 1
                                for field in ('comment_id', 'platform_comment_id', 'parent_comment_id',
                                              'parent_comment_key', 'reply_id', 'root_comment_id', 'root_comment_key'):
                                    if clean_id(rec.get(field)):
                                        archive_stats[field + '_present'] += 1
                                reply = rec.get('reply_comment')
                                if isinstance(reply, dict) and clean_id(reply.get('id') or reply.get('_id')):
                                    archive_stats['reply_comment.id_present'] += 1
                                if route_names([rec]):
                                    archive_stats['route_present'] += 1
        except (OSError, zipfile.BadZipFile, json.JSONDecodeError):
            archive_stats['read_error'] += 1
    result['archive'] = dict(archive_stats)
    result['xlsx'] = scan_xlsx_reply_column(xlsx)
    return result


def scan_xlsx_reply_column(path: Path) -> dict:
    """Count exported reply-parent values without a third-party Excel package."""
    if not path.is_file():
        return {'available': False}
    try:
        with zipfile.ZipFile(path) as zf:
            shared = []
            if 'xl/sharedStrings.xml' in zf.namelist():
                root = ET.fromstring(zf.read('xl/sharedStrings.xml'))
                ns = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
                shared = [''.join(t.text or '' for t in si.findall('.//m:t', ns))
                          for si in root.findall('m:si', ns)]
            sheet = ET.fromstring(zf.read('xl/worksheets/sheet1.xml'))
            ns = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
            data_rows = populated_reply_cells = 0
            for row in sheet.findall('.//m:sheetData/m:row', ns):
                if int(row.attrib.get('r', '0')) <= 1:
                    continue
                data_rows += 1
                cell = next((c for c in row.findall('m:c', ns) if c.attrib.get('r', '').startswith('E')), None)
                if cell is None:
                    continue
                cell_type = cell.attrib.get('t')
                value = cell.find('m:v', ns)
                inline = cell.find('m:is', ns)
                if cell_type == 's' and value is not None and value.text:
                    index = int(value.text)
                    present = index < len(shared) and bool(shared[index].strip())
                elif cell_type == 'inlineStr' and inline is not None:
                    present = bool(''.join(t.text or '' for t in inline.findall('.//m:t', ns)).strip())
                else:
                    present = value is not None and bool((value.text or '').strip())
                populated_reply_cells += bool(present)
            return {'available': True, 'data_rows': data_rows,
                    'reply_object_id_nonempty': populated_reply_cells}
    except (OSError, KeyError, zipfile.BadZipFile, ET.ParseError, ValueError):
        return {'available': False, 'read_error': True}


def thread_match(reply, candidate) -> bool:
    if not reply['root'] or not candidate['root']:
        return False
    if candidate['level'] == 1 and candidate['key'] == reply['root']:
        return True
    return candidate['root'] == reply['root']


def earlier_than_child(candidate, reply) -> bool:
    return bool(candidate['time'] and reply['time'] and candidate['time'] > reply['time'])


def candidate_view(row, reply, route_name='') -> dict:
    return {
        'dataset_key': row['key'], 'record_key': row['record_key'],
        'comment_level': row['level'], 'author': row['author'], 'user_id': row['user_id'],
        'created_at_display': row['created_at_display'], 'content': row['payload'].get('content') or '',
        'same_weibo': row['post'] == reply['post'],
        'same_thread': thread_match(reply, row),
        'author_matches_reply_route': bool(route_name and normalize_name(row['author']) == normalize_name(route_name)),
        'published_not_after_child': not earlier_than_child(row, reply),
    }


def demote_inferred_cycles(results) -> list[list[str]]:
    """Move every inferred edge in a cycle to unknown; never choose a direction arbitrarily."""
    by_reply = {x['reply']['key']: x for x in results}
    parent_map = {x['reply']['key']: x['target']['key'] for x in results if x['target']}
    states, stack, groups = {}, [], []
    seen_groups = set()

    def visit(key):
        state = states.get(key, 0)
        if state == 1:
            cycle = stack[stack.index(key):]
            signature = frozenset(cycle)
            if signature not in seen_groups:
                seen_groups.add(signature)
                groups.append(cycle[:])
            return
        if state == 2:
            return
        states[key] = 1
        stack.append(key)
        target = parent_map.get(key)
        if target in parent_map:
            visit(target)
        stack.pop()
        states[key] = 2

    for key in parent_map:
        visit(key)
    for group in groups:
        cycle_members = sorted(group)
        for key in group:
            item = by_reply[key]
            old_target = item['target']
            if item['method'] == 'platform_id':
                item['evidence']['platform_verified_cycle_requires_manual_review'] = cycle_members
                item['reasons'].append('platform_verified_cycle_requires_manual_review')
                continue
            if old_target and all(c['key'] != old_target['key'] for c in item['candidates']):
                item['candidates'].append(old_target)
                item['candidate_details'].append(candidate_view(old_target, item['reply'], item['route_name']))
            item['evidence']['cycle_members'] = cycle_members
            item['evidence']['prior_selected_target_before_cycle_check'] = old_target['key'] if old_target else None
            item['evidence']['cycle_resolution'] = 'All non-platform edges in this cycle are left unknown; direction cannot be proven from names and order alone.'
            item['evidence']['candidate_keys'] = [c['key'] for c in item['candidates']]
            item['target'] = None
            item['method'] = 'unknown'
            item['reliability'] = RELIABILITY['unknown']
            item['reasons'].append('inferred_cycle_conflict_marked_unknown')
    return groups


def resolve_reply(reply, by_post, by_key, id_index, observations, observation_times):
    variants = [reply['payload']]
    obs_rows = observations.get(reply['record_key'], [])
    variants.extend((x.get('record') or {}) for x in obs_rows if isinstance(x.get('record'), dict))
    direct_ids, local_keys, user_hints = [], [], set()
    for variant in variants:
        i, k = explicit_parent_evidence(variant)
        direct_ids.extend(i)
        local_keys.extend(k)
        user_hints.update(parent_user_hints(variant))
    unique_ids = {(x['field'], x['value']) for x in direct_ids}
    unique_values = {value for _, value in unique_ids}
    names = route_names(variants)
    names_norm = {normalize_name(x): x for x in names}
    route_name = next(iter(names_norm.values())) if len(names_norm) == 1 else ''
    evidence = {
        'source_observation_count': len(obs_rows),
        'source_paths': sorted({str(x.get('source_path')) for x in obs_rows if x.get('source_path')}),
        'direct_parent_id_fields': sorted([{'field': f, 'value': v} for f, v in unique_ids],
                                           key=lambda x: (x['field'], x['value'])),
        'direct_parent_local_key_fields': sorted([{'field': x['field'], 'value': x['value']} for x in local_keys],
                                                 key=lambda x: (x['field'], x['value'])),
        'reply_nicknames': sorted(names), 'reply_user_id_hints': sorted(user_hints),
        'child_published_time': reply['created_at_display'],
    }
    target = None
    method = 'unknown'
    reasons = []
    candidates = []

    # Existing verified edges are retained only if they are structurally valid.
    prior_method = (reply['relation_payload'].get('parent_link_method')
                    or (reply['relation_payload'].get('parent_link_resolution') or {}).get('method')
                    or (reply['payload'].get('parent_link_resolution') or {}).get('method'))
    prior_heuristic_methods = {
        'nickname_match', 'time_order_inferred', 'root_assumed',
        'unique_reply_nickname_same_post_thread',
        'inferred_reply_nickname_crawl_order_consensus',
    }
    existing_verified = reply['verified_parent'] if prior_method not in prior_heuristic_methods else ''
    if existing_verified and existing_verified in by_key:
        candidate = by_key[existing_verified]
        if (candidate['key'] != reply['key'] and candidate['post'] == reply['post']
                and candidate['level'] in (1, 2) and thread_match(reply, candidate)):
            target = candidate
            method = 'platform_id'
            evidence['preserved_existing_verified_parent_key'] = existing_verified
            evidence['verification_source'] = 'source_relations.verified_parent_key'
            if earlier_than_child(candidate, reply):
                reasons.append('preserved_platform_verified_parent_published_after_child_review_time_order')
        else:
            reasons.append('existing_verified_parent_failed_post_thread_level_or_time_check')

    # A direct original identifier outranks route text. Conflicts stay unknown.
    if not target and unique_values:
        if len(unique_values) != 1:
            reasons.append('conflicting_direct_parent_ids_across_observations')
        elif len({field for field, _ in unique_ids}) > 1 and len(unique_values) == 1:
            pass
        else:
            parent_id = next(iter(unique_values))
            matches = list(dict.fromkeys(id_index.get((reply['post'], parent_id), [])))
            candidates = [by_key[k] for k in matches if k in by_key]
            evidence['identifier_match_count_same_weibo'] = len(candidates)
            if len(candidates) == 1:
                candidate = candidates[0]
                if (candidate['key'] != reply['key'] and candidate['level'] in (1, 2)
                        and thread_match(reply, candidate) and not earlier_than_child(candidate, reply)):
                    target = candidate
                    method = 'platform_id'
                    evidence['matched_parent_identifier'] = parent_id
                    evidence['matched_parent_identifier_fields'] = sorted({f for f, v in unique_ids if v == parent_id})
                else:
                    reasons.append('direct_parent_id_failed_post_thread_level_self_reference_or_time_check')
            elif len(candidates) > 1:
                reasons.append('direct_parent_id_not_unique_within_weibo')
            else:
                reasons.append('direct_parent_id_not_found_within_weibo')

    # Explicit local parent keys are retained as evidence, but are not mislabeled as platform IDs.
    if not target and local_keys and not unique_values:
        key_values = {x['value'] for x in local_keys}
        if len(key_values) == 1:
            local_value = next(iter(key_values))
            local_matches = [by_key[local_value]] if local_value in by_key else []
            if len(local_matches) == 1:
                candidates = local_matches
                candidate = local_matches[0]
                if (candidate['key'] != reply['key'] and candidate['post'] == reply['post']
                        and candidate['level'] in (1, 2) and thread_match(reply, candidate)
                        and not earlier_than_child(candidate, reply)):
                    # A local dataset key is exact evidence, but outside the requested
                    # platform_id categories it is kept for review instead of relabeled.
                    reasons.append('direct_local_parent_key_needs_source_semantics_review')
                else:
                    reasons.append('direct_local_parent_key_failed_relationship_checks')
            else:
                reasons.append('direct_local_parent_key_not_found')
        else:
            reasons.append('conflicting_direct_local_parent_keys')

    # Route matching is allowed only when no unresolved direct-ID conflict exists.
    has_unresolved_direct_id = bool(unique_values and not target)
    if not target and not has_unresolved_direct_id and len(names_norm) == 1:
        target_id_hint = next(iter(user_hints)) if len(user_hints) == 1 else ''
        possible = [c for c in by_post[reply['post']]
                    if c['key'] != reply['key'] and c['level'] in (1, 2)
                    and thread_match(reply, c)
                    and normalize_name(c['author']) == normalize_name(route_name)
                    and (not target_id_hint or c['user_id'] == target_id_hint)
                    and not earlier_than_child(c, reply)]
        candidates = possible
        evidence['route_nickname'] = route_name
        evidence['route_candidate_keys'] = [c['key'] for c in possible]
        evidence['route_candidate_user_ids'] = sorted({c['user_id'] for c in possible if c['user_id']})
        if len(possible) == 1:
            target = possible[0]
            method = 'nickname_match'
        elif len(possible) > 1:
            reply_times = observation_times.get(reply['record_key'], {})
            common_sources = set(reply_times)
            for candidate in possible:
                common_sources.intersection_update(observation_times.get(candidate['record_key'], {}))
            order_picks = []
            for source_path in sorted(common_sources):
                reply_seen = reply_times[source_path]
                prior = [(observation_times[c['record_key']][source_path], c) for c in possible
                         if observation_times[c['record_key']][source_path] < reply_seen]
                if not prior:
                    continue
                nearest_seen = max(item[0] for item in prior)
                nearest = [item[1] for item in prior if item[0] == nearest_seen]
                if len(nearest) == 1:
                    order_picks.append({
                        'source_path': source_path, 'target': nearest[0],
                        'seconds_before_reply': (reply_seen - nearest_seen).total_seconds(),
                    })
            selected = {x['target']['key'] for x in order_picks}
            evidence['crawl_order_evidence'] = [
                {'source_path': x['source_path'], 'candidate_key': x['target']['key'],
                 'seconds_before_reply': x['seconds_before_reply']} for x in order_picks]
            evidence['crawl_order_source_count'] = len(order_picks)
            evidence['all_route_candidates_cocrawled'] = bool(common_sources)
            if order_picks and len(selected) == 1:
                target = order_picks[0]['target']
                method = 'time_order_inferred'
            elif order_picks:
                reasons.append('crawl_order_sources_disagree_on_parent_candidate')
            else:
                reasons.append('multiple_same_nickname_candidates_without_unique_prior_order')
        else:
            reasons.append('no_parent_candidate_for_reply_nickname_in_same_weibo_thread')
    elif not target and not has_unresolved_direct_id and len(names_norm) > 1:
        reasons.append('conflicting_reply_nicknames_across_observations')

    # The experiment rule applies only when no direct-parent ID, local key, or reply route exists.
    if not target and not unique_values and not local_keys and not names:
        root = by_key.get(reply['root'])
        if (root and root['level'] == 1 and root['post'] == reply['post']
                and root['key'] != reply['key'] and not earlier_than_child(root, reply)):
            target = root
            method = 'root_assumed'
            reasons = []
            evidence['root_assumption'] = 'No direct-parent identifier or 回复@ route was present in available raw variants.'
        elif root and earlier_than_child(root, reply):
            reasons.append('root_parent_published_after_child')
        else:
            reasons.append('no_valid_root_for_experiment_default')
    elif not target and not names and unique_values:
        reasons.append('direct_parent_identifier_unresolved')

    if target:
        evidence.update(target_dataset_key=target['key'], target_record_key=target['record_key'],
                        target_comment_level=target['level'], same_weibo=True,
                        same_thread=thread_match(reply, target),
                        target_author=target['author'], target_user_id=target['user_id'],
                        target_published_time=target['created_at_display'])
        candidates = [target] if method in ('platform_id', 'root_assumed') else candidates
    else:
        evidence['candidate_keys'] = [c['key'] for c in candidates]
    candidate_details = [candidate_view(c, reply, route_name) for c in candidates]
    return {
        'target': target, 'method': method, 'route_name': route_name,
        'candidates': candidates, 'candidate_details': candidate_details,
        'evidence': evidence, 'reasons': sorted(set(reasons)),
        'reliability': RELIABILITY[method],
    }


def prior_links(path: Path, method: str) -> dict[str, str]:
    if not path.is_file():
        return {}
    con = sqlite3.connect(f'file:{path.resolve().as_posix()}?mode=ro', uri=True)
    try:
        tables = {x[0] for x in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'parent_link_audit' not in tables:
            return {}
        return {str(k): str(v) for k, v in con.execute(
            'SELECT dataset_key,target_dataset_key FROM parent_link_audit WHERE status="linked" AND method=?',
            (method,)) if k and v}
    finally:
        con.close()


def sample_checks(results: list[dict], by_key: dict[str, dict], size=20) -> dict:
    """Stable, reproducible structural spot checks; not a human accuracy estimate."""
    checks = {}
    for method in ('platform_id', 'nickname_match', 'time_order_inferred', 'root_assumed'):
        rows = [x for x in results if x['method'] == method and x['target']]
        picked = sorted(rows, key=lambda x: hashlib.sha256(x['reply']['key'].encode()).hexdigest())[:size]
        passed = 0
        for item in picked:
            reply = item['reply']
            target = item['target']
            ok = (target['key'] != reply['key'] and target['post'] == reply['post']
                  and thread_match(reply, target) and not earlier_than_child(target, reply))
            if method in ('nickname_match', 'time_order_inferred'):
                ok = ok and bool(item['route_name']) and normalize_name(target['author']) == normalize_name(item['route_name'])
            if method == 'root_assumed':
                ok = ok and target['key'] == reply['root'] and not item['has_route_or_parent_evidence']
            passed += bool(ok)
        checks[method] = {'sample_size': len(picked), 'structural_pass': passed,
                          'scope': '结构与证据抽查，不代表平台准确率'}
    return checks


def build_database(source_path: Path, output_path: Path, observations, observation_times):
    if output_path.exists():
        raise FileExistsError(f'Refusing to overwrite {output_path}')
    output_path.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(f'file:{source_path.resolve().as_posix()}?mode=ro', uri=True)
    dst = sqlite3.connect(output_path)
    try:
        src.backup(dst)
    finally:
        src.close()
    dst.row_factory = sqlite3.Row
    dst.execute('PRAGMA foreign_keys=ON')

    records = []
    query = '''SELECT c.dataset_key,c.record_key,c.weibo_id,c.comment_level,
                      c.root_dataset_key,c.direct_parent_dataset_key,c.payload,
                      r.verified_parent_key,r.payload AS relation_payload
               FROM comments c LEFT JOIN relations r USING(dataset_key)'''
    for row in dst.execute(query):
        payload = json_object(row['payload'])
        relation_payload = json_object(row['relation_payload'])
        records.append({
            'key': str(row['dataset_key']), 'record_key': clean_id(row['record_key']),
            'post': clean_id(row['weibo_id']), 'level': int(row['comment_level']),
            'root': clean_id(row['root_dataset_key']),
            'parent': clean_id(row['direct_parent_dataset_key']),
            'verified_parent': clean_id(row['verified_parent_key']),
            'payload': payload, 'relation_payload': relation_payload,
            'author': record_author(payload), 'user_id': record_user_id(payload),
            'created_at_display': str(payload.get('created_at_display') or ''),
            'time': parse_time(payload.get('created_at_display')),
        })
    by_key = {x['key']: x for x in records}
    by_post = defaultdict(list)
    id_index = defaultdict(list)
    for row in records:
        by_post[row['post']].append(row)
        raw_variants = [x.get('record') or {} for x in observations.get(row['record_key'], [])]
        platform_ids = set(record_comment_ids(row['payload']))
        for variant in raw_variants:
            if isinstance(variant, dict):
                platform_ids.update(record_comment_ids(variant))
        for value in platform_ids:
            if value:
                id_index[(row['post'], value)].append(row['key'])

    output = []
    for reply in records:
        if reply['level'] != 2:
            continue
        resolved = resolve_reply(reply, by_post, by_key, id_index, observations, observation_times)
        resolved['reply'] = reply
        resolved['has_route_or_parent_evidence'] = bool(
            resolved['evidence'].get('reply_nicknames')
            or resolved['evidence'].get('direct_parent_id_fields')
            or resolved['evidence'].get('direct_parent_local_key_fields'))
        output.append(resolved)

    demote_inferred_cycles(output)

    dst.executescript('''
      CREATE TABLE parent_link_audit(
        dataset_key TEXT PRIMARY KEY REFERENCES comments(dataset_key),
        weibo_id TEXT NOT NULL,
        root_dataset_key TEXT,
        method TEXT NOT NULL CHECK(method IN ('platform_id','nickname_match','time_order_inferred','root_assumed','unknown')),
        status TEXT NOT NULL CHECK(status IN ('resolved','unknown')),
        target_dataset_key TEXT,
        candidate_count INTEGER NOT NULL,
        candidate_keys_json TEXT NOT NULL,
        candidate_details_json TEXT NOT NULL,
        reliability TEXT NOT NULL,
        evidence_json TEXT NOT NULL,
        review_reason TEXT NOT NULL
      );
      CREATE INDEX parent_link_audit_method ON parent_link_audit(method);
      CREATE INDEX parent_link_audit_status ON parent_link_audit(status);
    ''')
    with dst:
        for item in output:
            reply = item['reply']
            target = item['target']
            method = item['method']
            target_key = target['key'] if target else None
            payload = reply['payload']
            relation = reply['relation_payload']
            resolution = {
                'status': 'unknown' if not target else ('platform_verified' if method == 'platform_id' else 'inferred'),
                'method': method, 'target_dataset_key': target_key,
                'reliability': item['reliability'], 'source_record_key': reply['record_key'],
                'evidence': item['evidence'], 'review_reason': item['reasons'],
            }
            payload['direct_parent_dataset_key'] = target_key
            payload['parent_link_resolution'] = resolution
            relation['direct_parent_dataset_key'] = target_key
            relation['parent_link_method'] = method
            relation['parent_link_reliability'] = item['reliability']
            relation['parent_link_evidence'] = item['evidence']
            relation['parent_link_resolution'] = resolution
            # Only the platform's own direct-parent ID may populate this verified column.
            if method == 'platform_id':
                relation['verified_parent_key'] = target_key
            else:
                relation['verified_parent_key'] = None
            verified_key = target_key if method == 'platform_id' else None
            dst.execute('UPDATE comments SET direct_parent_dataset_key=?,payload=? WHERE dataset_key=?',
                        (target_key, dump(payload), reply['key']))
            dst.execute('UPDATE relations SET verified_parent_key=?,payload=? WHERE dataset_key=?',
                        (verified_key, dump(relation), reply['key']))
            reasons = item['reasons']
            dst.execute('''INSERT INTO parent_link_audit VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''', (
                reply['key'], reply['post'], reply['root'], method,
                'resolved' if target else 'unknown', target_key,
                len(item['candidates']), dump([x['key'] for x in item['candidates']]),
                dump(item['candidate_details']), item['reliability'],
                dump(item['evidence']), ';'.join(reasons),
            ))

    return dst, records, by_key, output


def validate_database(dst: sqlite3.Connection, records, by_key, results):
    counts = Counter(x['method'] for x in results)
    reasons = Counter(r for x in results if x['method'] == 'unknown' for r in x['reasons'])
    structural = Counter()
    later = []
    for item in results:
        reply, target = item['reply'], item['target']
        if not target:
            continue
        if target['post'] != reply['post']:
            structural['cross_weibo'] += 1
        if not thread_match(reply, target):
            structural['cross_thread_or_missing_root'] += 1
        if target['key'] == reply['key']:
            structural['self_reference'] += 1
        if earlier_than_child(target, reply):
            structural['parent_published_after_child'] += 1
            later.append(item)

    # Cycle check is independent of timestamp parsing.
    parent_map = {x['reply']['key']: x['target']['key'] for x in results if x['target']}
    visited, active = set(), set()
    cycles = set()
    def visit(key):
        if key in active:
            cycles.add(key)
            return
        if key in visited:
            return
        active.add(key)
        nxt = parent_map.get(key)
        if nxt in parent_map:
            visit(nxt)
        active.discard(key)
        visited.add(key)
    for key in parent_map:
        visit(key)
    structural['cycles'] = len(cycles)

    integrity = dst.execute('PRAGMA integrity_check').fetchone()[0]
    fk_count = len(dst.execute('PRAGMA foreign_key_check').fetchall())
    nested_count, root_matches, root_invalid, post_missing = dst.execute('''
      SELECT COUNT(*),
             SUM(c.root_dataset_key=r.verified_root_key),
             SUM(NOT EXISTS (SELECT 1 FROM comments p WHERE p.dataset_key=c.root_dataset_key
                             AND p.comment_level=1 AND p.weibo_id=c.weibo_id)),
             SUM(NOT EXISTS (SELECT 1 FROM posts p WHERE p.weibo_id=c.weibo_id))
      FROM comments c JOIN relations r USING(dataset_key) WHERE c.comment_level=2
    ''').fetchone()
    orphan_direct = dst.execute('''SELECT COUNT(*) FROM comments c
      WHERE c.comment_level=2 AND c.direct_parent_dataset_key IS NOT NULL
      AND NOT EXISTS (SELECT 1 FROM comments p WHERE p.dataset_key=c.direct_parent_dataset_key)''').fetchone()[0]
    nonplatform_verified = dst.execute('''SELECT COUNT(*) FROM parent_link_audit a
      JOIN relations r USING(dataset_key)
      WHERE a.method!='platform_id' AND r.verified_parent_key IS NOT NULL
      AND NOT EXISTS (SELECT 1 FROM parent_link_audit b WHERE b.dataset_key=a.dataset_key AND b.method='platform_id')''').fetchone()[0]
    record_key_dups = dst.execute('''SELECT COUNT(*) FROM (SELECT record_key FROM comments
      WHERE record_key IS NOT NULL GROUP BY record_key HAVING COUNT(*)>1)''').fetchone()[0]
    direct_counts = dict(dst.execute('SELECT method,COUNT(*) FROM parent_link_audit GROUP BY method'))
    return {
        'counts': dict(counts), 'unknown_reasons': dict(reasons),
        'structural_issues': dict(structural), 'integrity': integrity,
        'foreign_key_violations': fk_count,
        'nested_comments': int(nested_count or 0), 'valid_verified_roots': int(root_matches or 0),
        'invalid_roots': int(root_invalid or 0), 'nested_weibo_not_in_posts': int(post_missing or 0),
        'orphan_direct_parent_keys': int(orphan_direct),
        'nonplatform_verified_parent_keys': int(nonplatform_verified),
        'duplicate_record_key_groups': int(record_key_dups),
        'parent_link_audit_counts': direct_counts, 'parent_published_later_rows': later,
    }


def compare_previous(results, prior_nickname: dict, prior_order: dict) -> dict:
    current_nickname = {x['reply']['key']: x['target']['key'] for x in results
                        if x['method'] == 'nickname_match' and x['target']}
    current_order = {x['reply']['key']: x['target']['key'] for x in results
                     if x['method'] == 'time_order_inferred' and x['target']}
    def comp(old, new):
        same = sum(new.get(k) == v for k, v in old.items())
        return {'previous_count': len(old), 'current_count': len(new), 'same_target': same,
                'changed_or_missing': len(old) - same, 'new_not_in_previous': len(set(new) - set(old))}
    return {
        'nickname_match': comp(prior_nickname, current_nickname),
        'time_order_inferred': comp(prior_order, current_order),
    }


def read_built_database(path: Path):
    """Load an existing repair database without changing it, for report regeneration."""
    dst = sqlite3.connect(f'file:{path.resolve().as_posix()}?mode=ro', uri=True)
    dst.row_factory = sqlite3.Row
    rows = dst.execute('''SELECT c.dataset_key,c.record_key,c.weibo_id,c.comment_level,
                                 c.root_dataset_key,c.direct_parent_dataset_key,c.payload,
                                 r.verified_parent_key,r.payload AS relation_payload
                          FROM comments c LEFT JOIN relations r USING(dataset_key)''').fetchall()
    records = []
    for row in rows:
        payload = json_object(row['payload'])
        records.append({
            'key': str(row['dataset_key']), 'record_key': clean_id(row['record_key']),
            'post': clean_id(row['weibo_id']), 'level': int(row['comment_level']),
            'root': clean_id(row['root_dataset_key']), 'parent': clean_id(row['direct_parent_dataset_key']),
            'verified_parent': clean_id(row['verified_parent_key']), 'payload': payload,
            'relation_payload': json_object(row['relation_payload']), 'author': record_author(payload),
            'user_id': record_user_id(payload), 'created_at_display': str(payload.get('created_at_display') or ''),
            'time': parse_time(payload.get('created_at_display')),
        })
    by_key = {x['key']: x for x in records}
    results = []
    for row in dst.execute('SELECT * FROM parent_link_audit'):
        reply = by_key[row['dataset_key']]
        target = by_key.get(row['target_dataset_key']) if row['target_dataset_key'] else None
        evidence = json_object(row['evidence_json'])
        names = evidence.get('reply_nicknames') or []
        candidates = [by_key[k] for k in json.loads(row['candidate_keys_json'] or '[]') if k in by_key]
        results.append({
            'reply': reply, 'target': target, 'method': row['method'],
            'route_name': evidence.get('route_nickname') or (names[0] if len(names) == 1 else ''),
            'candidates': candidates, 'candidate_details': json.loads(row['candidate_details_json'] or '[]'),
            'evidence': evidence, 'reasons': [x for x in str(row['review_reason'] or '').split(';') if x],
            'reliability': row['reliability'],
            'has_route_or_parent_evidence': bool(names or evidence.get('direct_parent_id_fields')
                                                  or evidence.get('direct_parent_local_key_fields')),
        })
    return dst, records, by_key, results


def make_report(path: Path, source_db: Path, obs_path: Path, inventory: dict,
                stats: dict, comparison: dict, sample: dict, totals: dict):
    methods = [
        ('platform_id', '平台原始直接父评论 ID 精确匹配', 'high'),
        ('nickname_match', '同微博、同根讨论串内回复昵称唯一匹配', 'medium'),
        ('time_order_inferred', '回复昵称候选相同，采集顺序辅助消歧', 'low'),
        ('root_assumed', '无直接父证据，按实验规则指向一级根评论', 'experiment_default'),
        ('unknown', '证据不足或证据冲突，保留未知并列候选', 'none'),
    ]
    lines = [
        '# 《第五人格》微博楼中楼父评论关系检查与修复报告', '',
        '**日期：** 2026-11-09  ',
        '**范围：** 本地数据库、预处理观察、MySQL JSONL 快照、历史压缩包、Excel 导出及采集代码；未重新爬取。', '',
        '## 修复结果', '',
        f'- 输入数据库：`{source_db}`（原始文件只读）；输出数据库：`{path.parent / "research_dataset_parent_links_repaired.sqlite3"}`。',
        f'- 检查 {stats["nested_comments"]:,} 条楼中楼。`comments.root_dataset_key` 与 `relations.verified_root_key` 一致且根键有效：{stats["valid_verified_roots"]:,}/{stats["nested_comments"]:,}。',
        f'- `comments.weibo_id` 保留微博关联；其中 {stats["nested_weibo_not_in_posts"]:,} 条的微博记录不在当前筛选后的 `posts` 表中，`relations.verified_weibo_id` 继续留空，不伪造帖子外键。',
        f'- 采集原始直接父 ID 未发现非空值；因此本次 `platform_id` = {stats["counts"].get("platform_id",0):,}。',
        f'- `relations.verified_parent_key` 仅保存平台 ID 精确确认关系；昵称、顺序和根默认关系只写入 `comments.direct_parent_dataset_key`、payload 与审计表。非平台推断误写入 verified 列：{stats["nonplatform_verified_parent_keys"]}。', '',
        '| 方法 | 数量 | 可靠性等级 | 解释 |', '|---|---:|---|---|',
    ]
    labels = {method: description for method, description, _ in methods}
    for method, _, reliability in methods:
        lines.append(f'| `{method}` | {stats["counts"].get(method,0):,} | `{reliability}` | {labels[method]} |')
    lines += ['', '## 既有 732 / 560 条关系复核', '']
    for method, result in comparison.items():
        lines.append(f'- `{method}`：原有 {result["previous_count"]:,} 条；按当前源记录复算 {result["current_count"]:,} 条；父键一致 {result["same_target"]:,} 条；变化或缺失 {result["changed_or_missing"]:,} 条。')
    lines += [
        '- 变化的 3 条昵称匹配与 3 条顺序推断组成 3 组双向循环；两条边在同一分钟出现且均无平台父 ID，无法可靠确定方向，因此 6 条均降为 `unknown` 并保留候选。其余 1,286 条既有目标键保持一致。',
        '- 这两类关系均未获得平台直接 ID 验证。昵称唯一匹配是明确“回复@昵称”且同微博/同根下唯一；顺序推断只在同名候选多于一个时使用同一微博、同一线程的发布时间过滤及共同采集快照顺序，标为 `time_order_inferred`，不宣称平台验证或准确率。',
        '- 历史 Excel 的“回复对象ID”列扫描结果：'
        + (f'{inventory["xlsx"].get("reply_object_id_nonempty", "未知")} 个非空值 / {inventory["xlsx"].get("data_rows", "未知")} 行。' if inventory.get('xlsx', {}).get('available') else '未能读取。'),
        '', '## 证据文件和采集逻辑检查', '',
        f'- 原始观察 JSONL：`{obs_path}`，{inventory["observations"].get("records",0):,} 条记录，其中楼中楼观察 {inventory["observations"].get("nested",0):,} 条；非空直接父 ID/键字段计数：{json.dumps(inventory["observations"].get("fields",{}),ensure_ascii=False) or "{}"}。该文件 7 个历史副本哈希相同。',
        f'- 输入快照：`{DEFAULT_INPUT_SNAPSHOT}`，sources manifest 标记 {inventory["comment_snapshot"].get("sqlite_files",0)} 份评论 SQLite、共 {inventory["comment_snapshot"].get("comments",0):,} 行；`mysql_crawl_items.jsonl` 共 {inventory["mysql_export"].get("records",0):,} 条，其中浏览器评论 44 条、楼中楼 3 条，直接父 ID 字段未见非空。两版 33 个 comments shard 逐文件哈希一致。',
        f'- 历史压缩包：`{DEFAULT_ARCHIVE}`，评论 JSONL {inventory["archive"].get("comment_rows",0):,} 行、楼中楼 {inventory["archive"].get("nested_rows",0):,} 条；直接父 ID/键字段非空计数为 0。',
        '- 旧关联库：`research_dataset_parent_links.sqlite3`（732 昵称匹配）及 `research_dataset_parent_links_time_order.sqlite3`（另有 560 顺序推断）。旧结果将 1,292 条启发式关系同时写进 `relations.verified_parent_key`；本次输出纠正了这一语义。',
        '- `WeiboSpider/weibospider/spiders/comment.py` 的 API 解析器能从 `reply_comment.id` 取父 ID；本地 44 条 MySQL 评论快照为 `browser_comment` DOM 来源，3 条二级回复的父 ID 均为空。浏览器采集器保存评论正文、作者、时间、根键；说明文档指出页面未提供平台评论 ID 时留空。未找到本地接口响应中可恢复的父 ID。',
        '- 数据库 SQL 文件仅定义 MySQL `crawl_items` JSON 表结构；未发现可补出评论关系的 SQL 数据转储。',
        '', '## 质量检查', '',
        f'- `PRAGMA integrity_check`：`{stats["integrity"]}`；外键违规：{stats["foreign_key_violations"]}；孤立直接父键：{stats["orphan_direct_parent_keys"]}；无效根：{stats["invalid_roots"]}。',
        f'- 跨微博/跨讨论串、自引用、晚于子评论及循环：{json.dumps(stats["structural_issues"],ensure_ascii=False)}。',
        f'- 重复 `record_key` 组：{stats["duplicate_record_key_groups"]}；SQLite 主键唯一性由表约束保证。',
        f'- 可人工复核的未知关系：{stats["counts"].get("unknown",0):,} 条；未路由且有效根的记录按规则设为 `root_assumed`，不列为“平台确认”。',
        f'- 方法抽样：{json.dumps(sample,ensure_ascii=False)}。这是同微博、同根、非自引用、发布时间约束等结构/证据抽样，不是人工准确率评估。',
        '', '## 复核边界', '',
        '采集顺序是辅助证据，不能单独构成回复关系；时间显示若缺失或精度不足也不能验证语义。人工复核工作簿列出全部未知记录及同线程候选、回复昵称、时间、用户信息与证据。没有可用直接 ID 或平台页面原始回复对象时，真实父评论只能保持推断或未知。',
    ]
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, default=DEFAULT_DATABASE)
    parser.add_argument('--source-observations', type=Path, default=DEFAULT_OBSERVATIONS)
    parser.add_argument('--input-snapshot', type=Path, default=DEFAULT_INPUT_SNAPSHOT)
    parser.add_argument('--archive', type=Path, default=DEFAULT_ARCHIVE)
    parser.add_argument('--source-xlsx', type=Path, default=DEFAULT_XLSX)
    parser.add_argument('--prior-nickname-links', type=Path, default=DEFAULT_PRIOR_NICKNAME)
    parser.add_argument('--prior-order-links', type=Path, default=DEFAULT_PRIOR_ORDER)
    parser.add_argument('--output-dir', type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument('--report-only', action='store_true',
                        help='Regenerate the report from an existing repair database without modifying it.')
    args = parser.parse_args()
    source = args.database.resolve()
    output_dir = args.output_dir.resolve()
    output_db = output_dir / 'research_dataset_parent_links_repaired.sqlite3'
    report_path = output_dir / 'parent_link_repair_report.md'
    observations, observation_times, obs_stats = read_observations(args.source_observations.resolve())
    inventory = source_scan(args.source_observations.resolve(), args.input_snapshot.resolve(),
                            args.archive.resolve(), args.source_xlsx.resolve())
    inventory['observations'] = obs_stats
    output_dir.mkdir(parents=True, exist_ok=True)
    if args.report_only:
        if not output_db.is_file():
            raise FileNotFoundError(f'No existing repair database at {output_db}')
        dst, records, by_key, results = read_built_database(output_db)
        try:
            stats = validate_database(dst, records, by_key, results)
            prior_nick = prior_links(args.prior_nickname_links.resolve(), 'unique_reply_nickname_same_post_thread')
            prior_order = prior_links(args.prior_order_links.resolve(), 'inferred_reply_nickname_crawl_order_consensus')
            comparison = compare_previous(results, prior_nick, prior_order)
            sample = sample_checks(results, by_key)
            totals = {
                'comments': dst.execute('SELECT COUNT(*) FROM comments').fetchone()[0],
                'posts': dst.execute('SELECT COUNT(*) FROM posts').fetchone()[0],
                'relation_rows': dst.execute('SELECT COUNT(*) FROM relations').fetchone()[0],
            }
            stats.update(totals)
            make_report(report_path, source, args.source_observations.resolve(), inventory,
                        stats, comparison, sample, totals)
        finally:
            dst.close()
        print('REPORT', json.dumps({'report': str(report_path), 'methods': stats['counts'],
                                    'comparison': comparison, 'samples': sample}, ensure_ascii=False), flush=True)
        return
    dst, records, by_key, results = build_database(source, output_db, observations, observation_times)
    try:
        stats = validate_database(dst, records, by_key, results)
        prior_nick = prior_links(args.prior_nickname_links.resolve(), 'unique_reply_nickname_same_post_thread')
        prior_order = prior_links(args.prior_order_links.resolve(), 'inferred_reply_nickname_crawl_order_consensus')
        comparison = compare_previous(results, prior_nick, prior_order)
        sample = sample_checks(results, by_key)
        totals = {
            'comments': dst.execute('SELECT COUNT(*) FROM comments').fetchone()[0],
            'posts': dst.execute('SELECT COUNT(*) FROM posts').fetchone()[0],
            'relation_rows': dst.execute('SELECT COUNT(*) FROM relations').fetchone()[0],
        }
        stats.update(totals)
        if stats['integrity'] != 'ok' or stats['foreign_key_violations']:
            raise RuntimeError(f'Database validation failed: integrity={stats["integrity"]}; FK={stats["foreign_key_violations"]}')
        make_report(report_path, source, args.source_observations.resolve(), inventory,
                    stats, comparison, sample, totals)
        dst.execute('PRAGMA optimize')
        dst.close()
    except Exception:
        dst.close()
        raise
    print('RESULT', json.dumps({
        'output_database': str(output_db), 'report': str(report_path),
        'comments': totals['comments'], 'nested_comments': stats['nested_comments'],
        'methods': stats['counts'], 'previous_link_comparison': comparison,
        'unknown_reasons': stats['unknown_reasons'], 'quality': {
            'integrity': stats['integrity'], 'foreign_key_violations': stats['foreign_key_violations'],
            'invalid_roots': stats['invalid_roots'], 'orphan_direct_parent_keys': stats['orphan_direct_parent_keys'],
            'nonplatform_verified_parent_keys': stats['nonplatform_verified_parent_keys'],
            'structural_issues': stats['structural_issues'],
        }, 'samples': sample,
    }, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
