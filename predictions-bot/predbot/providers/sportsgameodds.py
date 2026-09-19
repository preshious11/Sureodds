"""SportsGameOdds v2 (https://sportsgameodds.com/docs).

One /v2/events call returns fixtures + every market (consensus fair/book odds,
per-book prices) + results/settled stat values, so it doubles as our most
complete results source. Billing is per *event object*, tracked in Budget.
"""
from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from predbot.providers.base import BaseProvider, Event, MarketLine, MatchResult
from predbot.providers.budget import Budget
from predbot.util.http import Http, ProviderError, RateLimiter
from predbot.util.names import same_team
from predbot.util.odds import american_to_decimal
from predbot.util.timeutil import iso, parse_iso

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


def _merge_sport_state(current: str, state: str) -> str:
    """Combine per-league states into one per-sport state.  A FAILED league fails
    the whole sport; else PARTIAL wins; else the sport is EMPTY until any event
    actually parses (callers upgrade to SUCCESS when events > 0)."""
    if state == "failed" or current == "failed":
        return "failed"
    if state == "partial" or current == "partial":
        return "partial"
    return "empty"


class SportsGameOddsProvider(BaseProvider):
    name = "sportsgameodds"

    def __init__(self, api_key: str, budget: Budget, sports: List[str], leagues: List[str],
                 event_limit: int = 25, monthly_limit: int = 1000,
                 allowed_stats: Optional[List[str]] = None, allowed_periods: Optional[List[str]] = None,
                 max_player_props: int = 0):
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
        self.monthly_limit = monthly_limit
        self._available_leagues: Optional[set] = None
        self._league_sport: Dict[str, str] = {}          # leagueID -> SPORT (SGO id)
        self._parsed_sport_by_league: Dict[str, str] = {}  # leagueID -> canonical sport seen in parsed events
        self.sport_status: Dict[str, Dict] = {}          # canonical sport -> status/events/leagues
        self.skipped_no_results: List[str] = []          # configured leagues with no results path (not fetched)
        self.parse_counts: Counter = Counter()           # rejection reasons
        self.http = Http("https://api.sportsgameodds.com/v2", headers={"X-Api-Key": api_key},
                         rate=RateLimiter(1.0))

    def enabled(self) -> bool:
        return bool(self.key)

    def available_leagues(self) -> Optional[set]:
        """LeagueIDs available to THIS key at its subscription tier (cached).

        GET /leagues is a cheap metadata call (it does not bill event objects).
        Returns None when the call fails - callers then keep their configured list.
        Also caches the league -> sport mapping used to keep event queries
        sport-consistent (SGO 400s when a leagueID does not match sportID).
        """
        if self._available_leagues is not None:
            return self._available_leagues
        try:
            data = self.http.get("/leagues")
        except ProviderError as e:
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
        return ids

    # ------------------------------------------------------------------ fetch
    def fetch_events(self, start: datetime, end: datetime) -> List[Event]:
        self.last_fetch_complete = True
        self.fetch_counts = {}
        self.parse_counts = Counter()
        self.sport_status = {}
        self._parsed_sport_by_league = {}
        allowance = self.budget.monthly_daily_allowance(self.name, self.monthly_limit)
        limit = max(0, min(self.event_limit, allowance))
        if limit <= 0:
            self.last_fetch_complete = False
            log.warning("SGO: no object allowance left today (monthly limit %d)", self.monthly_limit)
            return []
        if self.budget.is_exhausted(self.name):
            self.last_fetch_complete = False
            return []                              # tier/quota problem already seen today
        # Auto-discover leagues when none are configured: use ALL leagues
        # available to this key (paid tiers get every sport; free tiers
        # get whatever the /leagues endpoint returns).
        leagues = list(self.leagues) if self.leagues else None
        if not leagues:
            avail = self.available_leagues()
            if avail is not None and avail:
                leagues = sorted(avail)
                log.info("SGO: auto-discovered %d leagues from provider catalog", len(leagues))
            else:
                self.last_fetch_complete = False
                self.budget.mark_exhausted(self.name, "no leagues available for this key")
                log.warning("SGO: no leagues configured and auto-discovery returned empty - "
                            "check API key access or set SGO_LEAGUES")
                return []
        else:
            # When leagues are explicitly configured, drop any not covered by this
            # key's tier (e.g. EPL on a free plan).  This requires the /leagues
            # catalog, which is a cheap metadata call (not billed per event object).
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
            except Exception:  # noqa: BLE001
                pass
        avail_map = getattr(self, '_league_sport', None) or {}
        if avail_map and self.sports:
            sports_upper = {s.upper() for s in self.sports}
            mapped_leagues = [lg for lg in leagues if avail_map.get(lg, '')]
            if mapped_leagues:
                leagues = [lg for lg in leagues if avail_map.get(lg, '').upper() in sports_upper]
                log.info("SGO: filtered to %d leagues matching configured sports %s",
                         len(leagues), self.sports)
        events = []
        pending = []
        self.fetch_counts = {}
        # Share the existing object allowance across configured leagues so the
        # first sport in a multi-league response cannot consume the whole page.
        for index, league in enumerate(leagues):
            remaining = limit - len(events)
            if remaining <= 0:
                self.last_fetch_complete = False
                break
            page_limit = max(1, remaining // (len(leagues) - index))
            params = {"startsAfter": iso(start), "startsBefore": iso(end), "oddsAvailable": "true",
                      "type": "match", "includeAltLines": "false", "limit": min(page_limit, 100),
                      "leagueID": league}
            try:
                data = self.http.get("/events", params)
                if not isinstance(data, dict) or data.get("success") is False:
                    raise ProviderError("Invalid SGO events response")
            except ProviderError as exc:
                if "leagueID" in str(exc):
                    self.budget.mark_exhausted(self.name, "league access/configuration error")
                self.last_fetch_complete = False
                self.fetch_counts[league] = {"status": "failed"}
                continue
            batch = data.get("data", []) if isinstance(data, dict) else []
            self.budget.spend(self.name, len(batch))
            self.fetch_counts[league] = {"status": "complete", "raw_events": len(batch),
                                        "capped": bool(data.get("nextCursor"))}
            events.extend(batch)
            if data.get("nextCursor"):
                pending.append((league, params, data["nextCursor"]))
        # Reuse spare allowance from empty leagues without changing cursor filters.
        while pending:
            league, params, cursor = pending.pop(0)
            if len(events) + params["limit"] > limit:
                continue
            try:
                data = self.http.get("/events", {**params, "cursor": cursor})
            except ProviderError:
                self.last_fetch_complete = False
                self.fetch_counts[league]["status"] = "partial"
                continue
            batch = data.get("data", [])
            self.budget.spend(self.name, len(batch))
            events.extend(batch)
            self.fetch_counts[league]["raw_events"] += len(batch)
            self.fetch_counts[league]["capped"] = bool(data.get("nextCursor"))
            if batch and data.get("nextCursor") and data["nextCursor"] != cursor:
                pending.append((league, params, data["nextCursor"]))
        if self.fetch_counts and all(v["status"] == "failed" for v in self.fetch_counts.values()):
            raise ProviderError("All configured SGO league requests failed")
        out: List[Event] = []
        for ev in events:
            try:
                e = self._parse_event(ev)
                if e and e.markets and start <= e.start < end:
                    out.append(e)
                    self._parsed_sport_by_league[str(ev.get("leagueID", ""))] = e.sport
            except Exception as ex:  # noqa: BLE001
                log.debug("SGO parse error %s: %s", ev.get("eventID"), ex)
        # Per-sport payload status (SUCCESS / EMPTY / PARTIAL / FAILED) so the
        # cache tracks each sport independently: one league's failure must not
        # paint another sport complete.  League id -> sport comes from the
        # provider catalog where available, else from parsed events.
        league_sport = getattr(self, '_league_sport', None) or {}
        for league, rec in self.fetch_counts.items():
            sport = league_sport.get(league) or self._parsed_sport_by_league.get(league) or 'other'
            got = rec.get('raw_events', 0)
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
                 len(out), len(events), self.budget.used_month(self.name),
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
        if stat_id not in self.allowed_stats:
            self.parse_counts["UNSUPPORTED_STAT"] += 1
            return None
        if o.get("periodID", "game") not in self.allowed_periods:
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
        book_prices = [american_to_decimal(b.get("odds")) for b in by_book.values() if b.get("available")]
        book_prices = [p for p in book_prices if p]
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
            if _f(o.get("fairSpread")) != line:
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
            if _f(o.get("fairOverUnder")) != line:
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

        ml = MarketLine(market=market, selection=selection, price=price, period=period, stat=stat,
                        entity=entity if market != "btts" else "all", line=line, fair_price=fair,
                        bookmaker="consensus", n_books=n_books, provider_odd_id=odd_id,
                        label=market_name, market_name=market_name, is_player_prop=is_player,
                        book_prices=book_prices)
        if market == "prop" and not o.get("scoringSupported", False):
            self.parse_counts["PROP_UNSETTLEABLE"] += 1
            return None   # cannot be settled -> never a candidate
        return ml

    # ---------------------------------------------------------------- results
    def fetch_result(self, provider_event_id: str, event_hint: Optional[Dict] = None) -> Optional[MatchResult]:
        if not self.budget.can_spend(self.name, 1, monthly_limit=self.monthly_limit):
            return None
        data = self.http.get("/events", {"eventID": provider_event_id, "expandResults": "true"})
        events = data.get("data", []) if isinstance(data, dict) else []
        self.budget.spend(self.name, len(events))
        if not events:
            return None
        return self._parse_result(events[0])

    def find_result(self, sport: str, home: str, away: str, start: datetime) -> Optional[MatchResult]:
        sgo_sport = SPORT_TO_SGO.get(sport)
        if not sgo_sport:
            return None
        query_limit = 50
        if not self.budget.can_spend(self.name, query_limit, monthly_limit=self.monthly_limit):
            return None
        params = {"sportID": sgo_sport, "startsAfter": iso(start - timedelta(hours=2)),
                  "startsBefore": iso(start + timedelta(hours=2)), "type": "match",
                  "expandResults": "true", "limit": query_limit}
        # The free tier rejects sport-only event queries ("specify either a
        # leagueID or an eventID"); scoping to the configured leagues keeps the
        # cross-provider team+time lookup usable instead of a guaranteed 400.
        # Leagues the subscription tier does not cover (EPL etc. on free plans)
        # make the whole query fail with a 400, so they are dropped here the
        # same way fetch_events drops them - otherwise every settlement lookup
        # for a single out-of-tier league would return nothing at all.
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
            # is available.  When unknown, keep sportID and all usable leagues -
            # SGO accepts a sportID+leagueID query when they match.
            league_sport = getattr(self, '_league_sport', None) or {}
            if league_sport:
                matched = [lg for lg in usable if league_sport.get(lg, '').upper() == sgo_sport]
                if matched:
                    params["leagueID"] = ",".join(matched)
                else:
                    params["leagueID"] = ",".join(usable)
                    params.pop("sportID", None)
            else:
                params["leagueID"] = ",".join(usable)
        try:
            data = self.http.get("/events", params)
        except ProviderError as e:
            log.warning("SGO find_result failed: %s", e)
            return None
        events = data.get("data", []) if isinstance(data, dict) else []
        self.budget.spend(self.name, len(events))
        for ev in events:
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
        if "full" not in res.scores and "reg" in res.scores:
            res.scores["full"] = res.scores["reg"]
        if status == "final" and not res.scores:
            res.status = "unknown"
        return res
