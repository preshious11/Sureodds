"""Synthetic structural probes only: no captured user ticket, provider I/O or raw fixture."""
import json
import logging
from unittest.mock import Mock

import pytest

from predbot.ticket_audit import booking as bk
from predbot.ticket_audit import booking_structure as probe
from predbot.ticket_audit import booking_sources
from tests.test_ticket_audit_booking_codes import Resp, direct, service, store, firewall  # noqa: F401
from tests.test_ticket_audit_foundation import NOW, no_providers  # noqa: F401


def synthetic():
    # These invented names, ids, secrets and numbers are deliberate leak canaries.
    outcome = {'id': 'RAW-OUTCOME-923', 'desc': 'PRIVATE-OUTCOME-LABEL',
               'odds': '987.654321', 'selected': True, 'isActive': 1}
    market = {'id': 'RAW-MARKET-821', 'desc': 'PRIVATE-MARKET-LABEL',
              'specifier': 'total=987.654321', 'outcomes': [outcome]}
    event = {'eventId': 'RAW-EVENT-719', 'homeTeamName': 'PRIVATE-PARTICIPANT',
             'awayTeamName': 'PRIVATE-OPPONENT', 'competition': 'PRIVATE-COMPETITION',
             'markets': [market], 'status': 2}
    refs = {'selectionId': 'RAW-SELECTION-617', 'eventId': event['eventId'],
            'marketId': market['id'], 'outcomeId': outcome['id'], 'selected': False}
    return {'shareCode': 'SECRETBOOKING', 'url': 'https://private.invalid/SECRETBOOKING',
            'outcomes': [event], 'unavailableOutcomes': [dict(refs, unavailable=True)],
            'ticket': {'selections': [refs], 'bets': [{'betId': 'RAW-BET-515',
                'selectionIds': [refs['selectionId']]}], 'totalStake': 876.543219,
                'potentialWinnings': '765.432198', 'userId': 'PRIVATE-USER',
                'apiKey': 'PRIVATE-CREDENTIAL', 'displayTotalOdds': '654.321987'}}


def encoded(report):
    return json.dumps(report, separators=(',', ':'))


def entry(report, path):
    return next(e for e in report['entries'] if e['path'] == path)


def alias(report, path, field):
    return entry(report, path)['identifiers'][field]['alias']


def test_no_private_leaf_values_or_raw_identifiers_in_report_or_log(caplog):
    body = synthetic()
    caplog.set_level(logging.DEBUG, logger=probe.__name__)
    probe.log_structure(body)
    record, = [r for r in caplog.records if r.name == probe.__name__]
    report = json.loads(record.getMessage().split(' | structure=', 1)[1])
    text = encoded(report) + caplog.text
    for private in ('RAW-', 'PRIVATE-', 'SECRETBOOKING', 'private.invalid',
                    '987.654321', '876.543219', '765.432198', '654.321987'):
        assert private not in text
    assert 'homeTeamName' in text and 'odds' in text  # key names/types, never values
    assert body == synthetic()  # diagnostic must not mutate the response


def test_aliases_preserve_cross_collection_relationships_without_inference():
    report = probe.structural_report(synthetic())
    ref = 'data.ticket.selections[0]'
    assert alias(report, 'data.outcomes[0]', 'eventId') == alias(report, ref, 'eventId')
    assert alias(report, 'data.outcomes[0].markets[0]', 'id') == alias(report, ref, 'marketId')
    assert alias(report, 'data.outcomes[0].markets[0].outcomes[0]', 'id') == alias(report, ref, 'outcomeId')
    assert alias(report, ref, 'selectionId') == alias(report, 'data.ticket.bets[0].selectionIds[0]', 'value')
    assert alias(report, ref, 'outcomeId') == alias(report, 'data.unavailableOutcomes[0]', 'outcomeId')
    assert report['identifiers']['unique_count'] == 6  # five IDs plus aliased specifier
    assert report['identifiers']['cross_collection_count'] == 4
    assert report['identifiers']['unique_by_collection']['data.ticket.selections[*]'] == 4
    assert not report['truncated']
    assert probe.structural_report(synthetic()) == report  # deterministic within equivalent requests


def test_alias_registry_is_request_local_and_preserves_reference_types():
    first = probe.structural_report({'outcomes': [{'eventId': 'first-private-id'}]})
    second = probe.structural_report({'outcomes': [{'eventId': 'second-private-id'}]})
    assert alias(first, 'data.outcomes[0]', 'eventId') == alias(second, 'data.outcomes[0]', 'eventId') == 'id_001'
    report = probe.structural_report({'outcomes': [{'eventId': 123}],
                                     'ticket': {'selections': [{'eventId': '123'}]}})
    assert alias(report, 'data.outcomes[0]', 'eventId') == alias(report, 'data.ticket.selections[0]', 'eventId')
    assert entry(report, 'data.outcomes[0]')['identifiers']['eventId']['type'] == 'integer'
    assert entry(report, 'data.ticket.selections[0]')['identifiers']['eventId']['type'] == 'string'


def test_status_distributions_allow_only_fixed_enums_booleans_and_small_integer_codes():
    body = synthetic()
    body['ticket']['selections'] *= 24
    body['ticket']['selections'] += [{'selected': True}] * 8
    body['outcomes'][0]['markets'][0]['outcomes'] += [
        {'status': 'PRIVATE-PARTICIPANT'}, {'status': 987.654321},
        {'status': 987654321}, {'status': 'suspended'}, {'status': None}]
    report = probe.structural_report(body)
    assert report['shapes']['data.ticket.selections[*]']['states']['selected'] == {'false': 24, 'true': 8}
    assert report['shapes']['data.outcomes[*].markets[*].outcomes[*]']['states']['status'] == {
        'redacted_string:state_001': 1, 'redacted_number:state_002': 1,
        'redacted_integer:state_003': 1, 'suspended': 1, 'null': 1}
    assert 'PRIVATE-' not in encoded(report) and '987654321' not in encoded(report)


def test_unknown_keys_and_private_malformed_objects_are_never_logged():
    body = synthetic()
    body['outcomes'][0].update({'SECRETBOOKING': {'id': 'private-id'},
                               'PRIVATE-PARTICIPANT': 'hidden'})
    body['ticket']['totalStake'] = {'id': 'PRIVATE-CREDENTIAL'}
    report = probe.structural_report(body)
    assert all(secret not in encoded(report) for secret in ('SECRETBOOKING', 'PRIVATE-', 'private-id'))
    assert 'field_001' in encoded(report)
    assert not any(e['path'].startswith('data.ticket.totalStake.') for e in report['entries'])


@pytest.mark.parametrize('body', [None, [], 'private-string', {},
    {'outcomes': None, 'ticket': 'private-string'},
    {'outcomes': [None, 1, {'markets': [None, {'outcomes': 'private-string'}]}],
     'ticket': {'bets': None, 'selections': {}}, 'unavailableOutcomes': False}])
def test_missing_or_malformed_shapes_are_safe(body):
    report = probe.structural_report(body)
    assert 'data.ticket.bets' in report['shapes'] and 'data.ticket.selections' in report['shapes']
    assert 'private-string' not in encoded(report)
    assert len(encoded(report).encode()) <= probe.MAX_BYTES


def test_large_wide_deep_and_cyclic_inputs_are_bounded():
    outcomes = [{'id': k} for k in range(150)]
    markets = [{'id': j, 'outcomes': outcomes} for j in range(150)]
    wide = {'outcomes': [{'eventId': f'raw-{i}', 'markets': markets} for i in range(150)]}
    report = probe.structural_report(wide)
    assert report['truncated'] and report['nodes_inspected'] <= probe.MAX_NODES
    assert report['identifiers']['unique_count'] <= probe.MAX_IDS
    assert len(encoded(report).encode()) <= probe.MAX_BYTES and 'raw-' not in encoded(report)
    for body in ({'outcomes': [dict.fromkeys([f'private-key-{i}' for i in range(1000)], 'secret')]}, {}):
        if not body:
            body['ticket'] = body
        report = probe.structural_report(body)
        assert report['truncated'] and len(encoded(report).encode()) <= probe.MAX_BYTES
        assert 'private-key-' not in encoded(report)


def test_unknown_status_values_keep_distributions_without_disclosing_text():
    report = probe.structural_report({'outcomes': [{'status': 'PRIVATE-FIRST'}] * 24 +
                                                [{'status': 'PRIVATE-SECOND'}] * 8})
    assert report['shapes']['data.outcomes[*]']['states']['status'] == {
        'redacted_string:state_001': 24, 'redacted_string:state_002': 8}
    assert 'PRIVATE-' not in encoded(report)


def test_identifier_state_and_output_limits_are_explicit(monkeypatch):
    monkeypatch.setattr(probe, 'MAX_IDS', 2)
    monkeypatch.setattr(probe, 'MAX_STATES', 2)
    report = probe.structural_report({'outcomes': [
        {'eventId': f'RAW-{i}', 'status': f'PRIVATE-{i}'} for i in range(20)]})
    assert report['identifiers']['unique_count'] == 2
    assert {'identifier_limit', 'state_value_limit'} <= set(report['truncation_reasons'])
    monkeypatch.setattr(probe, 'MAX_BYTES', 2048)
    report = probe.structural_report(synthetic())
    assert 'output_limit' in report['truncation_reasons'] and len(encoded(report).encode()) <= 2048


def test_runtime_no_longer_dumps_structure_and_caches_all_selections(store, caplog):
    caplog.set_level(logging.DEBUG)
    body = synthetic()
    body['outcomes'] *= 32
    body['ticket']['selections'] *= 32
    resolver, transport = direct(store, Resp(body={'bizCode': 10000, 'data': body}))
    result = service(store, resolver).resolve('sportybet', 'SECRETBOOKING', NOW)
    assert result.valid and len(result.selections) == 32
    assert not [r for r in caplog.records if r.name == probe.__name__]
    assert len(transport.calls) == 1
    assert len(store.get(store.key('sportybet', 'SECRETBOOKING'), NOW).selections) == 32
    assert 'SECRETBOOKING' not in caplog.text and 'PRIVATE-' not in caplog.text



def test_probe_failure_does_not_change_resolver_result(store, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG, logger=probe.__name__)
    def broken(*args):
        raise RuntimeError('SECRETBOOKING PRIVATE-CREDENTIAL')
    monkeypatch.setattr(probe, 'structural_report', broken)
    resolver, _ = direct(store, Resp(body={'bizCode': 10000, 'data': {'outcomes': [None]}}))
    result = service(store, resolver).resolve('sportybet', 'SECRETBOOKING', NOW)
    assert result.status == bk.PARTIAL and len(result.selections) == 1
    probe.log_structure(synthetic())
    assert 'diagnostic unavailable' in caplog.text and 'SECRETBOOKING' not in caplog.text


def test_disabled_log_level_does_not_inspect_response(monkeypatch):
    monkeypatch.setattr(probe.log, 'isEnabledFor', lambda level: False)
    monkeypatch.setattr(probe, 'structural_report', lambda *args: pytest.fail('probe called'))
    probe.log_structure(synthetic())


def test_unknown_selection_values_and_boolean_distributions_are_opaque_and_request_local():
    body = synthetic()
    body['ticket']['selections'] = [dict(body['ticket']['selections'][0], privateGroupingField='PRIVATE-FIRST')] * 24
    body['ticket']['selections'] += [dict(body['ticket']['selections'][0], privateGroupingField='PRIVATE-SECOND')] * 8
    body['outcomes'] = [dict(body['outcomes'][0], privateFlag=True)] * 24 + [dict(body['outcomes'][0], privateFlag=False)] * 8
    report = probe.structural_report(body)
    fields = report['shapes']['data.ticket.selections[*]']['unknown_values']
    assert len(fields) == 1
    group = next(iter(fields.values()))
    assert group['unique_values'] == 2 and sorted(group['groups'].values()) == [8, 24]
    unknown_states = {k: v for k, v in report['shapes']['data.outcomes[*]']['states'].items() if k.startswith('field_')}
    assert list(unknown_states.values()) == [{'true': 24, 'false': 8}]
    assert all(private not in encoded(report) for private in ('PRIVATE-', 'privateGroupingField', 'privateFlag'))
    assert probe.structural_report(body) == report
    assert probe.structural_report({'outcomes': [{'unknown': 'another-value'}]})['entries'][0]['unknown_values']['field_001']['alias'] == 'value_001'


def test_unknown_value_groups_include_container_equality_and_keep_types_distinct():
    rows = [{'mystery': v} for v in ({'a': [1, 'PRIVATE']}, {'a': [1, 'PRIVATE']}, 1, True, None)]
    report = probe.structural_report({'ticket': {'selections': rows}})
    group, = report['shapes']['data.ticket.selections[*]']['unknown_values'].values()
    assert group['unique_values'] == 4 and sorted(group['groups'].values()) == [1, 1, 1, 2]
    assert 'PRIVATE' not in encoded(report)


def test_unknown_value_bounds_never_group_unexamined_values_together(monkeypatch):
    monkeypatch.setattr(probe, 'MAX_UNKNOWN_VALUES', 1)
    report = probe.structural_report({'ticket': {'selections': [
        {'mystery': 'first'}, {'mystery': 'second'}, {'mystery': 'x' * 10_000}]}})
    group, = report['shapes']['data.ticket.selections[*]']['unknown_values'].values()
    assert group['unique_values'] == 1 and group['unexamined'] == 2
    assert {'unknown_value_limit', 'unknown_value_unexamined'} <= set(report['truncation_reasons'])
    assert len(encoded(report).encode()) <= probe.MAX_BYTES


def reference_body(variant):
    rows = [{'eventId': f'PRIVATE-EVENT-{i}', 'marketId': 'PRIVATE-MARKET',
             'outcomeId': 'PRIVATE-OUTCOME', 'specifier': ''} for i in range(24)]
    extra = [dict(row) for row in rows[:8]]
    for row in extra:
        if variant == 'markets':
            row['marketId'] = 'PRIVATE-OTHER-MARKET'
        if variant == 'outcomes':
            row['outcomeId'] = 'PRIVATE-OTHER-OUTCOME'
    rows += extra
    events = [{'eventId': r['eventId'], 'markets': [{'id': r['marketId'],
        'specifier': r['specifier'], 'outcomes': [{'id': r['outcomeId']}]}]} for r in rows]
    return {'outcomes': events, 'ticket': {'selections': rows, 'bets': []}, 'unavailableOutcomes': []}


@pytest.mark.parametrize('variant', ['duplicates', 'markets', 'outcomes'])
def test_event_and_complete_tuple_analysis_does_not_deduplicate_or_change_guard(store, caplog, variant):
    caplog.set_level(logging.DEBUG, logger=probe.__name__)
    body = reference_body(variant)
    report = probe.structural_report(body)
    analysis = report['selection_structure']
    assert analysis['complete'] and analysis['reference_rows_analyzed'] == 32
    assert analysis['unique_event_count'] == 24 and analysis['duplicate_event_entries'] == 8
    assert analysis['repeated_event_count'] == 8
    assert analysis['unique_complete_tuple_count'] == (24 if variant == 'duplicates' else 32)
    assert analysis['duplicate_complete_tuple_entries'] == (8 if variant == 'duplicates' else 0)
    assert analysis['events_with_different_markets'] == (8 if variant == 'markets' else 0)
    assert analysis['events_with_different_outcomes'] == (8 if variant == 'outcomes' else 0)
    assert analysis == probe.analyze_selection_structure(report)  # existing map, no provider access
    assert len(body['ticket']['selections']) == len(body['outcomes']) == 32
    resolver, transport = direct(store, Resp(body={'bizCode': 10000, 'data': body}))
    result = service(store, resolver).resolve('sportybet', 'TEST32', NOW)
    assert result.valid and len(result.selections) == 32
    assert len(transport.calls) == 1 and 'PRIVATE-' not in caplog.text


def test_selection_analysis_never_claims_complete_counts_for_partial_maps():
    report = probe.structural_report(reference_body('duplicates'))
    report['truncated'] = True
    assert not probe.analyze_selection_structure(report)['complete']
    report['truncated'] = False
    report['entries'] = [e for e in report['entries'] if e['path'] != 'data.ticket.selections[0]']
    assert not probe.analyze_selection_structure(report)['complete']


def test_current_diagnostic_counts_all_decoded_legs_including_unresolved_ones(store, monkeypatch):
    import diagnose_booking_code as cli
    body = {'bizCode': 10000, 'data': {'outcomes': [None] * 23 + [
        {'sport': {'id': 'unmapped'}, 'markets': [{'id': 'unsupported', 'outcomes': [{'id': 'unknown'}]}]}]}}
    resolver, transport = direct(store, Resp(body=body))
    monkeypatch.setenv('BOOKING_DIAG_CODE', 'TEST24')
    monkeypatch.setattr(cli.bk, 'BookingStore', Mock(return_value=store))
    monkeypatch.setattr(cli, 'SportyBetDirectResolver', Mock(return_value=resolver))
    emit = Mock(return_value=0)
    monkeypatch.setattr(cli, 'emit', emit)
    assert cli.main(['--source', 'sportybet-direct', '--live']) == 0  # mocked GET only
    report = emit.call_args.args[0]
    assert report['legs'] == report['http']['legs'] == len(report['selections']) == 24
    assert report['result'] == bk.PARTIAL and all(l['unresolved'] for l in report['selections'])
    assert len(transport.calls) == 1 and not hasattr(bk, "MAX_LEGS")
