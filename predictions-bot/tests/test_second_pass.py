"""Second-pass corrections: dry-run uses the real pipeline, canonical fixture counting, disabled markets."""
from __future__ import annotations

from unittest.mock import Mock

from predbot.cache.fetcher import UnifiedFetcher
from predbot.config import Config
from predbot.jobs import daily_picks
from predbot.model.engine import representative_predictions
from predbot.selection.picker import PredictionPool
from predbot.storage.identity import failure_record, unique_failed_fixtures
from predbot.util.pipeline_report import pipeline_lines
from tests.conftest import dt
from tests.real_events import pickset
from tests.test_picker import ev, sc
from tests.pipeline_helpers import select

NOW = dt(hh=12)
DAY = "2030-06-01"


def _slate():
    return pickset([sc(ev(i), "result3", "home", 0.72 + i * 0.001, 1.45) for i in range(1, 5)])


def _fetcher(db, monkeypatch):
    monkeypatch.setattr("predbot.cache.fetcher.utcnow", lambda: NOW)
    monkeypatch.setattr(daily_picks, "utcnow", lambda: NOW)
    monkeypatch.setattr(daily_picks, "build_picks", lambda *a, **k: _slate())
    return UnifiedFetcher(Mock(), db, Config(timezone="UTC"))


def _shape(snapshot):
    key = lambda items: [(p.home, p.market, p.selection, p.price) for p in items]
    return key(snapshot.all_predictions), key(snapshot.top_picks), key(snapshot.daily_picks)


def test_dry_run_produces_all_top_and_daily_through_the_same_selectors_and_writes_nothing(db, monkeypatch):
    fetcher = _fetcher(db, monkeypatch)
    dry = fetcher.get_daily_snapshot(persist=False)
    assert db.get_prediction_cache(f"daily_predictions:UTC:{DAY}") is None and not db.has_picks_for_date(DAY)
    assert db.query("SELECT COUNT(*) AS n FROM prediction_memberships")[0]["n"] == 0
    assert len(dry.all_predictions) == 4 and dry.top_picks and dry.daily_picks      # complete pipeline

    real = fetcher.get_daily_snapshot(persist=True)
    assert _shape(real) == _shape(dry)                      # identical collections: one selector implementation
    assert db.has_picks_for_date(DAY)


def test_pool_interface_is_shared_and_a_scratch_pool_is_discarded(db):
    ps = _slate()
    scratch = PredictionPool.scratch(DAY)
    scratch.store(ps)
    scratch.package(Config(), "2030-06-01T08:00:00Z")
    assert len(scratch.rows("all")) == 4 and scratch.rows("top")
    scratch.close()
    persistent = PredictionPool.persistent(db, DAY)
    assert persistent.rows("all") == []                     # nothing leaked into the real store


def test_a_fixture_failing_through_two_providers_counts_once_and_a_resolved_one_not_at_all():
    a, b, c = ev(1), ev(2), ev(3)
    records = [failure_record(a, "unresolved_participant"),          # provider 1
               failure_record(a, "unresolved_participant"),          # provider 2: the same fixture
               failure_record(b, "unresolved_participant"),          # failed here, resolved via another provider
               failure_record(c, "unsupported_competition")]
    unique = unique_failed_fixtures(records, kept_events=[b])
    assert sorted(unique.values()) == ["unresolved_participant", "unsupported_competition"]
    assert len(unique) == 2


def test_event_level_and_line_level_rejections_are_reported_separately():
    e = ev(1)
    e.canonical_event_id = "football:e1"
    lines = [sc(e, "prop", "over", 0.6, 2.0, line=1.5) for _ in range(5)]      # five unpriceable lines, one event
    gen = representative_predictions(lines)
    assert gen.predictions == []
    assert gen.diagnostics["line_rejection_count"] == 5 and gen.diagnostics["event_no_valid_market"] == {"invalid_market": 1}
    assert gen.diagnostics["line_rejections_by_market"] == {"prop": {"invalid_market": 5}}
    text = "\n".join(pipeline_lines({"line_rejection_count": 5, "line_rejections": {"invalid_market": 5},
                                     "event_rejections": {"invalid_market": 1}, "markets_evaluated": 5}))
    assert "events not analysed: invalid_market=1" in text and "rejected: 5 (invalid_market=5)" in text


def test_a_disabled_market_stays_in_all_and_is_blocked_only_downstream():
    from dataclasses import replace as dc_replace
    e = ev(2)
    btts, win = sc(e, "btts", "yes", 0.80, 1.8), sc(e, "result3", "home", 0.70, 1.8)
    ps = select([btts, win], dc_replace(Config(), disabled_markets=["btts"]), dt())
    assert sorted(s.line.market for s in ps.all()) == ["btts", "result3"]       # both are predictions of the fixture
    assert all(s.line.market != "btts" for s in ps.top + ps.daily)
    assert ps.diagnostics["top"]["rejections"]["disabled_market"] == 1
