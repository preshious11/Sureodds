"""Pre-representative evaluations, isolated persistence and strictly local reads."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
import logging
import sqlite3
import threading
from unittest.mock import Mock

import pytest

from predbot.cache.fetcher import UnifiedFetcher
from predbot.config import Config
from predbot.jobs import daily_picks, live_results, settle, stats
from predbot.model.engine import representative_predictions
from predbot.model.ensemble import EventModel
from predbot.providers.chain import ProviderChain
from predbot.selection.evidence import assess
from predbot.selection.picker import PickSet, PredictionPool
from predbot.selection.representatives import select_representatives
from predbot.storage.db import Database
from predbot.util.timeutil import iso, to_local
from tests.conftest import dt
from tests.real_events import real_ev
from tests.test_picker import sc


DAY = '2030-06-01'
ZONE = 'Africa/Lagos'
NOW = dt(hh=12)


def alternatives(i=1, start=None):
    event = real_ev(i, home='Arsenal' if i == 1 else 'Everton',
                    away='Chelsea' if i == 1 else 'Fulham', start=start or dt(hh=15))
    event.canonical_event_id = f'evt-{i}'
    event.canonical_competition_id = 'soccer_epl'
    event.home_id, event.away_id = f'home-{i}', f'away-{i}'
    definitions = [
        ('result3', 'home', .66, 1.45, None), ('result3', 'draw', .20, 4.5, None),
        ('result3', 'away', .14, 7.0, None),
        ('dc', 'home_draw', .86, 1.22, None), ('dc', 'home_away', .80, 1.8, None),
        ('dc', 'away_draw', .34, 2.9, None),
        ('btts', 'yes', .63, 1.9, None), ('btts', 'no', .37, 2.4, None),
        ('dnb', 'home', .75, 1.4, None), ('dnb', 'away', .25, 3.5, None),
        ('result2', 'home', .75, 1.4, None), ('result2', 'away', .25, 3.5, None),
    ]
    for line, probability in ((1.5, .80), (2.5, .65), (3.5, .40)):
        definitions.extend([('total', 'over', probability, 1.8, line),
                            ('total', 'under', 1 - probability, 2.1, line)])
    for line in (-.5, -1.5, -2.5):
        definitions.extend([('handicap', 'home', .64, 1.8, line),
                            ('handicap', 'away', .36, 2.1, -line)])
    scored = [sc(event, market, selection, probability, price, line=line)
              for market, selection, probability, price, line in definitions]
    event.markets = [s.line for s in scored]
    return scored


def persist(db, candidates, day=DAY, zone=ZONE, at=None, complete=True):
    return db.store_custom_pick_candidates(day, zone, candidates, generated_at=iso(at or NOW),
                                           complete=complete, diagnostics={'complete': complete})


def prepared(scored, cfg, archive=True):
    generation = representative_predictions(scored)
    tables = {}
    for s in generation.predictions:
        evidence = assess(s, cfg, tables)
        s.quality, s.se, s.p_lcb, s.rank_score = evidence.quality, evidence.se, evidence.p_lcb, evidence.rank
        s.notes['evidence'] = evidence.as_notes()
        s.notes['independent'] = s.independent
    return PickSet(predictions=generation.predictions, diagnostics={'generated_at': iso(NOW)},
                   candidates=generation.candidates if archive else [],
                   candidate_timezone=cfg.timezone if archive else '')


@pytest.mark.parametrize('market,selections,count', [
    ('result3', {'home', 'draw', 'away'}, 3),
    ('dc', {'home_draw', 'away_draw', 'home_away'}, 3),
    ('btts', {'yes', 'no'}, 2), ('dnb', {'home', 'away'}, 2),
    ('result2', {'home', 'away'}, 2),
    ('total', {'over', 'under'}, 6), ('handicap', {'home', 'away'}, 6),
])
def test_evaluated_alternatives_coexist(db, market, selections, count):
    generation = representative_predictions(alternatives())
    persist(db, generation.candidates)
    rows = [r for r in db.get_custom_pick_candidates(DAY, ZONE) if r['market'] == market]
    assert len(rows) == count
    assert {r['selection'] for r in rows} == selections
    assert len({r['candidate_id'] for r in rows}) == count
    assert db.collection_predictions('all', DAY) == []
    assert db.pending_picks() == []


@pytest.mark.parametrize('changes,reason', [
    ({'p_model': 0.0}, 'invalid_probability'), ({'p_model': 1.0}, 'invalid_probability'),
    ({'p_model': float('nan')}, 'invalid_probability'),
    ({'p_model': float('inf')}, 'invalid_probability'),
    ({'price': 1.0}, 'missing_odds'), ({'price': float('inf')}, 'missing_odds'),
    ({'price': float('nan')}, 'missing_odds'), ({'sources': {}}, 'insufficient_model_inputs'),
    ({'market': 'prop'}, 'invalid_market'),
])
def test_existing_validation_rejections_are_not_archived(db, changes, reason):
    valid = alternatives()[0]
    invalid = deepcopy(valid)
    for key, value in changes.items():
        setattr(invalid.line if key in ('price', 'market') else invalid, key, value)
    generation = representative_predictions([valid, invalid])
    assert generation.diagnostics['line_rejections'] == {reason: 1}
    persist(db, generation.candidates)
    assert len(db.get_custom_pick_candidates(DAY, ZONE)) == 1


def test_results_coverage_rejection_is_not_archived(db):
    generation = representative_predictions(alternatives(), results_gate=lambda event: [])
    assert generation.candidates == []
    persist(db, generation.candidates)
    assert db.get_custom_pick_candidates(DAY, ZONE) == []


def test_only_evaluated_candidates_are_captured_including_nonrepresentatives(db):
    values = alternatives()
    # Valid but outside representative policy; preservation must not apply that policy.
    values[0].line.period = '1h'
    values[1].line.stat = 'corners'
    values[2].line.entity = 'home'
    generation = representative_predictions(values[:3])
    persist(db, generation.candidates)
    rows = db.get_custom_pick_candidates(DAY, ZONE)
    assert len(rows) == 3
    assert {r['market'] for r in rows} == {'result3'}
    assert any(r['period'] == '1h' for r in rows)
    assert any(r['stat'] == 'corners' for r in rows)
    assert not any(r['selection'] in ('yes', 'over') for r in rows)


def test_capture_preserves_evaluated_fields_and_is_detached_before_ranking(db):
    s = alternatives()[0]
    s.p_raw, s.p_fair, s.edge_ev = .67, .68, .04
    s.p_independent, s.independent_status, s.p_model_basis = .69, 'ok', 'market_blend'
    s.fair_books, s.independent = ['pinnacle'], ['sharp']
    s.notes = {'independent_model': {'source': 'football_poisson_v1', 'p': .69}}
    s.line.fair_price, s.line.provider_prob, s.line.bookmaker = 1.47, .68, 'book'
    s.line.book_quotes, s.line.book_prices, s.line.providers = {'book': 1.45}, [1.45], ['test']
    s.line.provider_odd_id = 'odd-1'
    s.event.extra = {'alt_ids': {'other': 'other-1'}, 'sport_key': 'soccer_epl'}
    generation = representative_predictions([s])
    captured = deepcopy(generation.candidates[0])
    assert 'representative' not in captured['notes']
    assess(s, Config())
    s.rank_score, s.quality, s.se, s.p_lcb = .7, 'main', .02, .62
    s.notes['evidence'] = {'rank': .7}
    s.sources['changed'] = .9
    s.line.book_quotes['changed'] = 2.0
    s.event.extra['alt_ids']['other'] = 'changed'
    assert generation.candidates[0] == captured
    persist(db, generation.candidates)
    row = db.get_custom_pick_candidates(DAY, ZONE)[0]
    for key, value in captured.items():
        assert row[key] == value
    assert row['p_independent'] == .69 and row['provider_prob'] == .68
    assert row['book_quotes'] == {'book': 1.45}
    assert row['bookmaker_count'] == row['provider_count'] == 1
    assert all(row[k] is None for k in ('rank_score', 'quality', 'se', 'p_lcb'))


def test_optional_nonfinite_fields_are_null_without_changing_validation(db):
    s = alternatives()[0]
    s.edge_ev = float('nan')
    generation = representative_predictions([s])
    assert len(generation.candidates) == 1
    persist(db, generation.candidates)
    assert db.get_custom_pick_candidates(DAY, ZONE)[0]['edge_ev'] is None


def test_identity_date_timezone_upsert_and_out_of_order_safety(db):
    first = representative_predictions(alternatives()).candidates
    second = representative_predictions(alternatives(2)).candidates
    persist(db, first + second)
    original = db.get_custom_pick_candidates(DAY, ZONE)
    assert len(original) == 48 and len({r['candidate_id'] for r in original}) == 48
    persist(db, first + second)
    assert db.get_custom_pick_candidates(DAY, ZONE) == original
    changed = deepcopy(first[0])
    changed['price'], changed['p_model'] = 1.55, .68
    persist(db, [changed], at=NOW + timedelta(minutes=5))
    persist(db, first, at=NOW - timedelta(minutes=5))
    row = next(r for r in db.get_custom_pick_candidates(DAY, ZONE)
               if r['canonical_event_id'] == 'evt-1' and r['market'] == 'result3' and r['selection'] == 'home')
    assert row['price'] == 1.55 and row['p_model'] == .68
    assert row['first_seen_at'] == iso(NOW)
    persist(db, second, day='2030-05-31')
    persist(db, second, zone='UTC')
    assert len(db.get_custom_pick_candidates('2030-05-31', ZONE)) == 24
    assert len(db.get_custom_pick_candidates(DAY, 'UTC')) == 24
    assert db.get_custom_pick_candidates('2030-06-02', ZONE) == []
    assert db.get_custom_pick_candidates(DAY, 'America/New_York') == []
    assert len(db.get_custom_pick_candidates(DAY, ZONE)) == 48


@pytest.mark.parametrize('coordinate,new_value', [
    ('period', '1h'), ('stat', 'corners'), ('entity', 'home'), ('line', 0.0), ('selection', 'draw'),
])
def test_every_bet_coordinate_distinguishes_candidates(db, coordinate, new_value):
    original = representative_predictions(alternatives()[:1]).candidates[0]
    modified = deepcopy(original)
    modified[coordinate] = new_value
    persist(db, [original, modified])
    assert len(db.get_custom_pick_candidates(DAY, ZONE)) == 2


def test_numeric_line_identity_is_stable_for_integer_float_and_signed_zero(db):
    row = representative_predictions(alternatives()[:1]).candidates[0]
    for line in (0, 0.0, -0.0):
        persist(db, [dict(row, line=line)])
    assert len(db.get_custom_pick_candidates(DAY, ZONE)) == 1


def test_partial_union_never_deletes_absent_or_failed_candidates(db):
    first = representative_predictions(alternatives()).candidates
    persist(db, first, complete=False)
    assert db.get_custom_pick_pool(DAY, ZONE)['status'] == 'PARTIAL'
    more = representative_predictions(alternatives(2)).candidates
    persist(db, more, at=NOW + timedelta(minutes=5))
    before = db.get_custom_pick_candidates(DAY, ZONE)
    metadata = db.get_custom_pick_pool(DAY, ZONE)
    assert len(before) == 48 and metadata['status'] == 'COMPLETE'
    persist(db, [], at=NOW + timedelta(minutes=10), complete=False)
    assert db.get_custom_pick_candidates(DAY, ZONE) == before
    assert db.get_custom_pick_pool(DAY, ZONE) == metadata
    # Even a complete later generation archives its valid rows without destructive pruning.
    persist(db, more, at=NOW + timedelta(minutes=15))
    assert len(db.get_custom_pick_candidates(DAY, ZONE)) == 48


@pytest.fixture
def generated(db, monkeypatch):
    clock = [NOW]
    for module in ('predbot.cache.fetcher', 'predbot.cache.providers', 'predbot.jobs.daily_picks'):
        monkeypatch.setattr(module + '.utcnow', lambda: clock[0])
    provider = Mock(spec=['enabled', 'fetch_events', 'last_fetch_complete', 'sports'])
    provider.enabled.return_value = True
    provider.sports = ['football']
    provider.partial = False
    provider.last_fetch_complete = True
    provider.fetch_events.return_value = [alternatives()[0].event]
    def fetch(*args):
        # The real collector resets this flag before each provider fetch.
        provider.last_fetch_complete = not provider.partial
        return provider.fetch_events.return_value
    provider.fetch_events.side_effect = fetch
    chain = ProviderChain({'test': provider}, ['test'])
    monkeypatch.setattr(daily_picks, 'make_results_gate', lambda *args: lambda event: 'test')
    # The lone mock is required here so a partial fetch still yields PARTIAL,
    # as under the pre-policy all-complete rule.
    cfg = Config(timezone=ZONE, independent_model_mode='off', required_providers=('test',))
    return UnifiedFetcher(chain, db, cfg), provider, clock


def test_normal_generation_evaluates_once_and_archives_before_reduction(generated, db, monkeypatch, caplog):
    app, provider, _ = generated
    original = EventModel.score_all
    evaluations = []
    def evaluate(model):
        values = original(model)
        evaluations.extend(values)
        return values
    monkeypatch.setattr(EventModel, 'score_all', evaluate)
    with caplog.at_level(logging.INFO):
        snapshot = app.get_daily_snapshot(scheduled=True)
    rows = db.get_custom_pick_candidates(DAY, ZONE)
    assert len(rows) == len(evaluations) == 24
    assert len(snapshot.all_predictions) < len(rows)
    assert len(db.collection_predictions('all', DAY)) == len(snapshot.all_predictions)
    assert all(r['rank_score'] is None and 'representative' not in r['notes'] for r in rows)
    for value in evaluations:
        row = next(r for r in rows if (r['market'], r['selection'], r['line']) ==
                   (value.line.market, value.line.selection, value.line.line))
        assert row['p_model'] == value.p_model and row['price'] == value.price
        assert row['sources'] == value.sources and row['ev'] == value.ev
    assert 'Custom Picks candidate archive | date=2030-06-01 | candidates=24 | fixtures=1' in caplog.text
    assert app.get_daily_snapshot().to_dict() == snapshot.to_dict()
    provider.fetch_events.assert_called_once()
    assert len(evaluations) == 24


def test_real_partial_retry_expands_and_updates_pool(generated, db):
    app, provider, clock = generated
    provider.partial = True
    assert app.get_daily_snapshot(scheduled=True).status == 'PARTIAL'
    initial = db.get_custom_pick_candidates(DAY, ZONE)
    assert len(initial) == 24
    provider.partial = False
    updated = deepcopy(provider.fetch_events.return_value[0])
    updated.markets[0].price = 1.6
    provider.fetch_events.return_value = [updated, alternatives(2)[0].event]
    clock[0] += timedelta(minutes=16)
    assert app.get_daily_snapshot(force_refresh=True, scheduled=True).status == 'COMPLETE'
    rows = db.get_custom_pick_candidates(DAY, ZONE)
    assert len(rows) == 48
    old_home = next(r for r in initial if r['market'] == 'result3' and r['selection'] == 'home')
    new_home = next(r for r in rows if r['candidate_id'] == old_home['candidate_id'])
    assert new_home['price'] == 1.6 and new_home['generated_at'] == iso(clock[0])
    assert db.get_custom_pick_pool(DAY, ZONE)['status'] == 'COMPLETE'
    assert provider.fetch_events.call_count == 2  # Only the two deliberate normal generation cycles.


@pytest.mark.parametrize('failure', ['generation', 'publication', 'empty_partial'])
def test_failed_refresh_keeps_archive_and_atomic_publication(generated, db, monkeypatch, failure):
    app, provider, clock = generated
    provider.partial = failure in ('publication', 'empty_partial')
    first = app.get_daily_snapshot(scheduled=True)
    before = db.get_custom_pick_candidates(DAY, ZONE)
    provenance = db.get_custom_pick_pool(DAY, ZONE)
    predictions = [dict(r) for r in db.query('SELECT * FROM picks')]
    clock[0] += timedelta(minutes=16)
    if failure == 'generation':
        monkeypatch.setattr(daily_picks, 'build_picks', Mock(side_effect=RuntimeError('generation failed')))
    elif failure == 'publication':
        provider.partial = True
        provider.fetch_events.return_value = [alternatives(2)[0].event]
        monkeypatch.setattr(app, '_store', Mock(side_effect=RuntimeError('publication failed')))
    else:
        provider.partial = True
        monkeypatch.setattr(daily_picks, 'score_events', lambda *args: [])
    returned = app.get_daily_snapshot(force_refresh=True, scheduled=True)
    if failure != 'empty_partial':
        assert returned.to_dict() == first.to_dict()
    assert db.get_custom_pick_candidates(DAY, ZONE) == before
    assert db.get_custom_pick_pool(DAY, ZONE) == provenance
    assert [dict(r) for r in db.query('SELECT * FROM picks')] == predictions


def test_archive_uses_application_date_at_lagos_midnight(generated, db):
    app, provider, clock = generated
    clock[0] = dt(hh=23, mm=30)  # June 2 in Lagos, June 1 in UTC.
    provider.fetch_events.return_value = [alternatives(start=dt(d=2, hh=2))[0].event]
    snapshot = app.get_daily_snapshot(scheduled=True)
    assert snapshot.date == to_local(clock[0], ZONE).date().isoformat() == '2030-06-02'
    assert db.get_custom_pick_candidates(DAY, ZONE) == []
    assert len(db.get_custom_pick_candidates('2030-06-02', ZONE)) == 24


@pytest.mark.parametrize('has_archive', [True, False])
def test_concurrent_read_path_is_select_only_with_all_provider_paths_forbidden(db, monkeypatch, has_archive):
    if has_archive:
        persist(db, representative_predictions(alternatives()).candidates)
    forbidden = Mock(side_effect=AssertionError('read triggered generation/provider/evaluation'))
    for obj, name in ((UnifiedFetcher, 'get_daily_snapshot'), (daily_picks, 'build_picks'),
                      (ProviderChain, 'collect'), (EventModel, 'score_all')):
        monkeypatch.setattr(obj, name, forbidden)
    statements = []
    changes = db.conn.total_changes
    db.conn.set_trace_callback(statements.append)
    try:
        with ThreadPoolExecutor(max_workers=8) as workers:
            results = list(workers.map(lambda _: db.get_custom_pick_candidates(DAY, ZONE), range(40)))
        assert db.get_custom_pick_pool('2030-06-02', ZONE) is None
    finally:
        db.conn.set_trace_callback(None)
    assert all(len(rows) == (24 if has_archive else 0) for rows in results)
    assert statements and all(sql.lstrip().upper().startswith('SELECT') for sql in statements)
    assert db.conn.total_changes == changes
    if has_archive:
        results[0][0]['sources']['user_change'] = 1
        assert 'user_change' not in results[1][0]['sources']
    forbidden.assert_not_called()


def test_archive_survives_restart_and_additive_schema_preserves_existing_rows(tmp_path, monkeypatch):
    path = str(tmp_path / 'production.db')
    db = Database(path)
    ps = prepared(alternatives(), Config(timezone=ZONE), archive=False)
    daily_picks.store_picks(ps, db, DAY)
    db.subscriber_add('42', 'user', 'User')
    db.set_kv('keep', 'original')
    old_tables = ('picks', 'prediction_memberships', 'subscribers', 'kv')
    before = {table: [dict(r) for r in db.query(f'SELECT * FROM {table}')] for table in old_tables}
    db.close()
    # Simulate a production database created before the two additive tables existed.
    with sqlite3.connect(path) as old:
        old.execute('DROP TABLE custom_pick_candidates')
        old.execute('DROP TABLE custom_pick_pools')
    db = Database(path)
    for table in old_tables:
        assert [dict(r) for r in db.query(f'SELECT * FROM {table}')] == before[table]
    persist(db, representative_predictions(alternatives()).candidates)
    saved = db.get_custom_pick_candidates(DAY, ZONE)
    db.close()
    reopened = Database(path)
    forbidden = Mock(side_effect=AssertionError('provider after restart'))
    monkeypatch.setattr(ProviderChain, 'collect', forbidden)
    monkeypatch.setattr(UnifiedFetcher, 'get_daily_snapshot', forbidden)
    monkeypatch.setattr(daily_picks, 'build_picks', forbidden)
    assert reopened.get_custom_pick_candidates(DAY, ZONE) == saved
    assert reopened.get_custom_pick_candidates('2030-06-02', ZONE) == []
    forbidden.assert_not_called()
    reopened.close()


def test_original_representatives_predictions_and_memberships_are_identical(tmp_path, monkeypatch):
    monkeypatch.setattr('predbot.storage.db._now', lambda: iso(NOW))
    scored = alternatives() + alternatives(2)
    original = select_representatives({f'evt-{i}': [s for s in deepcopy(scored)
                                                   if s.event.canonical_event_id == f'evt-{i}']
                                       for i in (1, 2)}).predictions
    produced = representative_predictions(deepcopy(scored)).predictions
    assert produced == original
    results = []
    cfg = Config(timezone=ZONE)
    for archive in (False, True):
        db = Database(str(tmp_path / f'{archive}.db'))
        pool = PredictionPool.persistent(db, DAY)
        ps = prepared(deepcopy(scored), cfg, archive=archive)
        with pool.transaction():
            pool.store(ps)
            diagnostics = pool.package(cfg, iso(NOW))
        results.append(([dict(r) for r in db.query('SELECT * FROM picks ORDER BY id')],
                        [dict(r) for r in db.query('SELECT * FROM prediction_memberships ORDER BY prediction_id,collection')],
                        {c: pool.rows(c) for c in ('all', 'top', 'daily')}, diagnostics))
        assert len(db.get_custom_pick_candidates(DAY, ZONE)) == (48 if archive else 0)
        db.close()
    assert results[0] == results[1]


def test_settlement_live_results_stats_and_recaps_ignore_archive(db, monkeypatch):
    cfg = Config(timezone=ZONE, live_updates_enabled=True, subscriber_delivery_enabled=False)
    baseline = stats.compute(db, NOW)
    baseline_collections = {c: stats.collection_stats(db, c, NOW) for c in ('all', 'top', 'daily')}
    # These fixtures are old enough that existing predictions would be voided, if they were visible.
    persist(db, representative_predictions(alternatives(start=dt(d=1, hh=0))).candidates)
    saved = db.get_custom_pick_candidates(DAY, ZONE)
    monkeypatch.setattr(settle, 'utcnow', lambda: dt(d=5))
    resolver = Mock(spec=['providers', 'run_lock', 'resolve'])
    resolver.providers, resolver.run_lock = {}, threading.RLock()
    resolver.resolve.side_effect = AssertionError('archive became a result watch')
    assert settle.run(resolver, db, cfg) == {'settled': 0, 'pending': 0, 'void': 0, 'errors': 0}
    live_results.run(resolver, db, cfg, Mock(), now=dt(d=5))
    assert db.pending_picks() == []
    assert db.live_picks(iso(dt()), iso(dt(d=5))) == []
    assert db.query('SELECT * FROM live_fixture_state') == []
    assert stats.compute(db, NOW) == baseline
    assert {c: stats.collection_stats(db, c, NOW) for c in baseline_collections} == baseline_collections
    assert db.get_custom_pick_candidates(DAY, ZONE) == saved
    resolver.resolve.assert_not_called()


def test_dry_run_does_not_persist_archive_in_production(generated, db):
    app, provider, _ = generated
    snapshot = app.get_daily_snapshot(scheduled=True, persist=False)
    assert snapshot.all_predictions
    assert db.get_custom_pick_candidates(DAY, ZONE) == []
    assert db.get_custom_pick_pool(DAY, ZONE) is None
    assert db.query('SELECT * FROM picks') == []
    assert db.query('SELECT * FROM prediction_cache') == []
    provider.fetch_events.assert_called_once()
