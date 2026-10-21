"""SportsGameOdds v2 (https://sportsgameodds.com/docs).

One /v2/events call returns fixtures + every market (consensus fair/book odds,
per-book prices) + results/settled stat values, so it doubles as our most
complete results source. Billing is per *event object*, tracked in Budget.
"""
from __future__ import annotations

import copy
import json
import logging
import math
import os
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from predbot.providers.base import SIDE_MARKETS, BaseProvider, Event, MarketLine, MatchResult, norm_book
from predbot.providers.budget import Budget
from predbot.providers.sgo_meter import SgoMeter
from predbot.util.http import Http, ProviderError, RateLimiter
from predbot.util.names import same_team
from predbot.util.odds import american_to_decimal
from predbot.util.duration import DURATION_H
from predbot.util.timeutil import iso, parse_iso, utcnow

log = logging.getLogger(__name__)

SPORT_MAP = {
    "SOCCER": "football", "BASKETBALL": "basketball", "FOOTBALL": "american_football",
    "BASEBALL": "baseball", "TENNIS": "tennis", "MMA": "mma",
    "GOLF": "golf",
}
SPORT_TO_SGO = {v: k for k, v in SPORT_MAP.items()}

DEFAULT_ALLOWED_STATS = ("points", "cornerKicks", "yellowCards", "weightedCards", "bothTeamsScored")
DEFAULT_ALLOWED_PERIODS = ("game", "reg", "1h", "2h")

PERIOD_MAP = {"game": "full", "reg": "reg", "1h": "1h", "2h": "2h", "1q": "1q", "2q": "2q", "3q": "3q",
              "4q": "4q", "1s": "1s", "2s": "2s", "3s": "3s",
              "1i5": "1i5"}
STAT_MAP = {"points": "points", "cornerKicks": "corners", "weightedCards": "cards", "yellowCards": "cards",
            "bothTeamsScored": "btts"}


def _f(v) -> Optional[float]:
    try:
        return float(str(v).replace("+", ""))
    except (TypeError, ValueError):
        return None


def _same_line(a: Optional[float], b: Optional[float]) -> bool:
    """Whether two independently-parsed line values represent the same line - tolerant of
    float noise from two separate str->float conversions, unlike a bare ``!=``."""
    if a is None or b is None:
        return a is b
    return math.isclose(a, b, rel_tol=0, abs_tol=1e-6)


def _merge_sport_state(current: str, state: str) -> str:
    """Combine per-league states into one per-sport state.  A FAILED league fails
    the whole sport; else PARTIAL wins; else the sport is EMPTY until any event
    actually parses (callers upgrade to SUCCESS when events > 0)."""
    if state == "failed" or current == "failed":
        return "failed"
    if state == "partial" or current == "partial":
        return "partial"
    return "empty"


def _attempts(http) -> int:
    """HTTP attempts the last call made (1 for a stand-in client that does not report it)."""
    n = getattr(http, "last_attempts", 1)
    return n if isinstance(n, int) and n >= 1 else 1


#: body cap for SGO pages (an event carries ~1,000 odds objects; a page holds up to 100 events)
SGO_MAX_BODY_BYTES = 200 * 1024 * 1024


class SgoUnavailable(ProviderError):
    """SGO is deliberately not being called (cool-down, exhausted quota, budget policy): no HTTP request was made."""


class SportsGameOddsProvider(BaseProvider):
    name = "sportsgameodds"

    def __init__(self, api_key: str, budget: Budget, sports: List[str], leagues: List[str],
                 event_limit: int = 25, monthly_limit: int = 2500,
                 allowed_stats: Optional[List[str]] = None, allowed_periods: Optional[List[str]] = None,
                 max_player_props: int = 0, min_interval: float = 6.5):
        self.key = api_key
        # Odd objects outside these stat/period ids are rejected at parse time
        # (SGO lists hundreds of inning/quarter/player variants per event).
        self.allowed_stats = set(DEFAULT_ALLOWED_STATS if allowed_stats is None else allowed_stats)
        self.allowed_periods = set(DEFAULT_ALLOWED_PERIODS if allowed_periods is None else allowed_periods)
        self.max_player_props = max(0, max_player_props)
        self.budget = budget
        self.sports = sports
        self.leagues = leagues
        self.event_limit = event_limit
        #: fallback cap used only until /account/usage has been read once (the plan's real cap replaces it)
        self.monthly_limit = monthly_limit
        self.db = getattr(budget, "db", None)
        self.meter = SgoMeter(self.db, event_limit=event_limit, fallback_cap=monthly_limit)
        #: who is driving this process: scheduled (main.py) | manual (cli.py) | diagnostic (diagnose_*.py)
        self.origin = os.environ.get("PREDBOT_SGO_ORIGIN", "scheduled")
        self._available_leagues: Optional[set] = None
        self._league_sport: Dict[str, str] = {}          # leagueID -> SPORT (SGO id)
        self._parsed_sport_by_league: Dict[str, str] = {}  # leagueID -> canonical sport seen in parsed events
        self.sport_status: Dict[str, Dict] = {}          # canonical sport -> status/events/leagues
        self.skipped_no_results: List[str] = []          # configured leagues with no results path (not fetched)
        self.parse_counts: Counter = Counter()           # rejection reasons
        # The Amateur plan allows 10 requests a minute: pace at 6.5 s (it used to be 1 s = 60/min, so bursts were
        # refused). retries=0 + sleep_on_429=False: this class owns retry and cool-down policy (see _call), so a 429
        # is never silently retried by the HTTP layer and one logical call can never multiply into many requests.
        limiter = RateLimiter(min_interval)
        # An SGO page carries ~1,000 odds objects per event, so its body cap is well above the shared default.
        self.http = Http("https://api.sportsgameodds.com/v2", headers={"X-Api-Key": api_key}, rate=limiter,
                         retries=0, sleep_on_429=False, max_bytes=SGO_MAX_BODY_BYTES)
        self._probe = Http("https://api.sportsgameodds.com/v2", headers={"X-Api-Key": api_key}, rate=limiter,
                           retries=0, timeout=15, sleep_on_429=False)
        # Zero is a valid monotonic timestamp on a fresh host, not "no failure".
        self._probe_failed_at: Optional[float] = None
        self._catalog_retry_at = 0.0
        # per-run / per-process memos that stop the same data being bought twice
        self._league_memo: Dict[Tuple[str, str], Tuple[datetime, List[Event], int]] = {}
        self._window_cache: Dict[Tuple, List[Dict]] = {}
        self._result_cache: Dict[str, Optional[MatchResult]] = {}

    def enabled(self) -> bool:
        return bool(self.key)

    def begin_run(self) -> None:
        """Forget the per-run result memos (a new settlement run wants fresh answers, but not twice within a run)."""
        self._window_cache, self._result_cache = {}, {}

    # ------------------------------------------------------------------ the provider's own quota
    def _refresh_usage(self) -> bool:
        """Read /account/usage and hand the monthly entity numbers to the meter. Only numbers are kept - the response
        also carries the account e-mail and key id, which are never stored, logged or printed."""
        if self._probe_failed_at is not None and time.monotonic() - self._probe_failed_at < 1800:
            return False
        try:
            data = self._probe.get("/account/usage")
        except ProviderError as e:
            self._probe_failed_at = time.monotonic()
            self.meter.record("quota_probe", "/account/usage", "rate_limited" if getattr(e, "status", None) == 429
                              else "error", attempts=_attempts(self._probe), note=self.origin)
            log.info("SGO: could not read /account/usage (%s); relying on the local estimate", type(e).__name__)
            return False
        month = (((data or {}).get("data") or {}).get("rateLimits") or {}).get("per-month") or {}
        used, cap = month.get("current-entities"), month.get("max-entities")
        self.meter.record("quota_probe", "/account/usage", "ok", objects=1,
                          attempts=_attempts(self._probe), note=self.origin)
        if isinstance(used, (int, float)) and isinstance(cap, (int, float)) and cap > 0:
            self.meter.observe(int(used), int(cap))
            return True
        return False                                    # a plan with no entity cap: nothing to protect

    def monthly_entities(self) -> Optional[Tuple[int, int]]:
        """(estimated used, cap) of SGO's monthly object quota, or None when unknown / uncapped. Read from
        /account/usage rarely (that call is billed too: every 12 h, every 3 h above 75 %, and right after a 429) and
        kept current in between from the objects this process bills itself."""
        if self.meter.probe_due():
            self._refresh_usage()
        return self.meter.reading()

    def quota_exhausted(self) -> bool:
        self.monthly_entities()
        return self.meter.exhausted()

    def quota_state(self) -> Dict:
        self.monthly_entities()
        snap = self.meter.snapshot()
        return {"monthly_entities_used": snap["estimated_used"], "monthly_entities_max": snap["cap"],
                "exhausted": self.meter.exhausted(), "level": snap["level"], "days_left": snap["days_left"],
                "generation_allowance_today": snap["generation_allowance_today"],
                "cooldown_until": snap["cooldown_until"]}

    # ------------------------------------------------------------------ the ONE place that talks to SGO
    @staticmethod
    def _is_429(e: ProviderError) -> bool:
        """A rate-limit / quota answer FROM SGO. Decided on the HTTP status; the message text is only consulted for an
        error with no status that is not a transport failure (a timeout's text can contain anything, URL included)."""
        status = getattr(e, "status", None)
        if status is not None:
            return status == 429
        if getattr(e, "kind", "") in ("transport", "timeout"):
            return False
        return "rate limited" in str(e).lower() or " 429" in str(e)

    @staticmethod
    def _is_league_config_error(e: ProviderError) -> bool:
        """SGO ANSWERED that the league request is not allowed for this key/tier (a 4xx whose body names leagueID,
        e.g. "You must specify either a leagueID or an eventID at this subscription tier"). A connection error,
        timeout, 429 or 5xx is not that, even though its message carries the request URL and so the word leagueID."""
        status = getattr(e, "status", None)
        if status is None or not 400 <= status < 500 or status == 429:
            return False
        return "leagueid" in (getattr(e, "body", "") or str(e)).lower()

    def _call(self, path: str, params: Optional[Dict], reason: str, *, sport: str = "", league: str = "",
              event_id: str = "", needs_sgo: bool = True) -> Dict:
        """Every SportsGameOdds request goes through here, so every request is gated, attributed and metered.

        Gates (no HTTP is made if one says no): the cool-down after a 429, the quota/budget policy for this ``reason``.
        Retry policy: at most ONE retry, for a 5xx / network error only; never for a 429 (that starts a cool-down).
        Metering: the billed objects (objects returned, minimum 1) and the HTTP attempts are recorded with the reason."""
        if self.meter.in_cooldown():
            raise SgoUnavailable("SGO is cooling down after a rate limit")
        self.monthly_entities()
        if not self.meter.allows(reason, self.origin, needs_sgo):
            raise SgoUnavailable(f"SGO budget does not allow '{reason}' right now")
        attempts = 0
        data = None
        last: Optional[ProviderError] = None
        for _try in range(2):
            try:
                data = self.http.get(path, params)
                attempts += _attempts(self.http)
                break
            except ProviderError as e:
                attempts += _attempts(self.http)
                last = e
                if self._is_429(e):
                    self.meter.record(reason, path, "rate_limited", sport=sport, league=league, event_id=event_id,
                                      attempts=attempts, note=self.origin)
                    self._on_rate_limited()
                    raise
                status = getattr(e, "status", None)
                if status is not None and status < 500:
                    break                                   # a 4xx (bad league, not found) will not get better
        if data is None:
            self.meter.record(reason, path, "error", sport=sport, league=league, event_id=event_id,
                              attempts=attempts, note=self.origin)
            raise last or ProviderError("SGO request failed")
        n = len(data["data"]) if isinstance(data, dict) and isinstance(data.get("data"), list) else 1
        self.meter.record(reason, path, "ok", sport=sport, league=league, event_id=event_id, attempts=attempts,
                          objects=max(1, n), note=self.origin)
        self.meter.succeeded()
        return data

    def _on_rate_limited(self) -> None:
        """A 429 means one of two different things. Ask SGO which: out of monthly quota (pause until the usage is due
        for a re-read) or only over the per-minute limit (pause briefly and escalate if it repeats)."""
        self._probe_failed_at = None
        self._refresh_usage()
        gone = self.meter.exhausted()
        if gone:
            self.budget.mark_exhausted(self.name, "SGO monthly quota used up")
        self.meter.rate_limited(gone)

    # ------------------------------------------------------------------ league catalogue
    CATALOG_TTL = timedelta(days=7)

    def _load_catalog(self) -> Optional[Tuple[set, Dict[str, str]]]:
        raw = self.db.get_kv("sgo:leagues") if self.db is not None else None
        if not raw:
            return None
        try:
            doc = json.loads(raw)
            at = datetime.strptime(doc["at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - at > self.CATALOG_TTL:
                return None
            return set(doc["ids"]), {k: str(v) for k, v in doc["sport"].items()}
        except (ValueError, KeyError, TypeError):
            return None

    def available_leagues(self) -> Optional[set]:
        """LeagueIDs available to THIS key at its subscription tier.

        GET /leagues IS billed (one object per league listed, per SGO's docs - this used to be called free), so the
        answer is kept in the database for 7 days: a restart, a CLI run or a failed attempt no longer buys it again.
        Returns None when it cannot be had - callers then keep their configured list.
        Also caches the league -> sport mapping used to keep event queries
        sport-consistent (SGO 400s when a leagueID does not match sportID).
        """
        if self._available_leagues is not None:
            return self._available_leagues
        cached = self._load_catalog()
        if cached:
            self._available_leagues, self._league_sport = cached
            return self._available_leagues
        if time.monotonic() < self._catalog_retry_at:
            return None
        try:
            data = self._call("/leagues", None, "catalog")
        except ProviderError as e:
            self._catalog_retry_at = time.monotonic() + 1800          # do not ask again for half an hour
            log.warning("SGO: could not list leagues: %s", e)
            return None
        rows = data.get("data", []) if isinstance(data, dict) else []
        ids = set()
        league_sport: Dict[str, str] = {}
        for r in rows:
            if not isinstance(r, dict) or not r.get("leagueID"):
                continue
            ids.add(r["leagueID"])
            sport = r.get("sportID")
            if not sport and isinstance(r.get("sport"), dict):
                sport = r["sport"].get("sportID")
            if sport:
                league_sport[r["leagueID"]] = str(sport).upper()
        log.info("SGO: %d leagues available at this tier", len(ids))
        self._available_leagues = ids
        self._league_sport = league_sport
        if self.db is not None and ids:
            self.db.set_kv("sgo:leagues", json.dumps({"at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                                       "ids": sorted(ids), "sport": league_sport}))
        return ids

    # ------------------------------------------------------------------ fetch
    def fetch_events(self, start: datetime, end: datetime) -> List[Event]:
        self.last_fetch_complete = True
        self.fetch_counts = {}
        self.parse_counts = Counter()
        self.sport_status = {}
        self._parsed_sport_by_league = {}
        if self.budget.is_exhausted(self.name):
            self.last_fetch_complete = False
            return []                              # tier/quota problem already seen today
        self.monthly_entities()                    # reads /account/usage when it is due (rarely)
        if self.meter.exhausted():
            self.last_fetch_complete = False
            self.budget.mark_exhausted(self.name, "SGO monthly quota used up")
            log.warning("SGO: monthly quota is used up (%s) - not calling it until the period resets; "
                        "settlement falls back to the free results sources", self.meter.reading())
            return []
        until = self.meter.cooldown_until()
        if until is not None:
            self.last_fetch_complete = False
            log.warning("SGO: cooling down after a rate limit until %s - skipping this run", until.isoformat())
            return []
        allowance = self.meter.generation_allowance()
        if allowance is None:                      # usage never read: the local calendar-month estimate
            limit = max(0, min(self.event_limit, self.budget.monthly_daily_allowance(self.name, self.monthly_limit)))
        else:
            limit = allowance                      # already <= event_limit and net of what today has spent
        if limit <= 0:
            self.last_fetch_complete = False
            log.warning("SGO: no object allowance left today (period budget %s, %d spent on generation today)",
                        self.meter.snapshot().get("generation_allowance_today"), self.meter.spent_today("generation"))
            return []
        # Auto-discover leagues when none are configured: use ALL leagues
        # available to this key (paid tiers get every sport; free tiers
        # get whatever the /leagues endpoint returns).
        leagues = list(self.leagues) if self.leagues else None
        if not leagues:
            avail = self.available_leagues()
            if avail:
                leagues = sorted(avail)
                log.info("SGO: auto-discovered %d leagues from provider catalog", len(leagues))
            elif avail is None:
                # the catalogue could not be read (network error, 5xx, cool-down): nothing is proven about the key,
                # so this run is skipped and SGO stays eligible for the next one
                self.last_fetch_complete = False
                log.warning("SGO: no leagues configured and the league catalogue is unavailable right now - "
                            "skipping this run")
                return []
            else:
                self.last_fetch_complete = False
                self.budget.mark_exhausted(self.name, "no leagues available for this key")
                log.warning("SGO: no leagues configured and auto-discovery returned empty - "
                            "check API key access or set SGO_LEAGUES")
                return []
        else:
            # When leagues are explicitly configured, drop any not covered by this
            # key's tier (e.g. EPL on a free plan).  The catalogue is kept for 7 days
            # (it is billed per league listed).
            avail = self.available_leagues()
            if avail is not None and avail:
                before = len(leagues)
                leagues = [lg for lg in leagues if lg in avail]
                dropped = before - len(leagues)
                if dropped:
                    log.warning("SGO: dropping %d leagues not covered by this tier", dropped)
                if not leagues:
                    self.last_fetch_complete = False
                    self.budget.mark_exhausted(self.name, "configured leagues unavailable at tier")
                    return []
        leagues = [lg for lg in leagues if lg not in self.skipped_no_results]      # nothing can settle these
        if not leagues:
            self.last_fetch_complete = False
            return []
        # Filter by configured sports when provided (empty sgo_sports = all sports).
        # The league -> sport map comes from the provider catalog so the filter
        # never relies on a hardcoded sports list.
        if self.sports and leagues and not getattr(self, '_league_sport', None):
            try:
                self.available_leagues()
            except Exception as e:  # noqa: BLE001 - sport filter degrades to "unfiltered", never a hard failure
                log.warning("SGO: available_leagues() failed, sport filter skipped this run: %s", e, exc_info=True)
        avail_map = getattr(self, '_league_sport', None) or {}
        if avail_map and self.sports:
            sports_upper = {s.upper() for s in self.sports}
            mapped_leagues = [lg for lg in leagues if avail_map.get(lg, '')]
            if mapped_leagues:
                leagues = [lg for lg in leagues if avail_map.get(lg, '').upper() in sports_upper]
                log.info("SGO: filtered to %d leagues matching configured sports %s",
                         len(leagues), self.sports)
        n_raw = 0                     # raw event objects fetched (the object allowance is measured in these)
        out: List[Event] = []
        pending = []
        self.fetch_counts = {}
        league_buf: Dict[str, List[Event]] = {}

        def absorb(batch, league=""):
            """Parse a fetched page straight away and let go of the raw payload: an SGO event carries ~1,000 odds
            objects, so keeping every raw page until the end held the whole response in memory at once."""
            for ev in batch:
                try:
                    e = self._parse_event(ev)
                    if e and e.markets and start <= e.start < end:
                        out.append(e)
                        league_buf.setdefault(league, []).append(e)
                        self._parsed_sport_by_league[str(ev.get("leagueID", ""))] = e.sport
                except Exception as ex:  # noqa: BLE001
                    log.debug("SGO parse error %s: %s", ev.get("eventID"), ex)
            batch.clear()
        # Share the existing object allowance across configured leagues so the
        # first sport in a multi-league response cannot consume the whole page.
        rate_limited_in_a_row = 0
        for index, league in enumerate(leagues):
            remaining = limit - n_raw
            if remaining <= 0:
                self.last_fetch_complete = False
                break
            if rate_limited_in_a_row >= 2:
                # two leagues in a row refused: the key is blocked, not busy. Stop instead of working through every
                # remaining league (that was a 291 s generation stall).
                self.last_fetch_complete = False
                log.warning("SGO: rate limited on %d consecutive leagues - stopping this run", rate_limited_in_a_row)
                break
            # A PARTIAL slate is retried within the same process: a league already bought for a window that covers
            # this one is reused instead of being bought (and billed) again.
            memo = self._league_memo.get((start.strftime("%Y-%m-%d"), league))
            if memo is not None and memo[0] <= start and memo[1] >= end:
                for e in memo[2]:
                    if start <= e.start < end:
                        out.append(copy.deepcopy(e))
                self.fetch_counts[league] = {"status": "complete", "raw_events": 0, "capped": False, "reused": True}
                continue
            page_limit = max(1, remaining // (len(leagues) - index))
            params = {"startsAfter": iso(start), "startsBefore": iso(end), "oddsAvailable": "true",
                      "type": "match", "includeAltLines": "false", "limit": min(page_limit, 100),
                      "leagueID": league}
            sport = (self._league_sport or {}).get(league, "")
            try:
                data = self._call("/events", params, "generation", sport=sport, league=league)
                if not isinstance(data, dict) or data.get("success") is False:
                    raise ProviderError("Invalid SGO events response")
            except SgoUnavailable as exc:
                self.last_fetch_complete = False
                self.fetch_counts[league] = {"status": "failed"}
                log.warning("SGO: %s - stopping this run", exc)
                break                                  # the cool-down / budget applies to every remaining league too
            except ProviderError as exc:
                if self._is_league_config_error(exc):
                    self.budget.mark_exhausted(self.name, "league access/configuration error")
                rate_limited_in_a_row = rate_limited_in_a_row + 1 if self._is_429(exc) else 0
                self.last_fetch_complete = False
                self.fetch_counts[league] = {"status": "failed"}
                continue
            rate_limited_in_a_row = 0
            batch = data.get("data", []) if isinstance(data, dict) else []
            self.budget.spend(self.name, len(batch))
            self.fetch_counts[league] = {"status": "complete", "raw_events": len(batch),
                                        "capped": bool(data.get("nextCursor"))}
            n_raw += len(batch)
            absorb(batch, league)
            if data.get("nextCursor"):
                pending.append((league, params, data["nextCursor"]))
            else:
                self._league_memo[(start.strftime("%Y-%m-%d"), league)] = (start, end, copy.deepcopy(league_buf.get(league, [])), len(batch))
        # Reuse spare allowance from empty leagues without changing cursor filters.
        while pending:
            league, params, cursor = pending.pop(0)
            if n_raw + params["limit"] > limit:
                continue
            try:
                data = self._call("/events", {**params, "cursor": cursor}, "generation",
                                  sport=(self._league_sport or {}).get(league, ""), league=league)
            except ProviderError:
                self.last_fetch_complete = False
                self.fetch_counts[league]["status"] = "partial"
                continue
            batch = data.get("data", [])
            self.budget.spend(self.name, len(batch))
            n_raw += len(batch)
            self.fetch_counts[league]["raw_events"] += len(batch)
            self.fetch_counts[league]["capped"] = bool(data.get("nextCursor"))
            more = bool(batch and data.get("nextCursor") and data["nextCursor"] != cursor)
            absorb(batch, league)
            if more:
                pending.append((league, params, data["nextCursor"]))
        if self.fetch_counts and all(v["status"] == "failed" for v in self.fetch_counts.values()):
            raise ProviderError("All configured SGO league requests failed")
        # Per-sport payload status (SUCCESS / EMPTY / PARTIAL / FAILED) so the
        # cache tracks each sport independently: one league's failure must not
        # paint another sport complete.  League id -> sport comes from the
        # provider catalog where available, else from parsed events.
        league_sport = getattr(self, '_league_sport', None) or {}
        for league, rec in self.fetch_counts.items():
            sgo_id = league_sport.get(league)
            # SGO's own sport id (SOCCER, FOOTBALL = American football, ...) -> the bot's canonical
            # name, so the status and the event counter below share one key space
            sport = (SPORT_MAP.get(str(sgo_id).upper(), 'other') if sgo_id
                     else self._parsed_sport_by_league.get(league) or 'other')
            got = rec.get('raw_events', 0) or (1 if rec.get('reused') else 0)
            origin = rec.get('status', 'failed')
            if origin == 'complete':
                state = 'success' if got else 'empty'
            elif origin == 'partial':
                state = 'partial'
            else:
                state = 'failed'
            cur = self.sport_status.setdefault(sport, {'status': state, 'events': 0, 'leagues': 0})
            cur['leagues'] += 1
            cur['status'] = _merge_sport_state(cur['status'], state)
        for e in out:
            if e.sport in self.sport_status:
                entry = self.sport_status[e.sport]
                entry['events'] += 1
                if entry['status'] == 'empty':
                    entry['status'] = 'success'
        log.info("SGO: %d events with markets (spent %d objects, %d used this month) | "
                 "rejections: %s | sport status: %s",
                 len(out), n_raw, self.budget.used_month(self.name),
                 dict(self.parse_counts), self.sport_status)
        return out

    # ------------------------------------------------------------------ parse
    def _parse_event(self, ev: Dict) -> Optional[Event]:
        teams = ev.get("teams") or {}
        home = (teams.get("home") or {}).get("names", {}).get("long") or (teams.get("home") or {}).get("names", {}).get("medium")
        away = (teams.get("away") or {}).get("names", {}).get("long") or (teams.get("away") or {}).get("names", {}).get("medium")
        start = parse_iso((ev.get("status") or {}).get("startsAt", ""))
        if not (home and away and start):
            self.parse_counts["MISSING_TEAM_OR_START"] += 1
            return None
        sport = SPORT_MAP.get(str(ev.get("sportID", "")).upper(), "other")
        players = ev.get("players") or {}
        e = Event(provider=self.name, provider_event_id=ev["eventID"], sport=sport,
                  league=str(ev.get("leagueID", "")).replace("_", " "), league_id=ev.get("leagueID", ""),
                  home=home, away=away, start=start, sources=[self.name])
        e.extra['participant_ids'] = {side: str((teams.get(side) or {}).get('teamID') or '') for side in ('home','away')}
        player_props = 0
        for odd_id, o in (ev.get("odds") or {}).items():
            ml = self._parse_odd(odd_id, o, sport, players)
            if ml:
                if ml.is_player_prop:
                    if player_props >= self.max_player_props:
                        self.parse_counts["PLAYER_PROP_LIMIT"] += 1
                        continue
                    player_props += 1
                e.markets.append(ml)
        if not e.markets:
            self.parse_counts["NO_USABLE_MARKETS"] += 1
            return None
        return e

    def _parse_odd(self, odd_id: str, o: Dict, sport: str, players: Dict) -> Optional[MarketLine]:
        if o.get("cancelled") or o.get("betTypeID") == "prop":
            self.parse_counts["CANCELLED_OR_PROP"] += 1
            return None
        if not o.get("bookOddsAvailable", True) and not o.get("bookOdds"):
            self.parse_counts["NO_BOOK_ODDS"] += 1
            return None
        stat_id = o.get("statID", "")
        if stat_id not in self.allowed_stats or stat_id not in STAT_MAP:
            self.parse_counts["UNSUPPORTED_STAT"] += 1
            return None
        if o.get("periodID", "game") not in self.allowed_periods or o.get("periodID", "game") not in PERIOD_MAP:
            self.parse_counts["UNSUPPORTED_PERIOD"] += 1
            return None
        # A missing statEntityID means "the match itself", not "a player" -
        # defaulting to "" (falsy) here made is_player=True for every odd
        # object missing this field, silently misrouting team markets.
        entity_id = o.get("statEntityID") or "all"
        period = PERIOD_MAP.get(o.get("periodID", "game"), o.get("periodID", "game"))
        if sport == "football" and period == "reg":
            period = "full"
        bet, side = o.get("betTypeID"), o.get("sideID")
        price = american_to_decimal(o.get("bookOdds"))
        by_book = o.get("byBookmaker") or {}
        book_quotes = {}
        for book, b in by_book.items():
            quoted = american_to_decimal(b.get("odds")) if isinstance(b, dict) and b.get("available") else None
            if quoted:
                book_quotes[norm_book(book)] = quoted
        book_prices = list(book_quotes.values())
        if not price and book_prices:
            price = max(book_prices)
        if not price:
            self.parse_counts["MISSING_PRICE"] += 1
            return None
        n_books = max(1, len(book_prices))
        fair = american_to_decimal(o.get("fairOdds"))
        is_player = entity_id not in ("home", "away", "all")
        entity = entity_id
        if is_player:
            entity = (players.get(entity_id) or {}).get("name") or entity_id
        stat = STAT_MAP.get(stat_id, stat_id)

        market_name = o.get("marketName", "")
        line: Optional[float] = None
        market: str
        selection: str

        if bet == "ml3way" and stat == "points" and not is_player:
            if side in ("home", "away", "draw"):
                market, selection = "result3", side
            elif side == "home+draw":
                market, selection = "dc", "home_draw"
            elif side == "away+draw":
                market, selection = "dc", "away_draw"
            elif side == "not_draw":
                market, selection = "dc", "home_away"
            else:
                self.parse_counts["UNKNOWN_SELECTION"] += 1
                return None
        elif bet == "ml" and stat == "points" and not is_player:
            # 2-way moneyline: a level score pushes. In football that is
            # exactly "draw no bet"; elsewhere it is the regular moneyline.
            market = "dnb" if sport == "football" else "result2"
            selection = side
            if selection not in ("home", "away"):
                self.parse_counts["UNKNOWN_SELECTION"] += 1
                return None
        elif bet == "sp" and not is_player:
            line = _f(o.get("bookSpread") if o.get("bookSpread") is not None else o.get("fairSpread"))
            if line is None:
                self.parse_counts["MISSING_LINE"] += 1
                return None
            if side not in ("home", "away"):
                self.parse_counts["UNKNOWN_SELECTION"] += 1
                return None
            if not _same_line(_f(o.get("fairSpread")), line):
                fair = None
            market, selection = "handicap", side
        elif bet == "ou":
            line = _f(o.get("bookOverUnder") if o.get("bookOverUnder") is not None else o.get("fairOverUnder"))
            if line is None:
                self.parse_counts["MISSING_LINE"] += 1
                return None
            if side not in ("over", "under"):
                self.parse_counts["UNKNOWN_SELECTION"] += 1
                return None
            if not _same_line(_f(o.get("fairOverUnder")), line):
                fair = None
            if is_player:
                market, selection = "prop", side
            else:
                market, selection = "total", side
        elif bet == "yn":
            if stat == "btts" and not is_player:
                market, selection, stat = "btts", side, "points"
            else:
                market, selection = "prop", side
        elif bet == "eo":
            market, selection = ("prop", side) if is_player else ("odd_even", side)
        else:
            self.parse_counts["UNKNOWN_MARKET"] += 1
            return None

        if market == "btts" or market in SIDE_MARKETS:
            entity = "all"      # SGO's statEntityID on a side market repeats the side, which is the selection
        ml = MarketLine(market=market, selection=selection, price=price, period=period, stat=stat,
                        entity=entity, line=line, fair_price=fair,
                        bookmaker="consensus", n_books=n_books, provider_odd_id=odd_id,
                        label=market_name, market_name=market_name, is_player_prop=is_player,
                        book_prices=book_prices, book_quotes=book_quotes)
        if market == "prop" and not o.get("scoringSupported", False):
            self.parse_counts["PROP_UNSETTLEABLE"] += 1
            return None   # cannot be settled -> never a candidate
        return ml

    # ---------------------------------------------------------------- results
    #: a team+time lookup buys every event in a window: queries are made per 4-hour bucket (window = bucket +- 2 h) and
    #: shared by every fixture in it, instead of one +-2 h query per fixture (~10 objects each, ~half of them identical)
    WINDOW_BUCKET_H = 4

    def _settlement_gate(self, key: str, free_alternative: bool) -> bool:
        """False = do not ask SGO now (and make no request). Reasons: cool-down / exhausted quota / budget policy, or this
        fixture was asked about recently and is not due again yet (2 h, 6 h, 12 h, then daily)."""
        if self.meter.in_cooldown():
            return False
        self.monthly_entities()
        if not self.meter.allows("settlement", self.origin, needs_sgo=not free_alternative):
            return False
        if not self.meter.settlement_due(key):
            log.debug("SGO: result for %s asked recently; backing off", key)
            return False
        return True

    def fetch_result(self, provider_event_id: str, event_hint: Optional[Dict] = None) -> Optional[MatchResult]:
        """Result of ONE known SGO event (1 object). At most once per run per event, and only when SGO is actually
        needed: the resolver asks the free sources first and says so in ``event_hint['_free_alt']``."""
        if provider_event_id in self._result_cache:
            return self._result_cache[provider_event_id]
        hint = event_hint or {}
        if self.meter.reading() is None and not self.budget.can_spend(self.name, 1, monthly_limit=self.monthly_limit):
            return None
        if not self._settlement_gate(provider_event_id, bool(hint.get("_free_alt"))):
            return None
        data = self._call("/events", {"eventID": provider_event_id, "expandResults": "true"}, "settlement",
                          event_id=provider_event_id, needs_sgo=not hint.get("_free_alt"))
        events = data.get("data", []) if isinstance(data, dict) else []
        self.budget.spend(self.name, len(events))
        result = self._parse_result(events[0]) if events else None
        self._result_cache[provider_event_id] = result
        return result

    def _window_events(self, sgo_sport: str, league_param: str, bucket: datetime) -> Optional[List[Dict]]:
        key = (sgo_sport, league_param, bucket.isoformat())
        if key in self._window_cache:
            return self._window_cache[key]
        params = {"sportID": sgo_sport, "startsAfter": iso(bucket - timedelta(hours=2)),
                  "startsBefore": iso(bucket + timedelta(hours=self.WINDOW_BUCKET_H + 2)), "type": "match",
                  "expandResults": "true", "limit": 50}
        if league_param:
            params["leagueID"] = league_param
        data = self._call("/events", params, "fallback", sport=sgo_sport, league=league_param)
        events = data.get("data", []) if isinstance(data, dict) else []
        self.budget.spend(self.name, len(events))
        self._window_cache[key] = events
        return events

    def find_result(self, sport: str, home: str, away: str, start: datetime,
                    event_hint: Optional[Dict] = None) -> Optional[MatchResult]:
        """Result by teams + kick-off. Never before the match can be over, never twice for the same fixture within the
        back-off, and one query per 4-hour window shared by every fixture in it."""
        sgo_sport = SPORT_TO_SGO.get(sport)
        if not sgo_sport:
            return None
        if utcnow() < start + timedelta(hours=DURATION_H.get(sport, 3.0)):
            return None                                   # still being played (or not started): nothing to settle
        if self.meter.reading() is None and not self.budget.can_spend(self.name, 50, monthly_limit=self.monthly_limit):
            return None
        key = f"{sport}|{home}|{away}|{start.strftime('%Y%m%d%H')}".lower()
        if not self._settlement_gate(key, bool((event_hint or {}).get("_free_alt"))):
            return None
        # The free tier rejects sport-only event queries ("specify either a
        # leagueID or an eventID"); scoping to the configured leagues keeps the
        # cross-provider team+time lookup usable instead of a guaranteed 400.
        # Leagues the subscription tier does not cover (EPL etc. on free plans)
        # make the whole query fail with a 400, so they are dropped here the
        # same way fetch_events drops them.
        league_param = ""
        if self.leagues:
            avail = self.available_leagues()
            usable = [lg for lg in self.leagues if avail is None or lg in avail]
            if not usable:
                log.info("SGO find_result: none of the configured leagues (%s) are "
                         "available at this tier; skipping the lookup", ",".join(self.leagues))
                return None
            # SGO rejects a query whose leagueID does not match sportID ("the
            # league NBA is not compatible with the provided sportIDs"), so the
            # leagues are narrowed to the queried sport when the league->sport map
            # is available.
            league_sport = getattr(self, '_league_sport', None) or {}
            if league_sport:
                matched = [lg for lg in usable if league_sport.get(lg, '').upper() == sgo_sport]
                league_param = ",".join(matched) if matched else ",".join(usable)
                sport_id = sgo_sport if matched else ""
            else:
                league_param, sport_id = ",".join(usable), sgo_sport
        else:
            sport_id = sgo_sport
        epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
        hours = int((start - epoch).total_seconds() // 3600)
        bucket = epoch + timedelta(hours=hours - hours % self.WINDOW_BUCKET_H)
        try:
            events = self._window_events(sport_id, league_param, bucket)
        except SgoUnavailable:
            return None
        except ProviderError as e:
            log.warning("SGO find_result failed: %s", e)
            return None
        self.meter.record("fallback", "/events", "ok", sport=sgo_sport, event_id=key, objects=0,
                          note="lookup")                   # marks THIS fixture as asked (the request itself is metered once)
        for ev in events or []:
            teams = ev.get("teams") or {}
            h = (teams.get("home") or {}).get("names", {}).get("long", "")
            a = (teams.get("away") or {}).get("names", {}).get("long", "")
            if same_team(h, home) and same_team(a, away):
                return self._parse_result(ev)
        return None

    @staticmethod
    def _parse_result(ev: Dict) -> MatchResult:
        st = ev.get("status") or {}
        if st.get("cancelled"):
            status = "cancelled"
        elif st.get("completed") or st.get("finalized") or st.get("ended"):
            status = "final"
        elif st.get("started"):
            status = "in_progress"
        else:
            status = "unknown"
        res = MatchResult(status=status, source="sportsgameodds")
        sport = SPORT_MAP.get(ev.get("sportID", ""), "other")

        # 1) results object: {periodID: {home: {statID: v}, away: {...}, all: {...}}}
        results = ev.get("results") or {}
        for period_id, per in results.items():
            if not isinstance(per, dict):
                continue
            p = PERIOD_MAP.get(period_id, period_id)
            if sport == "football" and p == "reg":
                p = "full"
            hs, as_ = per.get("home") or {}, per.get("away") or {}
            for stat_id, stat in STAT_MAP.items():
                if stat == "btts":
                    continue
                hv, av = _f(hs.get(stat_id)), _f(as_.get(stat_id))
                if hv is not None and av is not None:
                    if stat == "points":
                        res.scores.setdefault(p, (hv, av))
                    else:
                        res.stats.setdefault((stat, p), (hv, av))

        # 2) per-odd settled values (authoritative for the exact market)
        odds = ev.get("odds") or {}
        per_stat: Dict = {}
        for odd_id, o in odds.items():
            sc = _f(o.get("score"))
            if sc is None:
                continue
            res.odd_scores[odd_id] = sc
            p = PERIOD_MAP.get(o.get("periodID", ""), o.get("periodID", ""))
            if sport == "football" and p == "reg":
                p = "full"
            per_stat.setdefault((o.get("statID"), p), {})[o.get("statEntityID")] = sc
        for (stat_id, p), sides in per_stat.items():
            if "home" in sides and "away" in sides:
                stat = STAT_MAP.get(stat_id, stat_id)
                if stat == "points":
                    res.scores.setdefault(p, (sides["home"], sides["away"]))
                elif stat != "btts":
                    res.stats.setdefault((stat, p), (sides["home"], sides["away"]))
        # ``full`` (SGO "game") includes overtime, ``reg`` does not. With both, whether overtime changed anything
        # is known; with only ``reg``, MatchResult.score derives ``full`` only when regulation was not level.
        if "full" in res.scores and "reg" in res.scores:
            res.overtime = res.scores["full"] != res.scores["reg"]
        if status == "final" and not res.scores:
            res.status = "unknown"
        return res
