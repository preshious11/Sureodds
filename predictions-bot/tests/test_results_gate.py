"""Phases B and C: EuroLeague results, the competition table, the reworked results gate and resolver,
and "do not fetch what cannot be settled"."""
from __future__ import annotations

import copy
import logging
from types import SimpleNamespace as N
from unittest.mock import Mock

import pytest

from predbot.config import Config
from predbot.markets.competitions import BY_ID, COMPETITIONS, lookup
from predbot.markets.settlement import (ResultPath, coverage_lines, coverage_table, league_has_results,
                                        make_results_gate, result_paths, results_path, unsettleable_reason)
from predbot.providers.base import MatchResult
from predbot.providers.chain import ProviderChain, ResultsResolver, build_providers
from predbot.providers.espn import ESPNProvider
from predbot.providers.euroleague import EuroLeagueProvider, season_code
from predbot.providers.sharpapi import SharpApiProvider
from predbot.providers.budget import Budget
from predbot.selection.picker import select
from predbot.util.http import ProviderError
from predbot.util.timeutil import parse_iso
from tests.conftest import dt, load
from tests.test_picker import ev, sc


class _Http:
    def __init__(self, payload, error=None):
        self.payload, self.error, self.calls = payload, error, []

    def get(self, path, params=None):
        self.calls.append((path, dict(params or {})))
        if self.error:
            raise ProviderError(self.error)
        return self.payload


def _euro(**kw):
    p = EuroLeagueProvider()
    p.http = _Http(load("euroleague_games.json"), **kw)
    return p


# ================================================================ B: EuroLeague
def test_a_played_game_gives_full_quarters_and_halves():
    r = _euro().find_result("basketball", "Olympiacos Piraeus", "Real Madrid", parse_iso("2026-05-24T18:00:00Z"),
                            league_id="euroleague")
    assert r.status == "final" and r.source == "euroleague" and r.scores["full"] == (92.0, 85.0)
    assert r.scores["1q"] == (19.0, 26.0) and r.scores["4q"] == (31.0, 20.0)
    assert r.scores["1h"] == (46.0, 44.0) and r.scores["2h"] == (46.0, 41.0)


def test_overtime_is_part_of_the_full_score_and_the_second_half():
    """recorded: Zalgiris 90 - 94 Fenerbahce; quarters 20/22/20/18 v 25/22/14/19 (84-80) and one overtime (10-14)"""
    r = _euro().find_result("basketball", "Zalgiris Kaunas", "Fenerbahce Beko Istanbul",
                            parse_iso("2026-05-08T17:00:00Z"), league_id="euroleague")
    assert r.scores["full"] == (90.0, 94.0)
    assert r.scores["1h"] == (42.0, 47.0) and r.scores["2h"] == (48.0, 47.0)     # 2h = full - 1h, OT included


def test_an_unplayed_game_is_in_progress_not_a_0_0_final():
    r = _euro().find_result("basketball", "FC Barcelona", "Partizan Mozzart Bet Belgrade",
                            parse_iso("2027-04-16T18:30:00Z"), league_id="euroleague")
    assert r.status == "in_progress" and r.scores == {}


def test_names_match_on_any_alias_code_and_either_order():
    p = _euro()
    ko = parse_iso("2026-05-24T18:00:00Z")
    assert p.find_result("basketball", "Olympiacos", "Real", ko, league_id="euroleague").scores["full"] == (92.0, 85.0)
    assert p.find_result("basketball", "OLY", "MAD", ko, league_id="euroleague").status == "final"
    assert p.find_result("basketball", "Real Madrid", "Olympiacos Piraeus", ko,
                         league_id="euroleague").scores["full"] == (85.0, 92.0)


def test_wrong_time_league_or_sport_returns_none_and_one_request_serves_the_season():
    p = _euro()
    ko = parse_iso("2026-05-24T18:00:00Z")
    assert p.find_result("basketball", "Olympiacos", "Real", ko + __import__("datetime").timedelta(days=1),
                         league_id="euroleague") is None
    assert p.find_result("football", "Olympiacos", "Real", ko, league_id="euroleague") is None
    assert p.find_result("basketball", "Olympiacos", "Real", ko, league_id="nba") is None
    assert len(p.http.calls) == 1 and p.http.calls[0][0] == "/competitions/E/seasons/E2025/games"
    p.begin_run()
    p.find_result("basketball", "Olympiacos", "Real", ko, league_id="euroleague")
    assert len(p.http.calls) == 2


@pytest.mark.parametrize("when,expected", [("2026-09-30T18:00:00Z", "E2026"), ("2027-03-01T18:00:00Z", "E2026"),
                                           ("2026-06-30T18:00:00Z", "E2025"), ("2026-07-01T00:00:00Z", "E2026")])
def test_season_code_follows_the_july_start(when, expected):
    assert season_code("E", parse_iso(when)) == expected


def test_euroleague_errors_propagate_and_it_supplies_no_events():
    with pytest.raises(ProviderError):
        _euro(error="500").find_result("basketball", "A", "B", parse_iso("2026-05-24T18:00:00Z"), league_id="euroleague")
    assert EuroLeagueProvider().fetch_events(None, None) == []


# ================================================================ C1: one table
def test_every_competition_is_reachable_from_each_of_its_identifiers():
    for c in COMPETITIONS:
        for ident in (c.sharp, c.oddsapi, c.sgo):
            if ident:
                assert lookup(ident) is c, ident
    assert len({c.id for c in COMPETITIONS}) == len(COMPETITIONS)


def test_every_default_league_is_in_the_table_and_has_a_free_results_source():
    cfg = Config()
    free = ("football_data", "espn", "euroleague")
    for slug in cfg.sharpapi_leagues:
        c = lookup(slug)
        assert c is not None, slug
        assert c.espn or c.euroleague or c.football_data, slug


# ================================================================ C2: paths and capabilities
def _event(provider="sharpapi", sport="football", league_id="", extra=None, sources=None):
    return N(provider=provider, sport=sport, league="", league_id=league_id, extra=extra or {},
             sources=sources or [provider])


def _provs(*names):
    return {n: Mock() for n in names}


def test_paths_are_ordered_cheapest_and_most_reliable_first():
    e = _event("oddsapi", league_id="soccer_epl", extra={"sport_key": "soccer_epl"}, sources=["oddsapi", "sportsgameodds"])
    assert [p.name for p in result_paths(e, None, _provs("sportsgameodds", "football_data", "espn", "oddsapi"))] == [
        "sportsgameodds", "football_data", "espn", "oddsapi"]


def test_the_paid_scores_path_is_last_and_only_for_odds_api_soccer_events():
    e = _event("oddsapi", league_id="soccer_uefa_europa_league", extra={"sport_key": "soccer_uefa_europa_league"})
    assert [p.name for p in result_paths(e, None, _provs("espn", "oddsapi"))] == ["espn", "oddsapi"]
    assert [p.name for p in result_paths(_event(league_id="uefa_-_europa_league"), None, _provs("espn", "oddsapi"))] == ["espn"]


@pytest.mark.parametrize("league,sport,expected", [
    ("uefa_-_europa_league", "football", "espn"), ("uefa_-_europa_conference_league", "football", "espn"),
    ("turkey_-_super_lig", "football", "espn"), ("nigeria_-_premier_league", "football", "espn"),
    ("euroleague", "basketball", "euroleague"), ("atp", "tennis", "espn"), ("wta", "tennis", "espn"),
    ("england_-_premier_league", "football", "football_data"), ("nfl", "american_football", "espn")])
def test_leagues_that_had_no_path_now_have_one(league, sport, expected):
    assert results_path(_event(sport=sport, league_id=league), None, _provs("football_data", "espn", "euroleague")) == expected


def test_with_no_results_source_configured_a_league_has_no_path():
    assert results_path(_event(league_id="turkey_-_super_lig"), None, _provs("football_data")) is None
    assert results_path(_event(sport="tennis", league_id="atp"), None, {}) is None


def test_each_path_declares_what_it_can_grade():
    paths = {p.name: p for p in result_paths(_event(league_id="england_-_premier_league"), None,
                                             _provs("football_data", "espn"))}
    assert paths["football_data"].periods == frozenset({"full", "1h", "2h"})
    assert paths["espn"].periods == frozenset({"full"})                           # ESPN soccer has no per-period scores
    nba = result_paths(_event(sport="basketball", league_id="nba"), None, _provs("espn"))[0]
    assert {"full", "1h", "2h", "1q", "4q"} <= nba.periods
    tennis = result_paths(_event(sport="tennis", league_id="atp"), None, _provs("espn"))[0]
    assert tennis.stats == frozenset({"points", "games", "sets"})


def test_unsettleable_reason():
    espn = ResultPath("espn", frozenset({"full"}), frozenset({"points"}))
    fd = ResultPath("football_data", frozenset({"full", "1h", "2h"}), frozenset({"points"}))
    sgo = ResultPath("sportsgameodds")
    assert unsettleable_reason([espn], "1h", "points") == "period"
    assert unsettleable_reason([espn, fd], "1h", "points") is None                 # one path is enough
    assert unsettleable_reason([espn], "full", "corners") == "stat"
    assert unsettleable_reason([espn], "reg", "points") is None                    # reg == full
    assert unsettleable_reason([sgo], "1q", "corners") is None                     # unrestricted
    assert unsettleable_reason(["football_data"], "1h", "points") is None          # a bare name means unrestricted


def _paths_gate(*paths):
    return lambda event: list(paths)


def test_a_first_half_market_on_a_full_only_path_is_rejected_at_selection():
    a, b = ev(1), ev(2)
    full = sc(a, "result3", "home", 0.75, 1.45)
    first_half = sc(b, "result3", "home", 0.75, 1.45)
    first_half.line.period = "1h"
    ps = select([full, first_half], Config(), dt(), results_gate=_paths_gate(ResultPath("espn", frozenset({"full"}),
                                                                                       frozenset({"points"}))))
    assert ps.diagnostics["selection_rejections"]["PERIOD_UNSETTLEABLE"] == 1
    assert [s.event for s in ps.all()] == [a]


def test_the_same_market_passes_on_a_path_that_grades_halves():
    b = ev(2)
    first_half = sc(b, "result3", "home", 0.75, 1.45)
    first_half.line.period = "1h"
    gate = _paths_gate(ResultPath("football_data", frozenset({"full", "1h", "2h"}), frozenset({"points"})))
    ps = select([first_half, sc(ev(3), "result3", "home", 0.75, 1.45)], Config(), dt(), results_gate=gate)
    assert "PERIOD_UNSETTLEABLE" not in ps.diagnostics["selection_rejections"]
    assert b in [s.event for s in ps.all()]


def test_a_stat_no_path_can_grade_is_rejected():
    e = ev(1)
    corners = sc(e, "total", "over", 0.75, 1.45, line=9.5)
    corners.line.stat = "corners"
    ps = select([corners], Config(), dt(), results_gate=_paths_gate(ResultPath("espn", frozenset({"full"}), frozenset({"points"}))))
    assert ps.diagnostics["selection_rejections"]["STAT_UNSETTLEABLE"] == 1 and not ps.all()


# ================================================================ C3: the resolver, end to end with real parsers
def _espn_with(fixture):
    p = ESPNProvider()
    p.http = _Http(load(fixture))
    return p


def test_a_sharpapi_europa_league_pick_settles_from_an_espn_fixture():
    resolver = ResultsResolver({"espn": _espn_with("espn_soccer.json"), "oddsapi": Mock()})
    r = resolver.resolve("sharpapi", "x1", "football", "Levski Sofia", "RB Salzburg", parse_iso("2026-09-17T16:45Z"),
                         hints={"league_id": "uefa_-_europa_league", "league": "UEFA - EUROPA LEAGUE"})
    assert r.source == "espn" and r.scores["full"] == (0.0, 1.0)


def test_a_euroleague_pick_settles_from_the_euroleague_fixture():
    resolver = ResultsResolver({"euroleague": _euro()})
    r = resolver.resolve("sharpapi", "x2", "basketball", "Olympiacos Piraeus", "Real Madrid",
                         parse_iso("2026-05-24T18:00:00Z"), hints={"league_id": "euroleague"})
    assert r.source == "euroleague" and r.scores["full"] == (92.0, 85.0)


def test_a_wta_pick_settles_from_espn():
    p = ESPNProvider()
    p.http = _Http(load("espn_tennis.json"))
    r = ResultsResolver({"espn": p}).resolve("sharpapi", "x3", "tennis", "Alexandra Panova", "Katrina Scott",
                                             parse_iso("2026-09-12T16:05Z"), hints={"league_id": "wta"})
    assert r.source == "espn" and r.scores["full"] == (0.0, 2.0) and r.stats[("games", "full")] == (6.0, 12.0)


def test_the_odds_api_scores_path_is_never_called_when_a_free_source_answered():
    oddsapi = Mock()
    oddsapi.fetch_result.return_value = MatchResult(status="final", scores={"full": (9.0, 9.0)}, source="oddsapi")
    espn = _espn_with("espn_soccer.json")
    resolver = ResultsResolver({"espn": espn, "oddsapi": oddsapi})
    r = resolver.resolve("oddsapi", "e1", "football", "Levski Sofia", "RB Salzburg", parse_iso("2026-09-17T16:45Z"),
                         hints={"league_id": "soccer_uefa_europa_league", "sport_key": "soccer_uefa_europa_league"})
    assert r.source == "espn"
    oddsapi.fetch_result.assert_not_called()


def test_an_unknown_result_from_one_path_falls_through_to_the_next():
    unknown = Mock(); unknown.find_result.return_value = MatchResult(status="unknown", source="football_data")
    espn = Mock(); espn.find_result.return_value = MatchResult(status="final", scores={"full": (1.0, 0.0)}, source="espn")
    r = ResultsResolver({"football_data": unknown, "espn": espn}).resolve(
        "sharpapi", "1", "football", "A", "B", dt(), hints={"league_id": "england_-_premier_league"})
    assert r.source == "espn"


# ================================================================ C4: do not fetch what cannot be settled
def _sharp(leagues, **kw):
    p = SharpApiProvider("k", Budget(Mock()), leagues=leagues, **kw)
    return p


def test_a_league_with_no_results_path_is_not_fetched(caplog):
    cfg = Config(sharpapi_key="k", sharpapi_leagues=["turkey_-_super_lig", "england_-_premier_league"],
                 espn_results=False, euroleague_results=False, football_data_key="fd",
                 odds_api_key="", sgo_key="", odds_api_league_allowlist=[], sgo_leagues=[])
    with caplog.at_level(logging.WARNING):
        providers = build_providers(cfg, Mock())
    sharp = providers["sharpapi"]
    assert sharp.skipped_no_results == ["turkey_-_super_lig"]
    assert sharp._targets() == [{"league": "england_-_premier_league"}]
    assert sum("turkey_-_super_lig" in r.message and "no results path" in r.message for r in caplog.records) == 1


def test_when_every_league_is_skipped_nothing_is_fetched_not_the_unfiltered_feed():
    cfg = Config(sharpapi_key="k", sharpapi_leagues=["turkey_-_super_lig"], espn_results=False,
                 euroleague_results=False, football_data_key="", odds_api_key="", sgo_key="",
                 odds_api_league_allowlist=[], sgo_leagues=[])
    sharp = build_providers(cfg, Mock())["sharpapi"]
    assert sharp._targets() == []


def test_with_espn_on_nothing_configured_by_default_is_skipped():
    cfg = Config(sharpapi_key="k", odds_api_key="", sgo_key="")
    providers = build_providers(cfg, Mock())
    assert providers["sharpapi"].skipped_no_results == []
    assert "espn" in providers and "euroleague" in providers


def test_the_skipped_list_is_recorded_in_the_generation_diagnostics(db, monkeypatch):
    from predbot.jobs import daily_picks
    src = Mock(); src.enabled.return_value = True; src.fetch_events.return_value = []
    src.skipped_no_results = ["turkey_-_super_lig"]
    chain = ProviderChain({"sharpapi": src}, ["sharpapi"])
    chain.db, chain.cfg = None, None
    monkeypatch.setattr(daily_picks, "score_events", lambda *a: [])
    ps = daily_picks.build_picks(chain, db, Config(timezone="UTC", provider_order=["sharpapi"]), dt())
    assert ps.diagnostics["leagues_skipped_no_results"] == {"sharpapi": ["turkey_-_super_lig"]}


# ================================================================ C6: startup coverage
def test_the_coverage_table_lists_every_configured_league_and_flags_none(caplog):
    cfg = Config(sharpapi_leagues=["turkey_-_super_lig", "atp"], odds_api_league_allowlist=[], sgo_leagues=["MLS"])
    lines = coverage_lines(cfg, _provs("espn", "sportsgameodds"))
    text = "\n".join(lines)
    assert "turkey_-_super_lig" in text and "espn" in text
    none = coverage_lines(cfg, {})
    assert sum(line.rstrip().endswith("NONE") for line in none) == 3


def test_default_configuration_has_a_path_for_every_league_when_free_sources_are_on():
    cfg = Config()
    providers = {"espn": Mock(), "euroleague": Mock(), "football_data": Mock(),
                 "sportsgameodds": N(leagues=cfg.sgo_leagues, _available_leagues=None), "oddsapi": Mock()}
    assert [r for r in coverage_table(cfg, providers) if not r[2]] == []


def test_league_has_results():
    assert league_has_results("euroleague", {"euroleague": Mock()}) and not league_has_results("euroleague", {})
    assert not league_has_results("nope", {"espn": Mock()})
