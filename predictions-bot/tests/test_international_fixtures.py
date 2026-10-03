"""International football (Nations League, friendlies) must be fetched AND settleable.

Regression: for weeks the bot returned nothing on international match days because no international competition was
in the competition table or in any league list - the fixtures were never requested, and a fixture that did arrive
had no results path, so the results gate would have refused it."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from predbot.config import Config
from predbot.markets.competitions import competition_for_event, lookup
from predbot.markets.settlement import coverage_table, make_results_gate, result_paths
from predbot.providers.espn import ESPNProvider
from predbot.providers.sharpapi import sport_for_league

# (SharpAPI slug, Odds API key, ESPN scoreboard) - names verified against the providers' live catalogues
INTERNATIONAL = [
    ("uefa_-_nations_league", "soccer_uefa_nations_league", "soccer/uefa.nations"),
    ("concacaf_-_nations_league", "", "soccer/concacaf.nations.league"),
    ("world_-_international_friendlies", "", "soccer/fifa.friendly"),
]


@pytest.mark.parametrize("sharp,oddsapi,espn", INTERNATIONAL)
def test_each_international_competition_is_known_to_every_provider_that_carries_it(sharp, oddsapi, espn):
    comp = lookup(sharp)
    assert comp is not None and comp.sport == "football" and comp.espn == espn
    if oddsapi:
        assert lookup(oddsapi) is comp


@pytest.mark.parametrize("sharp,oddsapi,espn", INTERNATIONAL)
def test_a_fixture_is_fetched_by_default(sharp, oddsapi, espn):
    cfg = Config()
    assert sharp in cfg.sharpapi_leagues
    if oddsapi:
        assert oddsapi in cfg.odds_api_league_allowlist


@pytest.mark.parametrize("sharp,oddsapi,espn", INTERNATIONAL)
def test_sharpapi_files_the_slug_under_football(sharp, oddsapi, espn):
    """Otherwise the feed uses the generic market list instead of the football-specific one."""
    assert sport_for_league(sharp) == "football"


@pytest.mark.parametrize("sharp,oddsapi,espn", INTERNATIONAL)
def test_a_fixture_can_be_settled_so_the_results_gate_lets_it_through(sharp, oddsapi, espn):
    providers = {"espn": ESPNProvider()}
    event = SimpleNamespace(sport="football", league="", league_id=sharp, provider="sharpapi", sources=["sharpapi"],
                            extra={}, home="Croatia", away="England")
    assert [p.name for p in result_paths(event, Config(), providers)] == ["espn"]
    assert make_results_gate(Config(), providers)(event)                # not None: not NO_RESULTS_PATH


def test_the_odds_api_record_of_a_nations_league_match_resolves_too():
    event = SimpleNamespace(sport="football", league="UEFA Nations League", league_id="", provider="oddsapi",
                            sources=["oddsapi"], extra={"sport_key": "soccer_uefa_nations_league"})
    assert competition_for_event(event).id == "uefa_nations"


def test_the_startup_coverage_table_shows_a_results_path_for_every_configured_international_league():
    rows = {ident: paths for _origin, ident, paths in coverage_table(Config(), {"espn": ESPNProvider()})}
    for sharp, oddsapi, _espn in INTERNATIONAL:
        assert rows[sharp] == ["espn"], sharp
        if oddsapi:
            assert rows[oddsapi], oddsapi


def test_a_league_without_a_results_path_would_still_be_refused():
    """The gate itself is unchanged: an unknown competition is not fetched or picked."""
    event = SimpleNamespace(sport="football", league="", league_id="some_-_unknown_cup", provider="sharpapi",
                            sources=["sharpapi"], extra={}, home="A", away="B")
    assert result_paths(event, Config(), {"espn": ESPNProvider()}) == []
