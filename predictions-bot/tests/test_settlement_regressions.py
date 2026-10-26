"""Regression tests for three settlement gaps found in analysis/data/void_grades.csv (picks voided as not obtainable).

1. A bare "Championship" league name in football had no competition, so the pick had no results path.
2. Women's fixtures ("... Women", "... [W]") were sent to the men's EuroLeague results API, which never matches them.
3. (Not fixed here: ESPN name matching for MLS and Europa League misses. See the PR description.)
"""
from types import SimpleNamespace as Ev

from predbot.markets.competitions import competition_for_event
from predbot.markets.settlement import is_womens_fixture, result_paths


def _event(**kw):
    base = dict(sport="football", league="", league_id="", provider="oddsapi", sources=[], extra={},
                home="Home FC", away="Away FC")
    base.update(kw)
    return Ev(**base)


# ---- 1. Championship

def test_bare_championship_in_football_resolves_to_the_english_championship():
    comp = competition_for_event(_event(league="Championship"))
    assert comp is not None and comp.id == "championship"


def test_bare_championship_outside_football_is_not_guessed():
    assert competition_for_event(_event(sport="basketball", league="Championship")) is None


def test_full_english_championship_name_still_resolves():
    comp = competition_for_event(_event(league="England - Championship"))
    assert comp is not None and comp.id == "championship"


def test_bare_championship_pick_gets_a_results_path():
    providers = {"football_data": object(), "espn": object()}
    names = [p.name for p in result_paths(_event(league="Championship", home="Queens Park Rangers",
                                                 away="Derby County"), None, providers)]
    assert "football_data" in names and "espn" in names


# ---- 2. women's EuroLeague

def test_womens_fixtures_are_recognised_by_their_name_suffix():
    assert is_womens_fixture(Ev(home="Basket Zaragoza [W]", away="Basket Landes SASP Women"))
    assert is_womens_fixture(Ev(home="Crvena Zvezda Women", away="Kangoeroes Basket Mechelen Women"))
    assert not is_womens_fixture(Ev(home="Real Madrid", away="Barcelona"))
    assert not is_womens_fixture(Ev(home="Women of Town FC", away="Ajax"))  # the word is not a suffix


def test_womens_euroleague_fixture_is_never_sent_to_the_men_s_api():
    providers = {"euroleague": object()}
    women = _event(sport="basketball", league="Euroleague", home="Valencia Basket Club [W]",
                   away="Fenerbahce SK [W]")
    assert "euroleague" not in [p.name for p in result_paths(women, None, providers)]


def test_mens_euroleague_fixture_still_uses_the_euroleague_path():
    providers = {"euroleague": object()}
    men = _event(sport="basketball", league="Euroleague", home="Real Madrid", away="Barcelona")
    assert "euroleague" in [p.name for p in result_paths(men, None, providers)]
