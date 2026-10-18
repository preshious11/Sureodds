"""The Daily aims for DAILY_COMBINED_MIN_ODDS (10) on its OWN legs, never exceeds DAILY_COMBINED_MAX_ODDS (12), stops as
soon as the aim is reached, and is never padded with weak legs when it cannot get there. The Top's odds are not part
of its product."""
from __future__ import annotations

import math

import pytest

from predbot.config import Config
from tests.pipeline_helpers import select as _select


def select(scored, cfg, now, **kw):
    """Top/Daily mechanics on the given predictions (see pipeline_helpers.select)."""
    kw.setdefault("raw", True)
    return _select(scored, cfg, now, **kw)
from tests.conftest import dt
from tests.test_picker import _fx, ev, sc

PRICE = 1.35          # 1.35**8 = 11.03: eight legs land inside 10..12, seven (8.17) fall short, nine (14.9) overshoot
_KINDS = (("result3", "home", None), ("total", "over", 2.5), ("btts", "yes", None), ("dnb", "home", None))


def pool(n=40, prob=0.70, price=PRICE, **kw):
    """Different matches (distinct names) over four markets, so the Daily's per-market share is not what ends the
    ticket."""
    out = []
    for i in range(n):
        e = _fx(i)
        for key, value in kw.items():
            setattr(e, key, value)
        market, selection, line = _KINDS[i % 4]
        out.append(sc(e, market, selection, prob, price, line=line))
    return out


def own(ps):
    return math.prod(s.price for s in ps.daily)


def test_the_daily_alone_reaches_the_band_with_a_top_of_about_two():
    ps = select(pool(), Config(), dt())
    top_odds = math.prod(s.price for s in ps.top)
    assert 2.0 <= top_odds <= 2.5
    assert 10.0 <= own(ps) <= 12.0                               # the Daily on its own
    assert len(ps.daily) == 8                                    # the fewest legs that reach 10 (seven make 8.17)
    assert ps.diagnostics["daily"]["target_reached"]
    # it stops the moment the target is reached: one more leg would only add risk (and break the 12 ceiling)
    assert own(ps) * PRICE > 12.0


def test_seven_legs_worth_8_2_are_not_enough_but_nothing_weak_is_added_to_get_there():
    """Short of 10 with everything that qualifies: the ticket is posted short (1.35**7 = 8.17), never padded."""
    ps = select(pool(n=7), Config(), dt())
    assert len(ps.daily) == 7 - len(ps.top)
    assert own(ps) == pytest.approx(PRICE ** len(ps.daily), abs=0.01)
    assert not ps.diagnostics["daily"]["target_reached"]


def test_the_daily_never_exceeds_the_ceiling_even_when_the_next_leg_would_land_just_past_it():
    for price in (1.30, 1.35, 1.40, 1.45, 1.50, 1.60):
        ps = select(pool(price=price), Config(), dt())
        assert own(ps) <= 12.0 + 1e-9, price


def test_the_top_ticket_is_identical_whatever_the_daily_band():
    base = select(pool(), Config(), dt())
    other = select(pool(), Config(daily_odds_min=900.0, daily_odds_max=1000.0), dt())
    assert [(s.event.provider_event_id, s.line.selection, s.price) for s in base.top] ==         [(s.event.provider_event_id, s.line.selection, s.price) for s in other.top]


def test_the_daily_counts_its_own_legs_only_without_sharing_top_predictions():
    """No cross-tier exclusion: the Daily is built from the stored predictions alone. A prediction that is a Top
    leg may also be a Daily leg (one row, two memberships) and counts in the Daily's own product."""
    ps = select(pool(), Config(), dt())
    assert ps.top and ps.daily
    assert not ({id(s) for s in ps.top} & {id(s) for s in ps.daily})      # the strongest rows qualify for both
    assert own(ps) == pytest.approx(ps.diagnostics["daily"]["combined_odds"], abs=0.01)
    assert len({s.event.canonical_event_id for s in ps.daily}) == len(ps.daily)    # one pick per match


def test_the_league_cap_is_never_waived_to_reach_the_band():
    """There is no relaxed second pass: every event in ONE league leaves the Daily at the league cap, short of 10."""
    c = Config()
    ps = select(pool(league="One League"), c, dt())
    assert len(ps.daily) == c.daily_max_per_league
    assert own(ps) < c.daily_odds_min and not ps.diagnostics["daily"]["target_reached"]
    assert ps.diagnostics["daily"]["rejections"]["diversity_limit"] > 0
    assert len(ps.all()) == 40                                       # the other predictions still exist


def test_the_pick_cap_still_bounds_the_legs():
    ps = select(pool(), Config(daily_odds_min=900.0, daily_odds_max=1000.0), dt())
    assert len(ps.daily) <= Config().daily_target
    assert len(ps.all()) == 40


def test_diagnostics_report_the_own_odds_and_the_leg_count():
    ps = select(pool(), Config(), dt())
    d = ps.diagnostics["daily"]
    assert d["selected"] == len(ps.daily)
    assert d["combined_odds"] == pytest.approx(own(ps), abs=0.01)


def test_an_empty_daily_reports_no_odds():
    ps = select([], Config(), dt())
    assert ps.diagnostics["daily"]["selected"] == 0 and ps.diagnostics["daily"]["target_reached"] is False


from tests.test_lastrun import world  # noqa: E402,F401  (the fixture that builds a generated day)


def test_lastrun_shows_the_daily_combined_odds(world):
    from predbot.bot.lastrun import lastrun_text
    fetcher, db, cfg = world
    fetcher.get_daily_snapshot(scheduled=True)
    text = lastrun_text(db, cfg)
    assert "Daily" in text and "combined" in text
