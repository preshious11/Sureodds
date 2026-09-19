"""The cross-tier rule (owner): the same match may appear in the Top ticket and in the Daily list, but never
with the same tip - nor a tip nested in it. Under CROSS_TIER_SAME_FIXTURE=forbid the match is off-limits."""
from __future__ import annotations

import logging

import pytest

from predbot.config import Config
from predbot.providers.base import Event, MarketLine
from predbot.selection import cross_tier, picker
from predbot.selection.picker import select
from tests.conftest import dt
from tests.test_picker import sc


def _arsenal(provider="oddsapi", swapped=False, pid="1"):
    home, away = ("Arsenal", "Chelsea") if not swapped else ("Chelsea FC", "Arsenal FC")
    return Event(provider=provider, provider_event_id=pid, sport="football", league="Premier League", home=home,
                 away=away, start=dt(hh=21), sources=["a", "b"])


def _partner():
    return Event(provider="oddsapi", provider_event_id="9", sport="football", league="Primeira", home="Porto",
                 away="Braga", start=dt(hh=21), sources=["a", "b"])


def _top_legs():
    """Arsenal to win (1.40) + Porto to win (1.45) = 2.03: the Top ticket."""
    return [sc(_arsenal(), "result3", "home", 0.75, 1.40, sources=3), sc(_partner(), "result3", "home", 0.72, 1.45, sources=3)]


def _candidates(event, swapped):
    """The Daily candidates on the Top's fixture, in the orientation ``event`` lists the sides. Each is
    (name, Scored, expected rejection or None)."""
    home, away = ("away", "home") if swapped else ("home", "away")
    arsenal_or_draw = "away_draw" if swapped else "home_draw"
    return [
        ("Arsenal to win", sc(event, "result3", home, 0.73, 1.38, sources=3), "CROSS_TIER_DUPLICATE"),
        ("Arsenal or draw", sc(event, "dc", arsenal_or_draw, 0.85, 1.25, sources=3), "CROSS_TIER_NESTED"),
        ("Arsenal DNB", sc(event, "dnb", home, 0.78, 1.30, sources=3), "CROSS_TIER_NESTED"),
        ("Chelsea to win", sc(event, "result3", away, 0.62, 1.60, sources=3), "CONFLICT"),          # contradicts
        ("Over 2.5 goals", sc(event, "total", "over", 0.66, 1.55, line=2.5, sources=3), None),
        ("BTTS Yes", sc(event, "btts", "yes", 0.64, 1.60, sources=3), None),
    ]


def _run(candidate, mode="different_outcome", **kw):
    pool = _top_legs() + [candidate]
    return select(pool, Config(cross_tier_same_fixture=mode, **kw), dt())


@pytest.mark.parametrize("swapped", [False, True], ids=["same-order", "home-away-swapped-and-respelled"])
def test_each_daily_candidate_on_a_top_fixture_is_judged_against_the_top_leg(swapped):
    for name, cand, expected in _candidates(_arsenal("sportsgameodds", swapped, pid="2"), swapped):
        ps = _run(cand)
        assert {t.line.selection for t in ps.top} == {"home"} and len(ps.top) == 2, name        # Top is unchanged
        rejections = ps.diagnostics["selection_rejections"]
        if expected is None:
            assert cand in ps.daily, name
            assert "cross_tier" not in cand.notes, name
            assert not rejections.get("CROSS_TIER_DUPLICATE") and not rejections.get("CROSS_TIER_NESTED"), name
        else:
            assert cand not in ps.daily, name
            if expected != "CONFLICT":
                assert rejections.get(expected) == 1, (name, rejections)
                assert cand.notes["cross_tier"].startswith(expected), name


@pytest.mark.parametrize("swapped", [False, True], ids=["same-order", "swapped"])
def test_under_forbid_every_candidate_on_a_top_fixture_is_rejected(swapped):
    for name, cand, _ in _candidates(_arsenal("sportsgameodds", swapped, pid="2"), swapped):
        ps = _run(cand, mode="forbid")
        assert cand not in ps.daily, name
        if "Chelsea" not in name:                                     # a contradiction is judged by the earlier rule too
            assert ps.diagnostics["selection_rejections"].get("CROSS_TIER_DUPLICATE") == 1, name


def test_the_same_match_appears_in_both_tiers_with_different_outcomes():
    over = sc(_arsenal("sportsgameodds", pid="2"), "total", "over", 0.66, 1.55, line=2.5, sources=3)
    ps = _run(over)
    fixtures = {(s.event.home, s.line.market, s.line.selection) for s in ps.all()}
    assert ("Arsenal", "result3", "home") in fixtures and ("Arsenal", "total", "over") in fixtures
    assert ps.top[0].event is not ps.daily[0].event or ps.top[0].line.market != ps.daily[0].line.market


def test_max_picks_per_event_counts_per_tier_unless_forbid():
    over = sc(_arsenal("sportsgameodds", pid="2"), "total", "over", 0.66, 1.55, line=2.5, sources=3)
    btts = sc(_arsenal("sportsgameodds", pid="2"), "btts", "yes", 0.64, 1.60, sources=3)
    ps = select(_top_legs() + [over, btts], Config(), dt())
    on_arsenal = [s for s in ps.all() if s.event.home == "Arsenal"]
    assert len(on_arsenal) == 2                                       # one Top leg + ONE Daily pick (cap 1 per tier)
    assert sum(1 for s in ps.daily if s.event.home == "Arsenal") == 1
    ps = select(_top_legs() + [over, btts], Config(cross_tier_same_fixture="forbid"), dt())
    assert len([s for s in ps.all() if s.event.home == "Arsenal"]) == 1


def test_nesting_in_totals_ignores_the_line_and_in_handicaps_ignores_the_line():
    top = MarketLine("total", "over", 1.9, line=1.5)
    assert cross_tier.relation(top, MarketLine("total", "over", 1.9, line=2.5)) == cross_tier.NESTED
    assert cross_tier.relation(top, MarketLine("total", "under", 1.9, line=2.5)) is None
    assert cross_tier.relation(top, MarketLine("total", "over", 1.9, line=1.5)) == cross_tier.DUPLICATE
    assert cross_tier.relation(top, MarketLine("total", "over", 1.9, line=0.5, entity="home")) is None
    h = MarketLine("handicap", "home", 1.9, line=-1.5)
    assert cross_tier.relation(h, MarketLine("handicap", "home", 1.9, line=0.5)) == cross_tier.NESTED
    assert cross_tier.relation(h, MarketLine("btts", "yes", 1.9)) is None


def test_mirror_swaps_sides_lines_and_entities():
    m = cross_tier.mirror(MarketLine("handicap", "home", 1.9, line=-1.5))
    assert (m.selection, m.line) == ("away", 1.5)
    assert cross_tier.mirror(MarketLine("dc", "home_draw", 1.3)).selection == "away_draw"
    assert cross_tier.mirror(MarketLine("total", "over", 1.9, line=1.5, entity="home")).entity == "away"
    assert cross_tier.mirror(MarketLine("correct_score", "2-1", 9.0)).selection == "1-2"
    assert cross_tier.mirror(MarketLine("btts", "yes", 1.9)).selection == "yes"


# ------------------------------------------------------------------ hard invariant (selection layer, Phase 3 layer 1)
def test_the_invariant_removes_a_duplicate_the_selection_layer_missed_and_never_touches_the_top(monkeypatch, caplog):
    over = sc(_arsenal("sportsgameodds", pid="2"), "total", "over", 0.66, 1.55, line=2.5, sources=3)
    dup = sc(_arsenal("sportsgameodds", pid="3"), "dc", "home_draw", 0.90, 1.25, sources=3)      # nested in the Top leg
    other = sc(_partner_two(), "result3", "home", 0.70, 1.30, sources=3)
    pool = _top_legs() + [dup, over, other]
    clean = select(pool, Config(), dt())
    assert dup not in clean.daily
    monkeypatch.setattr(picker, "_cross_tier_verdict", lambda *a, **k: None)      # the pre-filter fails to see it
    with caplog.at_level(logging.WARNING):
        ps = select(pool, Config(), dt())
    assert over in ps.daily and dup not in ps.daily                                                    # the invariant caught it
    assert ps.diagnostics["selection_rejections"].get("CROSS_TIER_DUPLICATE_REMOVED", 0) >= 1
    assert any("cross-tier invariant" in r.message for r in caplog.records)
    assert [(s.event.provider_event_id, s.line.market, s.line.selection) for s in ps.top] == \
           [(s.event.provider_event_id, s.line.market, s.line.selection) for s in clean.top]
    assert cross_tier.violations(ps.top, ps.daily, picker.fixture_relation, "different_outcome") == []


def _partner_two():
    return Event(provider="oddsapi", provider_event_id="10", sport="football", league="Eredivisie", home="Ajax",
                 away="Feyenoord", start=dt(hh=21), sources=["a", "b"])


def test_config_default_and_bad_value(monkeypatch, caplog):
    monkeypatch.delenv("CROSS_TIER_SAME_FIXTURE", raising=False)
    assert Config().cross_tier_same_fixture == "different_outcome"
    monkeypatch.setenv("CROSS_TIER_SAME_FIXTURE", "FORBID")
    assert Config().cross_tier_same_fixture == "forbid"
    monkeypatch.setenv("CROSS_TIER_SAME_FIXTURE", "nonsense")
    with caplog.at_level(logging.WARNING):
        assert Config().cross_tier_same_fixture == "different_outcome"
    assert any("CROSS_TIER_SAME_FIXTURE" in r.message for r in caplog.records)
