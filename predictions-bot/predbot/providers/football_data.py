"""football-data.org (API v4) - a RESULTS-ONLY provider for football settlement.

It never supplies fixtures or odds (``fetch_events`` returns ``[]``) and is not a
member of ``PROVIDER_ORDER``. The resolver asks it for the final score of a
football match by teams + kick-off, so picks from The Odds API / SharpAPI can be
settled without spending SportsGameOdds objects or Odds API credits.

Endpoint shape (v4 docs)::

    GET /competitions/{code}/matches?dateFrom=YYYY-MM-DD&dateTo=YYYY-MM-DD
    -> {"matches": [{"utcDate": "...Z", "status": "FINISHED",
                     "homeTeam": {"name": ..., "shortName": ...}, "awayTeam": {...},
                     "score": {"duration": "REGULAR",
                               "fullTime": {"home": 2, "away": 1},
                               "halfTime": {"home": 1, "away": 0},
                               "regularTime": {...}}}]}          # regularTime only when duration != REGULAR

Auth is the ``X-Auth-Token`` header. The free tier allows 10 requests/minute, so
calls are spaced 6.5 s apart and every (competition, date) response is cached for
the duration of one settlement run (``begin_run`` clears it).
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from predbot.providers.base import BaseProvider, Event, MatchResult
from predbot.util.http import Http, ProviderError, RateLimiter
from predbot.util.names import same_team
from predbot.util.timeutil import parse_iso

log = logging.getLogger(__name__)

#: Competitions covered by the free tier that matter to us.
COMPETITIONS = ("PL", "ELC", "PD", "SA", "BL1", "FL1", "DED", "PPL", "CL", "EC", "WC", "BSA")

#: SharpAPI league slugs and Odds API sport keys -> football-data competition codes.
LEAGUE_TO_COMPETITION = {
    # SharpAPI slugs (``<country>_-_<competition>``)
    "england_-_premier_league": "PL", "england_-_championship": "ELC", "spain_-_la_liga": "PD",
    "italy_-_serie_a": "SA", "germany_-_bundesliga": "BL1", "france_-_ligue_1": "FL1",
    "netherlands_-_eredivisie": "DED", "portugal_-_primeira_liga": "PPL",
    "uefa_-_champions_league": "CL",
    # The Odds API sport keys
    "soccer_epl": "PL", "soccer_efl_champ": "ELC", "soccer_spain_la_liga": "PD",
    "soccer_italy_serie_a": "SA", "soccer_germany_bundesliga": "BL1", "soccer_france_ligue_one": "FL1",
    "soccer_netherlands_eredivisie": "DED", "soccer_portugal_primeira_liga": "PPL",
    "soccer_uefa_champs_league": "CL", "soccer_fifa_world_cup": "WC", "soccer_brazil_campeonato": "BSA",
    # SportsGameOdds league ids (only those seen in fixtures / config)
    "FR_LIGUE_1": "FL1", "UEFA_CHAMPIONS_LEAGUE": "CL",
}

STATUS_MAP = {
    "FINISHED": "final", "AWARDED": "final",
    "IN_PLAY": "in_progress", "PAUSED": "in_progress", "LIVE": "in_progress",
    "POSTPONED": "postponed", "SUSPENDED": "postponed",
    "CANCELLED": "cancelled",
}


def competition_for(league_id: str = "", league: str = "") -> Optional[str]:
    """football-data competition code for a stored league id / display name, or None."""
    for raw in (league_id, league):
        if not raw:
            continue
        if raw in LEAGUE_TO_COMPETITION:
            return LEAGUE_TO_COMPETITION[raw]
        slug = re.sub(r"[^a-z0-9]+", "_", str(raw).lower()).strip("_")
        for known, code in LEAGUE_TO_COMPETITION.items():
            if re.sub(r"[^a-z0-9]+", "_", known.lower()).strip("_") == slug:
                return code
    return None


def _pair(block) -> Optional[Tuple[float, float]]:
    if not isinstance(block, dict):
        return None
    h, a = block.get("home"), block.get("away")
    if h is None or a is None:
        return None
    try:
        return float(h), float(a)
    except (TypeError, ValueError):
        return None


class FootballDataProvider(BaseProvider):
    name = "football_data"
    sports = ["football"]

    def __init__(self, api_key: str, min_interval: float = 6.5):
        self.key = api_key
        self.http = Http("https://api.football-data.org/v4", headers={"X-Auth-Token": api_key},
                         rate=RateLimiter(min_interval), retries=0)
        self._matches: Dict[Tuple[str, str], List[Dict]] = {}

    def enabled(self) -> bool:
        return bool(self.key)

    def begin_run(self) -> None:
        """Forget cached match lists: results change between settlement runs."""
        self._matches = {}

    # results-only: never a fixture / odds source
    def fetch_events(self, start: datetime, end: datetime) -> List[Event]:
        return []

    # ---------------------------------------------------------------- lookup
    def _matches_on(self, code: Optional[str], day: datetime) -> List[Dict]:
        """Matches for one competition (or, with ``code`` None, every competition on
        this plan) within +-1 UTC day of ``day``; cached per (code, date)."""
        d = day.astimezone(timezone.utc).date()
        key = (code or "*", d.isoformat())
        if key in self._matches:
            return self._matches[key]
        path = f"/competitions/{code}/matches" if code else "/matches"
        data = self.http.get(path, {"dateFrom": (d - timedelta(days=1)).isoformat(),
                                    "dateTo": (d + timedelta(days=1)).isoformat()})
        matches = data.get("matches", []) if isinstance(data, dict) else []
        self._matches[key] = [m for m in matches if isinstance(m, dict)]
        return self._matches[key]

    def find_result(self, sport: str, home: str, away: str, start: datetime,
                    league_id: str = "", league: str = "") -> Optional[MatchResult]:
        if sport != "football" or not self.enabled():
            return None
        code = competition_for(league_id, league)
        try:
            matches = self._matches_on(code, start)
        except ProviderError as e:
            log.warning("football-data lookup failed (%s): %s", code or "all", e)
            raise
        for m in matches:
            when = parse_iso(m.get("utcDate", ""))
            if when is None or abs((when - start).total_seconds()) > 3 * 3600:
                continue
            if self._team_matches(home, m.get("homeTeam")) and self._team_matches(away, m.get("awayTeam")):
                return self._to_result(m)
        return None

    @staticmethod
    def _team_matches(name: str, team) -> bool:
        if not isinstance(team, dict):
            return False
        return any(t and same_team(name, t) for t in (team.get("name"), team.get("shortName"), team.get("tla")))

    @staticmethod
    def _to_result(m: Dict) -> Optional[MatchResult]:
        status = STATUS_MAP.get(str(m.get("status", "")).upper(), "unknown")
        result = MatchResult(status=status, source=FootballDataProvider.name)
        if status != "final":
            return result
        score = m.get("score") or {}
        # Football markets settle on 90 minutes. duration REGULAR: fullTime IS the 90
        # minutes (regularTime is absent). EXTRA_TIME / PENALTY_SHOOTOUT: fullTime
        # includes extra time and penalties, so only regularTime is safe - and if it
        # is missing the result is not usable.
        duration = str(score.get("duration", "REGULAR")).upper()
        full = _pair(score.get("fullTime")) if duration == "REGULAR" else _pair(score.get("regularTime"))
        if full is None:
            return MatchResult(status="unknown", source=FootballDataProvider.name)
        result.scores["full"] = full
        half = _pair(score.get("halfTime"))
        if half is not None:
            result.scores["1h"] = half
            result.scores["2h"] = (full[0] - half[0], full[1] - half[1])
        return result
