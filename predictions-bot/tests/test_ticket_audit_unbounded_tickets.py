"""No live I/O: complete tickets, bounded work/spending and isolated failures."""
import ast
from dataclasses import asdict, replace
from datetime import timedelta
import json
import re
from pathlib import Path
from unittest.mock import Mock

import pytest

from predbot.bot import audit_format
from predbot.bot.ticket_audit import INCOMPLETE, CODE_MESSAGES
from predbot.ticket_audit import booking as bk
from predbot.ticket_audit.analysis import EvidenceAnalyzer
from predbot.ticket_audit.resources import (
    MAX_JSON_BYTES, MAX_JSON_DEPTH, MAX_JSON_NODES, PROCESSING_BATCH_BYTES,
    ResourceLimit, selection_batches, validate_payload,
)
from predbot.ticket_audit.schemas import AuditUnavailable, TicketResourceLimit, ticket_from_json, ticket_json_schema
from predbot.ticket_audit.service import AuditService
from tests.test_ticket_audit_booking_codes import (  # noqa: F401
    Resp, direct, service, store, firewall, ui, code_prompt, tap, texts,
)
from tests.test_ticket_audit_foundation import (  # noqa: F401
    no_providers, selection, ticket, response, NOW, local,
)
from tests.test_ticket_audit_free_research import (
    setup_source, missing_resolver, free_client, extracted_payload,
)
from tests.test_ticket_audit_large_tickets import (
    share, researched_audit, batch_answering_client, epl_fixture_legs, multi_fixture_source,
)
from tests.test_ticket_audit_telegram import input_message


def product_guard_violations(source):
    """Restrict actual leg containers, not evidence, batch, display or parser bounds."""
    tree = ast.parse(source)
    errors = []
    containers = {'selections', 'legs', 'events', 'rows', 'outcomes', 'normalized_selections', 'decoded_selections'}

    def leg_container(expr):
        if isinstance(expr, ast.Name):
            return expr.id in containers
        if isinstance(expr, ast.Attribute):
            return expr.attr in containers
        if isinstance(expr, ast.Subscript) and isinstance(expr.slice, ast.Constant):
            return expr.slice.value in containers
        return False

    def leg_length(expr):
        return (isinstance(expr, ast.Call) and isinstance(expr.func, ast.Name)
                and expr.func.id == 'len' and len(expr.args) == 1 and leg_container(expr.args[0]))

    def threshold(expr):
        if isinstance(expr, ast.Constant):
            return type(expr.value) in (int, float)
        if isinstance(expr, (ast.Name, ast.Attribute)):
            name = expr.id if isinstance(expr, ast.Name) else expr.attr
            return name.isupper() or 'limit' in name.lower() or 'max' in name.lower()
        return False

    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in {'MAX_LEGS', 'MAX_SELECTIONS', 'MAX_TICKET_SELECTIONS'}:
            errors.append((node.lineno, 'product constant'))
        if (isinstance(node, ast.Subscript) and leg_container(node.value)
                and isinstance(node.slice, ast.Slice) and node.slice.upper is not None):
            # Full-ticket slices with any fixed/named upper bound risk losing legs.
            # Internal processing slices must use a separately named batch container.
            errors.append((node.lineno, 'selection slicing'))
        if isinstance(node, ast.Compare):
            # Equality with a dynamic batch length verifies output completeness;
            # len(legs)>=6 changes qualitative composition, never rejects a ticket.
            for lhs, op, rhs in zip([node.left, *node.comparators[:-1]], node.ops, node.comparators):
                if leg_length(lhs) and isinstance(op, (ast.Gt, ast.LtE)):
                    if threshold(rhs):
                        errors.append((node.lineno, 'selection upper bound'))
                if leg_length(rhs) and isinstance(op, (ast.Lt, ast.GtE)):
                    if threshold(lhs):
                        errors.append((node.lineno, 'selection upper bound'))
    return errors


def test_architecture_has_no_product_leg_guard_or_full_ticket_slicing():
    root = Path(__file__).resolve().parents[1]
    files = list((root / 'predbot/ticket_audit').glob('*.py'))
    files += [root / 'predbot/bot/ticket_audit.py', root / 'predbot/bot/audit_format.py']
    violations = []
    for path in files:
        # This optional offline probe bounds diagnostic output, never selections.
        if path.name == 'booking_structure.py':
            continue
        violations += [(path.name, *v) for v in product_guard_violations(path.read_text(encoding='utf-8'))]
    assert not violations
    assert 'maxItems' not in json.dumps(ticket_json_schema())


@pytest.mark.parametrize('source', [
    'MAX_SELECTIONS = 100', 'MAX_LEGS = 30',
    'legs = selections[:20]', 'legs = ticket.selections[:25]',
    "legs = data['outcomes'][:30]", 'legs = selections[:new_limit]',
    'if len(selections) > 30: raise ValueError()',
    'if len(events) <= LIMIT: return events',
])
def test_architecture_regression_detects_product_caps_and_slicing(source):
    assert product_guard_violations(source)


def test_old_too_many_cache_does_not_survive_restart_as_a_rejection(store):
    code = 'OLD123'
    key = store.key('sportybet', code)
    store.put(key, 'TOO_MANY', NOW + timedelta(minutes=10))
    restarted = bk.BookingStore(store.path)
    assert restarted.get(key, NOW) is None
    resolver, transport = direct(restarted, Resp(body=share(32)))
    result = service(restarted, resolver).resolve('sportybet', code, NOW)
    assert result.valid and len(result.selections) == 32 and len(transport.calls) == 1
    again = service(bk.BookingStore(store.path), resolver).resolve('sportybet', code, NOW)
    assert again.cached and len(again.selections) == 32 and len(transport.calls) == 1


def test_more_than_one_hundred_is_not_a_replacement_product_cap():
    payload = extracted_payload()
    payload['selections'] = [payload['selections'][0]] * 500
    normalized = ticket_from_json(payload)
    result = AuditService(missing_resolver()).audit(normalized, NOW)
    assert len(normalized.selections) == len(result.selections) == 500
    assert all(a.verdict == 'INSUFFICIENT_EVIDENCE' for a in result.selections)


def test_local_batches_pack_bytes_keep_order_and_share_one_snapshot(local, monkeypatch):
    resolver, s, _ = local
    selections = tuple(replace(s, raw_text=f'{i}:' + 'x' * 1500) for i in range(100))
    batches = list(selection_batches(selections))
    assert len(batches) > 1
    assert tuple(s for b in batches for s in b) == selections
    for batch in batches:
        assert sum(len(json.dumps(asdict(s), ensure_ascii=True).encode()) for s in batch) <= PROCESSING_BATCH_BYTES
    read = Mock(wraps=resolver._records)
    monkeypatch.setattr(resolver, '_records', read)
    result = AuditService(resolver).audit(ticket(*selections), NOW)
    assert read.call_count == 1 and len(result.selections) == 100
    assert all(a.evidence.local.status == 'EXACT' for a in result.selections)


def test_hundred_selections_reuse_fixture_and_downloaded_dataset(tmp_path):
    legs = epl_fixture_legs(50, 2)
    researcher, _, transport = setup_source(tmp_path, multi_fixture_source(legs))
    result = AuditService(missing_resolver(), researcher).audit(ticket(*legs), NOW)
    assert transport.get.call_count == 1 and len(result.selections) == 100
    assert all(a.evidence.research and a.key_facts for a in result.selections)
    assert [a.normalized_selection for a in result.selections] == legs


def test_research_failures_spend_only_three_slots_and_keep_hundred_legs():
    researcher = Mock()
    researcher.cached.return_value = None
    researcher.research.side_effect = RuntimeError('private provider details')
    legs = [selection(home=f'Home {i}', away=f'Away {i}') for i in range(100)]
    result = AuditService(missing_resolver(), researcher).audit(ticket(*legs), NOW)
    assert researcher.research.call_count == 3
    assert len(result.selections) == 100
    assert all(a.verdict == 'INSUFFICIENT_EVIDENCE' for a in result.selections)
    assert all('Per-ticket research budget exhausted' in a.limitations for a in result.selections[3:])
    assert 'private provider details' not in repr(result)


def test_failed_local_or_cache_leg_does_not_destroy_other_legs():
    resolver = missing_resolver()
    missing = resolver.resolve.return_value
    resolver.resolve.side_effect = [RuntimeError('private text'), missing, missing]
    researcher = Mock()
    researcher.cached.side_effect = RuntimeError('private cache text')
    result = AuditService(resolver, researcher).audit(ticket(*[selection()] * 3), NOW)
    assert len(result.selections) == 3 and all(a.verdict == 'INSUFFICIENT_EVIDENCE' for a in result.selections)
    researcher.research.assert_not_called()
    assert 'private' not in repr(result)


def test_hundred_researched_selections_keep_gemini_budget_and_output_bounds(tmp_path):
    original = researched_audit(tmp_path, 100)
    client, prompts = batch_answering_client(tmp_path)
    result = EvidenceAnalyzer(client).analyze_ticket(original, NOW)
    assert len(result.selections) == 100
    assert 1 <= client.transport.request.call_count <= EvidenceAnalyzer.MAX_BATCHES == 3
    assert all(len(p) <= EvidenceAnalyzer.MAX_PROMPT for p in prompts)
    calls = client.transport.request.call_args_list
    assert all('tools' not in c.kwargs['json'] for c in calls)
    assert all(c.kwargs['json']['generationConfig']['maxOutputTokens'] == EvidenceAnalyzer.MAX_OUTPUT_TOKENS for c in calls)
    assert client.usage.snapshot(NOW)['requests'] == len(prompts)
    sent = [leg['index'] for p in prompts for leg in json.loads(p[len(EvidenceAnalyzer.INSTRUCTIONS):])['legs']]
    assert sent == list(range(len(sent))) and len(sent) < 100
    assert [a.normalized_selection for a in result.selections] == [a.normalized_selection for a in original.selections]
    for a, b in zip(original.selections, result.selections):
        assert (a.key_facts, a.local_probability, a.market_probability, a.provenance) == (
            b.key_facts, b.local_probability, b.market_probability, b.provenance)


def test_middle_model_batch_failure_keeps_successful_batches_and_order(tmp_path, local):
    resolver, s, _ = local
    original = AuditService(resolver).audit(ticket(*[replace(s, raw_text=str(i)) for i in range(100)]), NOW)
    client = free_client(tmp_path, '{}')
    batches = []

    def generate(prompt, schema, **kwargs):
        entries = json.loads(prompt[len(EvidenceAnalyzer.INSTRUCTIONS):])['legs']
        batches.append([e['index'] for e in entries])
        assert kwargs['grounded'] is False
        if len(batches) == 2:
            raise AuditUnavailable('timeout')
        return json.dumps({'legs': [{'index': e['index'], 'verdict': 'VERY_RISKY',
            'fact_ids': list(e['facts'])[:1], 'risk_codes': []} for e in entries]}), {}, {}

    client.generate = Mock(side_effect=generate)
    result = EvidenceAnalyzer(client).analyze_ticket(original, NOW)
    assert len(batches) == client.generate.call_count == 3
    assert len(result.selections) == 100
    for i in batches[0] + batches[2]:
        assert result.selections[i].verdict == 'VERY_RISKY'
        assert not any('Gemini analysis unavailable' in note for note in result.selections[i].limitations)
    for i in batches[1]:
        assert result.selections[i].verdict == original.selections[i].verdict
        assert 'Gemini analysis unavailable for this batch' in result.selections[i].limitations
    assert [a.normalized_selection for a in result.selections] == [a.normalized_selection for a in original.selections]
    assert all(a.key_facts == b.key_facts for a, b in zip(original.selections, result.selections))


@pytest.mark.parametrize('kind', ['depth', 'nodes', 'bytes'])
def test_technical_json_limits_raise_actual_resource_reason(kind):
    if kind == 'depth':
        value = None
        for _ in range(MAX_JSON_DEPTH + 2):
            value = [value]
        reason = 'ticket_parser_depth_limit'
    elif kind == 'nodes':
        value = [None] * MAX_JSON_NODES
        reason = 'ticket_parser_node_limit'
    else:
        value = ' ' * (MAX_JSON_BYTES + 1)
        reason = 'ticket_input_too_large'
    with pytest.raises(ResourceLimit, match=reason):
        validate_payload(value)
    with pytest.raises(TicketResourceLimit, match=reason):
        ticket_from_json(value)


@pytest.mark.parametrize('kind', ['depth', 'nodes'])
def test_booking_parser_work_limits_stop_before_normalization(store, monkeypatch, kind):
    import predbot.ticket_audit.booking_sources as sources
    if kind == 'depth':
        raw = share(32)
        nested = None
        for _ in range(MAX_JSON_DEPTH + 2):
            nested = [nested]
        raw['extra'] = nested
    else:
        # 200 KB, below HTTP bytes but exceeds parser work bound.
        raw = [0] * MAX_JSON_NODES
    resolver, transport = direct(store, Resp(body=raw))
    finalize = Mock(side_effect=AssertionError('normalization must not start'))
    monkeypatch.setattr(sources, 'finalize', finalize)
    result = service(store, resolver).resolve('sportybet', 'WORK123', NOW)
    assert result.status == bk.PARSER_LIMIT and len(transport.calls) == 1
    finalize.assert_not_called()
    assert 'too many' not in CODE_MESSAGES[result.status].lower()


def test_normalized_resource_failure_prevents_research_and_model_requests():
    resolver, researcher, analyzer = missing_resolver(), Mock(), Mock()
    huge = ticket(selection(raw_text='x' * (MAX_JSON_BYTES + 1)))
    with pytest.raises(TicketResourceLimit, match='ticket_input_too_large'):
        AuditService(resolver, researcher, analyzer=analyzer).audit(huge, NOW)
    resolver.resolve.assert_not_called()
    researcher.research.assert_not_called()
    analyzer.analyze_ticket.assert_not_called()


@pytest.mark.parametrize('mode', ['warning', 'tokens'])
def test_partial_extraction_never_reaches_review_or_confirmation(ui, mode):
    payload = extracted_payload()
    payload['extraction_warnings'] = ['INCOMPLETE_INPUT'] if mode == 'warning' else []
    ui.client.transport.request.return_value.json.return_value = response(
        json.dumps(payload), reason='MAX_TOKENS' if mode == 'tokens' else 'STOP')
    ui.controller.start(input_message())
    ui.controller.message(input_message('Full mock ticket text to extract, with multiple selections'))
    assert ui.controller.sessions[('42', '42')].state == 'WAITING'
    assert ui.tg.sends[-1]['text'] == INCOMPLETE
    assert ui.client.transport.request.call_count == 1
    assert not any('Audit Ticket' in str(s.get('reply_markup')) for s in ui.tg.sends)


def test_hundred_long_review_and_result_blocks_are_not_split():
    legs = [selection(home=f'LONG-{i:03d}-' + 'x' * 140, away='y' * 160) for i in range(100)]
    t = ticket(*legs)
    result = AuditService(missing_resolver()).audit(t, NOW)
    review = audit_format.review(t)
    pages = audit_format.results(t, result)
    assert len(review) > 1 and len(pages) > 3
    assert all(len(p) <= 3500 for p in review + pages)
    for block in audit_format._legs(t):
        assert sum(block in p for p in review) == 1
    for i in range(100):
        assert sum(bool(re.search(rf'^{i+1}\. <b>', p, re.M)) for p in pages) == 1
        assert sum(f'LONG-{i:03d}-' in p for p in pages[1:-1]) == 1

