"""team_insight() (ESPNProvider) - recent form / H2H for Pro Picks' reason line.

Regression focus: the Bayern/Arsenal forensic audit found this never once
populated in production (0/5138 candidates over a week), and traced a real,
code-verifiable contributor - Event.league_id is never set for an event
whose surviving/kept provider is Odds API (predbot/providers/oddsapi.py),
so team_insight()'s own lookup(league_id, league) can never resolve the
competition for such a fixture. These tests prove the fix (passing the
event's own already-resolved canonical_competition_id, tried first via
BY_ID) without touching any real HTTP endpoint - synthetic ESPN-shaped
payloads only, injected through the same `self.http` seam
test_results_espn.py already uses."""
from __future__ import annotations

from datetime import datetime, timezone

from predbot.providers.espn import ESPNProvider


class _Http:
    def __init__(self, by_path=None):
        self.by_path, self.calls = by_path or {}, []

    def get(self, path, params=None):
        self.calls.append((path, dict(params or {})))
        return self.by_path.get(path, {"events": []})


def _competitor(name, side, team_id, winner=None):
    return {"id": team_id, "homeAway": side, "winner": winner,
            "team": {"id": team_id, "displayName": name}}


def _game(date_iso, home_name, away_name, home_id, away_id, home_won, final=True):
    status = {"type": {"name": "STATUS_FINAL" if final else "STATUS_SCHEDULED"}}
    return {
        "date": date_iso,
        "competitions": [{
            "date": date_iso, "status": status,
            "competitors": [
                _competitor(home_name, "home", home_id, winner=home_won if final else None),
                _competitor(away_name, "away", away_id, winner=(not home_won) if final else None),
            ],
        }],
    }


START = datetime(2026, 10, 21, 19, 0, tzinfo=timezone.utc)
SCOREBOARD_PATH = "/soccer/uefa.champions/scoreboard"
BAYERN_SCHEDULE_PATH = "/soccer/uefa.champions/teams/bay/schedule"
ARSENAL_SCHEDULE_PATH = "/soccer/uefa.champions/teams/ars/schedule"


def _provider_with(scoreboard_events, bayern_games, arsenal_games):
    p = ESPNProvider()
    p.http = _Http({
        SCOREBOARD_PATH: {"events": scoreboard_events},
        BAYERN_SCHEDULE_PATH: {"events": bayern_games},
        ARSENAL_SCHEDULE_PATH: {"events": arsenal_games},
    })
    return p


def _fixture():
    scoreboard = [{
        "date": "2026-10-21T19:00Z",
        "competitions": [{
            "date": "2026-10-21T19:00Z", "status": {"type": {"name": "STATUS_SCHEDULED"}},
            "competitors": [
                _competitor("FC Bayern München", "home", "bay"),
                _competitor("Arsenal", "away", "ars"),
            ],
        }],
    }]
    bayern_games = [_game("2026-10-01T19:00Z", "FC Bayern München", "Union Berlin", "bay", "uni", home_won=True)]
    arsenal_games = [_game("2026-10-05T19:00Z", "Arsenal", "Chelsea", "ars", "che", home_won=False)]
    return scoreboard, bayern_games, arsenal_games


def test_resolves_via_canonical_competition_id_even_with_no_league_id_or_league():
    # Mirrors an Event sourced/kept from Odds API: league_id is always "" there
    # (predbot/providers/oddsapi.py never sets it) and the display league name
    # is passed as something that does NOT happen to normalize-match any
    # indexed provider slug - the exact failure mode the audit traced.
    scoreboard, bayern_games, arsenal_games = _fixture()
    p = _provider_with(scoreboard, bayern_games, arsenal_games)
    out = p.team_insight("football", "FC Bayern München", "Arsenal", START,
                          league_id="", league="Not A Real League String",
                          competition_id="ucl")
    assert out is not None
    # Only 1 completed game is seeded, below the 5-game threshold, so
    # _team_schedule merges the current + prior season fetch - the fake
    # Http returns the same fixture for both, hence the duplicate.
    assert out["home_form"] == ["W", "W"]
    assert out["away_form"] == ["L", "L"]


def test_without_competition_id_the_same_unresolvable_league_id_and_league_returns_none():
    # Same event, but without the fix's new parameter - demonstrates the bug
    # exactly as it behaved in production before this change.
    scoreboard, bayern_games, arsenal_games = _fixture()
    p = _provider_with(scoreboard, bayern_games, arsenal_games)
    out = p.team_insight("football", "FC Bayern München", "Arsenal", START,
                          league_id="", league="Not A Real League String")
    assert out is None


def test_an_unresolvable_competition_id_falls_back_to_league_id_league_not_a_crash():
    # A bogus/unknown competition_id (e.g. a canonical id this table doesn't
    # have yet) must never raise or silently stay broken - it falls back to
    # the existing lookup(league_id, league) path exactly as before.
    scoreboard, bayern_games, arsenal_games = _fixture()
    p = _provider_with(scoreboard, bayern_games, arsenal_games)
    out = p.team_insight("football", "FC Bayern München", "Arsenal", START,
                          league_id="UEFA_CHAMPIONS_LEAGUE", league="UEFA Champions League",
                          competition_id="not_a_real_id")
    assert out is not None


def test_still_returns_none_when_neither_id_nor_league_resolves_anything_never_a_guess():
    scoreboard, bayern_games, arsenal_games = _fixture()
    p = _provider_with(scoreboard, bayern_games, arsenal_games)
    out = p.team_insight("football", "FC Bayern München", "Arsenal", START,
                          league_id="", league="", competition_id="")
    assert out is None


def test_tennis_is_still_excluded_even_with_a_competition_id_given():
    p = ESPNProvider()
    out = p.team_insight("tennis", "Player A", "Player B", START, competition_id="atp")
    assert out is None


def test_dates_ride_alongside_each_result_never_a_separately_ordered_list():
    # Workstream 1 (explanations must show evidence "sample sizes and
    # dates"): home_form/away_form/h2h keep their original W/L shape
    # (additive-only), with a same-length, same-order *_dates array built
    # from the SAME completed-game record as the result - never a second,
    # independently-sorted lookup that could drift out of alignment.
    scoreboard, bayern_games, arsenal_games = _fixture()
    p = _provider_with(scoreboard, bayern_games, arsenal_games)
    out = p.team_insight("football", "FC Bayern München", "Arsenal", START, competition_id="ucl")
    assert out["home_form"] == ["W", "W"]
    assert out["home_form_dates"] == ["2026-10-01", "2026-10-01"]
    assert len(out["home_form_dates"]) == len(out["home_form"])
    assert out["away_form"] == ["L", "L"]
    assert out["away_form_dates"] == ["2026-10-05", "2026-10-05"]
    assert out["h2h"] == [] and out["h2h_dates"] == []
