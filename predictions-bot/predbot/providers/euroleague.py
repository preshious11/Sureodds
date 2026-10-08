"""EuroLeague / EuroCup results - a free, keyless RESULTS-ONLY provider (api-live.euroleague.net v2).

Recorded live, 2026-09::

    GET https://api-live.euroleague.net/v2/competitions/{E|U}/seasons/{E2026}/games?limit=500
    -> {"total": 402,
        "data": [{"identifier": "E2025_406", "played": true, "utcDate": "2026-05-24T18:00:00Z",
                  "local": {"club": {"code": "OLY", "name": "Olympiacos Piraeus",
                                     "abbreviatedName": "Olympiacos", "editorialName": "Olympiacos"},
                            "score": 92,
                            "partials": {"partials1": 19, ..., "partials4": 31, "extraPeriods": {"1": 10}}},
                  "road": {...same...}}]}

* One call returns the whole season (402 games for E2025), so it is fetched once per
  (competition, season) per settlement run.
* The season code is ``<competition><start year>``; the season starts in July, so a
  kick-off in Sept-Dec 2026 is ``E2026`` and one in March 2027 is also ``E2026``.
* ``score`` includes overtime; ``partials1..4`` are the quarters, ``extraPeriods`` the overtimes.
* ``played`` is true once a game has finished (a scheduled game carries scores of 0).

It never supplies fixtures or odds and is not in ``PROVIDER_ORDER``.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

from predbot.markets.competitions import lookup
from predbot.providers.base import BaseProvider, Event, MatchResult
from predbot.util.http import Http, ProviderError, RateLimiter
from predbot.util.names import same_team
from predbot.util.timeutil import parse_iso

log = logging.getLogger(__name__)

BASE_URL = "https://api-live.euroleague.net/v2"


def season_code(competition: str, when: datetime) -> str:
    when = when.astimezone(timezone.utc)
    year = when.year if when.month >= 7 else when.year - 1
    return f"{competition}{year}"


def _num(value) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _names(side: Dict) -> List[str]:
    club = side.get("club") or {}
    return [str(club[k]) for k in ("name", "abbreviatedName", "editorialName", "code") if club.get(k)]


def _matches(name: str, side: Dict) -> bool:
    return any(same_team(name, n) for n in _names(side))


class EuroLeagueProvider(BaseProvider):
    name = "euroleague"
    sports = ["basketball"]

    def __init__(self, min_interval: float = 1.0):
        self.http = Http(BASE_URL, rate=RateLimiter(min_interval), retries=0, timeout=40)
        self._seasons: Dict[str, List[Dict]] = {}

    def enabled(self) -> bool:
        return True

    def begin_run(self) -> None:
        """Forget the cached season: results change between settlement runs."""
        self._seasons = {}

    def fetch_events(self, start: datetime, end: datetime) -> List[Event]:
        return []                                   # results-only

    def _games(self, competition: str, when: datetime) -> List[Dict]:
        season = season_code(competition, when)
        key = f"{competition}/{season}"
        if key not in self._seasons:
            data = self.http.get(f"/competitions/{competition}/seasons/{season}/games", {"limit": 500})
            games = data.get("data", []) if isinstance(data, dict) else []
            self._seasons[key] = [g for g in games if isinstance(g, dict)]
        return self._seasons[key]

    def find_result(self, sport: str, home: str, away: str, start: datetime,
                    league_id: str = "", league: str = "") -> Optional[MatchResult]:
        comp = lookup(league_id, league)
        if comp is None or not comp.euroleague or (sport and sport != "basketball"):
            return None
        try:
            games = self._games(comp.euroleague, start)
        except ProviderError as e:
            log.warning("EuroLeague lookup failed (%s): %s", comp.euroleague, e)
            raise
        for g in games:
            when = parse_iso(str(g.get("utcDate") or ""))
            if when is None or abs((when - start).total_seconds()) > 3 * 3600:
                continue
            local, road = g.get("local") or {}, g.get("road") or {}
            if _matches(home, local) and _matches(away, road):
                return self._to_result(g, local, road)
            if _matches(home, road) and _matches(away, local):
                return self._to_result(g, road, local)
        return None

    @staticmethod
    def _quarters(side: Dict) -> Optional[List[float]]:
        partials = side.get("partials") or {}
        q = [_num(partials.get(f"partials{i}")) for i in range(1, 5)]
        return None if any(v is None for v in q) else q

    def _to_result(self, game: Dict, home: Dict, away: Dict) -> MatchResult:
        if not game.get("played"):
            return MatchResult(status="in_progress", source=self.name)
        hs, as_ = _num(home.get("score")), _num(away.get("score"))
        if hs is None or as_ is None:
            return MatchResult(status="unknown", source=self.name)
        result = MatchResult(status="final", source=self.name)
        result.scores["full"] = (hs, as_)
        hq, aq = self._quarters(home), self._quarters(away)
        if hq and aq:
            reg = (sum(hq), sum(aq))
            result.overtime = reg != (hs, as_)                       # the extra periods are what is left over
            result.scores["reg"] = reg
            for i in range(4):
                result.scores[f"{i + 1}q"] = (hq[i], aq[i])
            h1, a1 = hq[0] + hq[1], aq[0] + aq[1]
            result.scores["1h"] = (h1, a1)
            result.scores["2h"] = (hs - h1, as_ - a1)                # includes overtime
        return result
