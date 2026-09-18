"""/lastrun: the last generation cycle, formatted for Telegram, admin only."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from predbot.bot.commands import CommandLoop
from predbot.bot.lastrun import lastrun_text
from predbot.cache.fetcher import UnifiedFetcher
from predbot.config import Config
from predbot.jobs import daily_picks
from predbot.selection.picker import PickSet
from tests.test_picker import ev, sc
from tests.test_subscribers_broadcast import FakeTG

LAGOS = "Africa/Lagos"
NOW = datetime(2030, 6, 2, 8, 0, 4, tzinfo=timezone.utc)          # 09:00:04 Lagos on 2030-06-02
ADMIN, USER = "111", "222"


def _pickset():
    a, b, c = ev(1), ev(2), ev(3)
    ps = PickSet(top=[sc(a, "result3", "home", 0.72, 1.45), sc(b, "result3", "home", 0.72, 1.45)],
                 daily=[sc(c, "result3", "home", 0.70, 1.50)])
    ps.complete = True
    ps.diagnostics = {
        "providers": {"api_football": {"status": "complete", "outcome": "success", "events": {"football": 30}},
                      "sharpapi": {"status": "partial", "outcome": "failed", "events": {"football": 12, "tennis": 5}}},
        "api_usage": {"api_football": {"cycle": 7, "today": 37, "limit": 100},
                      "sharpapi": {"cycle": 3, "today": 30, "limit": None}},
        "top_priority_provider_report": {"oddsapi": {
            "status": "complete", "outcome": "empty", "events": {},
            "quota": {"requests_remaining": 0, "requests_used": 500, "reserve_credits": 50,
                      "refused_to_spend": True,
                      "refusal": "x-requests-remaining=0, reserve=50 - refused to spend 3 credits"}}},
        "scored_by_sport": {"football": 900, "tennis": 40}, "base_by_sport": {"football": 800, "tennis": 30},
        "top_eligible_by_sport": {"football": 25}, "top_pool_rejections": {"BELOW_THRESHOLD": 700},
        "daily_eligible_by_sport": {"football": 90, "tennis": 4},
        "daily_pool_rejections": {"BELOW_THRESHOLD": 600, "RESERVED_FOR_TOP_PRIORITY_SOURCE": 12},
        "combinations_checked": 4, "top_odds": 2.1025,
        "daily_caps_relaxed": {"reason": "strict pass could not reach DAILY_COMBINED_MIN_ODDS",
                               "strict_ticket": 6.7, "exceeded": {"market": 2}},
    }
    return ps


@pytest.fixture
def world(db, monkeypatch):
    monkeypatch.setattr("predbot.cache.fetcher.utcnow", lambda: NOW)
    monkeypatch.setattr(daily_picks, "utcnow", lambda: NOW)
    monkeypatch.setattr(daily_picks, "build_picks", Mock(side_effect=lambda *a, **k: _pickset()))
    cfg = Config(timezone=LAGOS, post_time="09:00", telegram_token="sekret-token-123",
                 odds_api_key="sekret-odds-key", telegram_admin_ids=[ADMIN])
    return UnifiedFetcher(Mock(), db, cfg), db, cfg


def test_it_says_nothing_was_generated_when_nothing_was(db):
    assert "No generation has been recorded yet" in lastrun_text(db, Config(timezone=LAGOS))


def test_it_reports_the_whole_cycle(world):
    fetcher, db, cfg = world
    fetcher.get_daily_snapshot(scheduled=True)

    text = lastrun_text(db, cfg)

    assert "Last run" in text and "2030-06-02" in text and "status <b>COMPLETE</b>" in text
    assert "Trigger: scheduled/admin job" in text and "cache: <b>MISS</b>" in text
    # per-provider status and the calls THIS cycle spent
    assert "api_football" in text and "+7 calls (37/100 today)" in text
    assert "sharpapi" in text and "partial/failed" in text and "+3 calls (30/? today)" in text
    # Odds API remaining/used and the refusal
    assert "remaining <b>0</b>" in text and "used <b>500</b>" in text and "reserve 50" in text
    assert "refused to spend" in text
    # funnel and relaxations
    assert "scored 940 → eligible 830" in text and "BELOW_THRESHOLD=700" in text
    assert "RESERVED_FOR_TOP_PRIORITY_SOURCE=12" in text and "Top tickets evaluated: 4" in text
    assert "strict pass could not reach" in text and "&quot;market&quot;" not in text
    assert "Top 2 legs · combined 2.10" in text and "Daily 1 picks" in text


def test_it_shows_both_slates_exactly_as_the_subscriber_card_renders_them(world):
    fetcher, db, cfg = world
    fetcher.get_daily_snapshot(scheduled=True)

    text = lastrun_text(db, cfg)

    assert "Slates as sent" in text and "09:30" in text
    assert "TOP PICKS" in text and "DAILY PICKS" in text and "Prediction:" in text
    assert "Generated 9:00 AM WAT" in text


def test_it_reports_cache_hits_and_misses_for_that_day(world):
    fetcher, db, cfg = world
    fetcher.get_daily_snapshot(scheduled=True)              # the MISS that generated it
    fetcher.get_daily_snapshot()
    fetcher.get_daily_snapshot()                            # two HITs

    assert "reads that day: 2 hit / 1 miss" in lastrun_text(db, cfg)


def test_a_user_triggered_generation_is_labelled_as_such(world):
    fetcher, db, cfg = world
    fetcher.get_daily_snapshot()                            # 09:00:04 - after POST_TIME, a plain user read

    assert "Trigger: user command" in lastrun_text(db, cfg)


def test_it_never_prints_a_secret(world):
    fetcher, db, cfg = world
    fetcher.get_daily_snapshot(scheduled=True)
    db.set_kv("oddsapi:requests_remaining", "7")

    text = lastrun_text(db, cfg)

    assert "sekret-token-123" not in text and "sekret-odds-key" not in text


# ------------------------------------------------------------------ the command
def test_lastrun_is_admin_only(world):
    fetcher, db, cfg = world
    fetcher.get_daily_snapshot(scheduled=True)
    loop = CommandLoop(FakeTG(), db, cfg, {}, service=fetcher)

    assert loop.handle("/lastrun", USER, USER) == "Not allowed."
    admin_reply = loop.handle("/lastrun", ADMIN, ADMIN)
    assert "Last run" in admin_reply and "Slates as sent" in admin_reply


def test_help_lists_lastrun_for_admins_only(world):
    fetcher, db, cfg = world
    loop = CommandLoop(FakeTG(), db, cfg, {}, service=fetcher)

    assert "/lastrun" in loop.handle("/help", ADMIN, ADMIN)
    assert "/lastrun" not in loop.handle("/help", USER, USER)


def test_lastrun_never_generates_anything(world):
    """It only reads what the last cycle recorded - asking must not spend API calls."""
    fetcher, db, cfg = world
    loop = CommandLoop(FakeTG(), db, cfg, {}, service=fetcher)

    reply = loop.handle("/lastrun", ADMIN, ADMIN)

    assert "No generation has been recorded yet" in reply
    daily_picks.build_picks.assert_not_called()
