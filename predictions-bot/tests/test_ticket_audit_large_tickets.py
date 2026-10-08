"""Large tickets: resources, order, rendering, combined odds, research efficiency.

Synthetic reconstructions only, NOT saved SportyBet responses. The primary
32-selection case reproduces the captured relationship: distinct events, each
with one market/outcome, matching complete ticket references, no bets/unavailable
outcomes. All participant labels and payload contents are synthetic.
"""
from dataclasses import asdict, replace
from datetime import datetime, timezone
import json
import logging
import re
from unittest.mock import Mock

import pytest

from predbot.bot import audit_format
from predbot.bot.ticket_audit import RESOURCE_LIMIT, CODE_MESSAGES
from predbot.ticket_audit import booking as bk
from predbot.ticket_audit import booking_sources
from predbot.ticket_audit.analysis import EvidenceAnalyzer
from predbot.ticket_audit.booking_sources import MyBetCodeResolver, bookmaker_total_odds
from predbot.ticket_audit.local import LocalResolver
from predbot.ticket_audit.schemas import (AuditUnavailable, TicketResourceLimit, ticket_from_json,
                                          ticket_json_schema)
from predbot.ticket_audit.service import AuditService
from tests.test_ticket_audit_booking_codes import (Resp, Transport, direct, mbc, service, tap, code_prompt, texts,
                                                   firewall, store, ui, MBC_OK)  # noqa: F401
from tests.test_ticket_audit_foundation import no_providers, local, selection, ticket, response, NOW, db_dump  # noqa: F401
from tests.test_ticket_audit_free_research import setup_source, missing_resolver, free_client, extracted_payload, source_data
from tests.test_ticket_audit_telegram import KEY

INTERNAL = re.compile(r'\b(result2|result3|_1x2|over_under)\b')
BASE = int(datetime(2030, 6, 1, 15, 0, tzinfo=timezone.utc).timestamp() * 1000)
# (market id, label, outcome id, outcome label, specifier): three real ids, labels are placeholders.
SHAPES = [('60020', 'Over/Under', '12', 'Over 2.5', 'total=2.5'),
          ('60021', '1X2', '1', 'Home', ''),
          ('60022', '2nd Half - Over/Under', '13', 'Under 1.5', 'total=1.5'),
          ('1', '1X2', '3', 'Away', ''),
          ('18', 'Over/Under', '12', 'Over 3.5', 'total=3.5'),
          ('68', '1st Half - Over/Under', '12', 'Over 0.5', 'total=0.5')]


def leg_event(i, shape=None):
    mid, label, oid, olabel, spec = shape or SHAPES[i % len(SHAPES)]
    return {'eventId': f'sr:match:{i}', 'estimateStartTime': BASE + i * 60_000,
            'homeTeamName': f'Home {i:02d}', 'awayTeamName': f'Away {i:02d}',
            'sport': {'id': 'sr:sport:1', 'name': 'Football',
                      'category': {'name': 'Cat', 'tournament': {'name': f'League {i % 3}'}}},
            'markets': [{'id': mid, 'specifier': spec, 'desc': label,
                         'outcomes': [{'id': oid, 'odds': f'{1.1 + i / 100:.2f}', 'desc': olabel, 'isActive': 1}]}]}


def share(n, total='1,234.56', **ticket_extra):
    ticket = {'bets': [{'REDACTED': True}], 'displayBonus': '0.00', 'displayTotalOdds': total, 'orderType': 1,
              'potentialWinnings': '987654.32', 'selections': [{} for _ in range(n)], 'totalStake': '777.00',
              **ticket_extra}
    if total is None:
        ticket.pop('displayTotalOdds')
    return {'bizCode': 10000, 'isAvailable': True, 'message': '0#0',
            'data': {'shareCode': 'REDACTED', 'outcomes': [leg_event(i) for i in range(n)], 'ticket': ticket}}


def decode(store, body, code='BIG123'):
    r, t = direct(store, Resp(body=body))
    return service(store, r).resolve('sportybet', code, NOW), r, t


# ------------------------------------------------------------------ limits
def test_no_product_selection_limit_constant():
    from predbot.ticket_audit import schemas
    assert not hasattr(schemas, 'MAX_SELECTIONS') and not hasattr(bk, 'MAX_LEGS')
    assert not hasattr(booking_sources, 'MAX_LEGS')



@pytest.mark.parametrize('n', [20, 24, 30])
def test_booking_ticket_accepted_order_and_every_leg_preserved(store, n):
    result, _, t = decode(store, share(n))
    assert result.status == bk.OK and len(result.selections) == n and len(t.calls) == 1
    assert [l.home for l in result.selections] == [f'Home {i:02d}' for i in range(n)]
    ticket = bk.to_ticket(result)
    assert [s.home for s in ticket.selections] == [f'Home {i:02d}' for i in range(n)]
    assert [s.submitted_odds for s in ticket.selections] == [round(1.1 + i / 100, 2) for i in range(n)]


def test_thirty_one_events_preserved_never_truncated(store):
    result, _, _ = decode(store, share(31))
    assert result.status == bk.OK and len(result.selections) == 31



def test_multiple_selections_per_fixture_preserved(store):
    body = share(16)
    for e in body['data']['outcomes']:
        e['markets'][0]['outcomes'].append({'id': '2', 'odds': '3.1', 'desc': 'Draw', 'isActive': 1})
    result, _, _ = decode(store, body)
    assert result.valid and len(result.selections) == 32



def test_mybetcode_large_rows_preserved_never_truncated(store):
    body = json.loads(json.dumps(MBC_OK))
    body['data']['selections'] = [body['data']['selections'][0]] * 100
    body['data']['count'] = 100
    m, _ = mbc(store, Resp(body=body))
    result = service(store, mbc_resolver=m).resolve('bet9ja', 'V08G7Y', NOW)
    assert result.valid and len(result.selections) == 100



@pytest.mark.parametrize('n', [1, 10, 24, 32, 50, 100])
def test_normalized_validator_accepts_large_tickets(n):
    p = extracted_payload()
    p['selections'] = [p['selections'][0]] * n
    assert len(ticket_from_json(p).selections) == n



@pytest.mark.parametrize('n', [1, 10, 24, 32, 50, 100])
def test_audit_service_preserves_all_selections(n):
    t = ticket(*[selection(home=f'Team {i}') for i in range(n)])
    result = AuditService(missing_resolver()).audit(t, NOW)
    assert [a.normalized_selection.home for a in result.selections] == [f'Team {i}' for i in range(n)]



def test_gemini_schema_has_no_max_items_and_extraction_budget_fits_thirty(tmp_path):
    assert 'maxItems' not in json.dumps(ticket_json_schema())
    p = extracted_payload()
    p['selections'] = [p['selections'][0]] * 30
    c = free_client(tmp_path, json.dumps(p))
    assert len(c.extract(text='thirty legs').selections) == 30
    body = c.transport.request.call_args.kwargs['json']
    assert body['generationConfig']['maxOutputTokens'] == 8192 and 'maxItems' not in json.dumps(body)
    assert c.transport.request.call_count == 1


@pytest.mark.parametrize('n', [32, 50, 100])
def test_complete_screenshot_extraction_has_no_local_leg_cap(tmp_path, n):
    p = extracted_payload('image')
    p['selections'] = [p['selections'][0]] * n
    c = free_client(tmp_path, json.dumps(p))
    assert len(c.extract(image=('image/png', b'\x89PNG\r\n\x1a\nmock')).selections) == n



# ------------------------------------------------------------------ Gemini analysis: size-packed batches, never per leg
def batch_answering_client(tmp_path):
    """Mocked Gemini that answers exactly the indices each batched prompt supplies."""
    c = free_client(tmp_path, '{}')
    prompts = []

    def reply(method, url, **kw):
        prompt = kw['json']['contents'][0]['parts'][0]['text']
        prompts.append(prompt)
        data = json.loads(prompt[len(EvidenceAnalyzer.INSTRUCTIONS):])
        legs = [{'index': leg['index'], 'verdict': 'INSUFFICIENT_EVIDENCE', 'fact_ids': [], 'risk_codes': []}
                for leg in data['legs']]
        from unittest.mock import Mock
        return Mock(status_code=200, json=Mock(return_value=response(json.dumps({'legs': legs}))))
    c.transport.request.side_effect = reply
    return c, prompts


def researched_audit(tmp_path, n):
    source, _, _ = setup_source(tmp_path)
    return AuditService(missing_resolver(), source).audit(ticket(*[selection(line=1 + i / 10) for i in range(n)]), NOW)


def test_thirty_researched_legs_analyzed_in_few_bounded_batches(tmp_path):
    audit = researched_audit(tmp_path, 30)
    c, prompts = batch_answering_client(tmp_path)
    result = EvidenceAnalyzer(c).analyze_ticket(audit, NOW)
    assert 1 <= len(prompts) <= EvidenceAnalyzer.MAX_BATCHES < 30
    assert all(len(p) <= EvidenceAnalyzer.MAX_PROMPT for p in prompts)
    sent = [leg['index'] for p in prompts for leg in json.loads(p[len(EvidenceAnalyzer.INSTRUCTIONS):])['legs']]
    assert sent == sorted(sent) == list(range(len(sent)))  # original order, no duplicates
    assert len(result.selections) == 30
    # Repeated source metadata is sent once per request, not once per fact.
    assert prompts[0].count('"source_url"') == 1 and 'source_ref' in prompts[0]
    unanalyzed = [a for a in result.selections if any('analysis not applied' in l for l in a.limitations)]
    assert len(sent) + len(unanalyzed) == 30


def test_analysis_batch_cap_leaves_rest_deterministic_with_limitation(tmp_path, monkeypatch):
    audit = researched_audit(tmp_path, 30)
    single = len(EvidenceAnalyzer(None)._prompt([{'index': 0, 'facts': {}, 'selection': {}, 'evidence_level': 'X',
                 'local_metrics': {}, 'limitations': (), 'provenance': ()}], NOW))
    monkeypatch.setattr(EvidenceAnalyzer, 'MAX_PROMPT', single + 6000)  # ~1-2 researched legs per batch
    c, prompts = batch_answering_client(tmp_path)
    result = EvidenceAnalyzer(c).analyze_ticket(audit, NOW)
    assert len(prompts) == EvidenceAnalyzer.MAX_BATCHES == 3
    skipped = [i for i, a in enumerate(result.selections)
               if 'Gemini analysis not applied: per-ticket analysis budget reached' in a.limitations]
    assert skipped and skipped == list(range(30 - len(skipped), 30))
    for i in skipped:
        assert result.selections[i].verdict == audit.selections[i].verdict


def test_leg_too_large_to_send_is_skipped_not_truncated(tmp_path, monkeypatch):
    audit = researched_audit(tmp_path, 3)
    monkeypatch.setattr(EvidenceAnalyzer, 'MAX_PROMPT', 100)
    c, prompts = batch_answering_client(tmp_path)
    result = EvidenceAnalyzer(c).analyze_ticket(audit, NOW)
    assert prompts == [] and c.transport.request.call_count == 0
    assert all('Gemini analysis not applied: leg evidence too large' in a.limitations for a in result.selections)


def test_bad_batches_keep_all_deterministic_results(tmp_path):
    audit = researched_audit(tmp_path, 30)
    c = free_client(tmp_path, json.dumps({'legs': []}))
    service_ = AuditService(missing_resolver(), setup_source(tmp_path)[0], analyzer=EvidenceAnalyzer(c))
    result = service_.audit(ticket(*[selection(line=1 + i / 10) for i in range(30)]), NOW)
    assert len(result.selections) == 30 and c.transport.request.call_count <= EvidenceAnalyzer.MAX_BATCHES
    assert any('Gemini analysis unavailable' in l for a in result.selections for l in a.limitations)
    assert [a.verdict for a in result.selections] == [a.verdict for a in audit.selections]


def test_local_snapshot_read_once_per_audit(local, monkeypatch):
    resolver, s, _ = local
    calls = []
    real = LocalResolver._records
    monkeypatch.setattr(LocalResolver, '_records', lambda self, now: calls.append(1) or real(self, now))
    result = AuditService(resolver).audit(ticket(*[s] * 30), NOW)
    assert len(calls) == 1 and all(a.evidence.local.status == 'EXACT' for a in result.selections)


# ------------------------------------------------------------------ research efficiency
def epl_fixture_legs(fixtures, markets=2):
    legs = []
    for f in range(fixtures):
        for m in range(markets):
            legs.append(selection(home=f'Home Club {f}', away=f'Away Club {f}', line=1.5 + m))
    return legs


def multi_fixture_source(legs):
    data = {'name': 'English Premier League 2029/30', 'matches': []}
    seen = set()
    for s in legs:
        if (s.home, s.away) not in seen:
            seen.add((s.home, s.away))
            data['matches'].extend(source_data(s)['matches'])
    return data


def test_24_legs_on_12_fixtures_one_download_all_researched(tmp_path):
    legs = epl_fixture_legs(12)
    source, _, t = setup_source(tmp_path, multi_fixture_source(legs))
    result = AuditService(missing_resolver(), source).audit(ticket(*legs), NOW)
    assert len(result.selections) == 24 and t.get.call_count == 1
    assert all(a.key_facts and a.evidence.research for a in result.selections)
    assert not any('budget exhausted' in lim for a in result.selections for lim in a.limitations)
    assert [a.normalized_selection.home for a in result.selections] == [s.home for s in legs]


def test_unsupported_large_ticket_makes_no_research_requests(tmp_path):
    legs = [selection(competition='Some Other League', home=f'H{i}', away=f'A{i}') for i in range(24)]
    source, _, t = setup_source(tmp_path)
    result = AuditService(missing_resolver(), source).audit(ticket(*legs), NOW)
    assert t.get.call_count == 0 and all(a.verdict == 'INSUFFICIENT_EVIDENCE' for a in result.selections)


# ------------------------------------------------------------------ combined odds
@pytest.mark.parametrize('raw,expected', [('1,234.56', 1234.56), ('16834.50', 16834.5), ('2.84', 2.84),
                                          (45.7, 45.7), (1234567, 1234567.0)])
def test_display_total_odds_parsed_and_preserved(store, raw, expected):
    result, r, _ = decode(store, share(24, total=raw))
    assert result.total_odds == expected and result.status == bk.OK
    assert r.last_diagnostic['combined_odds'] == 'parsed'
    t = bk.to_ticket(result)
    assert t.submitted_combined_odds == expected
    review = '\n'.join(audit_format.booking_review(t, result))
    assert f'Combined odds: {audit_format.odds(expected)}' in review


@pytest.mark.parametrize('raw', ['abc', '1,85', '1,850', '-3', '0.5', '1', 'NaN', 'inf', True, {}, [], '1e5', '12,34.5'])
def test_malformed_total_odds_null_without_invalidating_legs(store, raw):
    result, r, _ = decode(store, share(24, total=raw))
    assert result.total_odds is None and result.status == bk.OK and len(result.selections) == 24
    assert result.unresolved == ('Combined odds supplied by the bookmaker could not be read',)
    assert r.last_diagnostic['combined_odds'] == 'malformed'
    assert bk.to_ticket(result).submitted_combined_odds is None


def test_missing_total_odds_stays_null_never_computed(store):
    result, r, _ = decode(store, share(24, total=None))
    assert result.total_odds is None and result.unresolved == () and r.last_diagnostic['combined_odds'] == 'absent'


def test_stake_winnings_bonus_never_read_or_exposed(store):
    result, r, _ = decode(store, share(24))
    blob = json.dumps(asdict(result)) + json.dumps(r.last_diagnostic)
    review = '\n'.join(audit_format.booking_review(bk.to_ticket(result), result))
    for secret in ('987654.32', '777.00', 'REDACTED'):
        assert secret not in blob and secret not in review


def test_combined_odds_helper_bounds():
    assert bookmaker_total_odds('1,234.5678') == 1234.5678 and bookmaker_total_odds(' 3.10 ') == 3.1
    assert bookmaker_total_odds(None) is None and bookmaker_total_odds('1' * 14) is None


# ------------------------------------------------------------------ real market ids: never assumed
def test_real_unknown_ids_normalize_only_through_consistent_labels(store):
    result, r, _ = decode(store, share(6))
    a, b, c = result.selections[:3]
    assert (a.provider_market_id, a.market, a.period, a.outcome, a.line) == ('60020', 'total', 'full', 'over', 2.5)
    assert (b.provider_market_id, b.market, b.period, b.outcome) == ('60021', 'result3', 'full', 'home')
    assert (c.provider_market_id, c.market, c.period, c.outcome, c.line) == ('60022', 'total', '2h', 'under', 1.5)


@pytest.mark.parametrize('mid', ['60020', '60021', '60022'])
def test_real_unknown_ids_with_unknown_label_stay_unresolved(store, mid):
    body = share(1)
    body['data']['outcomes'][0] = leg_event(0, (mid, 'Mystery Market', '1', 'Something', ''))
    result = decode(store, body)[0]
    assert result.status == bk.PARTIAL and {'market', 'outcome'} <= set(result.selections[0].unresolved)


def test_known_id_conflicting_label_fails_closed_in_large_ticket(store):
    body = share(24)
    body['data']['outcomes'][10] = leg_event(10, ('68', '2nd Half - Over/Under', '12', 'Over 0.5', 'total=0.5'))
    result = decode(store, body)[0]
    assert result.status == bk.PARTIAL and len(result.selections) == 24
    assert 'market' in result.selections[10].unresolved
    assert all(not l.unresolved for i, l in enumerate(result.selections) if i != 10)


# ------------------------------------------------------------------ Telegram
@pytest.mark.parametrize('n', [24, 30])
def test_telegram_review_and_results_for_large_ticket(ui, db, n):
    before = db_dump(db)
    ui.transport.responses = [Resp(body=share(n))]
    s = code_prompt(ui)
    ui.controller.callback(tap(ui.controller, 'bk:sportybet'))
    assert s.state == 'PENDING_REVIEW'
    review_sends = [x for x in ui.tg.sends if 'Home 00' in x['text'] or 'Is this the right ticket?' in x['text']]
    assert all(len(x['text']) <= 3500 for x in ui.tg.sends)
    marked = [x for x in ui.tg.sends if x.get('reply_markup')]
    assert marked[-1]['text'].endswith('Is this the right ticket?')
    assert all(len(b['callback_data'].encode()) <= 64 for row in marked[-1]['reply_markup']['inline_keyboard'] for b in row)
    review = texts(ui)
    positions = [review.index(f'Home {i:02d} vs Away {i:02d}') for i in range(n)]
    assert positions == sorted(positions) and 'Combined odds: 1234.56' in review and review_sends
    start = len(ui.tg.sends)
    ui.controller.callback(tap(ui.controller, 'confirm'))
    results = ui.tg.sends[start:]
    out = '\n\n'.join(x['text'] for x in results)
    assert f'Ticket Audit\n\n{n} selections · Combined odds 1234.56' in out
    assert all(len(x['text']) <= 3500 for x in results) and len(results) <= 8
    pos = [out.index(f'Home {i:02d} vs Away {i:02d}') for i in range(n)]
    assert pos == sorted(pos) and not INTERNAL.search(review + out)
    assert len(ui.transport.calls) == 1 and db_dump(db) == before
    ui.client.transport.request.assert_not_called()


def test_telegram_31_leg_code_reaches_review(ui):
    ui.transport.responses = [Resp(body=share(31))]
    s = code_prompt(ui)
    ui.controller.callback(tap(ui.controller, 'bk:sportybet'))
    assert s.state == 'PENDING_REVIEW' and len(s.ticket.selections) == 31



def retrieval_diagnostics(caplog):
    return [json.loads(r.getMessage().split(' | diagnostics=', 1)[1])
            for r in caplog.records if r.name == bk.__name__ and
            r.getMessage().startswith('Booking-code retrieval |')]


def test_configured_service_24_legs_with_overlapping_ticket_views_reaches_telegram_audit(
        ui, db, tmp_path, monkeypatch, caplog):
    """Use the production service factory, not a parser-only or handcrafted route.

    This is a mock, not a captured production payload. Redundant collections must
    not get merged; the existing response contract reads data.outcomes only.
    """
    assert not hasattr(bk, "MAX_LEGS")
    assert booking_sources.finalize is bk.finalize
    before = db_dump(db)
    body = share(24)
    body['data']['ticket']['bets'] = [{'event': e} for e in body['data']['outcomes']]
    body['data']['ticket']['selections'] = [{'event': e} for e in body['data']['outcomes']]
    body['data']['shareCode'] = 'PRIVATE-PROVIDER-CODE'
    env = tmp_path / 'booking.env'
    env.write_text('AUDIT_BOOKING_CODES_ENABLED=true\nSPORTYBET_DIRECT_ENABLED=true\n'
                   'SPORTYBET_DIRECT_MAX_REQUESTS_PER_DAY=50\nMYBETCODE_ENABLED=false\n')
    for key in ('AUDIT_BOOKING_CODES_ENABLED', 'SPORTYBET_DIRECT_ENABLED',
                'SPORTYBET_DIRECT_MAX_REQUESTS_PER_DAY', 'MYBETCODE_ENABLED'):
        monkeypatch.delenv(key, raising=False)
    ui.cfg.env_file = str(env)
    ui.transport.responses = [Resp(body=body)]
    factory = Mock(side_effect=lambda: bk.build_booking_service(ui.cfg, transport=ui.transport))
    ui.controller.booking_factory = factory
    audit = Mock(wraps=ui.audit.audit)
    ui.audit.audit = audit
    caplog.set_level(logging.INFO, logger=bk.__name__)
    s = code_prompt(ui)
    ui.controller.callback(tap(ui.controller, 'bk:sportybet'))
    assert factory.call_count == 1 and s.state == 'PENDING_REVIEW'
    assert s.booking.status == bk.OK and len(s.booking.selections) == len(s.ticket.selections) == 24
    expected = [f'Home {i:02d}' for i in range(24)]
    assert [l.home for l in s.booking.selections] == [l.home for l in s.ticket.selections] == expected
    review = texts(ui)
    positions = [review.index(f'Home {i:02d} vs Away {i:02d}') for i in range(24)]
    assert positions == sorted(positions)
    audit.assert_not_called()
    ui.controller.callback(tap(ui.controller, 'confirm'))
    assert s.state == 'COMPLETE' and audit.call_count == 1
    assert [l.home for l in audit.call_args.args[0].selections] == expected
    assert len(ui.transport.calls) == 1 and db_dump(db) == before
    ui.client.transport.request.assert_not_called()
    diagnostic, = retrieval_diagnostics(caplog)
    assert diagnostic['raw_outcome_count'] == diagnostic['decoded_selection_count'] == diagnostic['normalized_selection_count'] == 24
    assert diagnostic['ticket_bet_count'] == diagnostic['ticket_selection_count'] == 24
    assert not {'max_selections', 'max_legs', 'resolver_max_legs'} & diagnostic.keys()
    assert diagnostic['booking_module'] == bk.__file__ and diagnostic['resolver_module'] == booking_sources.__file__
    assert diagnostic['stage'] == 'finalized' and diagnostic['http_attempted'] and diagnostic['http_status'] == 200
    assert 'AB12CD' not in caplog.text and 'PRIVATE-PROVIDER-CODE' not in caplog.text and 'Home 00' not in caplog.text


@pytest.mark.parametrize('origin', ['body_size', 'depth'])
def test_resource_failure_logs_boundary_without_payloads(store, caplog, origin):
    if origin == 'body_size':
        body = {'bizCode': 10000, 'data': {'outcomes': [], 'unused': 'x' * 512001}}
    else:
        nested = []
        for _ in range(20): nested = [nested]
        body = {'bizCode': 10000, 'data': {'outcomes': [], 'unused': nested}}
    caplog.set_level(logging.INFO, logger=bk.__name__)
    result, _, _ = decode(store, body, code='SECRET123')
    assert result.status == (bk.RESPONSE_TOO_LARGE if origin == 'body_size' else bk.PARSER_LIMIT)
    diagnostic, = retrieval_diagnostics(caplog)
    assert diagnostic['rejection_function'] == 'fetch' and diagnostic['http_status'] == 200
    assert 'SECRET123' not in caplog.text



def test_booking_failure_cache_diagnostic_marks_no_new_http(store, caplog):
    resolver, transport = direct(store, Resp(body={'bizCode': 10000, 'data': None}))
    svc = service(store, resolver)
    caplog.set_level(logging.INFO, logger=bk.__name__)
    svc.resolve('sportybet', 'CACHEFAIL', NOW)
    second = svc.resolve('sportybet', 'CACHEFAIL', NOW)
    assert second.status == bk.NOT_FOUND and second.cached and len(transport.calls) == 1
    cached_record, = [r for r in caplog.records if r.getMessage().startswith('Booking-code cache hit |')]
    diagnostic = json.loads(cached_record.getMessage().split(' | diagnostics=', 1)[1])
    assert diagnostic['cached'] and not diagnostic['http_attempted']



def test_booking_diagnostics_are_request_local_for_concurrent_codes(store, monkeypatch, caplog):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    barrier = Barrier(2)
    real = booking_sources.finalize

    def interleaved_finalize(*args, **kwargs):
        barrier.wait(timeout=5)
        return real(*args, **kwargs)

    monkeypatch.setattr(booking_sources, 'finalize', interleaved_finalize)
    resolver, _ = direct(store, Resp(body=share(24)), Resp(body=share(30)))
    svc = service(store, resolver)
    caplog.set_level(logging.INFO, logger=bk.__name__)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(svc.resolve, 'sportybet', code, NOW) for code in ('FIRST24', 'NEXT30')]
        assert sorted(len(f.result(timeout=10).selections) for f in futures) == [24, 30]
    diagnostics = retrieval_diagnostics(caplog)
    assert sorted(d['raw_outcome_count'] for d in diagnostics) == [24, 30]
    assert all(d['raw_outcome_count'] == d['decoded_selection_count'] == d['normalized_selection_count']
               for d in diagnostics)


@pytest.mark.parametrize('n', [1, 10, 24, 32, 50, 100])
def test_configured_large_sportybet_pipeline_review_confirmation_and_complete_result(
        ui, db, tmp_path, monkeypatch, caplog, n):
    before = db_dump(db)
    env = tmp_path / 'booking.env'
    env.write_text('AUDIT_BOOKING_CODES_ENABLED=true\nSPORTYBET_DIRECT_ENABLED=true\nSPORTYBET_DIRECT_MAX_REQUESTS_PER_DAY=50\nMYBETCODE_ENABLED=false\n')
    for key in ('AUDIT_BOOKING_CODES_ENABLED', 'SPORTYBET_DIRECT_ENABLED', 'SPORTYBET_DIRECT_MAX_REQUESTS_PER_DAY', 'MYBETCODE_ENABLED'):
        monkeypatch.delenv(key, raising=False)
    ui.cfg.env_file = str(env)
    body = share(n)
    events = body['data']['outcomes']
    body['data']['ticket']['bets'] = []
    body['data']['unavailableOutcomes'] = []
    body['data']['ticket']['selections'] = [{'eventId': e['eventId'], 'marketId': e['markets'][0]['id'],
        'specifier': e['markets'][0]['specifier'], 'outcomeId': e['markets'][0]['outcomes'][0]['id']} for e in events]
    ui.transport.responses = [Resp(body=body)]
    ui.controller.booking_factory = Mock(side_effect=lambda: bk.build_booking_service(ui.cfg, transport=ui.transport))
    audit = Mock(wraps=ui.audit.audit)
    ui.audit.audit = audit
    caplog.set_level(logging.INFO)
    s = code_prompt(ui)
    start = len(ui.tg.sends)
    ui.controller.callback(tap(ui.controller, 'bk:sportybet'))
    assert s.state == 'PENDING_REVIEW' and s.booking.status == bk.OK
    assert len(s.booking.selections) == len(s.ticket.selections) == n
    expected = [f'Home {i:02d}' for i in range(n)]
    assert [l.home for l in s.ticket.selections] == expected
    sends = ui.tg.sends[start:]
    review = '\n\n'.join(x['text'] for x in sends)
    positions = [review.index(f'{name} vs Away {i:02d}') for i, name in enumerate(expected)]
    assert positions == sorted(positions) and all(len(x['text']) <= 3500 for x in sends)
    assert all(not x.get('reply_markup') for x in sends[:-1]) and sends[-1]['reply_markup']
    audit.assert_not_called()
    start = len(ui.tg.sends)
    ui.controller.callback(tap(ui.controller, 'confirm'))
    assert s.state == 'COMPLETE' and audit.call_count == 1
    assert [l.home for l in audit.call_args.args[0].selections] == expected
    result = '\n\n'.join(x['text'] for x in ui.tg.sends[start:])
    assert f'{n} selections' in result and 'Insufficient Evidence' in result
    positions = [result.index(f'{name} vs Away {i:02d}') for i, name in enumerate(expected)]
    assert positions == sorted(positions)
    assert all(len(x['text']) <= 3500 for x in ui.tg.sends[start:])
    assert len(ui.transport.calls) == 1 and db_dump(db) == before
    ui.client.transport.request.assert_not_called()
    diagnostic, = retrieval_diagnostics(caplog)
    assert diagnostic['raw_selection_count'] == diagnostic['decoded_selection_count'] == diagnostic['normalized_selection_count'] == n
    assert diagnostic['stage'] == 'finalized'
    assert 'SportyBet structural diagnostic' not in caplog.text
    assert 'AB12CD' not in caplog.text and 'Home 00' not in caplog.text



def test_sportybet_diagnostics_separate_containers_outcomes_and_unverified_references(store, caplog):
    body = share(16)
    events = body['data']['outcomes']
    for e in events[:8]:
        e['markets'][0]['outcomes'].append({'id': '2', 'desc': 'Draw', 'odds': '3.1', 'isActive': 1})
    events[0]['markets'][0]['outcomes'][0]['isSelected'] = True
    events[0]['markets'][0]['outcomes'][1]['isSelected'] = False
    # Synthetic references test diagnostics only; no claim about the real contract.
    body['data']['ticket']['selections'] = [
        {'eventId': 'anonymous-event', 'marketId': '1', 'outcomeId': '1'},
        {'eventId': 'anonymous-event', 'marketId': '1'},
        {'eventId': True, 'marketId': '1', 'outcomeId': '1'}]
    body['data']['unavailableOutcomes'] = [{}] * 8
    caplog.set_level(logging.INFO, logger=bk.__name__)
    result, resolver, _ = decode(store, body)
    diagnostic, = retrieval_diagnostics(caplog)
    assert diagnostic['raw_event_count'] == diagnostic['raw_market_count'] == 16
    assert diagnostic['raw_nested_outcome_count'] == 24
    assert diagnostic['ticket_selection_count'] == 3 and diagnostic['ticket_complete_reference_count'] == 1
    assert diagnostic['unavailable_outcome_count'] == 8 and diagnostic['explicit_selected_flag_count'] == 1
    assert diagnostic['selected_selection_count'] is diagnostic['unavailable_selected_count'] is None
    # New diagnostics cannot change filtering, silently drop legs or guess membership.
    assert len(result.selections) == diagnostic['decoded_selection_count'] == 24
    assert resolver.last_diagnostic['raw_nested_outcome_count'] == 24
    assert 'anonymous-event' not in caplog.text and 'Home 00' not in caplog.text


def test_sportybet_structure_diagnostics_tolerate_malformed_containers(store, caplog):
    body = share(1)
    body['data']['outcomes'].extend([None, {'markets': [None, {'outcomes': [None, 1]}]}])
    body['data']['ticket'] = {'selections': 'not-a-list', 'bets': None}
    body['data']['unavailableOutcomes'] = {'unknown': 'shape'}
    caplog.set_level(logging.INFO, logger=bk.__name__)
    result, _, _ = decode(store, body)
    diagnostic, = retrieval_diagnostics(caplog)
    assert result.status == bk.PARTIAL and len(result.selections) == 3
    assert diagnostic['raw_event_count'] == diagnostic['raw_market_count'] == diagnostic['raw_nested_outcome_count'] == 3
    assert diagnostic['ticket_complete_reference_count'] is diagnostic['unavailable_outcome_count'] is None
    assert diagnostic['explicit_selected_flag_count'] == 0  # isActive is not isSelected


def test_results_pages_bounded_and_complete_for_thirty_long_legs():
    s = selection(home='Long Home ' + 'x' * 170, away='Long Away ' + 'y' * 170)
    from tests.test_ticket_audit_telegram import leg
    base = AuditService(missing_resolver()).audit(ticket(), NOW)
    a = replace(base, selections=tuple(replace(leg(None), normalized_selection=s) for _ in range(30)))
    pages = audit_format.results(ticket(*[s] * 30), a)
    assert all(len(p) <= 3500 for p in pages) and '\n'.join(pages).count('Long Home') == 30 + 1  # +1 weakest footer
