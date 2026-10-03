"""Boundaries of the refactored pipeline: provider -> canonical identity -> prediction engine -> stored predictions
-> collection selectors (Top / Daily) over STORED rows.

Prediction existence is independent of Top/Daily; collections are memberships of one stored row, never copies."""
from __future__ import annotations

import inspect
import math

import pytest

from predbot.config import Config
from predbot.jobs import stats as stats_mod
from predbot.jobs.daily_picks import store_picks
from predbot.model import engine
from predbot.model.engine import representative_predictions
from predbot.selection.daily import select_daily
from predbot.selection.evidence import assess
from predbot.selection.picker import PickSet, package_predictions
from predbot.selection.top import select_top
from predbot.util.fixtures import fixture_key
from predbot.util.timeutil import iso
from tests.conftest import dt
from tests.pipeline_helpers import select
from tests.test_picker import _fx, ev, sc

KINDS = (("result3", "home", None), ("total", "over", 2.5), ("btts", "yes", None), ("dnb", "home", None))
DAY = dt().date().isoformat()


def varied(n, p=0.70, price=1.35, **kw):
    return [sc(_fx(i), KINDS[i % 4][0], KINDS[i % 4][1], p, price, line=KINDS[i % 4][2], **kw) for i in range(n)]


def stored(db, scored, cfg=None, now=None):
    """Generate -> store -> package, exactly the production order, into a real database."""
    cfg, now = cfg or Config(), now or dt()
    for s in scored:
        s.event.canonical_event_id = fixture_key(s.event)
    generation = representative_predictions(scored)
    tables = {}
    for s in generation.predictions:
        e = assess(s, cfg, tables)
        s.quality, s.se, s.p_lcb, s.rank_score = e.quality, e.se, e.p_lcb, e.rank
        s.notes['evidence'] = e.as_notes()
        s.notes['independent'] = s.independent
    ps = PickSet(predictions=generation.predictions, diagnostics=generation.diagnostics)
    store_picks(ps, db, DAY)
    ps.diagnostics.update(package_predictions(db, DAY, cfg, iso(now)))
    return ps


def key(s):
    return (s.event.canonical_event_id, s.line.market, s.line.selection, s.line.line, s.p_model, s.price)


# ============================================================ generation is independent of the collections
def test_the_engine_knows_nothing_about_tickets_or_collections():
    source = inspect.getsource(engine)
    imported = [l for l in source.splitlines() if l.startswith(("from predbot.selection", "import predbot.selection"))]
    assert imported == ["from predbot.selection.representatives import select_representatives"]     # the one pure module
    assert "picker" not in source
    for word in ("top_", "daily_", "combined", "portfolio"):
        assert word not in source, word


def test_generation_is_identical_whatever_the_top_and_daily_configuration():
    def run(cfg):
        return [key(s) for s in select(varied(12), cfg, dt()).predictions]

    base = run(Config())
    starved = run(Config(top_odds_target=900.0, top_odds_max=1000.0, daily_target=0, daily_odds_min=900.0,
                         daily_odds_max=1000.0, top_min_prob=0.99, daily_min_prob=0.99))
    assert base == starved and len(base) == 12


# ============================================================ All Predictions never depends on Top / Daily
def test_all_predictions_stay_populated_when_top_is_empty():
    ps = select(varied(10, p=0.58), Config(), dt())            # above the Daily floor, below the Top floor
    assert ps.top == [] and ps.daily != []
    assert len(ps.all()) == 10
    assert ps.diagnostics["top"]["rejections"]["below_probability"] == 10


def test_all_predictions_stay_populated_when_daily_is_empty():
    pool = [sc(_fx(i), "result3", "home", 0.70, 1.45) for i in range(6)]
    ps = select(pool, Config(daily_target=0), dt())
    assert ps.daily == [] and ps.top != []
    assert len(ps.all()) == 6


def test_all_predictions_stay_populated_when_both_collections_are_empty(db):
    ps = stored(db, varied(8, p=0.52, price=1.85))
    assert ps.diagnostics["top_selected"] == 0 and ps.diagnostics["daily_selected"] == 0
    assert len(db.collection_predictions('all', DAY)) == 8
    assert db.collection_predictions('top', DAY) == [] and db.collection_predictions('daily', DAY) == []


# ============================================================ selectors consume stored rows
def test_selectors_take_stored_dict_rows_never_scored_objects(db):
    stored(db, varied(12))
    rows = [dict(r) for r in db.collection_predictions('all', DAY)]
    assert rows and all(isinstance(r, dict) and 'canonical_event_id' in r and 'prob' in r for r in rows)
    cfg, now = Config(), iso(dt())
    top, top_diag = select_top(rows, cfg, now)
    daily, daily_diag = select_daily(rows, cfg, now)
    for chosen in top + daily:
        assert isinstance(chosen, dict)                      # a stored row, not a model object
        assert chosen in rows
    # the stored memberships are exactly what the selectors chose from the same rows
    assert [r['id'] for r in db.collection_predictions('top', DAY)] == [r['id'] for r in top]
    assert [r['id'] for r in db.collection_predictions('daily', DAY)] == [r['id'] for r in daily]
    assert top_diag['selected'] == len(top) and daily_diag['selected'] == len(daily)


def test_selectors_work_from_plain_hand_written_rows_without_the_model():
    """No Scored, no provider data, no engine: a row dict is the whole input contract."""
    def row(i, **kw):
        base = dict(id=i, canonical_event_id=f"e{i}", fixture_key=f"f{i}", status='pending', prob=0.72, price=1.45,
                    ev=0.04, books=5, quality='main', rank_score=0.7 - i / 100, edge_ev=None, market='result3',
                    stat='points', sport='football', league=f'L{i}', start_utc='2030-06-01T20:00:00Z',
                    sources='{}', canonical_competition_id='')
        return {**base, **kw}
    rows = [row(i) for i in range(4)]
    top, diag = select_top(rows, Config(), '2030-06-01T18:00:00Z')
    assert [r['id'] for r in top] == [0, 1] and diag['target_reached']
    assert select_daily(rows, Config(), '2030-06-01T18:00:00Z')[0]


# ============================================================ gates never delete predictions
def test_combined_odds_failure_does_not_delete_predictions(db):
    cfg = Config(daily_odds_min=1.05, daily_odds_max=1.1, top_odds_max=1.1)     # no leg fits under the ceilings
    ps = stored(db, varied(8, price=1.45), cfg)
    assert ps.diagnostics['daily']['rejections']['combined_odds_ceiling'] == 8
    assert ps.diagnostics['daily']['selected'] == 0
    assert len(db.collection_predictions('all', DAY)) == 8   # the ceiling shaped Daily only


def test_value_failure_does_not_delete_a_model_prediction(db):
    bad = sc(_fx(1), "result3", "home", 0.70, 1.50, sources=1, ev_val=-0.12)
    bad.sources, bad.independent = {"market": 0.68, "elo": 0.72}, ["elo"]      # the independent model says: overpriced
    good = sc(_fx(2), "result3", "home", 0.70, 1.50, sources=1, ev_val=0.05)
    good.sources, good.independent = {"market": 0.68, "elo": 0.72}, ["elo"]
    ps = stored(db, [bad, good])
    rows = {r['canonical_event_id']: r for r in db.collection_predictions('all', DAY)}
    assert len(rows) == 2 and rows[bad.event.canonical_event_id]['ev'] == pytest.approx(-0.12)
    promoted = {r['canonical_event_id'] for c in ('top', 'daily') for r in db.collection_predictions(c, DAY)}
    assert bad.event.canonical_event_id not in promoted and good.event.canonical_event_id in promoted
    assert ps.diagnostics['daily']['rejections']['poor_model_value'] == 1


def test_diversity_and_target_never_delete_predictions():
    pool = [sc(ev(i, league="One League"), "result3", "home", 0.66, 1.4) for i in range(30)]
    ps = select(pool, Config(), dt())
    assert len(ps.daily) < 30 and len(ps.all()) == 30
    assert ps.diagnostics['daily']['rejections']['diversity_limit'] > 0


# ============================================================ one prediction, many collections, one settlement
def test_a_prediction_in_top_and_daily_is_one_row_and_settles_once(db):
    stored(db, varied(12))
    top_ids = {r['id'] for r in db.collection_predictions('top', DAY)}
    daily_ids = {r['id'] for r in db.collection_predictions('daily', DAY)}
    shared = top_ids & daily_ids
    assert shared, "the strongest prediction qualifies for both collections"
    pid = next(iter(shared))
    assert db.query("SELECT COUNT(*) AS n FROM picks WHERE id=?", (pid,))[0]['n'] == 1
    assert db.query("SELECT COUNT(*) AS n FROM picks WHERE pick_date=?", (DAY,))[0]['n'] == 12
    assert db.query("SELECT COUNT(*) AS n FROM prediction_memberships WHERE prediction_id=?", (pid,))[0]['n'] == 3

    db.settle_pick(pid, "won")
    db.settle_pick(pid, "lost")                               # a second settlement must not rewrite the result
    assert db.query("SELECT status FROM picks WHERE id=?", (pid,))[0]['status'] == 'won'

    today = dt(hh=23)
    top_all = stats_mod.collection_stats(db, 'top', today)['all']
    daily_all = stats_mod.collection_stats(db, 'daily', today)['all']
    every = stats_mod.collection_stats(db, 'all', today)['all']
    assert top_all['won'] == 1 and top_all['lost'] == 0       # counted once in Top ...
    assert daily_all['won'] == 1 and daily_all['lost'] == 0   # ... once in Daily
    assert every['won'] == 1 and every['issued'] == 12        # ... and exactly once among all predictions
    assert top_all['issued'] == len(top_ids) and daily_all['issued'] == len(daily_ids)


def test_regenerating_never_creates_a_second_row_for_an_event_and_family(db):
    stored(db, varied(6))
    again = stored(db, varied(6))
    assert db.query("SELECT COUNT(*) AS n FROM picks WHERE pick_date=?", (DAY,))[0]['n'] == 6
    assert again.diagnostics['stored_all_predictions'] == 6


# ============================================================ Top size: a target, never a requirement
def _top(pool):
    ps = select(pool, Config(), dt())
    return ps.top, ps.diagnostics['top']


def test_top_may_have_no_legs_when_nothing_qualifies():
    top, diag = _top([sc(_fx(i), "result3", "home", 0.50, 1.8) for i in range(5)])
    assert top == [] and diag['target_reached'] is False and diag['combined_odds'] == 1.0


def test_top_may_have_one_leg_when_it_reaches_the_target_alone():
    top, diag = _top([sc(_fx(i), "result3", "home", 0.70 - i / 100, 2.2) for i in range(5)])
    assert len(top) == 1 and diag['target_reached'] is True


def test_top_may_have_two_legs():
    top, diag = _top([sc(_fx(i), "result3", "home", 0.70, 1.45) for i in range(6)])
    assert len(top) == 2 and math.prod(s.price for s in top) == pytest.approx(2.1025)


def test_top_may_have_three_legs_and_never_more():
    top, diag = _top([sc(_fx(i), "result3", "home", 0.70, 1.27) for i in range(8)])
    assert len(top) == 3 and diag['target_reached'] is True


def test_top_never_adds_a_weak_leg_to_chase_the_target():
    pool = [sc(_fx(0), "result3", "home", 0.72, 1.40)] + [sc(_fx(i), "result3", "home", 0.50, 1.80) for i in range(1, 8)]
    top, diag = _top(pool)
    assert [s.price for s in top] == [1.40]                   # one honest leg, below the 2.00 target
    assert diag['target_reached'] is False


# ============================================================ one representative per fixture x family
def test_a_fixture_keeps_one_prediction_per_market_family_whatever_the_lines():
    e = _fx(1)
    e.canonical_event_id = fixture_key(e)
    pool = [sc(e, "result3", "home", 0.66, 1.7), sc(e, "result3", "draw", 0.20, 4.5), sc(e, "result3", "away", 0.14, 7.0),
            sc(e, "total", "over", 0.64, 1.8, line=2.5), sc(e, "total", "under", 0.36, 2.2, line=2.5),
            sc(e, "total", "over", 0.93, 1.05, line=0.5), sc(e, "total", "under", 0.07, 9.0, line=0.5),
            sc(e, "btts", "yes", 0.63, 1.9), sc(e, "btts", "no", 0.37, 2.4)]
    result = representative_predictions(pool)
    assert sorted(s.line.market for s in result.predictions) == ["btts", "result3", "total"]
    assert len(result.predictions) == 3                                   # not 9 lines, and not one prediction
    total = next(s for s in result.predictions if s.line.market == "total")
    assert (total.line.selection, total.line.line) == ("over", 2.5)       # the main line, never Over 0.5 at 93%


def test_one_representative_per_canonical_event_and_family_even_across_provider_event_objects():
    a, b = ev(1, home="Arsenal", away="Chelsea"), ev(2, home="Arsenal FC", away="Chelsea")
    a.canonical_event_id = b.canonical_event_id = "football:arsenal-chelsea:2030-06-01"
    result = representative_predictions([sc(a, "result3", "home", 0.70, 1.5), sc(b, "total", "over", 0.68, 1.6, line=2.5),
                                         sc(b, "result3", "home", 0.69, 1.55)])
    assert sorted(s.line.market for s in result.predictions) == ["result3", "total"]       # two families, one fixture


def test_the_engine_requires_canonical_identity():
    with pytest.raises(ValueError):
        representative_predictions([sc(ev(1), "result3", "home", 0.70, 1.5)])      # unresolved event: refused, not guessed


def test_the_database_enforces_one_row_per_event_family_and_day(db):
    e = _fx(1)
    e.canonical_event_id = fixture_key(e)
    stored(db, [sc(e, "result3", "home", 0.70, 1.5), sc(e, "total", "over", 0.62, 1.8, line=2.5)])
    rows = [dict(r) for r in db.collection_predictions('all', DAY)]
    assert len(rows) == 2 and {r['market'] for r in rows} == {"result3", "total"}      # two predictions, one fixture
    row = dict(rows[0])
    first = row['id']
    for k in ('id', 'collection', 'collection_rank'):
        row.pop(k)
    assert db.store_prediction(dict(row, price=9.9)) == first               # first write wins, immutable
    assert len(db.collection_predictions('all', DAY)) == 2
    assert db.query("SELECT price FROM picks WHERE id=?", (first,))[0]['price'] != 9.9


def test_several_predictions_of_one_fixture_are_independent_rows_that_settle_independently(db):
    e = _fx(1)
    e.canonical_event_id = fixture_key(e)
    stored(db, [sc(e, "result3", "home", 0.70, 1.5), sc(e, "total", "over", 0.62, 1.8, line=2.5)])
    ids = {r['market']: r['id'] for r in db.collection_predictions('all', DAY)}
    db.settle_pick(ids["result3"], "won")
    db.settle_pick(ids["total"], "lost")
    assert db.query("SELECT status FROM picks WHERE id=?", (ids["result3"],))[0]['status'] == 'won'
    assert db.query("SELECT status FROM picks WHERE id=?", (ids["total"],))[0]['status'] == 'lost'
    block = stats_mod.collection_stats(db, 'all', dt(hh=23))['all']
    assert block['issued'] == 2 and block['fixtures'] == 1 and block['won'] == 1 and block['lost'] == 1
