"""Settlement math, competing fixture alternatives, and immutable promotion."""
import json
import math
from dataclasses import asdict, replace

import pytest

from predbot.config import Config
from predbot.markets.settlement import grade
from predbot.model.calibration import Calibrator
from predbot.model.elo import Elo
from predbot.model.engine import representative_predictions
from predbot.model.ensemble import EventModel
from predbot.model.margin import MarginModel
from predbot.model.payoff import Payoff, line_payoff
from predbot.model.poisson import ScoreGrid, settlement_line
from predbot.providers.base import MarketLine, MatchResult
from predbot.selection.daily import select_daily
from predbot.selection.evidence import assess
from predbot.selection.picker import PickSet, PredictionPool
from predbot.selection.top import select_top
from predbot.storage.db import Database
from predbot.util.timeutil import iso
from tests.conftest import dt
from tests.test_picker import ev, sc


@pytest.mark.parametrize('market,side,line', [
    ('total', 'over', 2), ('total', 'under', 2),
    ('total', 'over', 2.25), ('total', 'under', 2.25),
    ('total', 'over', 2.75), ('total', 'under', 2.75),
    ('handicap', 'home', -1.25), ('handicap', 'away', .75),
    ('handicap', 'home', 0), ('dnb', 'away', None)])
def test_score_distribution_matches_actual_full_and_partial_payouts(market, side, line):
    ml = MarketLine(market, side, 1.8, line=line)
    grid = ScoreGrid(1.6, 1.1)
    payoff = settlement_line(ml, 1.6, 1.1, 'football')
    masses = dict.fromkeys(asdict(payoff), 0.)
    profit = {'won': .8, 'lost': -1., 'push': 0., 'half_won': .4, 'half_lost': -.5, 'void': 0.}
    expected = 0.
    for h in range(grid.n+1):
        for a in range(grid.n+1):
            status = grade(ml, MatchResult(status='final', scores={'full': (h, a)}), 'football')
            masses[status] += grid.grid[h][a]
            expected += grid.grid[h][a]*profit[status]
    assert asdict(payoff) == pytest.approx(masses)
    assert payoff.ev(1.8) == pytest.approx(expected)
    assert sum(asdict(payoff).values()) == pytest.approx(1)


def test_push_ev_and_conditional_confidence_are_separate():
    payoff = Payoff(won=.45, lost=.35, push=.20)
    assert payoff.fair_probability == pytest.approx(.5625)
    assert payoff.won == .45
    assert payoff.ev(1.8) == pytest.approx(.01)
    assert payoff.ev(1/payoff.fair_probability) == pytest.approx(0)
    assert payoff.variance(1.8) > 0


@pytest.mark.parametrize('line,side,outcome', [(2.25, 'over', 'half_lost'),
                                             (2.25, 'under', 'half_won'),
                                             (1.75, 'over', 'half_won')])
def test_quarter_lines_keep_partial_event_probability(line, side, outcome):
    payoff = line_payoff({2: 1.}, line, side)
    assert getattr(payoff, outcome) == 1
    assert payoff.push == 0


def test_repricing_preserves_void_and_partial_shapes_without_false_binary_ev():
    original = Payoff(won=.3, lost=.2, push=.1, half_won=.15, half_lost=.2, void=.05)
    changed = original.reprice(.7)
    assert changed.fair_probability == pytest.approx(.7)
    assert changed.push == original.push and changed.void == original.void
    assert changed.half_won/changed.won == pytest.approx(original.half_won/original.won)
    assert changed.ev(1/.7) == pytest.approx(0)


@pytest.mark.parametrize('line', [2., 2.25, 2.75, -1.25])
def test_gaussian_integer_and_quarter_settlements(line):
    model = MarginModel('basketball', 3., 220.)
    ml = MarketLine('handicap', 'home', 1.9, line=line)
    payoff = model.settlement(ml)
    assert sum(asdict(payoff).values()) == pytest.approx(1)
    assert payoff.ev(1/payoff.fair_probability) == pytest.approx(0)
    assert model.prob(ml) == pytest.approx(payoff.fair_probability)


def row(i, fixture, market='total', side='over', line=1.5, p=.80, odds=1.5, quality='secondary', books=5):
    return dict(id=i, canonical_event_id=fixture, fixture_key=fixture, sport='football',
                league=fixture, start_utc=iso(dt(hh=22)), status='pending', market=market,
                selection=side, line=line, stat='points', prob=p, price=odds,
                quality=quality, books=books, ev=p*odds-1, edge_ev=None,
                rank_score=p-.035, sources=json.dumps({'_notes': {'evidence': {'p_lcb': p-.035}}}))


@pytest.mark.parametrize('market,side,line', [('total', 'over', 1.5), ('total', 'under', 3.5),
                                           ('btts', 'yes', None), ('btts', 'no', None),
                                           ('handicap', 'home', -.5)])
def test_alternates_and_btts_can_qualify_for_both_tickets(market, side, line):
    cfg = replace(Config(), daily_odds_min=2., daily_odds_max=2.5)
    rows = [row('a', 'fixture-a', market, side, line), row('b', 'fixture-b')]
    for selector in (select_top, select_daily):
        chosen, _ = selector(rows, cfg, iso(dt()))
        assert len(chosen) == 2
        assert 'a' in {r['id'] for r in chosen}
        assert len({r['canonical_event_id'] for r in chosen}) == len(chosen)
        assert 2 <= math.prod(r['price'] for r in chosen) <= 2.5


@pytest.mark.parametrize('selector', [select_top, select_daily])
def test_lower_ranked_fixture_alternative_remains_available_for_odds_band(selector):
    cfg = replace(Config(), daily_odds_min=2., daily_odds_max=2.5)
    rows = [row('favourite', 'a', market='dc', side='home_draw', p=.90, odds=1.05, quality='main'),
            row('alternate', 'a', p=.80, odds=1.5), row('other', 'b', p=.82, odds=1.5)]
    chosen, _ = selector(rows, cfg, iso(dt()))
    assert {r['id'] for r in chosen} == {'alternate', 'other'}


@pytest.mark.parametrize('selector', [select_top, select_daily])
def test_stronger_fixture_alternative_replaces_result_and_thresholds_hold(selector):
    cfg = replace(Config(), daily_odds_min=2., daily_odds_max=2.5)
    rows = [row('win', 'a', market='result3', side='home', p=.66, odds=1.5, quality='main'),
            row('over', 'a', p=.80, odds=1.5), row('other', 'b'),
            row('thin', 'c', market='btts', p=.95, books=2), row('weak', 'd', p=.49)]
    chosen, diag = selector(rows, cfg, iso(dt()))
    assert {r['id'] for r in chosen} == {'over', 'other'}
    assert diag['rejections']['insufficient_books'] >= 1
    assert diag['rejections']['below_probability'] >= 1


def evaluated_pool():
    event = ev(1)
    event.canonical_event_id = 'fixture-a'
    candidates = [sc(event, 'total', 'over', .8, 1.5, line=1.5),
                  sc(event, 'total', 'under', .2, 4., line=1.5),
                  sc(event, 'total', 'over', .52, 1.85, line=2.5),
                  sc(event, 'total', 'under', .48, 2., line=2.5)]
    event.markets = [s.line for s in candidates]
    other = ev(2); other.canonical_event_id = 'fixture-b'
    candidates.append(sc(other, 'btts', 'yes', .81, 1.5))
    gen = representative_predictions(candidates)
    cfg = replace(Config(), daily_odds_min=2., daily_odds_max=2.5)
    for s in gen.ticket_predictions:
        evidence = assess(s, cfg)
        s.quality, s.se, s.p_lcb, s.rank_score = evidence.quality, evidence.se, evidence.p_lcb, evidence.rank
        s.notes['evidence'] = evidence.as_notes()
    return gen, cfg


def test_real_promotion_preserves_all_view_history_and_exact_outcome_identity(tmp_path):
    gen, cfg = evaluated_pool()
    pool = PredictionPool(Database(str(tmp_path/'pool.db')), '2030-06-01')
    try:
        ps = PickSet(predictions=gen.predictions, ticket_predictions=gen.ticket_predictions)
        pool.store(ps)
        before = pool.rows('all')
        assert next(r for r in before if r['market']=='total')['line'] == 2.5
        pool.package(cfg, iso(dt()))
        assert pool.rows('all') == before
        for kind in ('top', 'daily'):
            assert next(r for r in pool.rows(kind) if r['market']=='total')['line'] == 1.5
        top = pool.rows('top')
        promoted = next(r for r in top if r['market']=='total')
        pool.db.settle_pick(promoted['id'], 'won', {'score': '2-1'})
        historical = dict(pool.db.query('SELECT * FROM picks WHERE id=?', (promoted['id'],))[0])
        # Publication freezes ticket membership; retries cannot overwrite forecasts.
        pool.db.mark_post('picks_top', pool.day, 'test-message')
        gen.ticket_predictions[0].p_model = .95
        pool.store(ps); pool.package(cfg, iso(dt()))
        assert dict(pool.db.query('SELECT * FROM picks WHERE id=?', (promoted['id'],))[0]) == historical
        assert {r['id'] for r in pool.rows('top')} == {r['id'] for r in top}
        assert pool.db.query('SELECT COUNT(*) AS n FROM ticket_candidates')[0]['n'] == 5
        # Reopening applies the migration idempotently without altering old forecasts.
        path = pool.db.path; pool.db.close(); pool.db = Database(path)
        assert dict(pool.db.query('SELECT * FROM picks WHERE id=?', (promoted['id'],))[0]) == historical
    finally:
        pool.db.close()


def test_top_and_daily_memberships_resolve_to_real_stored_picks(tmp_path):
    """Regression for the 2026-10-14 incident: generation and D1 sync both
    reported success (2 top, 5 daily memberships written) while the staging
    bot's /top and /daily still rendered empty. The engine/D1 side turned
    out fine on inspection; this locks in the invariant that would have
    actually caused that symptom from the engine side — a 'top'/'daily'
    membership row left pointing at a prediction_id with no matching `picks`
    row (dangling, e.g. from a ticket-candidate id that was never resolved to
    a stored pick). Note a promoted alternate CAN legitimately differ from
    the fixture's 'all' representative (see the promotion test above), so
    this checks against every stored pick, not just the 'all' collection."""
    gen, cfg = evaluated_pool()
    pool = PredictionPool(Database(str(tmp_path / 'pool2.db')), '2030-06-01')
    try:
        pool.store(PickSet(predictions=gen.predictions, ticket_predictions=gen.ticket_predictions))
        diagnostics = pool.package(cfg, iso(dt()))
        stored_ids = {r['id'] for r in pool.db.query('SELECT id FROM picks')}
        for kind in ('top', 'daily'):
            rows = pool.rows(kind)
            assert rows, f'{kind} selection must not be empty for this fixture'
            assert {r['id'] for r in rows} <= stored_ids, f'{kind} membership points at a nonexistent pick'
            assert len(rows) == diagnostics[f'{kind}_selected']
    finally:
        pool.db.close()


def test_model_exposes_strict_dnb_probability_and_unconditional_ev():
    db = Database(':memory:')
    try:
        event = ev(1)
        event.markets = [MarketLine('result3', 'home', 1.8), MarketLine('result3', 'draw', 3.5),
                         MarketLine('result3', 'away', 4.5), MarketLine('dnb', 'home', 1.4),
                         MarketLine('dnb', 'away', 3.)]
        s = EventModel(event, Elo(db), Calibrator(db, scope='settlement_v2')).score(event.markets[3])
        masses = s.notes['settlement']
        assert s.p_model == masses['won'] < masses['fair_probability']
        assert s.ev == pytest.approx(masses['won']*.4-masses['lost'])
        assert masses['push'] > 0
    finally:
        db.close()


def test_legacy_calibration_is_not_used_for_new_settlement_basis():
    db = Database(':memory:')
    try:
        db.set_calibration('global', .5, .6, 300)
        assert Calibrator(db, scope='settlement_v2').apply(.7) == .7
        assert Calibrator(db).apply(.7) != .7
    finally:
        db.close()


def test_unidentified_push_rates_fail_closed_instead_of_inventing_ev():
    db = Database(':memory:')
    try:
        event = ev(1, sport='tennis')
        event.markets = [MarketLine('total', 'over', 1.9, line=22),
                         MarketLine('total', 'under', 1.9, line=22)]
        assert EventModel(event, Elo(db), Calibrator(db)).score_all() == []
    finally:
        db.close()


def test_baseball_regulation_moneyline_preserves_draw_push():
    ml = MarketLine('result2', 'home', 1.8, period='reg')
    payoff = settlement_line(ml, 4.5, 3.5, 'baseball')
    assert payoff.push > 0
    assert payoff.won < payoff.fair_probability


def test_old_schema_migration_preserves_all_forecast_and_result_values(tmp_path):
    import sqlite3
    from predbot.jobs.daily_picks import prediction_row
    path = str(tmp_path/'legacy.db')
    db = Database(path)
    candidate = sc(ev(1), 'result3', 'home', .7, 1.5)
    candidate.event.canonical_event_id = 'historical'
    pid = db.store_prediction(prediction_row(candidate, '2020-06-01'))
    db.settle_pick(pid, 'lost', {'scores': [0, 1]})
    before = dict(db.query('SELECT * FROM picks WHERE id=?', (pid,))[0])
    before.pop('is_representative')
    db.close()
    conn = sqlite3.connect(path)
    conn.execute('DROP INDEX uq_prediction_event_family_day')
    conn.execute('ALTER TABLE picks DROP COLUMN is_representative')
    conn.commit(); conn.close()
    db = Database(path)
    try:
        after = dict(db.query('SELECT * FROM picks WHERE id=?', (pid,))[0])
        assert after.pop('is_representative') == 1
        assert after == before
    finally:
        db.close()


def test_strict_win_calibration_ignores_legacy_and_half_payoffs_are_weighted():
    from predbot.jobs.daily_picks import prediction_row
    db = Database(':memory:')
    try:
        for i in range(160):
            candidate = sc(ev(i), 'total', 'over', .6, 1.5, line=2.25)
            candidate.event.canonical_event_id = str(i)
            candidate.notes['probability_basis'] = 'strict_win_v2'
            candidate.notes['settlement'] = {'quote_raw': .7}
            pid = db.store_prediction(prediction_row(candidate, '2030-06-01'))
            db.settle_pick(pid, 'half_won')
        assert Calibrator(db, scope='settlement_v2').refit() is None  # 80 decisive units
        assert Calibrator(db).refit() is not None  # legacy scope stays separate
    finally:
        db.close()


def test_refresh_does_not_mix_legacy_conditional_probabilities_with_strict_win():
    from predbot.jobs.daily_picks import prediction_row
    db = Database(':memory:')
    try:
        candidate = sc(ev(1), 'dnb', 'home', .8, 1.4)
        candidate.event.canonical_event_id = 'legacy'
        legacy = prediction_row(candidate, '2030-06-01')
        pid = db.store_prediction(legacy)
        candidate.p_model = .6
        candidate.notes['probability_basis'] = 'strict_win_v2'
        db.archive_ticket_candidate(prediction_row(candidate, '2030-06-01'))
        assert db.ticket_candidate_rows('2030-06-01') == []
        assert db.query('SELECT prob FROM picks WHERE id=?', (pid,))[0]['prob'] == .8
        pool = PredictionPool(db, '2030-06-01')
        pool.package(Config(), iso(dt()))
        assert pool.rows('top') == []  # no fallback to incompatible legacy rows
    finally:
        db.close()


def test_unused_alternate_can_gain_coverage_on_retry_without_rewriting_forecasts():
    from predbot.jobs.daily_picks import prediction_row
    db = Database(':memory:')
    try:
        event = ev(1); event.canonical_event_id = 'retry-fixture'
        s = sc(event, 'total', 'over', .8, 1.5, line=1.5, books=1, sources=1)
        s.quality = 'secondary'
        db.archive_ticket_candidate(prediction_row(s, '2030-06-01'))
        assert db.ticket_candidate_rows('2030-06-01')[0]['books'] == 1
        s.line.n_books = 5
        db.archive_ticket_candidate(prediction_row(s, '2030-06-01'))
        row = db.ticket_candidate_rows('2030-06-01')[0]
        assert row['books'] == 5
        pid = db.store_ticket_prediction(row)
        recorded = dict(db.query('SELECT * FROM picks WHERE id=?', (pid,))[0])
        s.p_model = .9
        db.archive_ticket_candidate(prediction_row(s, '2030-06-01'))
        assert db.ticket_candidate_rows('2030-06-01')[0]['prob'] == .8
        assert dict(db.query('SELECT * FROM picks WHERE id=?', (pid,))[0]) == recorded
    finally:
        db.close()
