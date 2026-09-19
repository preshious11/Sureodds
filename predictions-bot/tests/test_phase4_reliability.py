"""Phase 4: scheduler and cache reliability - PARTIAL retry, pick replacement,
deadline-keyed dynamic job, DELIVERY_TIME, non-blocking reads, resilient loop."""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from predbot.bot.pagination import empty_view_message
from predbot.cache.models import DailySnapshot
from predbot.config import Config
from predbot.jobs import daily_picks, scheduler, settle
from predbot.jobs.scheduler import DynamicJob
from predbot.selection.picker import select
from tests.conftest import dt
from tests.test_picker import ev, sc
from tests.test_sqlite_prediction_cache import clock, service  # noqa: F401  (fixtures)


def utc(hh, mm=0):
    return datetime(2030, 6, 1, hh, mm, tzinfo=timezone.utc)


# ================================================================== 4a retry_partial
@pytest.fixture
def partial_day(db, monkeypatch, clock):
    """A PARTIAL snapshot generated at 09:00 WAT (08:00 UTC); football failed."""
    clock[0] = utc(8)
    app, football, baseball = service(db, monkeypatch, football_error=True)
    assert app.get_daily_snapshot(scheduled=True).status == "PARTIAL"
    return app, football, baseball, clock


def test_retry_partial_regenerates_once_retry_after_has_passed(partial_day):
    app, football, baseball, clock = partial_day
    football.fetch_events.side_effect = None                     # the provider recovered
    clock[0] = utc(8, 5)
    assert app.retry_partial() is None                           # retry_after (+15 min) not reached
    clock[0] = utc(8, 16)

    snapshot = app.retry_partial()

    assert snapshot is not None and snapshot.status == "COMPLETE"
    assert app.get_daily_snapshot().status == "COMPLETE"          # what users are now served


def test_retry_partial_does_nothing_for_a_complete_snapshot(db, monkeypatch, clock):
    clock[0] = utc(8)
    app, football, baseball = service(db, monkeypatch)
    assert app.get_daily_snapshot(scheduled=True).status == "COMPLETE"
    clock[0] = utc(8, 20)
    assert app.retry_partial() is None and football.fetch_events.call_count == 1


def test_retry_partial_stops_at_delivery_time(partial_day):
    app, football, baseball, clock = partial_day
    football.fetch_events.side_effect = None
    clock[0] = utc(8, 30)                                        # 09:30 WAT == DELIVERY_TIME
    assert app.retry_partial() is None


def test_retry_partial_stops_once_anything_has_been_delivered(partial_day, db):
    app, football, baseball, clock = partial_day
    football.fetch_events.side_effect = None
    db.mark_delivery("picks", "2030-06-01", "42")
    clock[0] = utc(8, 16)
    assert app.retry_partial() is None


def test_retry_partial_is_registered_as_an_interval_job():
    import main
    src = open(main.__file__, encoding="utf-8").read()
    assert 'IntervalJob("retry_partial", max(1, cfg.cache_retry_seconds // 60)' in src


def test_the_partial_message_says_how_long_it_retries():
    snapshot = DailySnapshot(date="2030-06-01", status="PARTIAL",
                             generation={"retry_after": "2030-06-01T08:15:00Z", "retry_until": "09:30"})
    assert "retries automatically until 09:30" in empty_view_message(snapshot, "daily", "UTC")


# ============================================================ 4b regeneration replaces
def _pickset(*events):
    return select([sc(e, "result3", "home", 0.75, 1.45) for e in events], Config(), dt())


def _pending(db):
    return {r["provider_event_id"] for r in db.picks_for_date("2030-06-01")}


def test_regenerating_a_day_nobody_was_sent_replaces_its_pending_picks(db):
    a, b, c = ev(1), ev(2), ev(3)
    daily_picks.store_picks(_pickset(a, b), db, "2030-06-01")
    assert _pending(db) == {"1", "2"}

    daily_picks.store_picks(_pickset(a, c), db, "2030-06-01")

    assert _pending(db) == {"1", "3"}                          # 2 was stale, 3 is new, 1 is untouched


def test_after_delivery_the_stored_rows_are_never_touched(db):
    a, b, c = ev(1), ev(2), ev(3)
    daily_picks.store_picks(_pickset(a, b), db, "2030-06-01")
    db.mark_delivery("picks", "2030-06-01", "42")

    daily_picks.store_picks(_pickset(a, c), db, "2030-06-01")

    assert _pending(db) == {"1", "2", "3"}


def test_settled_picks_are_not_deleted_by_a_replacement(db):
    a, b = ev(1), ev(2)
    daily_picks.store_picks(_pickset(a, b), db, "2030-06-01")
    row = [r for r in db.picks_for_date("2030-06-01") if r["provider_event_id"] == "2"][0]
    db.settle_pick(row["id"], "won", {})

    daily_picks.store_picks(_pickset(a), db, "2030-06-01")

    assert _pending(db) == {"1", "2"}


def test_a_failed_regeneration_that_keeps_the_previous_picks_keeps_their_rows(db, monkeypatch, clock):
    clock[0] = utc(8)
    app, football, baseball = service(db, monkeypatch, football_error=True)
    first = app.get_daily_snapshot(scheduled=True)                   # PARTIAL, baseball picks only
    stored = _pending(db)
    assert first.status == "PARTIAL" and stored and first.total_picks()
    monkeypatch.setattr(daily_picks, "build_picks", lambda *a, **k: _incomplete_empty())

    kept = app.get_daily_snapshot(force_refresh=True, scheduled=True)

    assert kept.total_picks() == first.total_picks()                 # the published selections stay ...
    assert _pending(db) == stored                                    # ... and so do their rows


def _incomplete_empty():
    from predbot.selection.picker import PickSet
    ps = PickSet()
    ps.complete = False
    return ps


# ============================================================ 4c deadline-keyed job
def test_dynamic_job_runs_for_a_late_deadline_after_an_early_one_on_the_same_local_day():
    deadlines = [datetime(2030, 6, 2, 0, 30, tzinfo=timezone.utc)]        # 01:30 WAT, yesterday's late match
    runs = []
    job = DynamicJob("settle", "Africa/Lagos", lambda now: deadlines[0], lambda: runs.append(1))

    assert job.due(datetime(2030, 6, 2, 0, 31, tzinfo=timezone.utc)) is True
    job.run(datetime(2030, 6, 2, 0, 31, tzinfo=timezone.utc))
    assert job.due(datetime(2030, 6, 2, 0, 40, tzinfo=timezone.utc)) is False     # same deadline: once only

    deadlines[0] = datetime(2030, 6, 2, 22, 0, tzinfo=timezone.utc)        # 23:00 WAT, today's matches
    assert job.due(datetime(2030, 6, 2, 21, 0, tzinfo=timezone.utc)) is False     # not reached yet
    assert job.due(datetime(2030, 6, 2, 22, 5, tzinfo=timezone.utc)) is True      # SAME local day: still runs
    time.sleep(0.05)
    assert runs == [1]


def test_dynamic_job_with_no_deadline_is_not_due():
    assert DynamicJob("s", "UTC", lambda now: None, lambda: None).due(utc(12)) is False


# ================================================================ 4d DELIVERY_TIME
def test_delivery_time_defaults_to_0930_and_warns_when_not_after_post_time(caplog, monkeypatch):
    monkeypatch.delenv("DELIVERY_TIME", raising=False)
    assert Config().delivery_time == "09:30"
    with caplog.at_level(logging.WARNING):
        Config(post_time="09:30", delivery_time="09:30")
        Config(post_time="10:00", delivery_time="09:30")
    assert sum("DELIVERY_TIME" in r.message for r in caplog.records) == 2
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        Config(post_time="09:00", delivery_time="09:30")
    assert not any("DELIVERY_TIME" in r.message for r in caplog.records)


# ================================================== 4e reads never queue behind generation
def _hold_lock(db, seconds):
    ready, release = threading.Event(), threading.Event()

    def hold():
        with db.prediction_lock:
            ready.set()
            release.wait(seconds)

    t = threading.Thread(target=hold, daemon=True)
    t.start()
    ready.wait(2)
    return release, t


@pytest.mark.parametrize("kwargs", [{}, {"allow_api_call": False}])
def test_a_user_read_during_generation_gets_a_placeholder_without_waiting(db, monkeypatch, clock, kwargs):
    clock[0] = utc(9)
    app, football, baseball = service(db, monkeypatch)
    release, t = _hold_lock(db, 5)
    started = time.monotonic()

    snapshot = app.get_daily_snapshot(**kwargs)

    release.set(); t.join(2)
    assert time.monotonic() - started < 1.0
    assert snapshot.status == "PARTIAL" and snapshot.generation["reason"] == "generating"
    assert "being generated" in empty_view_message(snapshot, "daily", "UTC") and "try again in a minute" in \
        empty_view_message(snapshot, "top", "UTC")
    football.fetch_events.assert_not_called()


def test_a_read_during_generation_still_serves_an_already_published_card(db, monkeypatch, clock):
    clock[0] = utc(9)
    app, football, baseball = service(db, monkeypatch)
    published = app.get_daily_snapshot(scheduled=True)
    release, t = _hold_lock(db, 5)

    snapshot = app.get_daily_snapshot(allow_api_call=False)

    release.set(); t.join(2)
    assert snapshot.total_picks() == published.total_picks() and snapshot.total_picks() > 0


def test_scheduled_generation_still_waits_for_the_lock(db, monkeypatch, clock):
    clock[0] = utc(9)
    app, football, baseball = service(db, monkeypatch)
    release, t = _hold_lock(db, 5)
    threading.Timer(0.3, release.set).start()
    started = time.monotonic()

    snapshot = app.get_daily_snapshot(scheduled=True)

    assert time.monotonic() - started >= 0.25 and snapshot.total_picks() > 0
    t.join(2)


# ============================================================ 4f resilient scheduler
class _Stop(BaseException):
    pass


def test_one_broken_job_does_not_stop_the_scheduler_loop(monkeypatch, caplog):
    monkeypatch.setattr(scheduler.signal, "signal", lambda *a: None)
    monkeypatch.setattr(scheduler.time, "sleep", Mock(side_effect=_Stop))

    class Broken:
        name = "broken"

        def due(self, now):
            raise RuntimeError("bad due")

    class BrokenRun:
        name = "broken-run"

        def due(self, now):
            return True

        def run(self, now):
            raise RuntimeError("bad run")

    ran = []

    class Fine:
        name = "fine"

        def due(self, now):
            return True

        def run(self, now):
            ran.append(1)

    with caplog.at_level(logging.ERROR), pytest.raises(_Stop):
        scheduler.loop([Broken(), BrokenRun(), Fine()], poll_seconds=0)

    assert ran == [1]
    assert sum("failed to check/dispatch" in r.message for r in caplog.records) == 2
    assert all(r.exc_info for r in caplog.records if "failed to check/dispatch" in r.message)


def _insert(db, i, start):
    db.insert_pick({"pick_date": "2030-06-01", "tier": "daily", "rank": i, "provider": "p", "provider_event_id": str(i),
                    "sport": "football", "league": "L", "home": "A", "away": "B", "start_utc": start,
                    "market": "result3", "period": "full", "stat": "points", "entity": "all", "line": None,
                    "selection": "home", "label": "x", "price": 1.5, "prob": 0.7, "ev": 0.05, "p_market": 0.68,
                    "sources": {}, "provider_odd_id": ""})


def test_settlement_deadline_skips_unparseable_start_times(db):
    _insert(db, 1, "2030-06-01T18:00:00Z")
    _insert(db, 2, "garbage")
    _insert(db, 3, "2030-06-01T20:00:00Z")

    deadline = settle.compute_settlement_deadline(db, "2030-06-01")

    assert deadline == datetime(2030, 6, 1, 20, 0, tzinfo=timezone.utc) + timedelta(hours=2.2, minutes=15)


def test_settlement_deadline_is_none_when_no_start_time_parses(db):
    _insert(db, 1, "garbage")
    _insert(db, 2, "")
    assert settle.compute_settlement_deadline(db, "2030-06-01") is None
