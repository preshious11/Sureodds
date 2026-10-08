"""Temporary SportyBet shape probe. No provider I/O, persisted payload or membership rule."""
from collections import Counter
from predbot import ticket_audit_switch
from itertools import islice
import hashlib
import json
import logging
import re

log = logging.getLogger(__name__)
MAX_NODES = 2048
MAX_DEPTH = 10
MAX_ITEMS = 128
MAX_FIELDS = 64
MAX_IDS = 1024
MAX_STATES = 256
MAX_UNKNOWN_VALUES = 1024
MAX_BYTES = 96_000

# Provider-controlled key names can themselves contain private data. Unknown
# keys therefore receive aliases too; only reviewed schema vocabulary is named.
KNOWN_KEYS = frozenset('outcomes markets ticket bets selections unavailableOutcomes '
    'event market outcome selection bet references reference refs ref ids id '
    'sport category tournament name desc description label homeTeamName awayTeamName '
    'participants competition odds price prices stake totalStake potentialWinnings '
    'displayTotalOdds displayBonus shareCode bookingCode code userId url '
    'estimateStartTime kickoff deadline specifier specifiers'.split())
ID_FIELD = re.compile(r'^(?:(?:event|market|outcome|selection|bet|match)(?:_?(?:id|ref|reference))s?|ids?|refs?|references?)$', re.I)
STATE_FIELD = re.compile(r'^(?:is_?)?(?:(?:event|market|outcome|selection|bet)_?)?'
                         r'(?:selected|active|unavailable|available|status|valid|checked|suspended|removed)$', re.I)
SAFE_STATES = frozenset('true false active inactive available unavailable selected unselected '
                       'valid invalid checked unchecked suspended removed open closed pending settled void'.split())


def _value_fingerprint(value):
    """Exact bounded equality token, kept in RAM only; never expose the digest.

    Reject incomplete fingerprints instead of grouping truncated values together.
    Canonical dictionary ordering preserves equality across key order differences.
    """
    digest = hashlib.sha256()
    nodes = 0

    def visit(item, depth=0):
        nonlocal nodes
        nodes += 1
        if nodes > 128 or depth > 6:
            raise ValueError
        digest.update(type(item).__name__.encode() + b':')
        if type(item) is dict:
            if len(item) > 64 or any(type(k) is not str for k in item):
                raise ValueError
            for key in sorted(item):
                visit(key, depth + 1)
                visit(item[key], depth + 1)
        elif type(item) is list:
            if len(item) > 128:
                raise ValueError
            for child in item:
                visit(child, depth + 1)
        elif type(item) in (str, int, float, bool, type(None)):
            if type(item) is str and len(item) > 256:
                raise ValueError
            raw = json.dumps(item, allow_nan=False).encode()
            if len(raw) > 1024:
                raise ValueError
            digest.update(raw)
        else:
            raise ValueError
        digest.update(b';')

    try:
        visit(value)
        return digest.digest()
    except (ValueError, TypeError, OverflowError, RecursionError):
        return None


def analyze_selection_structure(report):
    """Analyze an existing anonymized map; never alter or deduplicate a ticket."""
    rows = [e for e in report.get('entries', [])
            if re.fullmatch(r'data\.ticket\.selections\[\d+\]', e.get('path', ''))]
    refs = []
    for row in rows:
        ids = {k.replace('_', '').casefold(): v for k, v in row.get('identifiers', {}).items()}
        triple = [ids.get(k, {}).get('alias') for k in ('eventid', 'marketid', 'outcomeid')]
        spec = ids.get('specifier', {})
        if all(triple) and spec.get('type') in (None, 'string', 'null'):
            refs.append((triple[0], triple[1], spec.get('alias'), triple[2]))
    events, tuples = Counter(r[0] for r in refs), Counter(refs)
    groups = []
    for event, count in events.items():
        legs = [r for r in refs if r[0] == event]
        groups.append({'event_alias': event, 'selection_count': count,
                       'market_count': len({r[1] for r in legs}),
                       'market_specifier_count': len({r[1:3] for r in legs}),
                       'outcome_count': len({r[3] for r in legs}),
                       'unique_tuple_count': len(set(legs))})
    declared = next((c['length'] for c in report.get('collections', [])
                     if c['path'] == 'data.ticket.selections'), None)
    return {'complete': not report.get('truncated', False) and declared == len(rows) == len(refs),
            'reference_rows_analyzed': len(refs), 'unique_event_count': len(events),
            'duplicate_event_entries': sum(n - 1 for n in events.values()),
            'repeated_event_count': sum(n > 1 for n in events.values()),
            'unique_complete_tuple_count': len(tuples),
            'duplicate_complete_tuple_entries': sum(n - 1 for n in tuples.values()),
            'repeated_complete_tuple_count': sum(n > 1 for n in tuples.values()),
            'events_with_different_markets': sum(g['market_count'] > 1 for g in groups),
            'events_with_different_outcomes': sum(g['outcome_count'] > 1 for g in groups),
            'event_groups': groups}


def structural_report(body):
    """Counts, field types and request-local equality aliases, never leaf text.

    Equal identifier values share a neutral id_NNN alias, even across contexts.
    Equality is diagnostic evidence only: market/outcome ids can be locally
    scoped, and sharing an alias does NOT establish actual ticket membership.
    """
    aliases, unknown_fields, id_info, opaque_states, unknown_values = {}, {}, {}, {}, {}
    shapes, collections, entries = {}, [], []
    reasons = set()
    nodes = 0

    def typename(value):
        return {dict: 'object', list: 'array', str: 'string', int: 'integer',
                float: 'number', bool: 'boolean', type(None): 'null'}.get(type(value), 'other')

    def field_name(key):
        if isinstance(key, str) and (key in KNOWN_KEYS or ID_FIELD.fullmatch(key) or STATE_FIELD.fullmatch(key)):
            return key
        # Unknown key text never enters the report, including dictionary keys
        # that an unexpected provider schema might use to hold a code/name.
        if key not in unknown_fields:
            unknown_fields[key] = f'field_{len(unknown_fields) + 1:03d}'
        return unknown_fields[key]

    def identifier(value, collection):
        if type(value) not in (str, int) or len(str(value)) > 256 or value == '':
            return {'type': typename(value), 'alias': None}
        # Preserve field types in entries, compare canonical textual identifiers.
        key = str(value)
        if key not in aliases:
            if len(aliases) >= MAX_IDS:
                reasons.add('identifier_limit')
                return {'type': typename(value), 'alias': None}
            alias = f'id_{len(aliases) + 1:03d}'
            aliases[key] = alias
            id_info[alias] = {'alias': alias, 'occurrences': 0, 'collections': []}
        alias = aliases[key]
        info = id_info[alias]
        info['occurrences'] += 1
        if collection not in info['collections']:
            if len(info['collections']) < 16:
                info['collections'].append(collection)
            else:
                reasons.add('reference_collection_limit')
        return {'type': typename(value), 'alias': alias}

    def state(value):
        if type(value) is bool or value is None:
            return json.dumps(value)
        if type(value) is int and -32 <= value <= 32:
            return str(value)
        if type(value) is str and value.casefold() in SAFE_STATES:
            return value.casefold()
        category = 'redacted_' + typename(value)
        if type(value) in (str, int, float) and len(str(value)) <= 256:
            key = (type(value), value)
            if key not in opaque_states:
                if len(opaque_states) >= MAX_STATES:
                    reasons.add('state_value_limit')
                    return category
                opaque_states[key] = f'state_{len(opaque_states) + 1:03d}'
            return category + ':' + opaque_states[key]
        return category

    def unknown_value(value):
        fingerprint = _value_fingerprint(value)
        if fingerprint is None:
            reasons.add('unknown_value_unexamined')
            return None
        if fingerprint not in unknown_values:
            if len(unknown_values) >= MAX_UNKNOWN_VALUES:
                reasons.add('unknown_value_limit')
                return None
            unknown_values[fingerprint] = f'value_{len(unknown_values) + 1:03d}'
        return unknown_values[fingerprint]

    def walk(value, path, shape_path, depth):
        nonlocal nodes
        if nodes >= MAX_NODES or depth > MAX_DEPTH:
            reasons.add('node_limit' if nodes >= MAX_NODES else 'depth_limit')
            return
        nodes += 1
        shape = shapes.setdefault(shape_path, {'types': Counter(), 'fields': {}, 'states': {}})
        shape['types'][typename(value)] += 1
        if isinstance(value, list):
            collection = {'path': path, 'type': 'array', 'length': len(value), 'inspected': 0}
            collections.append(collection)
            if len(value) > MAX_ITEMS:
                reasons.add('collection_limit')
            for i, item in enumerate(value[:MAX_ITEMS]):
                if nodes >= MAX_NODES:
                    reasons.add('node_limit')
                    break
                before = nodes
                walk(item, f'{path}[{i}]', shape_path + '[*]', depth + 1)
                collection['inspected'] += nodes > before
        elif isinstance(value, dict):
            entry = {'path': path, 'keys': [], 'identifiers': {}, 'states': {}, 'collections': {}}
            if len(value) > MAX_FIELDS:
                reasons.add('field_limit')
            for key, item in islice(value.items(), MAX_FIELDS):
                field = field_name(key)
                entry['keys'].append(field)
                types = shape['fields'].setdefault(field, Counter())
                types[typename(item)] += 1
                is_id = isinstance(key, str) and (ID_FIELD.fullmatch(key) or key in ('specifier', 'specifiers'))
                is_state = isinstance(key, str) and STATE_FIELD.fullmatch(key)
                if is_id and not isinstance(item, (dict, list)):
                    entry['identifiers'][field] = identifier(item, shape_path)
                if is_state:
                    category = state(item)
                    entry['states'][field] = category
                    shape['states'].setdefault(field, Counter())[category] += 1
                if field.startswith('field_') and shape_path in ('data.outcomes[*]', 'data.ticket.selections[*]'):
                    value_alias = unknown_value(item)
                    entry.setdefault('unknown_values', {})[field] = {
                        'type': typename(item), 'alias': value_alias}
                    group = shape.setdefault('unknown_values', {}).setdefault(field, {
                        'unique_values': 0, 'groups': Counter(), 'unexamined': 0})
                    if value_alias is None:
                        group['unexamined'] += 1
                    else:
                        group['groups'][value_alias] += 1
                        group['unique_values'] = len(group['groups'])
                    if type(item) is bool:
                        entry['states'][field] = json.dumps(item)
                        shape['states'].setdefault(field, Counter())[json.dumps(item)] += 1
                if isinstance(item, list):
                    entry['collections'][field] = len(item)
                if isinstance(item, (dict, list)):
                    # Never descend into text/financial/private fields, even if
                    # malformed input represents them as objects instead of text.
                    if key in KNOWN_KEYS and key not in (
                            'outcomes', 'markets', 'ticket', 'bets', 'selections', 'unavailableOutcomes',
                            'event', 'market', 'outcome', 'selection', 'bet', 'references', 'reference',
                            'refs', 'ref', 'ids', 'id', 'specifier', 'specifiers'):
                        continue
                    walk(item, path + '.' + field, shape_path + '.' + field, depth + 1)
            entries.append(entry)
        elif shape_path.endswith('[*]') and type(value) in (str, int):
            # Scalar reference arrays are possible too. Alias equality only;
            # these entries are not declared to be selected betting legs.
            entries.append({'path': path, 'identifiers': {'value': identifier(value, shape_path)}})

    # Fixed roots only: never inspect share/account credentials or unrelated data.
    body = body if isinstance(body, dict) else {}
    ticket = body.get('ticket')
    ticket = ticket if isinstance(ticket, dict) else {}
    for field in ('outcomes', 'unavailableOutcomes'):
        walk(body.get(field), 'data.' + field, 'data.' + field, 0)
    walk(body.get('ticket'), 'data.ticket', 'data.ticket', 0)
    # Ensure missing or malformed ticket collections have explicit shape records.
    for field in ('bets', 'selections'):
        path = 'data.ticket.' + field
        if path not in shapes:
            walk(ticket.get(field), path, path, 0)
    report = {'version': 1, 'limits': dict(nodes=MAX_NODES, depth=MAX_DEPTH,
        collection_items=MAX_ITEMS, object_fields=MAX_FIELDS, identifiers=MAX_IDS,
        opaque_states=MAX_STATES, unknown_values=MAX_UNKNOWN_VALUES, bytes=MAX_BYTES),
        'nodes_inspected': nodes, 'truncated': bool(reasons), 'truncation_reasons': sorted(reasons),
        'collections': collections, 'shapes': shapes, 'entries': entries,
        'identifiers': {'unique_count': len(aliases),
            'repeated_count': sum(v['occurrences'] > 1 for v in id_info.values()),
            'cross_collection_count': sum(len(v['collections']) > 1 for v in id_info.values()),
            'aliases': list(id_info.values())}}
    collection_ids = Counter(c for info in id_info.values() for c in info['collections'])
    report['identifiers']['unique_by_collection'] = dict(collection_ids)
    report['selection_structure'] = analyze_selection_structure(report)
    # A hard output bound independent of input nesting/key variety. Partial
    # evidence is explicitly marked; it must never support a membership inference.
    for field in ('entries', 'aliases', 'shapes', 'collections', 'unique_by_collection', 'event_groups'):
        while len(json.dumps(report, separators=(',', ':')).encode()) > MAX_BYTES:
            target = (report['identifiers'] if field in ('aliases', 'unique_by_collection') else
                      report['selection_structure'] if field == 'event_groups' else report)
            rows = target[field]
            if not rows:
                break
            target[field] = dict(islice(rows.items(), len(rows) // 2)) if isinstance(rows, dict) else rows[:len(rows) // 2]
            report['truncated'] = True
            report['truncation_reasons'] = sorted(reasons | {'output_limit'})
    return report


def log_structure(body):
    """One atomic anonymized log record; probe failure cannot change retrieval."""
    if not ticket_audit_switch.ENABLED or not log.isEnabledFor(logging.DEBUG):
        return
    try:
        report = structural_report(body)
        log.debug('SportyBet structural diagnostic | structure=%s',
                 json.dumps(report, separators=(',', ':')))
    except Exception:
        # No exception message/traceback: unexpected objects can contain secrets.
        try:
            log.warning('SportyBet structural diagnostic unavailable')
        except Exception:
            pass
