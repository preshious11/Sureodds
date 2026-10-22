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
tennis ``full`` is SETS won and ``stats[("games","full")]`` the total games per player. When the per-period
linescores are present, ``scores["reg"]`` is the regulation score (4 quarters; 2 halves in NCAAB; 9 innings) and
``overtime`` says whether more periods were played; without them regulation is unknown and a regulation-time bet
stays unresolved.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterator, List, Optional, Tuple

import requests

from predbot.markets.competitions import BY_ID, Competition, lookup
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
        self._schedules: Dict[Tuple[str, str, int], List[Dict]] = {}

    def enabled(self) -> bool:
        return True

    def begin_run(self) -> None:
        """Forget cached scoreboards/schedules: results change between runs."""
        self._days = {}
        self._schedules = {}

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

    # ---------------------------------------------------------------- recent form / head-to-head
    def team_insight(self, sport: str, home: str, away: str, start: datetime,
                     league_id: str = "", league: str = "", competition_id: str = "") -> Optional[Dict]:
        """Real recent form (last 5 completed games, either side) and real
        head-to-head (last 5 completed meetings between these exact two
        teams), built ONLY from ESPN's own schedule data for teams it can
        positively identify. Returns None - never a guess, never a
        fabricated line - when the competition, either team, or any
        meaningful history can't be resolved. Tennis is excluded: ESPN has
        no per-player "team" schedule to walk (see _candidates' tennis
        special-case above), and `espn.py` already treats tennis legs as
        player names, not teams.

        Reuses exactly the matching this provider's settlement path already
        trusts (`lookup`, `_scoreboard`, `_matches`/`same_team`) - no new
        provider, no new API, just a second use of the same free endpoint.

        ``competition_id`` (optional): the event's own already-resolved
        ``canonical_competition_id`` (predbot/storage/identity.py, set from
        the SAME 3-way sport_key/league_id/league match that
        competition_for_event() does for every other consumer). Tried FIRST,
        via the id-indexed BY_ID table - league_id/league alone miss
        whenever the surviving/"kept" provider for a merged fixture is Odds
        API (predbot/providers/oddsapi.py never sets Event.league_id at all;
        only extra["sport_key"] carries the competition, which this
        function's own league_id/league lookup() never consults).
        """
        if sport == "tennis":
            return None
        comp = (BY_ID.get(competition_id) if competition_id else None) or lookup(league_id, league)
        if comp is None or not comp.espn or (sport and comp.sport != sport) or comp.sport == "tennis":
            return None
        utc_day = start.astimezone(timezone.utc)
        home_id = away_id = None
        for day in (utc_day, utc_day - timedelta(days=1), utc_day + timedelta(days=1)):
            try:
                events = self._scoreboard(comp, day)
            except ProviderError:
                continue
            for c in self._candidates(comp, events):
                pair = _by_side(c.get("competitors") or [])
                if pair is None:
                    continue
                h, a = pair
                if _matches(home, h) and _matches(away, a):
                    home_id, away_id = h.get("team", {}).get("id"), a.get("team", {}).get("id")
                elif _matches(home, a) and _matches(away, h):
                    home_id, away_id = a.get("team", {}).get("id"), h.get("team", {}).get("id")
                if home_id and away_id:
                    break
            if home_id and away_id:
                break
        if not home_id or not away_id:
            return None
        home_games = self._team_schedule(comp, home_id, start)
        if not home_games:
            return None
        home_form = self._recent_results(home_games, home_id, start)
        h2h = self._recent_meetings(home_games, home_id, away_id, start)
        away_games = self._team_schedule(comp, away_id, start)
        away_form = self._recent_results(away_games, away_id, start) if away_games else []
        if not home_form and not away_form and not h2h:
            return None
        # Dates ride alongside each result (never a separately-ordered list)
        # so "home_form" and "home_form_dates" can never be mismatched by a
        # consumer - see _recent_results/_recent_meetings. Additive-only:
        # home_form/away_form/h2h keep their original List[str] shape, so
        # nothing already reading them (e.g. pro_picks.ts wlRecord()) breaks.
        return {
            "home_form": [r for r, _ in home_form], "home_form_dates": [d for _, d in home_form],
            "away_form": [r for r, _ in away_form], "away_form_dates": [d for _, d in away_form],
            "h2h": [r for r, _ in h2h], "h2h_dates": [d for _, d in h2h],
        }

    def _team_schedule(self, comp: Competition, team_id: str, start: datetime) -> List[Dict]:
        """A team's schedule, spanning the CURRENT season and - only if that
        alone has fewer than 5 completed games strictly before `start` -
        the prior season too (cross-conference/cup pairings can need more
        than one season to find 5 real meetings). Cached per (league, team,
        season) for the run, same discipline as `_scoreboard`."""
        season = start.year if start.month >= 7 else start.year - 1    # ESPN's "season" label (e.g. NBA 2025-26 -> 2026)
        games = self._schedule_season(comp, team_id, season + 1)
        completed = [g for g in games if self._before(g, start)]
        if len(completed) < 5:
            games = self._schedule_season(comp, team_id, season) + games
        return games

    def _schedule_season(self, comp: Competition, team_id: str, season: int) -> List[Dict]:
        key = (comp.espn, team_id, season)
        if key not in self._schedules:
            try:
                data = self.http.get(f"/{comp.espn}/teams/{team_id}/schedule", {"season": season})
            except ProviderError as e:
                log.warning("ESPN schedule lookup failed (%s team %s season %s): %s", comp.espn, team_id, season, e)
                self._schedules[key] = []
            else:
                events = data.get("events", []) if isinstance(data, dict) else []
                self._schedules[key] = [e for e in events if isinstance(e, dict)]
        return self._schedules[key]

    @staticmethod
    def _before(game: Dict, start: datetime) -> bool:
        when = parse_iso(str(game.get("date") or ""))
        comp0 = (game.get("competitions") or [{}])[0]
        status = str(((comp0.get("status") or game.get("status") or {}).get("type") or {}).get("name", "")).upper()
        return when is not None and when < start and (status in FINAL or status in BEYOND_REGULATION)

    @staticmethod
    def _result_for(game: Dict, team_id: str) -> Optional[str]:
        comp0 = (game.get("competitions") or [{}])[0]
        for c in comp0.get("competitors") or []:
            if str(c.get("id") or c.get("team", {}).get("id")) == str(team_id):
                if c.get("winner") is True:
                    return "W"
                if c.get("winner") is False:
                    return "L"
                return None
        return None

    def _recent_results(self, games: List[Dict], team_id: str, start: datetime, limit: int = 5) -> List[Tuple[str, str]]:
        """Newest-first (result, ISO kickoff date) pairs - the date rides
        alongside the result (never a separate, independently-ordered list)
        so a caller can never mismatch which date goes with which "W"/"L"."""
        completed = [g for g in games if self._before(g, start)]
        completed.sort(key=lambda g: parse_iso(str(g.get("date") or "")) or start, reverse=True)
        out = []
        for g in completed:
            r = self._result_for(g, team_id)
            if r:
                out.append((r, str(g.get("date") or "")[:10]))
            if len(out) >= limit:
                break
        return out

    def _recent_meetings(self, home_games: List[Dict], home_id: str, away_id: str,
                         start: datetime, limit: int = 5) -> List[Tuple[str, str]]:
        """Last `limit` completed meetings between these two teams specifically,
        from the HOME team's own schedule (any game the away team also appears
        in), newest first - e.g. [("W","2026-03-01"), ...] from the home
        team's perspective, result paired with its own kickoff date."""
        completed = [g for g in home_games if self._before(g, start)]
        meetings = []
        for g in completed:
            comp0 = (g.get("competitions") or [{}])[0]
            ids = {str(c.get("id") or c.get("team", {}).get("id")) for c in comp0.get("competitors") or []}
            if str(away_id) in ids:
                meetings.append(g)
        meetings.sort(key=lambda g: parse_iso(str(g.get("date") or "")) or start, reverse=True)
        out = []
        for g in meetings[:limit]:
            r = self._result_for(g, home_id)
            if r:
                out.append((r, str(g.get("date") or "")[:10]))
        return out

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
        status_type = (c.get("status") or {}).get("type") or {}
        state = str(status_type.get("state", "")).lower()
        result = MatchResult(status="unknown", source=self.name)
        if name in POSTPONED:
            result.status = "postponed"
            return result
        if name in CANCELLED:
            result.status = "cancelled"
            return result
        if name not in FINAL and name not in BEYOND_REGULATION and not status_type.get("completed"):
            if state == "pre":
                result.status = "scheduled"
            elif state == "in":
                result.status = "in_progress"
            elif name == "STATUS_SCHEDULED":
                result.status = "scheduled"
            elif name == "STATUS_IN_PROGRESS":
                result.status = "in_progress"
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
    def _add_regulation(result: MatchResult, comp: Competition, h: List[float], a: List[float]) -> None:
        """Regulation score and the overtime flag, from the per-period linescores (only when they are complete)."""
        if comp.sport in ("basketball", "american_football"):
            n = 2 if comp.id == "ncaab" else 4
        elif comp.sport == "baseball":
            n = 9
        else:
            return
        if len(h) != len(a) or len(h) < (n if comp.sport != "baseball" else 8):
            return                                                   # incomplete: regulation unknown
        result.overtime = len(h) > n
        result.scores["reg"] = (sum(h[:n]), sum(a[:n])) if result.overtime else result.scores["full"]

    @staticmethod
    def _add_periods(result: MatchResult, comp: Competition, h: List[float], a: List[float],
                     hs: float, as_: float) -> None:
        ESPNProvider._add_regulation(result, comp, h, a)
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
