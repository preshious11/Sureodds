"""Accumulator maths and section rules requested in the brief.

Covers: 1/2/3/5-selection accumulators, invalid odds, empty lists, duplicate
handling (no repeated match outcome across sections), and section total counts.
"""
from __future__ import annotations

import math
import re

import pytest

from predbot.bot.format import combined_odds, picks_message
from predbot.config import Config
from tests.pipeline_helpers import select

from tests.conftest import dt
from tests.test_picker import _fx, cfg, ev, sc

DAY = dt()


# ----------------------------------------------------- accumulator maths
@pytest.mark.parametrize("prices,expected", [
    ([2.0], 2.0),                       # 1 selection
    ([1.5, 2.0], 3.0),                  # 2 selections: multiplied, not added
    ([1.5, 1.5, 1.5], 3.375),           # 3 selections
    ([1.5, 1.5, 1.5, 1.5, 1.5], 7.59375),  # 5 selections
])
def test_accumulators_multiply_decimal_odds(prices, expected):
    total = combined_odds(prices)
    assert math.isclose(total, expected, rel_tol=1e-9)
    # never the sum
    assert total != sum(prices) or len(prices) == 1


def test_string_odds_are_accepted():
    assert combined_odds(["1.5", "2.0"]) == 3.0


# ------------------------------------------------------- invalid odds
@pytest.mark.parametrize("bad", [1.0, 0.9, -2.0, "junk", None, float("nan")])
def test_invalid_odds_raise(bad):
    with pytest.raises(ValueError):
        combined_odds([1.5, bad])


def test_invalid_odds_never_enter_a_ticket():
    now = dt()
    pool = [sc(ev(1), "result3", "home", 0.70, 1.8),
            sc(ev(2), "result3", "home", 0.70, 0.9)]        # 0.9 is not a price
    ps = select(pool, cfg(), now)
    for s in ps.all():
        assert s.price > 1.0


# --------------------------------------------------------- empty lists
def test_empty_list_combined_odds_is_neutral():
    assert combined_odds([]) == 1.0


def test_empty_pool_returns_empty_sections_and_evaluated_empty_card():
    ps = select([], cfg(), dt())
    assert ps.top == [] and ps.daily == []
    card = picks_message([], [], DAY, "UTC")
    assert "No selections qualified today." in card
    assert "Combined odds" not in card


# -------------------------------------------------- duplicate handling
def test_same_match_same_outcome_appears_only_once():
    now = dt()
    e = ev(1)
    pool = [sc(e, "result3", "home", 0.70, 1.8) for _ in range(6)]   # identical outcome x6
    ps = select(pool, cfg(), now)
    keys = [(s.event.dedupe_key(), s.line.market, s.line.selection, s.line.line) for s in ps.all()]
    assert len(keys) == len(set(keys))


def test_equivalent_outcomes_collapse_to_one_prediction_so_sections_cannot_hold_duplicates():
    """1X2 and Moneyline are different families, so a fixture offering both keeps one representative of each in All;
    Top and Daily (which only reference stored predictions) hold at most ONE prediction per fixture, and the same
    prediction may legitimately sit in both sections."""
    now = dt()
    pool = []
    for i in range(25):
        e = ev(i)
        pool.append(sc(e, "result3", "home", 0.70, 1.80))       # top-quality pick
        pool.append(sc(e, "result2", "home", 0.65, 1.70))       # near-equivalent outcome
    ps = select(pool, cfg(), now)
    assert len(ps.all()) == 50
    for section in (ps.top, ps.daily):
        events = [s.event.canonical_event_id for s in section]
        assert len(events) == len(set(events))
    for t in ps.top:
        for d in ps.daily:
            if t.event.canonical_event_id == d.event.canonical_event_id:
                assert t is d


def test_same_match_with_different_outcomes_is_capped_at_one_pick():
    """One prediction per fixture in Top and Daily: two legs on one match are correlated, so even genuinely different outcomes never both make
    Top or Daily - although both are All Predictions of the fixture."""
    now = dt()
    e = ev(1)
    pool = [sc(e, "result3", "home", 0.70, 1.80),
            sc(e, "total", "over", 0.64, 1.80, line=2.5)]
    ps = select(pool, cfg(), now)
    assert len(ps.all()) == 2
    assert len(ps.top) <= 1 and len(ps.daily) <= 1


# -------------------------------------------------- section total counts
def test_top_section_returns_between_1_and_3_strong_picks():
    now = dt()
    pool = [sc(ev(i), "result3", "home", 0.70, 1.45) for i in range(12)]
    ps = select(pool, cfg(), now)
    assert 1 <= len(ps.top) <= 3
    ticket = math.prod(t.price for t in ps.top)
    assert ticket >= cfg().top_odds_target * 0.9      # aiming at ~2.00


def test_top_ticket_never_overshoots_the_target():
    now = dt()
    pool = [sc(ev(i), "result3", "home", 0.70, 1.50) for i in range(12)]
    ps = select(pool, cfg(), now)
    ticket = math.prod(t.price for t in ps.top)
    assert ticket <= cfg().top_odds_target * 1.7


def test_daily_section_lands_in_the_10_to_12_window():
    now = dt()
    kinds = [("result3", "home", None), ("total", "over", 2.5), ("btts", "yes", None), ("dnb", "home", None)]
    pool = [sc(_fx(i), kinds[i % 4][0], kinds[i % 4][1], 0.62, 1.35, line=kinds[i % 4][2]) for i in range(40)]
    ps = select(pool, cfg(), now)
    ticket = math.prod(s.price for s in ps.daily)
    c = cfg()
    assert (c.daily_odds_min, c.daily_odds_max) == (10.0, 12.0)
    assert c.daily_odds_min <= ticket <= c.daily_odds_max      # 10..12: the Daily ALONE, the Top is not counted
    assert len(ps.daily) <= c.daily_target                     # hard pick cap on the Daily section


def test_total_counts_are_reported_per_section():
    now = dt()
    pool = [sc(ev(i), "result3", "home", 0.70, 1.45) for i in range(12)]
    ps = select(pool, cfg(), now)
    shown = len(ps.top) + len(ps.daily)                       # a prediction in both sections is shown in both
    card = picks_message(ps.top, ps.daily, DAY, "UTC")
    odds_lines = [ln for ln in card.splitlines() if ln.startswith("💰")]
    assert len(odds_lines) == shown                           # one odds line per section entry
    assert card.count("📊") == shown                          # one block per section entry
    headers = [ln for ln in card.splitlines() if ln.startswith("🎟 Combined odds")]
    assert [int(re.search(r"· (\d+) picks?", h).group(1)) for h in headers] ==         [n for n in (len(ps.top), len(ps.daily)) if n]        # each header counts its own ticket
