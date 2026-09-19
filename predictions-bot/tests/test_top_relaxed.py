"""The Top ticket's relaxed fallback: only when the strict search finds nothing; band first, then the
probability floor; never one leg, never more than three; TOP_RELAXED_FALLBACK=0 turns it off."""
from __future__ import annotations

import math

import pytest

from predbot.config import Config
from predbot.selection.picker import select
from tests.conftest import dt
from tests.test_picker import ev, sc


def _top(ps):
    return sorted((s.event.provider_event_id, s.line.selection, round(s.price, 2)) for s in ps.top)


def _pool(prices, p):
    return [sc(ev(i), "result3", "home", p, price, sources=3) for i, price in enumerate(prices)]


def _cfg(**kw):
    return Config(**kw)


def test_a_pool_the_strict_search_solves_gets_exactly_the_same_ticket_with_the_fallback_on():
    pool = _pool([1.42, 1.42, 1.40, 1.45, 1.38, 1.50], 0.70)
    on = select(pool, _cfg(top_relaxed_fallback=True), dt())
    off = select(pool, _cfg(top_relaxed_fallback=False), dt())
    assert on.top and _top(on) == _top(off)
    assert on.diagnostics["top_relaxed"] is None and off.diagnostics["top_relaxed"] is None
    assert 2.0 <= math.prod(s.price for s in on.top) <= 2.5


def test_the_band_is_widened_when_the_best_combination_is_2_62():
    pool = _pool([1.62, 1.62], 0.70)                                  # 1.62 x 1.62 = 2.62, outside 2.00-2.50
    assert select(pool, _cfg(top_relaxed_fallback=False), dt()).top == []
    ps = select(pool, _cfg(), dt())
    assert len(ps.top) == 2 and math.prod(s.price for s in ps.top) == pytest.approx(2.6244)
    assert ps.diagnostics["top_relaxed"] == "band"


def test_the_probability_floor_is_lowered_when_every_leg_is_0_59_to_0_61():
    pool = [sc(ev(i), "result3", "home", p, 1.45, sources=3) for i, p in enumerate((0.59, 0.60, 0.61, 0.60))]
    assert select(pool, _cfg(top_relaxed_fallback=False), dt()).top == []
    ps = select(pool, _cfg(), dt())
    assert 2 <= len(ps.top) <= 3 and ps.diagnostics["top_relaxed"] == "prob"
    assert all(s.p_model >= 0.62 - 0.04 - 1e-9 for s in ps.top)


def test_a_pool_with_one_eligible_leg_gets_no_ticket():
    ps = select(_pool([1.45], 0.70), _cfg(), dt())
    assert ps.top == [] and ps.diagnostics["top_relaxed"] is None


def test_legs_below_the_relaxed_floor_never_join():
    pool = [sc(ev(i), "result3", "home", 0.55, 1.45, sources=3) for i in range(4)]         # 0.55 < 0.58
    ps = select(pool, _cfg(), dt())
    assert ps.top == [] and ps.diagnostics["top_relaxed"] is None


def test_the_sources_requirement_is_not_relaxed():
    pool = [sc(ev(i), "result3", "home", 0.60, 1.45, sources=1) for i in range(4)]
    for s in pool:
        s.sources = {"only": 0.6}
    assert select(pool, _cfg(), dt()).top == []


def test_the_relaxed_ticket_is_never_a_single_leg_and_never_over_three_legs():
    # only 1.30 legs: strict needs 2.0-2.5 (4 legs x1.30=2.86, 3 legs=2.20 fits -> strict succeeds with 3)
    ps = select(_pool([1.30] * 8, 0.70), _cfg(), dt())
    assert len(ps.top) == 3 and ps.diagnostics["top_relaxed"] is None
    # only 1.18 legs: 3 legs = 1.64 - even the widened 1.75 floor is out of reach, and a 4-leg ticket is not allowed
    ps = select(_pool([1.18] * 8, 0.70), _cfg(), dt())
    assert ps.top == [] and ps.diagnostics["top_relaxed"] is None


def test_the_band_and_probability_amounts_are_configurable(monkeypatch):
    monkeypatch.setenv("TOP_RELAX_BAND", "0.05")
    monkeypatch.setenv("TOP_RELAX_PROB", "0.01")
    monkeypatch.setenv("TOP_RELAXED_FALLBACK", "0")
    c = Config()
    assert (c.top_relaxed_fallback, c.top_relax_band, c.top_relax_prob) == (False, 0.05, 0.01)
    monkeypatch.delenv("TOP_RELAX_BAND"), monkeypatch.delenv("TOP_RELAX_PROB"), monkeypatch.delenv("TOP_RELAXED_FALLBACK")
    c = Config()
    assert (c.top_relaxed_fallback, c.top_relax_band, c.top_relax_prob) == (True, 0.25, 0.04)


def test_lastrun_shows_the_fallback():
    from predbot.bot.lastrun import _funnel
    assert any("relaxed by band" in line for line in _funnel({"top_relaxed": "band"}))
    assert any("not needed" in line for line in _funnel({"top_relaxed": None}))
