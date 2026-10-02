"""Phase A: ESPN results provider, parsed from payloads recorded live (tests/fixtures/espn_*.json).

Statuses other than the ones ESPN returned for the recorded events are produced by changing the
status name on a copy of a recorded event, never by inventing a payload."""
from __future__ import annotations

import copy

import pytest

from predbot.markets.competitions import competition_for_event, lookup
from predbot.markets.settlement import WON, LOST, PUSH, grade
from predbot.providers.base import MarketLine
from predbot.providers.espn import ESPNProvider
from predbot.util.http import ProviderError
from predbot.util.timeutil import parse_iso
from tests.conftest import load


class _Http:
    def __init__(self, payload=None, by_date=None, error=None):
        self.payload, self.by_date, self.error, self.calls = payload, by_date or {}, error, []

    def get(self, path, params=None):
        self.calls.append((path, dict(params or {})))
        if self.error:
            raise ProviderError(self.error)
        if self.by_date:
            return self.by_date.get((params or {}).get("dates"), {"events": []})
        return self.payload


def _espn(fixture=None, **kw):
    p = ESPNProvider()
    p.http = _Http(load(fixture) if fixture else None, **kw)
    return p


def _first(fixture, i=0):
    """(home, away, kickoff) of the i-th recorded team-sport event."""
    e = load(fixture)["events"][i]
    c = e["competitions"][0]
    side = lambda ha: next(k for k in c["competitors"] if k["homeAway"] == ha)["team"]["displayName"]
    return side("home"), side("away"), parse_iso(e["date"])


# ------------------------------------------------------------------ soccer
def test_soccer_full_time_is_final_with_the_full_score_only():
    home, away, ko = _first("espn_soccer.json")
    r = _espn("espn_soccer.json").find_result("football", home, away, ko, league_id="england_-_premier_league")
    assert r.status == "final" and r.source == "espn" and r.scores == {"full": (2.0, 2.0)}       # no linescores -> no 1h/2h


def test_europa_league_super_lig_and_mls_are_reachable_by_their_provider_ids():
    p = _espn("espn_soccer.json")
    assert p.find_result("football", "Levski Sofia", "RB Salzburg", parse_iso("2026-09-17T16:45Z"),
                         league_id="uefa_-_europa_league").scores["full"] == (0.0, 1.0)
    assert p.find_result("football", "Levski Sofia", "RB Salzburg", parse_iso("2026-09-17T16:45Z"),
                         league_id="soccer_uefa_europa_league").status == "final"


@pytest.mark.parametrize("status,expected", [("STATUS_POSTPONED", "postponed"), ("STATUS_CANCELED", "cancelled"),
                                             ("STATUS_IN_PROGRESS", "in_progress"), ("STATUS_SCHEDULED", "scheduled")])
def test_non_final_statuses(status, expected):
    payload = copy.deepcopy(load("espn_soccer.json"))
    e = payload["events"][0]
    e["status"]["type"] = {"name": status}
    e["competitions"][0]["status"]["type"] = {"name": status}
    home, away, ko = _first("espn_soccer.json")
    p = ESPNProvider()
    p.http = _Http(payload)
    r = p.find_result("football", home, away, ko, league_id="england_-_premier_league")
    assert r.status == expected and r.scores == {}


@pytest.mark.parametrize("status_type,expected", [
    ({"state": "pre"}, "scheduled"),
    ({"state": "in"}, "in_progress"),
    ({"name": "STATUS_SCHEDULED", "state": "in"}, "in_progress"),
    ({"name": "STATUS_FINAL"}, "final"),
    ({"completed": True, "state": "post"}, "final"),
    ({"state": "post"}, "unknown"),
    ({}, "unknown"),
    ({"name": "STATUS_POSTPONED", "state": "pre"}, "postponed"),
    ({"name": "STATUS_CANCELLED", "completed": True}, "cancelled"),
])
def test_status_type_state_and_completion(status_type, expected):
    result = ESPNProvider()._to_result(lookup("soccer_epl"), {"status": {"type": status_type}},
                                       {"score": "2"}, {"score": "1"})
    assert result.status == expected


def test_extra_time_without_period_scores_is_not_guessed():
    payload = copy.deepcopy(load("espn_soccer.json"))
    payload["events"][0]["competitions"][0]["status"]["type"]["name"] = "STATUS_FINAL_AET"
    home, away, ko = _first("espn_soccer.json")
    p = ESPNProvider()
    p.http = _Http(payload)
    assert p.find_result("football", home, away, ko, league_id="soccer_epl").status == "unknown"


def test_teams_match_on_short_name_and_abbreviation_and_either_home_away_order():
    p = _espn("espn_soccer.json")
    ko = parse_iso("2026-09-12T14:00Z")
    assert p.find_result("football", "Bournemouth", "Brentford", ko, league_id="soccer_epl").status == "final"
    assert p.find_result("football", "BOU", "BRE", ko, league_id="soccer_epl").status == "final"
    r = p.find_result("football", "Brentford", "AFC Bournemouth", ko, league_id="soccer_epl")   # sides swapped by the caller
    assert r.scores["full"] == (2.0, 2.0)
    r = p.find_result("football", "Aston Villa", "Nottingham Forest", parse_iso("2026-09-12T14:00Z"), league_id="soccer_epl")
    assert r.scores["full"] == (1.0, 2.0)
    swapped = p.find_result("football", "Nottingham Forest", "Aston Villa", parse_iso("2026-09-12T14:00Z"), league_id="soccer_epl")
    assert swapped.scores["full"] == (2.0, 1.0)                     # the score follows the names the caller used


def test_wrong_kickoff_unknown_league_or_wrong_sport_returns_none():
    p = _espn("espn_soccer.json")
    home, away, ko = _first("espn_soccer.json")
    assert p.find_result("football", home, away, parse_iso("2026-09-13T14:00Z"), league_id="soccer_epl") is None
    assert p.find_result("football", home, away, ko, league_id="atlantis_-_league") is None
    assert p.find_result("basketball", home, away, ko, league_id="soccer_epl") is None
    assert p.find_result("football", "Nobody", "Ghost", ko, league_id="soccer_epl") is None


# --------------------------------------------------------- other team sports
def test_nba_gives_quarters_halves_and_the_full_score():
    home, away, ko = _first("espn_nba.json")
    r = _espn("espn_nba.json").find_result("basketball", home, away, ko, league_id="nba")
    assert r.scores["full"] == (127.0, 107.0) and r.scores["1q"] == (33.0, 41.0)
    assert r.scores["1h"] == (66.0, 60.0) and r.scores["2h"] == (61.0, 47.0)


def test_college_basketball_has_halves_not_quarters():
    home, away, ko = _first("espn_ncaab.json")
    r = _espn("espn_ncaab.json").find_result("basketball", home, away, ko, league_id="ncaab")
    assert r.scores["full"] == (68.0, 71.0) and r.scores["1h"] == (30.0, 30.0) and "1q" not in r.scores


def test_nfl_and_college_football_have_quarters():
    home, away, ko = _first("espn_nfl.json")
    r = _espn("espn_nfl.json").find_result("american_football", home, away, ko, league_id="nfl")
    assert r.scores["full"] == (33.0, 27.0) and r.scores["4q"] == (6.0, 7.0) and r.scores["1h"] == (24.0, 10.0)
    home, away, ko = _first("espn_cfb.json")
    assert _espn("espn_cfb.json").find_result("american_football", home, away, ko, league_id="ncaaf").scores["full"] == (24.0, 23.0)


def test_baseball_is_full_score_only():
    home, away, ko = _first("espn_mlb.json")
    r = _espn("espn_mlb.json").find_result("baseball", home, away, ko, league_id="mlb")
    assert r.scores == {"full": (7.0, 4.0)}


# ------------------------------------------------------------ requests made
def test_one_day_per_request_cached_for_the_run_and_the_day_before_only_when_needed():
    home, away, ko = _first("espn_nba.json")
    day_before = load("espn_nba.json")
    p = _espn(by_date={"20260402": {"events": []}, "20260401": day_before})        # kick-off day empty, previous day has it
    r = p.find_result("basketball", home, away, ko, league_id="nba")
    assert r.status == "final"
    assert [c[1]["dates"] for c in p.http.calls] == ["20260402", "20260401"]
    p.find_result("basketball", home, away, ko, league_id="nba")
    assert len(p.http.calls) == 2                                                    # cached
    p.begin_run()
    p.find_result("basketball", home, away, ko, league_id="nba")
    assert len(p.http.calls) == 4                                                    # results change between runs


def test_the_first_day_is_enough_when_it_holds_the_match():
    home, away, ko = _first("espn_nba.json")
    p = _espn("espn_nba.json")
    p.find_result("basketball", home, away, ko, league_id="nba")
    assert len(p.http.calls) == 1 and p.http.calls[0][0] == "/basketball/nba/scoreboard"
    assert "groups" not in p.http.calls[0][1] and p.http.calls[0][1]["limit"] == 300


@pytest.mark.parametrize("fixture,league,groups", [("espn_ncaab.json", "ncaab", "50"), ("espn_cfb.json", "ncaaf", "80")])
def test_college_leagues_send_their_groups_filter(fixture, league, groups):
    home, away, ko = _first(fixture)
    p = _espn(fixture)
    p.find_result("basketball" if league == "ncaab" else "american_football", home, away, ko, league_id=league)
    assert p.http.calls[0][1]["groups"] == groups


def test_provider_errors_propagate_so_the_resolver_can_skip_them():
    with pytest.raises(ProviderError):
        _espn(error="400").find_result("football", "A", "B", parse_iso("2026-09-12T14:00Z"), league_id="soccer_epl")


def test_it_identifies_itself_as_the_default_python_requests_agent():
    """ESPN's edge returned 403 to every other agent tried (predbot/2.0, browser-style) on 2026-09-19."""
    import requests
    assert ESPNProvider().http._headers["User-Agent"] == requests.utils.default_user_agent()


def test_it_never_supplies_events_and_needs_no_key():
    p = ESPNProvider()
    assert p.fetch_events(None, None) == [] and p.enabled()


def test_nigeria_and_the_conference_league_have_espn_slugs():
    assert lookup("nigeria_-_premier_league").espn == "soccer/nga.1"
    assert lookup("uefa_-_europa_conference_league").espn == "soccer/uefa.europa.conf"


def test_competition_resolves_from_whichever_identifier_the_origin_recorded():
    from types import SimpleNamespace as N
    assert competition_for_event(N(league_id="", league="", extra={"sport_key": "soccer_turkey_super_league"})).id == "super_lig"
    assert competition_for_event(N(league_id="MLS", league="MLS", extra={})).id == "mls"
    assert competition_for_event(N(league_id="", league="england_-_premier_league", extra={})).id == "epl"
    assert competition_for_event(N(league_id="zzz", league="zzz", extra={})) is None


# ------------------------------------------------------------------ tennis
def _tennis():
    p = ESPNProvider()
    p.http = _Http(load("espn_tennis.json"))
    return p


def _ko(iso):
    return parse_iso(iso)


def test_a_three_set_match_reports_sets_and_total_games():
    """recorded: Pavlovic beat Varillas 6-3 6-7 7-5 -> sets 2-1, games 19-15"""
    r = _tennis().find_result("tennis", "Luka Pavlovic", "Juan Pablo Varillas", _ko("2026-08-24T15:10Z"), league_id="atp")
    assert r.status == "final" and r.scores["full"] == (2.0, 1.0)
    assert r.stats[("sets", "full")] == (2.0, 1.0) and r.stats[("games", "full")] == (19.0, 15.0)


def test_tennis_sides_may_be_the_other_way_round_and_names_can_be_partial():
    r = _tennis().find_result("tennis", "Juan Pablo Varillas", "Luka Pavlovic", _ko("2026-08-24T15:10Z"), league_id="atp")
    assert r.scores["full"] == (1.0, 2.0) and r.stats[("games", "full")] == (15.0, 19.0)
    assert _tennis().find_result("tennis", "Pavlovic L.", "Varillas J.", _ko("2026-08-24T15:10Z"), league_id="atp").status == "final"


def test_a_straight_sets_match():
    r = _tennis().find_result("tennis", "Jacob Fearnley", "Roberto Carballes Baena", _ko("2026-08-24T15:05Z"), league_id="atp")
    assert r.scores["full"] == (2.0, 0.0) and r.stats[("games", "full")] == (13.0, 9.0)


@pytest.mark.parametrize("home,away,when,expected", [
    ("SoonWoo Kwon", "Dusan Lajovic", "2026-08-25T15:05Z", "cancelled"),            # retired
    ("Otto Virtanen", "Grigor Dimitrov", "2026-08-27T16:30Z", "cancelled"),         # walkover
    ("Clara Burel", "Joelle Lilly Sophie Steur", "2026-09-19T15:05Z", "in_progress"),
    ("Alicia Herrero Linana", "Yasmine Kabbaj", "2026-09-19T16:30Z", "scheduled"),
])
def test_tennis_statuses(home, away, when, expected):
    league = "atp" if "08-2" in when else "wta"
    r = _tennis().find_result("tennis", home, away, _ko(when), league_id=league)
    assert r.status == expected and r.scores == {}


def test_a_tennis_league_slug_is_needed():
    assert _tennis().find_result("tennis", "Luka Pavlovic", "Juan Pablo Varillas", _ko("2026-08-24T15:10Z")) is None


# ------------------------------------------------------- grading tennis in games
def _line(market, selection, line=None, stat="points"):
    return MarketLine(market, selection, 2.0, stat=stat, line=line)


@pytest.fixture
def three_setter():
    return _tennis().find_result("tennis", "Luka Pavlovic", "Juan Pablo Varillas", _ko("2026-08-24T15:10Z"), league_id="atp")


def test_the_match_winner_is_graded_on_sets(three_setter):
    assert grade(_line("result2", "home"), three_setter, "tennis") == WON
    assert grade(_line("result2", "away"), three_setter, "tennis") == LOST


@pytest.mark.parametrize("selection,line,expected", [
    ("over", 30.5, WON), ("over", 34.5, LOST), ("under", 34.5, WON), ("under", 32.5, LOST), ("over", 34.0, PUSH)])
@pytest.mark.parametrize("stat", ["games", "points"])         # 34 games in total; providers label it either way
def test_a_games_total_is_graded_on_total_games_not_sets(three_setter, selection, line, expected, stat):
    assert grade(_line("total", selection, line, stat), three_setter, "tennis") == expected


@pytest.mark.parametrize("selection,line,expected", [
    ("home", -2.5, WON),          # 19 - 2.5 = 16.5 > 15
    ("home", -4.5, LOST),         # 14.5 < 15
    ("home", -4.0, PUSH),         # 15 = 15
    ("away", 4.5, WON),           # 15 + 4.5 = 19.5 > 19
    ("away", 4.0, PUSH), ("away", 3.5, LOST)])
def test_a_games_handicap_is_graded_on_games(three_setter, selection, line, expected):
    assert grade(_line("handicap", selection, line, "games"), three_setter, "tennis") == expected


@pytest.mark.parametrize("market,selection,line,expected", [
    ("handicap", "home", -1.5, LOST),      # sets 2-1: 2 - 1.5 = 0.5 < 1
    ("handicap", "away", 1.5, WON),        # 1 + 1.5 = 2.5 > 2
    ("total", "over", 2.5, WON),           # three sets played
    ("total", "under", 2.5, LOST)])
def test_a_sets_market_is_graded_on_sets(three_setter, market, selection, line, expected):
    assert grade(_line(market, selection, line, "sets"), three_setter, "tennis") == expected
