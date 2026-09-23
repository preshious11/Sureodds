"""ESPN scoreboard - a free, keyless RESULTS-ONLY provider for every sport we pick.

It never supplies fixtures or odds (``fetch_events`` returns ``[]``) and is not in
``PROVIDER_ORDER``. The resolver asks it for the final score of a match by
competition + teams + kick-off.

Endpoint (recorded live, 2026-09)::

    GET https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard?dates=YYYYMMDD&limit=300

* ``dates`` accepts ONE day. A ``YYYYMMDD-YYYYMMDD`` range answered 400 for the
  team-sport leagues, so the provider asks for the kick-off's UTC day and, when the
  match is not there, the day before (ESPN groups by US Eastern date, which is never
  later than the UTC date). Each (league, day) payload is cached for the run.
* College basketball only lists its featured games unless ``groups=50`` is sent
  (2 events without, 46 with, for one March day). College football returned the same
  80 events with and without ``groups=80``; both are sent from the competitions table.

Event shape (team sports)::

    events[].date, events[].status.type.name  STATUS_FINAL | STATUS_FULL_TIME (soccer) |
                                               STATUS_FINAL_AET / _PEN | STATUS_POSTPONED | ...
    events[].competitions[0].competitors[]    homeAway, score ("2"), winner,
                                               team.displayName / shortDisplayName / abbreviation,
                                               linescores[].{period, value}   (NOT present for soccer)

Tennis is different: an event is a TOURNAMENT and matches sit in
``events[].groupings[].competitions[]`` (singles and doubles groupings); competitors carry
``athlete`` (``displayName`` / ``shortName``) and ``linescores[].value`` = games per set.
Tennis statuses seen: STATUS_FINAL, STATUS_RETIRED, STATUS_WALKOVER, STATUS_CANCELED,
STATUS_SCHEDULED, STATUS_IN_PROGRESS.

Result contract: ``scores["full"]`` is the final score including overtime (basketball, NFL, cfb);
tennis ``full`` is SETS won and ``stats[("games","full")]`` the total games per player.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterator, List, Optional, Tuple

import requests

from predbot.markets.competitions import Competition, lookup
from predbot.providers.base import BaseProvider, Event, MatchResult
from predbot.util.http import Http, ProviderError, RateLimiter
from predbot.util.names import same_team
from predbot.util.timeutil import parse_iso

log = logging.getLogger(__name__)

BASE_URL = "https://site.api.espn.com/apis/site/v2/sports"

FINAL = {"STATUS_FINAL", "STATUS_FULL_TIME"}
BEYOND_REGULATION = {"STATUS_FINAL_AET", "STATUS_FINAL_PEN", "STATUS_FINAL_OT_SO", "STATUS_FINAL_SO"}
POSTPONED = {"STATUS_POSTPONED", "STATUS_SUSPENDED", "STATUS_ABANDONED"}
CANCELLED = {"STATUS_CANCELED", "STATUS_CANCELLED", "STATUS_RETIRED", "STATUS_WALKOVER", "STATUS_FORFEIT",
             "STATUS_VOID"}


def _num(value) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _names(competitor: Dict) -> List[str]:
    src = competitor.get("team") or competitor.get("athlete") or {}
    return [str(src[k]) for k in ("displayName", "shortDisplayName", "abbreviation", "shortName", "fullName",
                                  "name", "location") if src.get(k)]


def _matches(name: str, competitor: Dict) -> bool:
    return any(same_team(name, n) for n in _names(competitor))


def _by_side(competitors: List[Dict]) -> Optional[Tuple[Dict, Dict]]:
    home = next((c for c in competitors if c.get("homeAway") == "home"), None)
    away = next((c for c in competitors if c.get("homeAway") == "away"), None)
    if home is None or away is None:
        return None
    return home, away


def _swap(pair):
    return None if pair is None else (pair[1], pair[0])


class ESPNProvider(BaseProvider):
    name = "espn"
    sports = ["football", "basketball", "american_football", "baseball", "tennis"]

    def __init__(self, min_interval: float = 1.0):
        # ESPN's edge answered 403 ("Access Denied") to `predbot/2.0`, a browser-style agent and a
        # `Mozilla/5.0 (compatible; ...)` agent, and 200 to the default python-requests agent
        # (checked 2026-09-19), so that one is sent explicitly.
        self.http = Http(BASE_URL, headers={"User-Agent": requests.utils.default_user_agent()},
                         rate=RateLimiter(min_interval), retries=0)
        self._days: Dict[Tuple[str, str, str], List[Dict]] = {}

    def enabled(self) -> bool:
        return True

    def begin_run(self) -> None:
        """Forget cached scoreboards: results change between settlement runs."""
        self._days = {}

    def fetch_events(self, start: datetime, end: datetime) -> List[Event]:
        return []                                   # results-only

    # ---------------------------------------------------------------- lookup
    def _scoreboard(self, comp: Competition, day) -> List[Dict]:
        key = (comp.espn, day.strftime("%Y%m%d"), comp.espn_groups)
        if key not in self._days:
            params = {"dates": key[1], "limit": 300}
            if comp.espn_groups:
                params["groups"] = comp.espn_groups
            data = self.http.get(f"/{comp.espn}/scoreboard", params)
            events = data.get("events", []) if isinstance(data, dict) else []
            self._days[key] = [e for e in events if isinstance(e, dict)]
        return self._days[key]

    def find_result(self, sport: str, home: str, away: str, start: datetime,
                    league_id: str = "", league: str = "") -> Optional[MatchResult]:
        comp = lookup(league_id, league)
        if comp is None or not comp.espn or (sport and comp.sport != sport):
            return None
        utc_day = start.astimezone(timezone.utc)
        for day in (utc_day, utc_day - timedelta(days=1)):
            try:
                events = self._scoreboard(comp, day)
            except ProviderError as e:
                log.warning("ESPN lookup failed (%s %s): %s", comp.espn, day.strftime("%Y%m%d"), e)
                raise
            found = self._find(comp, events, home, away, start)
            if found is not None:
                return found
        return None

    # ---------------------------------------------------------------- matching
    @staticmethod
    def _candidates(comp: Competition, events: List[Dict]) -> Iterator[Dict]:
        """Every match as a competition dict; tennis matches are unwrapped from
        their tournament's singles groupings."""
        for e in events:
            if comp.sport == "tennis":
                for g in e.get("groupings") or []:
                    label = str((g.get("grouping") or {}).get("displayName", ""))
                    if "singles" not in label.lower():
                        continue
                    for c in g.get("competitions") or []:
                        yield c
            else:
                for c in e.get("competitions") or []:
                    yield {**c, "date": c.get("date") or e.get("date"),
                           "status": c.get("status") or e.get("status") or {}}
                    break

    def _find(self, comp: Competition, events: List[Dict], home: str, away: str,
              start: datetime) -> Optional[MatchResult]:
        for c in self._candidates(comp, events):
            when = parse_iso(str(c.get("date") or ""))
            if when is None or abs((when - start).total_seconds()) > 3 * 3600:
                continue
            sides = c.get("competitors") or []
            pair = _by_side(sides)
            if pair is None:
                continue
            h, a = pair
            if _matches(home, h) and _matches(away, a):
                return self._to_result(comp, c, h, a)
            if _matches(home, a) and _matches(away, h):          # provider disagrees on home/away
                return self._to_result(comp, c, a, h)
        return None

    # ---------------------------------------------------------------- parsing
    @staticmethod
    def _status(c: Dict) -> str:
        return str(((c.get("status") or {}).get("type") or {}).get("name", "")).upper()

    def _to_result(self, comp: Competition, c: Dict, home: Dict, away: Dict) -> MatchResult:
        name = self._status(c)
        result = MatchResult(status="in_progress", source=self.name)
        if name in POSTPONED:
            result.status = "postponed"
            return result
        if name in CANCELLED:
            result.status = "cancelled"
            return result
        if name not in FINAL and name not in BEYOND_REGULATION:
            return result
        if comp.sport == "tennis":
            return self._tennis(result, home, away)
        hs, as_ = _num(home.get("score")), _num(away.get("score"))
        if hs is None or as_ is None:
            return MatchResult(status="unknown", source=self.name)
        h_lines = self._periods(home)
        a_lines = self._periods(away)
        if name in BEYOND_REGULATION:
            # score includes extra time; only the first two 45-minute periods are the 90 minutes
            if comp.sport == "football" and len(h_lines) >= 2 and len(a_lines) >= 2:
                hs, as_ = sum(h_lines[:2]), sum(a_lines[:2])
            else:
                return MatchResult(status="unknown", source=self.name)
        result.status = "final"
        result.scores["full"] = (hs, as_)
        self._add_periods(result, comp, h_lines, a_lines, hs, as_)
        return result

    @staticmethod
    def _periods(competitor: Dict) -> List[float]:
        lines = sorted((x for x in competitor.get("linescores") or [] if isinstance(x, dict)),
                       key=lambda x: x.get("period", 0))
        vals = [_num(x.get("value")) for x in lines]
        return [] if any(v is None for v in vals) else vals

    @staticmethod
    def _add_periods(result: MatchResult, comp: Competition, h: List[float], a: List[float],
                     hs: float, as_: float) -> None:
        if len(h) < 2 or len(h) != len(a):
            return                                                   # no per-period scores: only `full`
        if comp.sport in ("basketball", "american_football") and len(h) >= 4 and comp.id != "ncaab":
            for i in range(4):
                result.scores[f"{i + 1}q"] = (h[i], a[i])
            result.scores["1h"] = (h[0] + h[1], a[0] + a[1])
        elif comp.sport in ("basketball", "american_football", "football"):
            result.scores["1h"] = (h[0], a[0])                       # two halves (NCAAB, soccer if ever present)
        else:
            return                                                   # baseball innings etc.: full only
        first = result.scores["1h"]
        result.scores["2h"] = (hs - first[0], as_ - first[1])

    def _tennis(self, result: MatchResult, home: Dict, away: Dict) -> MatchResult:
        hg, ag = self._periods(home), self._periods(away)
        if not hg or len(hg) != len(ag):
            return MatchResult(status="unknown", source=self.name)
        sets_h = sum(1 for x, y in zip(hg, ag) if x > y)
        sets_a = sum(1 for x, y in zip(hg, ag) if y > x)
        result.status = "final"
        result.scores["full"] = (float(sets_h), float(sets_a))
        result.stats[("sets", "full")] = (float(sets_h), float(sets_a))
        result.stats[("games", "full")] = (sum(hg), sum(ag))
        return result
