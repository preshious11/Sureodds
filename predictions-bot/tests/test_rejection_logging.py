"""Part 4 (2026-10-23): durable rejection logging for select_top()/select_daily().

The single most important property this file proves: wiring in rejection logging never changes what
gets selected. Everything else (full-detail rows, the duplicate_event winner, the three volume-controlled
summary reasons, joinability to match_results) is secondary to that guarantee.
"""
from __future__ import annotations

import copy
from collections import Counter

import pytest

from predbot.config import Config
from predbot.history.store import HistoryStore
from predbot.selection.daily import select_daily
from predbot.selection.picker import PredictionPool, package_predictions
from predbot.selection.rejections import SUMMARY_ONLY_REASONS, collector
from predbot.selection.top import select_top
from predbot.storage.db import Database
from tests.conftest import dt
from tests.pipeline_helpers import select as _select
from tests.test_picker import cfg, ev, sc


def select(scored, cfg, now, **kw):
    kw.setdefault("raw", True)
    return _select(scored, cfg, now, **kw)


def _pool(db, day):
    return PredictionPool(db, day).rows('all')


def _build_pool(now):
    """A realistic mixed pool: some clean picks, some duplicate-event collisions (same fixture, two
    markets), some below the probability floor, some single-book, so every rejection reason this test
    cares about actually fires."""
    db = Database(':memory:')
    scored = []
    for i in range(8):
        e = ev(i)
        scored.append(sc(e, "result3", "home", 0.72, 1.6))
        scored.append(sc(e, "total", "over", 0.70, 1.55, line=2.5))   # same fixture -> duplicate_event for one of them
    for i in range(20, 26):
        scored.append(sc(ev(i), "result3", "home", 0.10, 1.9, ev_val=-0.3))  # below_probability
    for i in range(30, 33):
        scored.append(sc(ev(i), "result3", "home", 0.70, 1.8, sources=1, books=1))  # insufficient_books
    ps = select(scored, cfg(), now)
    day = now.date().isoformat()
    return ps, day


# ------------------------------------------------------------- the critical guarantee
def test_selection_output_is_byte_identical_with_and_without_logging():
    """select_top/select_daily called TWICE on the exact same input rows - once with no sink (the old
    behaviour) and once with a real sink wired in - must return identical (selected, diagnostics)."""
    now = dt()
    ps, day = _build_pool(now)
    db = Database(':memory:')
    try:
        from predbot.jobs.daily_picks import store_picks
        from predbot.selection.picker import PickSet
        full = PickSet(predictions=[s for s in ps.predictions], diagnostics=ps.diagnostics)
        store_picks(full, db, day)
        pool = _pool(db, day)

        now_utc_iso = now.isoformat()
        c = cfg()

        baseline_top = select_top(copy.deepcopy(pool), c, now_utc_iso)
        logged_calls = []
        sink = lambda row, reason, winner_row=None: logged_calls.append((reason, winner_row))
        logged_top = select_top(copy.deepcopy(pool), c, now_utc_iso, reject_sink=sink)
        assert baseline_top == logged_top
        assert logged_calls   # the sink really was exercised, not a no-op test

        baseline_daily = select_daily(copy.deepcopy(pool), c, now_utc_iso)
        logged_calls_d = []
        sink_d = lambda row, reason, winner_row=None: logged_calls_d.append((reason, winner_row))
        logged_daily = select_daily(copy.deepcopy(pool), c, now_utc_iso, reject_sink=sink_d)
        assert baseline_daily == logged_daily
        assert logged_calls_d
    finally:
        db.close()


def test_package_predictions_selection_unaffected_by_rejection_logging():
    """The higher-level entry point (picker.package, which now always wires a sink for a persistent
    pool) must select exactly the same Top/Daily as it would with logging ripped out entirely."""
    now = dt()
    ps, day = _build_pool(now)
    # ps already went through the now-instrumented package_predictions (via tests.pipeline_helpers.select).
    # Rebuild the same pool from scratch through the RAW selectors with no sink and compare.
    db = Database(':memory:')
    try:
        from predbot.jobs.daily_picks import store_picks
        from predbot.selection.picker import PickSet
        full = PickSet(predictions=[s for s in ps.predictions], diagnostics={})
        store_picks(full, db, day)
        pool = _pool(db, day)
        c = cfg()
        raw_top, raw_top_diag = select_top(pool, c, now.isoformat())
        raw_daily, _ = select_daily([r for r in pool if r['id'] not in {t['id'] for t in raw_top}], c, now.isoformat())
        assert {t.event.canonical_event_id for t in ps.top} == {t['canonical_event_id'] for t in raw_top} or not ps.top
        assert len(ps.top) == len(raw_top)
        assert len(ps.daily) == len(raw_daily)
    finally:
        db.close()


# ------------------------------------------------------------- persistence / detail
def test_full_detail_rejection_rows_are_persisted_with_candidate_identity():
    now = dt()
    ps, day = _build_pool(now)
    db = Database(':memory:')
    try:
        from predbot.jobs.daily_picks import store_picks
        from predbot.selection.picker import PickSet
        full = PickSet(predictions=[s for s in ps.predictions], diagnostics={})
        store_picks(full, db, day)
        PredictionPool(db, day).package(cfg(), now.isoformat())
        rows = db.rejections_for(day)
        assert rows
        full_detail = [r for r in rows if r['reason'] not in SUMMARY_ONLY_REASONS]
        assert full_detail
        for r in full_detail:
            assert r['candidate_id']                       # a real sha256 identity, never blank
            assert r['count'] == 1
        dup = [r for r in rows if r['reason'] == 'duplicate_event']
        assert dup and all(r['duplicate_winner_candidate_id'] for r in dup)
        # the winner really exists among the stored candidate identities for that pool
        pool_ids = {db._candidate_identity(row) for row in _pool(db, day)}
        for r in dup:
            assert r['duplicate_winner_candidate_id'] in pool_ids
    finally:
        db.close()


def test_volume_controlled_reasons_collapse_into_one_summary_row_per_day_and_product():
    now = dt()
    ps, day = _build_pool(now)
    db = Database(':memory:')
    try:
        from predbot.jobs.daily_picks import store_picks
        from predbot.selection.picker import PickSet
        full = PickSet(predictions=[s for s in ps.predictions], diagnostics={})
        store_picks(full, db, day)
        PredictionPool(db, day).package(cfg(), now.isoformat())
        rows = db.rejections_for(day)
        for reason in SUMMARY_ONLY_REASONS:
            summary_rows = [r for r in rows if r['reason'] == reason and r['candidate_id'] is None]
            # at most one row per (product, reason): group and check
            by_product = Counter(r['product'] for r in summary_rows)
            assert all(c == 1 for c in by_product.values())
            for r in summary_rows:
                assert r['candidate_id'] is None and r['canonical_event_id'] is None
                assert r['count'] >= 1
    finally:
        db.close()


def test_scratch_pool_never_writes_rejection_rows():
    """A scratch pool's database is thrown away; logging into it would just be wasted writes (see
    PredictionPool.package's docstring)."""
    now = dt()
    ps, day = _build_pool(now)
    scratch = PredictionPool.scratch(day)
    try:
        from predbot.jobs.daily_picks import store_picks
        from predbot.selection.picker import PickSet
        full = PickSet(predictions=[s for s in ps.predictions], diagnostics={})
        store_picks(full, scratch.db, day)
        scratch.package(cfg(), now.isoformat())
        assert scratch.db.rejections_for(day) == []
    finally:
        scratch.close()


# ------------------------------------------------------------- joinability
def test_rejected_candidates_canonical_event_id_joins_to_match_results():
    """canonical_event_id must sit in the SAME identity space match_results/HistoryStore use
    (canonical_events.id -> competition_id/home_id/away_id/kickoff_utc), not a lookalike id that
    happens to print the same - verified with a REAL join, not just by inspecting the column."""
    now = dt()
    db = Database(':memory:')
    try:
        e = ev(0)
        e.canonical_event_id = "event:joinable-test"
        e.home_id, e.away_id, e.canonical_competition_id = "home-1", "away-1", "epl"
        db.execute("INSERT OR IGNORE INTO canonical_events VALUES(?,?,?,?,?)",
                  (e.canonical_event_id, e.canonical_competition_id, e.home_id, e.away_id,
                   e.start.strftime("%Y-%m-%dT%H:%M:%SZ")))
        HistoryStore(db).add_result(sport="football", competition_id=e.canonical_competition_id,
                                    kickoff=e.start, home_id=e.home_id, away_id=e.away_id,
                                    home_goals=2, away_goals=1, source="test", source_match_id="joinable-1")
        # Both legs clear every promotion gate on their own (real prob, real book coverage); the second
        # loses ONLY to duplicate_event because it shares the fixture with the higher-ranked first leg.
        winner_scored = sc(e, "result3", "home", 0.72, 1.6)
        dup_scored = sc(e, "total", "over", 0.65, 1.55, line=2.5)
        from predbot.jobs.daily_picks import store_picks
        from predbot.selection.picker import PickSet
        day = now.date().isoformat()
        store_picks(PickSet(predictions=[winner_scored, dup_scored], diagnostics={}), db, day)
        PredictionPool(db, day).package(cfg(), now.isoformat())
        rows = [r for r in db.rejections_for(day) if r['canonical_event_id'] == e.canonical_event_id]
        assert rows, "expected at least one rejection row for the joinable fixture"
        joined = db.query(
            "SELECT m.home_goals, m.away_goals FROM selection_rejections sr "
            "JOIN canonical_events ce ON ce.id = sr.canonical_event_id "
            "JOIN match_results m ON m.competition_id = ce.competition_id AND m.home_id = ce.home_id "
            "AND m.away_id = ce.away_id AND substr(m.kickoff_utc,1,10) = substr(ce.kickoff_utc,1,10) "
            "WHERE sr.canonical_event_id = ?", (e.canonical_event_id,))
        assert joined and joined[0]['home_goals'] == 2 and joined[0]['away_goals'] == 1
    finally:
        db.close()


def test_a_row_rejected_in_the_greedy_pass_but_published_by_search_is_not_logged_as_rejected():
    """select_top/select_daily log rejections during their greedy pass, then search() may publish a
    combination that includes one of those rows. flush(selected) must drop it from the log."""
    db = Database(':memory:')
    sink, flush = collector(db, "2026-10-23", "daily", "run-a", 20)
    published = {"id": "p1", "canonical_event_id": "e1", "market": "result3", "price": 1.6, "prob": 0.7}
    rejected = {"id": "r1", "canonical_event_id": "e2", "market": "result3", "price": 1.5, "prob": 0.7}
    sink(published, "combined_odds_ceiling")     # rejected greedily, then chosen by search()
    sink(rejected, "diversity_limit")
    flush([published])
    reasons = [r["reason"] for r in db.query("SELECT reason FROM selection_rejections")]
    assert reasons == ["diversity_limit"]


def test_selection_is_identical_and_the_run_finishes_when_the_rejections_table_is_missing():
    """Migration 0015 not applied (or the table otherwise unwritable): every write fails, the failure is
    counted, and selection output is exactly what it is with logging off."""
    now = dt()
    ps, day = _build_pool(now)
    db = Database(':memory:')
    from predbot.jobs.daily_picks import store_picks
    from predbot.selection.picker import PickSet
    store_picks(PickSet(predictions=list(ps.predictions), diagnostics=ps.diagnostics), db, day)
    pool = _pool(db, day)
    c, now_utc_iso = cfg(), now.isoformat()
    baseline = select_daily(copy.deepcopy(pool), c, now_utc_iso)

    db.execute("DROP TABLE selection_rejections")
    sink, flush = collector(db, day, 'daily', 'run-a', 20)
    logged = select_daily(copy.deepcopy(pool), c, now_utc_iso, reject_sink=sink)
    errors = flush(logged[0])
    assert logged == baseline
    assert errors > 0


def _volume_sink(db, n, reason="below_probability", product="daily", sample_size=20, run_id="run-a"):
    """Feed ``n`` distinct below-probability candidates through a collector and flush them."""
    day = "2026-10-23"
    sink, flush = collector(db, day, product, run_id, sample_size)
    rows = [{"canonical_event_id": f"event:{i}", "market": "result3", "period": "full", "stat": "points",
             "entity": "all", "line": None, "selection": "home", "price": 1.6, "prob": 0.3, "ev": -0.1,
             "rank_score": 0.3, "quality": "main", "books": 4} for i in range(n)]
    for row in rows:
        sink(row, reason)
    flush([])
    return rows


def test_volume_sample_is_capped_and_deterministic():
    db_a, db_b = Database(':memory:'), Database(':memory:')
    _volume_sink(db_a, 75)
    _volume_sink(db_b, 75, run_id="run-b")
    sampled_a = sorted(r['candidate_id'] for r in db_a.query(
        "SELECT candidate_id FROM selection_rejections WHERE sampled=1"))
    sampled_b = sorted(r['candidate_id'] for r in db_b.query(
        "SELECT candidate_id FROM selection_rejections WHERE sampled=1"))
    assert len(sampled_a) == 20
    assert sampled_a == sampled_b   # same candidates -> same sample, regardless of run
    summary = db_a.query("SELECT count FROM selection_rejections WHERE candidate_id IS NULL")
    assert [r['count'] for r in summary] == [75]   # the summary still counts every candidate


def test_sample_size_is_configurable_and_zero_disables_sampling():
    db = Database(':memory:')
    _volume_sink(db, 30, sample_size=5)
    assert db.query("SELECT COUNT(*) AS n FROM selection_rejections WHERE sampled=1")[0]['n'] == 5
    db0 = Database(':memory:')
    _volume_sink(db0, 30, sample_size=0)
    assert db0.query("SELECT COUNT(*) AS n FROM selection_rejections WHERE sampled=1")[0]['n'] == 0


def test_two_runs_on_one_day_write_each_candidate_once_and_keep_the_first_run_id():
    db = Database(':memory:')
    _volume_sink(db, 30, run_id="run-first")
    _volume_sink(db, 30, run_id="run-second")
    total = db.query("SELECT COUNT(*) AS n FROM selection_rejections WHERE candidate_id IS NOT NULL")[0]['n']
    assert total == 20                       # the sample of the first run only; the second is ignored
    runs = {r['run_id'] for r in db.query("SELECT run_id FROM selection_rejections")}
    assert runs == {"run-first"}


def test_a_sampled_row_joins_to_match_results():
    db = Database(':memory:')
    db.execute("INSERT OR IGNORE INTO canonical_events VALUES(?,?,?,?,?)",
               ("event:sampled-join", "epl", "home-x", "away-x", dt().strftime("%Y-%m-%dT%H:%M:%SZ")))
    HistoryStore(db).add_result(sport="football", competition_id="epl", kickoff=dt(), home_id="home-x",
                                away_id="away-x", home_goals=1, away_goals=1, source="test",
                                source_match_id="sampled-join-1")
    sink, flush = collector(db, "2026-10-23", "daily", "run-a", 20)
    row = {"canonical_event_id": "event:sampled-join", "market": "result3", "period": "full", "stat": "points",
           "entity": "all", "line": None, "selection": "home", "price": 1.6, "prob": 0.3, "ev": -0.1,
           "rank_score": 0.3, "quality": "main", "books": 4}
    sink(row, "below_probability")
    flush([])
    joined = db.query(
        "SELECT m.home_goals, m.away_goals FROM selection_rejections sr "
        "JOIN canonical_events ce ON ce.id = sr.canonical_event_id "
        "JOIN match_results m ON m.competition_id = ce.competition_id AND m.home_id = ce.home_id "
        "AND m.away_id = ce.away_id AND substr(m.kickoff_utc,1,10) = substr(ce.kickoff_utc,1,10) "
        "WHERE sr.sampled = 1")
    assert joined and joined[0]['home_goals'] == 1 and joined[0]['away_goals'] == 1
