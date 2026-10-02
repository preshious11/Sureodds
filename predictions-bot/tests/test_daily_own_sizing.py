"""The Daily aims for DAILY_COMBINED_MIN_ODDS (10) on its OWN legs, never exceeds DAILY_COMBINED_MAX_ODDS (12), stops as
soon as the aim is reached, and is never padded with weak legs when it cannot get there. The Top's odds are not part
of its product."""
from __future__ import annotations

import math

import pytest

from predbot.config import Config
from predbot.selection.picker import select
from tests.conftest import dt
from tests.test_picker import _fx, ev, sc

PRICE = 1.35          # 1.35**8 = 11.03: eight legs land inside 10..12, seven (8.17) fall short, nine (14.9) overshoot


def pool(n=40, prob=0.70, price=PRICE, **kw):
    """Different matches (distinct names), alternating home / away so the outcome-family cap is not what ends the ticket."""
    out = []
    for i in range(n):
        e = _fx(i)
        for key, value in kw.items():
            setattr(e, key, value)
        out.append(sc(e, "result3", "home" if i % 2 == 0 else "away", prob, price))
    return out


def own(ps):
    return math.prod(s.price for s in ps.daily)


def test_the_daily_alone_reaches_the_band_with_a_top_of_about_two():
    ps = select(pool(), Config(), dt())
    top_odds = math.prod(s.price for s in ps.top)
    assert 2.0 <= top_odds <= 2.5
    assert 10.0 <= own(ps) <= 12.0                               # the Daily on its own
    assert len(ps.daily) == 8                                    # the fewest legs that reach 10 (seven make 8.17)
    assert ps.diagnostics["daily_target_reached"] and ps.diagnostics["daily_stopped"] == "target odds reached"
    # it stops the moment the target is reached: one more leg would only add risk (and break the 12 ceiling)
    assert own(ps) * PRICE > 12.0


def test_seven_legs_worth_8_2_are_not_enough_but_nothing_weak_is_added_to_get_there():
    """Short of 10 with everything that qualifies: the ticket is posted short (1.35**7 = 8.17), never padded."""
    ps = select(pool(n=10), Config(), dt())          # 3 legs go to the Top, 7 remain
    assert len(ps.daily) == 7 and own(ps) == pytest.approx(PRICE ** 7, abs=0.01)
    assert not ps.diagnostics["daily_target_reached"] and ps.diagnostics["daily_stopped"] == "pool exhausted"


def test_the_daily_never_exceeds_the_ceiling_even_when_the_next_leg_would_land_just_past_it():
    for price in (1.30, 1.35, 1.40, 1.45, 1.50, 1.60):
        ps = select(pool(price=price), Config(), dt())
        assert own(ps) <= 12.0 + 1e-9, price


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


def test_the_relaxed_pass_waives_diversity_shares_never_the_band_or_a_quality_gate():
    # every event in ONE league: the per-league cap (3) stops the strict pass far short of 10
    ps = select(pool(league="One League"), Config(), dt())
    assert "daily_caps_relaxed" in ps.diagnostics                 # the league share was waived ...
    assert 10.0 <= own(ps) <= 12.0                                # ... the band never
    assert all(s.p_model >= Config().daily_min_prob and s.line.distinct_books() >= Config().daily_min_books
               for s in ps.daily)                                 # ... and no quality gate


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
