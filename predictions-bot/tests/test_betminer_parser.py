"""BetMiner payload -> canonical Event / MarketLine."""
from __future__ import annotations

from datetime import datetime, timezone

from predbot.markets.catalog import CATALOG
from predbot.providers.betminer import BetMinerProvider

from tests.conftest import load


def prov(budget, leagues=None):
    p = BetMinerProvider("k", budget, leagues=leagues, timezone="UTC")
    p.fetch_counts = {"bad_kickoff": 0}
    return p


def rows():
    return load("betminer_matches.json")["data"]


def test_parse_full_match(budget):
    p = prov(budget)
    ev = p._parse_match(rows()[0])
    assert ev.provider == "betminer" and ev.sport == "football"
    assert ev.home == "Arsenal" and ev.away == "Chelsea"
    assert ev.league == "Premier League"
    assert ev.start == datetime(2030, 6, 1, 18, 0, tzinfo=timezone.utc)
    assert ev.start.tzinfo is not None
    assert ev.sources == ["betminer"]


def test_markets_use_canonical_vocabulary(budget):
    ev = prov(budget)._parse_match(rows()[0])
    assert ev.markets
    for m in ev.markets:
        assert m.market in CATALOG
        assert m.price > 1.0
        assert m.period == "full" and m.stat == "points" and m.entity == "all"
    r3 = {m.selection: m.price for m in ev.markets if m.market == "result3"}
    assert r3 == {"home": 2.10, "draw": 3.40, "away": 3.60}
    tot = {(m.selection, m.line): m.price for m in ev.markets if m.market == "total"}
    assert tot[("over", 2.5)] == 1.85 and tot[("over", 1.5)] == 1.28
    dc = {m.selection for m in ev.markets if m.market == "dc"}
    assert dc == {"home_draw", "home_away", "away_draw"}
    assert {m.selection for m in ev.markets if m.market == "btts"} == {"yes", "no"}


def test_probabilities_are_metadata_only(budget):
    ev = prov(budget)._parse_match(rows()[0])
    assert ev.extra["betminer_probabilities"]["home_win"] == 48
    assert ev.extra["betminer_predictions"]["result"] == "home_win"
    # nothing derived from the probability block leaks into a price
    assert all(m.price > 1.0 for m in ev.markets)
    assert "p_model" not in ev.extra


def test_missing_odds_block_yields_no_markets(budget):
    ev = prov(budget)._parse_match(rows()[1])      # odds: null
    assert ev is not None and ev.markets == []     # never fabricated from probabilities


def test_partial_odds_block_only_emits_present_fields(budget):
    ev = prov(budget)._parse_match(rows()[3])      # no over_15 / over_35
    lines = {(m.market, m.selection, m.line) for m in ev.markets}
    assert ("total", "over", 2.5) in lines
    assert ("total", "over", 1.5) not in lines
    assert ("total", "over", 3.5) not in lines


def test_unparseable_kickoff_is_skipped_not_fatal(budget):
    p = prov(budget)
    assert rows()[4]["kickoff"] == "01/06/2030 17:30"   # not ISO-8601
    assert p._parse_match(rows()[4]) is None
    assert p.fetch_counts["bad_kickoff"] == 1


def test_league_filter_matches_by_name(budget):
    p = prov(budget)
    assert p._league_allowed({"name": "Premier League"})
    assert p._league_allowed({"name": "Serie A"})
    assert not p._league_allowed({"name": "Danish 1st Division"})
    assert not p._league_allowed({})


def test_configured_leagues_override_defaults(budget):
    p = prov(budget, leagues=["Danish 1st Division"])
    assert p._league_allowed({"name": "Danish 1st Division"})
    assert not p._league_allowed({"name": "Premier League"})
