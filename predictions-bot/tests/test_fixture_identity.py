"""One match, however each provider spells it: same_team with the US-sports rules, home/away swaps, the
competition veto and fixture_key. Names come from analysis/cross_tier_repro.py (the Phase 0 cases)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from predbot.providers.base import Event
from predbot.selection.picker import fixture_conflicts, fixture_relation, same_fixture
from predbot.util.fixtures import fixture_key, fixture_orientation
from predbot.util.names import canonical_team, same_team


def _t(hh, mm=0):
    return datetime(2026, 9, 19, hh, mm, tzinfo=timezone.utc)


def _e(provider, sport, home, away, start, league="", league_id="", extra=None):
    return Event(provider=provider, provider_event_id=f"{provider}-{home}", sport=sport, league=league, home=home,
                 away=away, start=start, league_id=league_id, extra=extra or {}, sources=[provider])


# --------------------------------------------------------------------------- same_team
@pytest.mark.parametrize("a,b,sport", [
    ("LA Dodgers", "Los Angeles Dodgers", "baseball"),
    ("TEX Rangers", "Texas Rangers", "baseball"),
    ("BOS Red Sox", "Boston Red Sox", "baseball"),
    ("Athletics", "Oakland Athletics", "baseball"),
    ("Rangers", "Texas Rangers", "baseball"),
    ("LA Lakers", "Los Angeles Lakers", "basketball"),
    ("GS Warriors", "Golden State Warriors", "basketball"),
    ("NY Giants", "New York Giants", "american_football"),
])
def test_us_style_names_match_with_the_sport(a, b, sport):
    assert same_team(a, b, sport=sport)


def test_the_abbreviation_and_nickname_rules_are_off_without_a_us_sport():
    assert not same_team("LA Dodgers", "Los Angeles Dodgers")               # unchanged default behaviour
    assert not same_team("Athletics", "Oakland Athletics")
    assert not same_team("LA Dodgers", "Los Angeles Dodgers", sport="football")


@pytest.mark.parametrize("a,b,sport", [
    ("Chicago White Sox", "Boston Red Sox", "baseball"),                    # both end in "Sox"
    ("LA Lakers", "LA Clippers", "basketball"),
    ("LA Dodgers", "Los Angeles Angels", "baseball"),
    ("New York Yankees", "New York Mets", "baseball"),
])
def test_different_teams_are_not_merged_by_the_nickname_rule(a, b, sport):
    assert not same_team(a, b, sport=sport)


def test_the_threshold_is_untouched_and_existing_distinctions_hold():
    assert not same_team("Manchester United", "Manchester City")
    assert not same_team("Real Madrid", "Atletico Madrid")
    assert not same_team("Barcelona", "Barcelona B")
    assert same_team("Arsenal", "Arsenal FC")
    assert same_team("Espanyol", "RCD Espanyol")                            # rcd is a generic club affix


def test_canonical_team_reduces_us_names_to_the_nickname():
    assert canonical_team("BOS Red Sox", "baseball") == canonical_team("Boston Red Sox", "baseball") == "red sox"
    assert canonical_team("LA Dodgers", "baseball") == canonical_team("Los Angeles Dodgers", "baseball") == "dodgers"
    assert canonical_team("Chicago White Sox", "baseball") != canonical_team("Boston Red Sox", "baseball")
    assert canonical_team("Manchester City", "football") == "manchester city"


# --------------------------------------------------------------------------- same_fixture / orientation
def test_phase0_mlb_pair_with_a_city_abbreviation_links():
    a = _e("sharpapi", "baseball", "LA Dodgers", "San Diego Padres", _t(2), "MLB")
    b = _e("sportsgameodds", "baseball", "Los Angeles Dodgers", "San Diego Padres", _t(2, 10), "MLB")
    assert same_fixture(a, b) and fixture_orientation(a, b) == "same"


def test_phase0_mlb_pair_with_prefix_and_swapped_sides_links_as_swapped():
    a = _e("sharpapi", "baseball", "TEX Rangers", "BOS Red Sox", _t(18, 10), "MLB")
    b = _e("sportsgameodds", "baseball", "Boston Red Sox", "Texas Rangers", _t(18, 10), "MLB")
    assert fixture_orientation(a, b) == "swapped" and fixture_relation(a, b) is True
    assert fixture_relation(b, a) is True


def test_a_football_home_away_swap_links_as_swapped():
    a = _e("sharpapi", "football", "Le Mans", "Lorient", _t(18, 45), "FRANCE - LIGUE 2")
    b = _e("oddsapi", "football", "Lorient", "Le Mans", _t(18, 45), "Ligue 2 - France")
    assert fixture_orientation(a, b) == "swapped"


def test_different_sport_or_far_apart_kickoffs_never_link():
    a = _e("x", "football", "Le Mans", "Lorient", _t(18, 45))
    assert fixture_orientation(a, _e("y", "hockey", "Le Mans", "Lorient", _t(18, 45))) is None
    assert fixture_orientation(a, _e("y", "football", "Le Mans", "Lorient", _t(23, 0))) is None
    assert fixture_relation(a, _e("y", "football", "Nantes", "Lorient", _t(18, 45))) is None


def test_a_premier_league_match_cannot_link_to_a_championship_one():
    epl = _e("sharpapi", "football", "Leeds United", "Norwich City", _t(15), league_id="england_-_premier_league")
    champ = _e("sharpapi", "football", "Leeds United", "Norwich City", _t(15), league_id="england_-_championship")
    assert fixture_orientation(epl, champ) is None and not same_fixture(epl, champ)
    assert fixture_key(epl) != fixture_key(champ)
    unknown = _e("oddsapi", "football", "Leeds United", "Norwich City", _t(15), league="Somewhere")
    assert same_fixture(epl, unknown)                                       # an unresolved competition does not veto


def test_the_existing_two_provider_link_still_holds():
    a1 = _e("oddsapi", "football", "Arsenal", "Chelsea", _t(21), "L")
    a2 = _e("sharpapi", "football", "Arsenal FC", "Chelsea", _t(22, 30), "L")
    b = _e("oddsapi", "football", "Porto", "Braga", _t(21), "L")
    assert fixture_conflicts([a1, a2, b]) == [{1}, {0}, set()]


# --------------------------------------------------------------------------- fixture_key
@pytest.mark.parametrize("a,b", [
    (("baseball", "LA Dodgers", "San Diego Padres", _t(2), "MLB"), ("baseball", "Los Angeles Dodgers", "San Diego Padres", _t(2, 10), "MLB")),
    (("baseball", "TEX Rangers", "BOS Red Sox", _t(18, 10), "MLB"), ("baseball", "Boston Red Sox", "Texas Rangers", _t(18, 10), "MLB")),
    (("baseball", "Athletics", "Seattle Mariners", _t(1, 40), "MLB"), ("baseball", "Oakland Athletics", "Seattle Mariners", _t(1, 40), "MLB")),
    (("basketball", "LA Lakers", "GS Warriors", _t(0, 30), "NBA"), ("basketball", "Los Angeles Lakers", "Golden State Warriors", _t(0, 30), "NBA")),
    (("football", "Le Mans", "Lorient", _t(18, 45), "FRANCE - LIGUE 2"), ("football", "Lorient", "Le Mans", _t(18, 45), "Ligue 2 - France")),
])
def test_the_phase0_pairs_produce_identical_keys(a, b):
    ea = _e("sharpapi", a[0], a[1], a[2], a[3], a[4])
    eb = _e("sportsgameodds", b[0], b[1], b[2], b[3], b[4])
    assert fixture_key(ea) == fixture_key(eb)


def test_the_key_uses_the_odds_api_sport_key_to_know_the_competition():
    a = _e("sharpapi", "football", "Espanyol", "Elche", _t(19), league_id="spain_-_la_liga")
    b = _e("oddsapi", "football", "RCD Espanyol", "Elche CF", _t(19), league="La Liga - Spain",
           extra={"sport_key": "soccer_spain_la_liga"})
    assert fixture_key(a) == fixture_key(b) and "la_liga" in fixture_key(a)


def test_different_matches_have_different_keys():
    a = _e("x", "baseball", "Boston Red Sox", "Texas Rangers", _t(18, 10), "MLB")
    assert fixture_key(a) != fixture_key(_e("x", "baseball", "Boston Red Sox", "Texas Rangers", _t(22, 10), "MLB"))
    assert fixture_key(a) != fixture_key(_e("x", "baseball", "Chicago White Sox", "Texas Rangers", _t(18, 10), "MLB"))
