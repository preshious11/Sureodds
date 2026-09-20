"""The Daily reaches DAILY_COMBINED_MIN..MAX_ODDS on its OWN legs - the Top's odds are no longer part of its product.
(It used to start from the Top's odds, so with a Top of ~2.0 the Daily alone stopped at ~9 and could read 8.93.)"""
from __future__ import annotations

import math

import pytest

from predbot.config import Config
from predbot.selection.picker import select
from tests.conftest import dt
from tests.test_picker import ev, sc

PRICE = 1.45


def pool(n=40, prob=0.70, price=PRICE, **kw):
    return [sc(ev(i, **kw), "result3", "home", prob, price) for i in range(n)]


def own(ps):
    return math.prod(s.price for s in ps.daily)


def test_the_daily_alone_reaches_the_band_with_a_top_of_about_two():
    ps = select(pool(), Config(), dt())
    top_odds = math.prod(s.price for s in ps.top)
    assert 2.0 <= top_odds <= 2.5
    assert 10.0 <= own(ps) <= 20.0                               # the Daily on its own
    assert len(ps.daily) >= 7                                    # 1.45**7 = 13.5; six legs (9.3) is no longer where it stops
    # the old rule counted the Top: it would have stopped once Top x Daily broke 20, i.e. at ~9.5 on its own
    assert top_odds * own(ps) > 20.0
    # and it stops before it exceeds 20 on its own
    assert own(ps) * PRICE > 20.0


def test_six_legs_worth_8_9_are_no_longer_enough():
    """1.45**6 = 9.29 < 10: under the old rule Top 2.1 x 9.29 = 19.5 was 'in the band' and the Daily stopped here."""
    ps = select(pool(), Config(), dt())
    assert len(ps.daily) > 6 and own(ps) > 9.29


def test_the_top_ticket_is_identical_whatever_the_daily_band():
    base = select(pool(), Config(), dt())
    other = select(pool(), Config(daily_odds_min=900.0, daily_odds_max=1000.0), dt())
    assert [(s.event.provider_event_id, s.line.selection, s.price) for s in base.top] == \
        [(s.event.provider_event_id, s.line.selection, s.price) for s in other.top]


def test_the_top_legs_are_still_consulted_for_duplicates_but_not_counted():
    ps = select(pool(), Config(), dt())
    top_events = {s.event.provider_event_id for s in ps.top}
    assert top_events and not top_events & {s.event.provider_event_id for s in ps.daily}     # one pick per match
    assert own(ps) == pytest.approx(ps.diagnostics["daily_own_odds"], abs=0.01)


def test_the_relaxed_pass_waives_caps_never_the_band():
    # every event in ONE league: the per-league cap (3) stops the strict pass far short of 10
    ps = select(pool(league="One League"), Config(), dt())
    assert "daily_caps_relaxed" in ps.diagnostics                 # the caps were waived ...
    assert 10.0 <= own(ps) <= 20.0                                # ... the band never
    tight = select(pool(league="One League"), Config(daily_odds_min=10.0, daily_odds_max=12.0), dt())
    assert own(tight) <= 12.0


def test_the_pick_cap_still_bounds_the_legs():
    ps = select(pool(), Config(daily_odds_min=900.0, daily_odds_max=1000.0), dt())
    assert len(ps.all()) <= Config().daily_target


def test_diagnostics_report_the_own_odds_the_leg_count_and_the_old_measure():
    ps = select(pool(), Config(), dt())
    d = ps.diagnostics
    top_odds = math.prod(s.price for s in ps.top)
    assert d["daily_legs"] == len(ps.daily)
    assert d["daily_own_odds"] == pytest.approx(own(ps), abs=0.01)
    assert d["daily_odds_with_top"] == pytest.approx(top_odds * own(ps), abs=0.02)


def test_an_empty_daily_reports_no_odds():
    ps = select([], Config(), dt())
    assert ps.diagnostics.get("daily_legs") in (0, None) and not ps.diagnostics.get("daily_own_odds")


from tests.test_lastrun import world  # noqa: E402,F401  (the fixture that builds a generated day)


def test_lastrun_shows_own_odds_and_the_old_sizing(world):
    from predbot.bot.lastrun import lastrun_text
    fetcher, db, cfg = world
    fetcher.get_daily_snapshot(scheduled=True)
    text = lastrun_text(db, cfg)
    assert "own odds" in text and "(with the Top:" in text and "old sizing" in text
