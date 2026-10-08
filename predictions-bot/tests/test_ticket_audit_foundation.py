"""No live HTTP. Every test runs with sports/generation entry points forbidden."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import timedelta
import importlib
import json
from pathlib import Path
import threading
from unittest.mock import Mock

import pytest
import requests

from predbot.model.candidates import capture_candidate
from predbot.ticket_audit.gemini import GeminiClient, Settings, UsageGuard
from predbot.ticket_audit.model_access import LEGACY_FREE_SEARCH_MODEL as MODEL
from predbot.ticket_audit.local import LocalResolver
from predbot.ticket_audit.research import (ResearchCache, Researcher, SourcePolicy, parse_research,
    research_key, research_level, research_prompt, research_ttl, FIXTURE_FIELDS, SELECTION_FIELDS)
from predbot.ticket_audit.schemas import (AuditUnavailable, NormalizedSelection, NormalizedTicket,
    ResearchEvidence, ResearchFact, ticket_from_json)
from predbot.ticket_audit.service import AuditService, local_level
from tests.test_custom_pick_candidates import alternatives, persist, DAY, ZONE, NOW


def forbidden(*a, **k):
    pytest.fail('Ticket Audit invoked a forbidden provider/generation/write entry point')


@pytest.fixture(autouse=True)
def no_providers(db, monkeypatch):
    # Retain enabled-mode coverage while the production feature is paused.
    from predbot import ticket_audit_switch
    monkeypatch.setattr(ticket_audit_switch, 'ENABLED', True)
    from predbot.storage.db import Database
    from predbot.providers.chain import ProviderChain, ResultsResolver
    from predbot.model.ensemble import EventModel
    from predbot.cache.fetcher import UnifiedFetcher
    from predbot.jobs import daily_picks
    from predbot.model import engine
    for obj, method in ((Database, '__init__'), (ProviderChain, 'collect'), (ResultsResolver, 'resolve'),
                        (EventModel, 'score'), (EventModel, 'score_all'), (UnifiedFetcher, 'get_daily_snapshot'),
                        (daily_picks, 'build_picks'), (daily_picks, 'store_picks'), (engine, 'generate')):
        monkeypatch.setattr(obj, method, forbidden)
    for module, cls in (('oddsapi', 'OddsApiProvider'), ('sportsgameodds', 'SportsGameOddsProvider'),
                        ('sharpapi', 'SharpApiProvider'), ('espn', 'ESPNProvider'), ('football_data', 'FootballDataProvider')):
        klass = getattr(importlib.import_module('predbot.providers.' + module), cls)
        for method in ('fetch_events', 'fetch_result'):
            if hasattr(klass, method):
                monkeypatch.setattr(klass, method, forbidden)
    monkeypatch.setattr(requests.Session, 'request', forbidden)


def selection(**kw):
    return replace(NormalizedSelection(sport='football', competition='England Premier League', home='Arsenal', away='Chelsea',
        kickoff='2030-06-01T15:00:00+00:00', market_family='total', selection='over', line=2.5,
        period='full', stat='points', entity='all'), **kw)


def ticket(*legs):
    return NormalizedTicket('normalized', None, None, None, tuple(legs or [selection()]), 'HIGH')


@pytest.fixture
def local(db):
    records = [capture_candidate(s) for s in alternatives()]
    persist(db, records)
    # Actual fixture league/coordinates, not assumptions in test helper.
    r = next(r for r in records if r['market'] == 'total' and r['selection'] == 'over' and r['line'] == 2.5)
    s = selection(competition=r['league'], period=r['period'], stat=r['stat'], entity=r['entity'])
    return LocalResolver(db.path, ZONE), s, r


def client(tmp_path, response=None, error=None, settings=None):
    transport = Mock()
    if error:
        transport.request.side_effect = error
    else:
        transport.request.return_value = Mock(status_code=200, json=Mock(return_value=response))
    usage = UsageGuard(tmp_path / 'audit-usage.sqlite3')
    c = GeminiClient(settings or Settings('SECRET-ONLY-HEADER', MODEL, True, True), usage, transport, lambda: NOW)
    return c


def response(text, grounding=None, reason='STOP'):
    return {'candidates': [{'finishReason': reason, 'content': {'parts': [{'text': text}]},
                            'groundingMetadata': grounding or {}}], 'usageMetadata': {'totalTokenCount': 30}}


def grounded_payload(s=None):
    urls = ('https://premierleague.com/news/test', 'https://bbc.com/sport/test', 'https://espn.com/soccer/test')
    facts = [{'category': 'form', 'claim': f'Test verified result {i}', 'value': str(i),
              'source_url': url, 'source_title': 'untrusted model title', 'observed_at': NOW.isoformat()}
             for i, url in enumerate(urls)]
    coords = (s or selection()).coordinates()
    data = {'fixture': {k: coords[k] for k in FIXTURE_FIELDS}, 'selection': {k: coords[k] for k in SELECTION_FIELDS},
            'facts': facts, 'interpretations': ['These reported results give limited qualitative support'],
            'limitations': [], 'verdict': 'REASONABLE'}
    grounding = {'groundingChunks': [{'web': {'uri': url, 'title': f'Actual source {i}'}} for i, url in enumerate(urls)],
                 'groundingSupports': [{'segment': {'text': f['claim']}, 'groundingChunkIndices': [i]}
                                       for i, f in enumerate(facts)], 'webSearchQueries': ['test sports form'],
                 'searchEntryPoint': {'renderedContent': '<div>Google Search attribution</div>'}}
    return data, grounding


def evidence(s=None, dynamic=False):
    data, g = grounded_payload(s)
    if dynamic:
        data['facts'][0]['category'] = 'injury'
    return parse_research(json.dumps(data), g, s or selection(), MODEL, NOW)


def db_dump(db):
    return tuple(db.conn.iterdump())


def test_exact_archive_metrics_preserved_and_every_database_table_unchanged(local, db):
    resolver, s, r = local
    before = db_dump(db)
    result = AuditService(resolver).audit(ticket(s), NOW).selections[0]
    assert result.local_probability == r['p_model']
    assert result.market_probability == r['p_market']
    assert result.evidence.local.metrics['ev'] == r['ev']
    assert result.evidence.local.metrics['p_raw'] == r['p_raw']
    assert result.evidence.local.metrics['notes'] == r['notes']
    assert result.evidence.local.provenance == ('custom_pick_candidates',)
    assert result.safer_alternative is None
    assert db_dump(db) == before  # memberships, watches, stats, caches, all/top/daily, history, schema


@pytest.mark.parametrize('changes', [dict(line=3.0), dict(line=2.50001), dict(market_family='handicap'),
    dict(selection='yes'), dict(period='first'), dict(stat='corners'), dict(entity='home'),
    dict(home='Arsenal U21'), dict(away='Chelsea Women'), dict(home='Arsenal Reserves'),
    dict(kickoff='2030-06-02T15:00:00Z'), dict(canonical_event_id='wrong'), dict(competition='Other League'),
    dict(sport='basketball'), dict(kickoff=None), dict(period=None)])
def test_no_wrong_fixture_or_coordinate_matches(local, changes):
    resolver, s, _ = local
    assert resolver.resolve(replace(s, **changes), NOW).status != 'EXACT'


def test_decimal_equivalence_and_normalized_names(local):
    resolver, s, _ = local
    assert resolver.resolve(replace(s, line=2.50, home='  ARSENAL  '), NOW).status == 'EXACT'


def test_handicap_signed_lines_are_exact(local):
    resolver, s, _ = local
    assert resolver.resolve(replace(s, market_family='handicap', selection='home', line=-1.5), NOW).status == 'EXACT'
    assert resolver.resolve(replace(s, market_family='handicap', selection='home', line=-1), NOW).status == 'MISSING'


def test_ambiguous_kickoffs_do_not_guess(local, db):
    resolver, s, r = local
    persist(db, [{**r, 'canonical_event_id': 'evt-new', 'start_utc': '2030-06-01T15:05:00Z'}])
    assert resolver.resolve(s, NOW).status == 'AMBIGUOUS'
    assert resolver.resolve(replace(s, canonical_event_id='evt-1'), NOW).status == 'EXACT'


def test_missing_file_does_not_create_database(tmp_path):
    p = tmp_path / 'missing.sqlite'
    assert LocalResolver(p).resolve(selection(), NOW).status == 'UNAVAILABLE'
    assert not p.exists()


def test_old_archive_not_todays_evidence(local):
    resolver, s, _ = local
    assert resolver.resolve(s, NOW + timedelta(days=1)).status == 'MISSING'


def test_no_remote_calls_for_exact_local_even_with_researcher(local, tmp_path):
    resolver, s, _ = local
    c = client(tmp_path, error=AssertionError('no Gemini'))
    AuditService(resolver, Researcher(c)).audit(ticket(s), NOW)
    c.transport.request.assert_not_called()


def test_no_remote_calls_for_incomplete_or_ambiguous_input(local, db, tmp_path):
    resolver, s, r = local
    c = client(tmp_path)
    persist(db, [{**r, 'canonical_event_id': 'evt-new', 'start_utc': '2030-06-01T15:05:00Z'}])
    AuditService(resolver, Researcher(c)).audit(ticket(s, replace(s, kickoff=None)), NOW)
    c.transport.request.assert_not_called()


@pytest.mark.parametrize('status,code', [(429, 'gemini_rate_limited'), (401, 'key_or_capability_rejected'),
    (403, 'key_or_capability_rejected'), (404, 'model_unavailable'), (500, 'gemini_service_unavailable'),
    (503, 'gemini_service_unavailable'), (302, 'gemini_request_rejected'), (400, 'gemini_request_rejected')])
def test_remote_failure_never_falls_back_or_retries(db, tmp_path, status, code):
    c = client(tmp_path)
    c.transport.request.return_value.status_code = status
    a = AuditService(LocalResolver(db.path), Researcher(c)).audit(ticket(), NOW).selections[0]
    assert code in a.limitations
    assert a.local_probability is None and a.verdict == 'INSUFFICIENT_EVIDENCE'
    assert c.transport.request.call_count == 1
    assert c.usage.snapshot(NOW)['failures'] == 1
    assert c.usage.snapshot(NOW)['rate_limits'] == int(status == 429)


@pytest.mark.parametrize('settings,reason', [(Settings(), 'gemini_disabled'),
    (Settings('', MODEL, True, True), 'missing_key'), (Settings('x', MODEL, True, False), 'free_tier_unconfirmed'),
    (Settings('x', 'paid-model', True, True), 'unsupported_free_model')])
def test_capability_gates_make_zero_calls(db, tmp_path, settings, reason):
    c = client(tmp_path, settings=settings)
    a = AuditService(LocalResolver(db.path), Researcher(c)).audit(ticket(), NOW).selections[0]
    assert reason in a.limitations
    c.transport.request.assert_not_called()


@pytest.mark.parametrize('error,reason', [(requests.Timeout('SECRET'), 'gemini_timeout'),
    (requests.ConnectionError('SECRET'), 'gemini_transport_or_json_failure'), (ValueError('SECRET'), 'gemini_transport_or_json_failure')])
def test_timeout_and_transport_fail_closed_and_redacted(db, tmp_path, error, reason):
    c = client(tmp_path, error=error)
    result = AuditService(LocalResolver(db.path), Researcher(c)).audit(ticket(), NOW)
    assert reason in result.selections[0].limitations
    assert 'SECRET' not in repr(result)
    assert c.transport.request.call_count == 1


def test_research_only_has_no_model_probability_and_keeps_provenance(db, tmp_path):
    data, g = grounded_payload()
    c = client(tmp_path, response(json.dumps(data), g))
    a = AuditService(LocalResolver(db.path), Researcher(c)).audit(ticket(), NOW).selections[0]
    assert a.evidence_level == 'RESEARCHED' and a.verdict == 'REASONABLE'
    assert a.local_probability is None and a.market_probability is None and a.safer_alternative is None
    assert a.key_facts[0].source_title == 'Actual source 0'
    assert a.evidence.research.grounding_metadata == g
    assert 'Injury/lineup availability UNKNOWN' in a.limitations


@pytest.mark.parametrize('invalid', ['invalid json', '{}', '{"p_model":0.99}', 'null', '[]'])
def test_malformed_research_fail_closed(db, tmp_path, invalid):
    c = client(tmp_path, response(invalid, grounded_payload()[1]))
    a = AuditService(LocalResolver(db.path), Researcher(c)).audit(ticket(), NOW).selections[0]
    assert 'malformed_research_output' in a.limitations
    assert c.transport.request.call_count == 1


@pytest.mark.parametrize('extra', ['p_model', 'safer_alternative', 'probability', 'local_probability'])
def test_gemini_cannot_supply_local_metrics_or_alternatives(extra):
    d, g = grounded_payload()
    d[extra] = 0.99
    with pytest.raises(AuditUnavailable):
        parse_research(json.dumps(d), g, selection(), MODEL, NOW)


@pytest.mark.parametrize('kind', ['empty', 'no_sources', 'no_supports', 'wrong_source', 'unsupported_claim'])
def test_unverified_provenance_cannot_establish_facts(kind):
    d, g = grounded_payload()
    if kind == 'empty':
        g = {}
    elif kind == 'no_sources':
        g['groundingChunks'] = []
    elif kind == 'no_supports':
        g['groundingSupports'] = []
    elif kind == 'wrong_source':
        for f in d['facts']:
            f['source_url'] = 'https://evil.example/fabricated'
    else:
        for f in d['facts']:
            f['claim'] = 'Unsupported fabricated injury'
    with pytest.raises(AuditUnavailable):
        parse_research(json.dumps(d), g, selection(), MODEL, NOW)


def test_contradictions_and_low_trust_reduce_completeness():
    re = evidence()
    assert research_level(re, NOW) == 'RESEARCHED'
    assert research_level(replace(re, limitations=('Conflicting reports remain unresolved',)), NOW) == 'LIMITED'
    assert research_level(replace(re, facts=tuple(replace(f, source_quality='LOW') for f in re.facts)), NOW) == 'INSUFFICIENT'
    assert research_level(replace(re, facts=re.facts[:1]), NOW) == 'LIMITED'
    assert research_level(re, NOW + timedelta(hours=2)) == 'INSUFFICIENT'


@pytest.mark.parametrize('url,tier', [('https://nba.com/news', 'A'), ('https://www.nba.com/news', 'A'),
    ('https://nba.com.evil.example', 'LOW'), ('https://fake-nba.com', 'LOW'), ('http://nba.com', 'LOW'),
    ('https://u:password@nba.com', 'LOW'), ('https://affiliate.example/tips', 'LOW'), ('https://bbc.com/sport', 'B')])
def test_domain_classifier_is_conservative_and_extensible(url, tier):
    assert SourcePolicy().classify(url) == tier
    assert SourcePolicy({'verified-club.example': 'A', 'reviewed-reporting.example': 'C'}).classify('https://verified-club.example/news') == 'A'


@pytest.mark.parametrize('sport,term', [('football', 'home/away'), ('tennis', 'surface'), ('basketball', 'back-to-back'),
    ('baseball', 'pitchers'), ('american_football', 'quarterback'), ('mma', 'weight class')])
def test_sport_specific_prompts_no_raw_text_identity_or_probability_request(sport, term):
    s = selection(sport=sport, raw_text='PRIVATE ACCOUNT ID')
    p = research_prompt(s, NOW)
    assert term in p and 'PRIVATE ACCOUNT ID' not in p
    assert 'No invented probabilities' in p


@pytest.mark.parametrize('market,term', [('btts', 'both-team'), ('total', 'EXACT submitted line'),
    ('handicap', 'signed line'), ('result3', 'win/draw'), ('dnb', 'refund'), ('dc', 'loss avoidance')])
def test_market_specific_prompts(market, term):
    assert term in research_prompt(selection(market_family=market), NOW)


@pytest.mark.parametrize('changes', [dict(line=3.5), dict(line=-1.5), dict(home='Other'), dict(away='Other'),
    dict(canonical_event_id='different'), dict(kickoff='2030-06-02T15:00:00Z'), dict(market_family='btts'),
    dict(selection='under'), dict(period='half'), dict(stat='corners'), dict(entity='home'), dict(competition='Other')])
def test_cache_keys_never_collide(changes):
    assert research_key(selection(), MODEL) != research_key(selection(**changes), MODEL)


def test_equivalent_decimal_and_utc_keys():
    assert research_key(selection(), MODEL) == research_key(selection(line=2.50, kickoff='2030-06-01T16:00:00+01:00'), MODEL)
    assert research_key(selection(), MODEL) != research_key(selection(), 'new-model')


def test_cache_hit_avoids_gemini_and_detaches_mutable_data(tmp_path):
    d, g = grounded_payload()
    c = client(tmp_path, response(json.dumps(d), g))
    researcher = Researcher(c)
    first = researcher.research(selection(), NOW)
    first.grounding_metadata.clear()
    second = researcher.research(selection(), NOW + timedelta(seconds=1))
    assert second.grounding_metadata == g
    assert c.transport.request.call_count == 1
    assert c.usage.snapshot(NOW)['cache_hits'] == 1


def test_dynamic_ttls_and_no_started_reuse():
    for delta, historical, news in [(48, 3600, 600), (10, 900, 300), (1, 120, 120)]:
        s = selection(kickoff=(NOW + timedelta(hours=delta)).isoformat())
        assert research_ttl(evidence(s), NOW) == historical
        assert research_ttl(evidence(s, dynamic=True), NOW) == news
    assert research_ttl(evidence(), NOW + timedelta(hours=3)) == 0


def test_cache_expiry_lru_and_no_future_timestamp():
    cache = ResearchCache(maximum=1)
    cache.get_or_load('one', NOW, evidence)
    assert cache.peek('one', NOW + timedelta(seconds=900)) is None
    cache.get_or_load('two', NOW, evidence)
    cache.get_or_load('three', NOW, evidence)
    assert cache.peek('two', NOW) is None and cache.peek('three', NOW - timedelta(seconds=1)) is None


def test_twenty_simultaneous_audits_single_flight(tmp_path):
    d, g = grounded_payload()
    c = client(tmp_path)
    started, release = threading.Event(), threading.Event()
    def request(*a, **k):
        started.set()
        assert release.wait(5)
        return Mock(status_code=200, json=Mock(return_value=response(json.dumps(d), g)))
    c.transport.request.side_effect = request
    researcher = Researcher(c)
    with ThreadPoolExecutor(max_workers=20) as workers:
        futures = [workers.submit(researcher.research, selection(), NOW) for _ in range(20)]
        assert started.wait(3)
        release.set()
        assert all(f.result(timeout=5).facts for f in futures)
    assert c.transport.request.call_count == 1
    assert not researcher.cache.flights


def test_single_flight_wait_is_bounded_and_failure_cleans_up():
    cache = ResearchCache(wait_seconds=.01)
    started, release = threading.Event(), threading.Event()
    def load():
        started.set()
        assert release.wait(3)
        raise AuditUnavailable('controlled_failure')
    with ThreadPoolExecutor(max_workers=2) as workers:
        owner = workers.submit(cache.get_or_load, 'key', NOW, load)
        assert started.wait(3)
        with pytest.raises(AuditUnavailable, match='research_wait_timeout'):
            cache.get_or_load('key', NOW, evidence)
        release.set()
        with pytest.raises(AuditUnavailable, match='controlled_failure'):
            owner.result(timeout=3)
    assert not cache.flights
    with pytest.raises(AuditUnavailable, match='controlled_failure'):
        cache.get_or_load('key', NOW, evidence)
    assert cache.get_or_load('key', NOW + timedelta(seconds=31), evidence).facts


def test_three_requests_maximum_for_twenty_leg_ticket(db, tmp_path):
    d, g = grounded_payload()
    c = client(tmp_path, response(json.dumps(d), g))
    def transport_request(*a, **k):
        body = k['json']['contents'][0]['parts'][0]['text']
        coords = json.loads(body.split('coordinates:\n', 1)[1].split('\nSport focus:', 1)[0])
        leg = NormalizedSelection(**coords)
        data, grounding = grounded_payload(leg)
        return Mock(status_code=200, json=Mock(return_value=response(json.dumps(data), grounding)))
    c.transport.request.side_effect = transport_request
    legs = [selection(home=f'Team {i}') for i in range(20)]
    audit = AuditService(LocalResolver(db.path), Researcher(c)).audit(ticket(*legs), NOW)
    assert c.transport.request.call_count == 3
    assert sum('Per-ticket research budget exhausted' in a.limitations for a in audit.selections) == 17


def test_local_quota_persists_across_restart_and_failed_requests(db, tmp_path):
    c = client(tmp_path, error=requests.Timeout(), settings=Settings('x', MODEL, True, True, 1))
    service = AuditService(LocalResolver(db.path), Researcher(c))
    service.audit(ticket(), NOW)
    c.usage = UsageGuard(tmp_path / 'audit-usage.sqlite3')
    a = service.audit(ticket(), NOW + timedelta(seconds=31)).selections[0]
    assert 'local_quota_exhausted' in a.limitations
    assert c.transport.request.call_count == 1
    assert c.usage.snapshot(NOW)['requests'] == 1


def test_atomic_quota_across_instances_and_pacific_reset(tmp_path):
    p = tmp_path / 'ledger.sqlite'
    guards = [UsageGuard(p) for _ in range(20)]
    def reserve(g):
        try:
            g.reserve(3, True, NOW)
            return True
        except AuditUnavailable:
            return False
    with ThreadPoolExecutor(max_workers=20) as pool:
        assert sum(pool.map(reserve, guards)) == 3
    assert guards[0].snapshot(NOW)['grounded'] == 3
    guards[0].reserve(3, False, NOW + timedelta(days=1))
    assert guards[0].snapshot(NOW + timedelta(days=1))['requests'] == 1


def test_usage_guard_never_modifies_prediction_schema(db):
    before = db_dump(db)
    with pytest.raises(AuditUnavailable, match='usage_path_must_be_separate'):
        UsageGuard(db.path)
    assert db_dump(db) == before


def extraction_json(input_type='text'):
    d = asdict(ticket(NormalizedSelection(home='Arsenal', away='Chelsea', line=2.5)))
    d['input_type'] = input_type
    d['selections'] = [asdict(s) for s in ticket(NormalizedSelection(home='Arsenal', away='Chelsea', line=2.5)).selections]
    d['extraction_warnings'] = []
    return d


@pytest.mark.parametrize('kind', ['text', 'image'])
def test_extraction_preserves_nulls_and_no_search(kind, tmp_path):
    from diagnose_gemini_audit import synthetic_image
    c = client(tmp_path, response(json.dumps(extraction_json(kind))))
    ticket = c.extract(text='test' if kind == 'text' else None, image=('image/png', synthetic_image()) if kind == 'image' else None)
    s = ticket.selections[0]
    assert s.sport is None and s.kickoff is None and s.competition is None and s.period is None
    body = c.transport.request.call_args.kwargs['json']
    assert 'tools' not in body and body['generationConfig']['responseFormat']['text']['mimeType'] == 'APPLICATION_JSON'
    assert c.transport.request.call_args.kwargs['headers'] == {'x-goog-api-key': 'SECRET-ONLY-HEADER'}
    assert 'SECRET' not in c.transport.request.call_args.args[1] and 'SECRET' not in repr(c.settings)
    assert c.transport.request.call_args.kwargs['allow_redirects'] is False


def test_json_contract_deterministic_adapter_needs_no_gemini():
    assert ticket_from_json(json.dumps(extraction_json())).selections[0].kickoff is None


@pytest.mark.parametrize('mutation', ['extra', 'probability', 'bad_date', 'bool_odds', 'infinite_line', 'unknown_verdict'])
def test_strict_extraction_rejects_untrusted_values(mutation):
    d = extraction_json()
    if mutation in ('extra', 'probability'):
        d['selections'][0][mutation] = .8
    elif mutation == 'bad_date':
        d['selections'][0]['kickoff'] = 'tomorrow'
    elif mutation == 'bool_odds':
        d['submitted_combined_odds'] = True
    elif mutation == 'infinite_line':
        d['selections'][0]['line'] = float('inf')
    else:
        d['extraction_confidence'] = 'CONFIDENT'
    with pytest.raises(AuditUnavailable):
        ticket_from_json(d)


@pytest.mark.parametrize('reason', ['SAFETY', 'MAX_TOKENS', 'RECITATION'])
def test_safety_and_incomplete_output_fail_closed(tmp_path, reason):
    c = client(tmp_path, response('ignored', reason=reason))
    with pytest.raises(AuditUnavailable, match='incomplete_extraction' if reason == 'MAX_TOKENS' else 'safety_refusal'):
        c.extract('test')


def test_evidence_levels_use_actual_metrics_not_gemini(local):
    resolver, s, _ = local
    match = resolver.resolve(s, NOW)
    assert local_level(match, NOW) == 'GOOD_LOCAL'
    rich = replace(match, metrics={**match.metrics, 'p_independent': .7, 'p_model_basis': 'independent',
                                  'bookmaker_count': 3, 'fair_books': ['reference'], 'edge_ev': .05})
    assert local_level(rich, NOW) == 'STRONG_LOCAL'
    assert local_level(replace(match, metrics={**match.metrics, 'p_model': None}), NOW) == 'LIMITED'


def test_canonical_id_can_resolve_unknown_names_without_guessing(local):
    resolver, s, _ = local
    match = resolver.resolve(replace(s, canonical_event_id='evt-1', home=None, away=None,
                                     competition=None, kickoff=None), NOW)
    assert match.status == 'EXACT' and match.record['home'] == 'Arsenal'


def test_under_selection_returns_its_own_evaluation(local):
    resolver, s, _ = local
    over = resolver.resolve(s, NOW)
    under = resolver.resolve(replace(s, selection='under'), NOW)
    assert under.status == 'EXACT' and under.record['selection'] == 'under'
    assert under.metrics['p_model'] != over.metrics['p_model']


def add_snapshot(db, s):
    item = {'sport': s.sport, 'league': s.competition, 'home': s.home, 'away': s.away,
            'kickoff_utc': s.kickoff, 'market': s.market_family, 'selection': s.selection,
            'period': s.period, 'stat': s.stat, 'entity': s.entity, 'line': s.line,
            'fixture_id': 'evt-1', 'price': 1.8, 'prob': .71, 'p_market': .53, 'n_books': 2}
    db.upsert_prediction_cache(dict(cache_key='daily_predictions:' + ZONE + ':' + DAY,
        local_date=DAY, timezone=ZONE, status='COMPLETE', payload_json=json.dumps({'all_predictions': [item]}),
        sport_status_json='{}', generated_at=NOW.isoformat(), updated_at=NOW.isoformat(),
        expires_at=(NOW + timedelta(hours=1)).isoformat(), retry_after=None, cache_version=4))


def test_snapshot_fallback_preserves_only_existing_metrics(db):
    s = selection()
    add_snapshot(db, s)
    before = db_dump(db)
    match = LocalResolver(db.path, ZONE).resolve(s, NOW)
    assert match.status == 'EXACT' and match.provenance == ('prediction_cache',)
    assert match.metrics['p_model'] == .71 and match.metrics['p_raw'] is None
    assert match.metrics['p_independent'] is None and match.metrics['fair_books'] is None
    assert db_dump(db) == before


def test_raw_provider_quotes_have_no_invented_model_probability(db):
    s = selection()
    raw = {'sport': s.sport, 'league': s.competition, 'home': s.home, 'away': s.away, 'start': s.kickoff,
           'markets': [{'market': s.market_family, 'selection': s.selection, 'period': s.period,
                        'stat': s.stat, 'entity': s.entity, 'line': s.line, 'price': 1.8, 'book_quotes': {'book1': 1.8}}]}
    db.upsert_provider_cache('raw-today', DAY, [raw], {}, (NOW + timedelta(hours=4)).isoformat())
    match = LocalResolver(db.path, ZONE).resolve(s, NOW)
    assert match.status == 'EXACT' and match.provenance == ('provider_cache',)
    assert match.metrics['p_model'] is None and match.metrics['p_market'] is None
    assert match.metrics['book_quotes'] == {'book1': 1.8}
    assert local_level(match, NOW) == 'LIMITED'
    # Raw provider duplicates without canonical IDs must not make archive/snapshot evidence ambiguous.
    add_snapshot(db, s)
    match = LocalResolver(db.path, ZONE).resolve(s, NOW)
    assert match.status == 'EXACT' and match.provenance == ('prediction_cache',)


def test_persisted_picks_fallback_reads_notes_without_writes(db):
    s = selection()
    db.insert_pick({'pick_date': DAY, 'tier': 'top', 'rank': 1, 'provider': 'stored', 'provider_event_id': 'test-id',
                   'sport': s.sport, 'league': s.competition, 'home': s.home, 'away': s.away, 'start_utc': s.kickoff,
                   'market': s.market_family, 'period': s.period, 'stat': s.stat, 'entity': s.entity, 'line': s.line,
                   'selection': s.selection, 'label': 'Over 2.5', 'price': 1.8, 'prob': .65, 'prob_raw': .64,
                   'p_market': .55, 'ev': .17, 'p_fair': .56, 'sources': {'_notes': {'edge': .1}},
                   'canonical_event_id': 'evt-1', 'books': 3, 'created_at': NOW.isoformat()})
    before = db_dump(db)
    match = LocalResolver(db.path, ZONE).resolve(s, NOW)
    assert match.provenance == ('picks',) and match.metrics['p_raw'] == .64
    assert match.metrics['notes']['edge'] == .1
    assert match.metrics['agreement'] is None  # Not present in pick storage: never reconstruct it.
    assert db_dump(db) == before


def test_research_scope_cannot_change_the_submitted_leg():
    d, g = grounded_payload()
    d['selection']['line'] = 3.5
    with pytest.raises(AuditUnavailable, match='research_coordinate_mismatch'):
        parse_research(json.dumps(d), g, selection(), MODEL, NOW)


def test_missing_and_old_fact_dates_do_not_establish_researched_level():
    re = evidence()
    assert research_level(replace(re, facts=tuple(replace(f, observed_at=None) for f in re.facts)), NOW) != 'RESEARCHED'
    old = (NOW - timedelta(days=8)).isoformat()
    assert research_level(replace(re, facts=tuple(replace(f, observed_at=old) for f in re.facts)), NOW) != 'RESEARCHED'


def test_cached_research_used_before_uncached_budget(db, tmp_path):
    d, g = grounded_payload()
    c = client(tmp_path, response(json.dumps(d), g))
    researcher = Researcher(c)
    researcher.research(selection(), NOW)
    result = AuditService(LocalResolver(db.path), researcher, max_uncached_research=0).audit(ticket(), NOW)
    assert result.selections[0].evidence_level == 'RESEARCHED' and c.transport.request.call_count == 1


def test_failed_equivalent_searches_are_cooled_down(tmp_path):
    c = client(tmp_path, error=requests.Timeout())
    researcher = Researcher(c)
    for _ in range(20):
        with pytest.raises(AuditUnavailable, match='gemini_timeout'):
            researcher.research(selection(), NOW)
    assert c.transport.request.call_count == 1


def test_settings_are_fail_closed_and_do_not_mutate_environment(tmp_path, monkeypatch):
    import os
    for k in ('GEMINI_API_KEY', 'GEMINI_AUDIT_MODEL', 'AUDIT_GEMINI_ENABLED',
              'AUDIT_GEMINI_FREE_TIER_CONFIRMED', 'AUDIT_GEMINI_MAX_REQUESTS_PER_DAY'):
        monkeypatch.delenv(k, raising=False)
    p = tmp_path / 'audit.env'
    p.write_text('GEMINI_API_KEY=test-secret\nGEMINI_AUDIT_MODEL=gemini-2.5-flash\n', encoding='utf-8')
    s = Settings.from_environment(p)
    assert s.api_key == 'test-secret' and not s.enabled and not s.free_tier_confirmed
    assert s.max_requests_per_day == 20 and 'GEMINI_API_KEY' not in os.environ
    monkeypatch.setenv('AUDIT_GEMINI_MAX_REQUESTS_PER_DAY', 'not-an-integer')
    assert Settings.from_environment(p).max_requests_per_day == 0


def test_diagnostic_is_offline_by_default_and_import_safe(capsys, tmp_path, monkeypatch):
    import diagnose_gemini_audit as diag
    p = tmp_path / 'no-ledger.sqlite'
    assert diag.main(['search', '--usage-file', str(p)]) == 0
    assert not p.exists()
    assert 'PLAN_ONLY' in capsys.readouterr().out
    monkeypatch.setattr(diag.Settings, 'from_environment', lambda *a: Settings())
    assert diag.main(['image', '--live', '--confirm-free-tier', '--usage-file', str(p)]) == 2
    assert not p.exists()
    assert 'missing_key' in capsys.readouterr().out


def test_synthetic_png_is_ephemeral_and_valid_header():
    from diagnose_gemini_audit import synthetic_image
    p = synthetic_image()
    assert p.startswith(b'\x89PNG\r\n\x1a\n') and len(p) < 10000


def test_audit_package_cannot_import_generation_providers_or_telegram():
    import ast
    root = Path(__file__).parents[1] / 'predbot' / 'ticket_audit'
    for path in root.glob('*.py'):
        tree = ast.parse(path.read_text(encoding='utf-8'))
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(n.name for n in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.append(node.module or '')
        assert not any(n.startswith(('predbot.providers', 'predbot.model', 'predbot.cache', 'predbot.jobs',
                                     'predbot.bot', 'predbot.storage', 'predbot.custom_picks')) for n in imports)
