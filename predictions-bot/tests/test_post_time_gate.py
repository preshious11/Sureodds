"""A plain user read on a cache miss BEFORE POST_TIME must not generate the day's
slate (whoever asked first used to freeze it for everyone at a random early
moment). At/after POST_TIME, and for the deliberate generators, it is unchanged."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from predbot.bot import callbacks
from predbot.cache.fetcher import UnifiedFetcher
from predbot.config import Config
from predbot.jobs import daily_picks
from predbot.selection.picker import PickSet
from tests.test_picker import sc
from tests.real_events import pickset
from tests.test_picker import ev
from tests.test_subscribers_broadcast import FakeTG

LAGOS = "Africa/Lagos"          # UTC+1, POST_TIME 09:00 -> 08:00Z


def _pickset():
    a, b, c = ev(1), ev(2), ev(3)
    return pickset([sc(a, "result3", "home", 0.72, 1.45), sc(b, "result3", "home", 0.72, 1.45),
                    sc(c, "result3", "home", 0.70, 1.50)])


@pytest.fixture
def world(db, monkeypatch):
    clock = {"now": datetime(2030, 6, 1, 7, 59, 59, tzinfo=timezone.utc)}      # 08:59:59 Lagos
    monkeypatch.setattr("predbot.cache.fetcher.utcnow", lambda: clock["now"])
    monkeypatch.setattr(daily_picks, "utcnow", lambda: clock["now"])
    built = Mock(side_effect=lambda *a, **k: _pickset())
    monkeypatch.setattr(daily_picks, "build_picks", built)
    cfg = Config(timezone=LAGOS, post_time="09:00")
    fetcher = UnifiedFetcher(Mock(), db, cfg)

    def at(hh, mm, ss=0):
        clock["now"] = datetime(2030, 6, 1, hh, mm, ss, tzinfo=timezone.utc)

    return fetcher, db, cfg, built, at


def _stored(db):
    return db.get_prediction_cache(f"daily_predictions:{LAGOS}:2030-06-01"), db.has_picks_for_date("2030-06-01")


# ------------------------------------------------------------------ both sides
def test_a_user_read_one_second_before_post_time_does_not_generate(world):
    fetcher, db, _, built, at = world

    snap = fetcher.get_daily_snapshot()                          # 08:59:59 Lagos

    built.assert_not_called()
    assert snap.total_picks() == 0
    assert snap.generation["reason"] == "before_post_time"
    assert snap.generation["ready_at"] == "09:00 WAT"
    assert _stored(db) == (None, False)                          # nothing cached or stored


def test_a_user_read_at_post_time_generates_and_caches_for_everyone(world):
    fetcher, db, _, built, at = world
    at(8, 0, 0)                                                  # 09:00:00 Lagos

    first = fetcher.get_daily_snapshot()
    second = fetcher.get_daily_snapshot()

    assert built.call_count == 1                                 # generated once...
    assert first.total_picks() == 3 and second.total_picks() == 3    # ...then served from cache
    row, has_picks = _stored(db)
    assert row is not None and has_picks


def test_the_scheduled_generation_is_never_blocked(world):
    """Admin /run and the POST_TIME job pass scheduled=True: they build the slate
    at any hour (the job itself only fires at POST_TIME)."""
    fetcher, db, _, built, at = world

    snap = fetcher.get_daily_snapshot(scheduled=True)            # still 08:59:59

    assert built.call_count == 1 and snap.total_picks() == 3


def test_a_forced_refresh_is_not_blocked_either(world):
    fetcher, db, _, built, at = world

    fetcher.get_daily_snapshot(force_refresh=True)

    assert built.call_count == 1


def test_once_generated_an_early_read_is_a_plain_cache_hit(world):
    fetcher, db, _, built, at = world
    fetcher.get_daily_snapshot(scheduled=True)
    at(7, 30)                                                    # 08:30 Lagos, still before POST_TIME

    snap = fetcher.get_daily_snapshot()

    assert built.call_count == 1 and snap.total_picks() == 3


def test_delivery_polling_is_unchanged(world):
    fetcher, db, _, built, at = world
    at(9, 0)
    snap = fetcher.published_snapshot()                          # allow_api_call=False

    built.assert_not_called()
    assert snap.total_picks() == 0


def test_the_boundary_follows_the_configured_post_time_and_zone(db, monkeypatch):
    monkeypatch.setattr(daily_picks, "build_picks", Mock(side_effect=lambda *a, **k: _pickset()))
    cfg = Config(timezone="America/New_York", post_time="07:30")
    now = {"v": datetime(2030, 6, 1, 11, 29, tzinfo=timezone.utc)}          # 07:29 EDT
    monkeypatch.setattr("predbot.cache.fetcher.utcnow", lambda: now["v"])
    monkeypatch.setattr(daily_picks, "utcnow", lambda: now["v"])
    fetcher = UnifiedFetcher(Mock(), db, cfg)

    early = fetcher.get_daily_snapshot()
    now["v"] = datetime(2030, 6, 1, 11, 30, tzinfo=timezone.utc)                # 07:30 EDT
    on_time = fetcher.get_daily_snapshot()

    assert early.generation["ready_at"] == "07:30 EDT" and early.total_picks() == 0
    assert on_time.total_picks() == 3


# ------------------------------------------------------- what the user is told
def test_top_and_daily_commands_tell_the_user_when_picks_are_ready(world):
    fetcher, db, cfg, built, at = world
    tg = FakeTG()

    for cmd in ("/top", "/daily"):
        callbacks.handle_command(cmd, "42", service=fetcher, tg=tg, cfg=cfg, db=db)

    assert [s["text"] for s in tg.sends] == ["📋 Today's picks are ready at 09:00 WAT."] * 2
    built.assert_not_called()


def test_after_post_time_the_same_command_shows_the_picks(world):
    fetcher, db, cfg, built, at = world
    at(8, 0)
    tg = FakeTG()

    callbacks.handle_command("/top", "42", service=fetcher, tg=tg, cfg=cfg, db=db)

    assert "ready at" not in tg.sends[0]["text"] and "TOP PICKS" in tg.sends[0]["text"]


def test_stats_still_works_before_post_time(world):
    fetcher, db, cfg, built, at = world
    tg = FakeTG()

    callbacks.handle_command("/stats", "42", service=fetcher, tg=tg, cfg=cfg, db=db)

    assert "STATS" in tg.sends[0]["text"] or "No results yet" in tg.sends[0]["text"]
    assert "ready at" not in tg.sends[0]["text"]
    built.assert_not_called()
