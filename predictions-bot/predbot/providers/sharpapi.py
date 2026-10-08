"""SharpAPI (https://docs.sharpapi.io) - GET /api/v1/odds rows.

Free tier: 12 req/min, DraftKings + FanDuel, 60 s delay, all sports.
SharpAPI has no results endpoint on the free tier (game state is a paid
add-on), so picks sourced here are settled through the cross-provider
results chain (SportsGameOdds) by team names + kick-off.

Feed shape (measured against the live free tier): ``/odds`` is ordered by
kick-off ascending and each match carries ~40-200 price rows, so the *earliest*
fixtures in the window are already on the first pages while season-long
outrights (``outright``, ``conference_winner``, ...) are mixed in at the front
of a league feed.  Two consequences drive this module:

  * a ``market=`` filter (``SHARPAPI_MARKETS``) drops outrights and props so the
    page budget is spent on markets the picker can actually use
  * when the feed has moved past the requested window there is nothing left for
    us, so paging stops on ``SHARPAPI_MAX_PAGES``, on
    ``SHARPAPI_EVENTS_PER_TARGET`` priced match events, or as soon as a page
    starts after the window ends.
"""
from __future__ import annotations

import logging
import re
import time
from collections import defaultdict, Counter
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from predbot.markets.catalog import pretty_league
from predbot.providers.base import BaseProvider, Event, MarketLine, apply_price_mode, norm_book
from predbot.providers.budget import Budget
from predbot.util.http import Http, ProviderError, RateLimiter
from predbot.util.names import is_doubles
from predbot.util.odds import parse_decimal
from predbot.util.timeutil import parse_iso

log = logging.getLogger(__name__)

SPORT_MAP = {
    "soccer": "football", "football": "american_football", "basketball": "basketball",
    "baseball": "baseball", "tennis": "tennis", "mma": "mma",
}

#: SharpAPI league ids that are NOT soccer: bare ids (``nba``) or
#: ``<country>_-_<competition>`` slugs (``usa_-_basketball``).
LEAGUE_SPORTS = {
    "mlb": "baseball", "ncaab": "basketball", "nba": "basketball", "wnba": "basketball",
    "ncaaf": "american_football", "nfl": "american_football",
    "atp": "tennis", "wta": "tennis", "itf_men": "tennis", "itf_women": "tennis",
    "euroleague": "basketball", "ufc": "mma", "pga": "golf", "nascar": "motorsports", "formula_1": "motorsports",
    "indycar": "motorsports", "dota_2": "esports", "league_of_legends": "esports",
    "cs2": "esports", "valorant": "esports", "politics": "politics",
}

#: Competition names that identify a soccer league slug.  SharpAPI writes soccer
#: leagues as ``<country>_-_<competition>`` (``spain_-_la_liga``), but the shape
#: alone is not enough: ``table_tennis_-_tt_cup`` looks the same.
SOCCER_HINTS = ("soccer", "futbol", "premier_league", "la_liga", "serie_a", "serie_b",
                "bundesliga", "ligue_1", "ligue_2", "eredivisie", "primeira_liga",
                "super_lig", "superliga", "champions_league", "europa_league",
                "conference_league", "nations_league", "major_league_soccer", "mls",
                "copa", "libertadores", "championship", "league_one", "league_two", "friendl", "nations",
                "j1_league", "k_league", "a_league", "allsvenskan", "eliteserien",
                "veikkausliiga", "premiership", "professional_league", "liga", "league")

#: Slugs that contain a soccer hint word but belong to another sport.
NON_SOCCER_PREFIXES = ("table_tennis", "esports", "league_of_legends", "basketball", "baseball", "hockey",
                       "tennis", "volleyball", "handball", "cricket", "darts", "rugby")

_SPORT_ALIASES = {"soccer": "football", "football": "american_football"}

#: ``market=`` values kept from the raw feed: everything the picker can price,
#: without the season outrights that dominate an unfiltered page.
DEFAULT_MARKETS = ("moneyline", "spread", "total")


def discover_sport_catalog(http, db, ttl=None) -> Dict:
    """Live sport catalog for this key (GET /sports), cached in SQLite.

    Capabilities come from the provider itself, never a hardcoded list: soccer
    through esports, cricket, table tennis, darts - whatever this key reaches.
    Sports without a canonical mapping are reported as
    ``DISCOVERED_UNSUPPORTED_SPORT`` instead of silently dropped.
    """
    from predbot.providers import capabilities
    cached = capabilities.load_cached(db, SharpApiProvider.name)
    if cached:
        return cached
    payload: Dict = {"provider": SharpApiProvider.name, "sports": [], "unsupported": {}}
    try:
        data = http.get("/sports")
    except ProviderError as e:
        log.warning("SharpAPI: sport discovery failed (%s); using configured leagues only", e)
        return capabilities.store(db, SharpApiProvider.name, {
            "provider": SharpApiProvider.name, "sports": [], "unsupported": {}, "error": str(e)},
            ttl=timedelta(minutes=30) if ttl is None else ttl)
    rows = data.get("data", []) if isinstance(data, dict) else []
    for row in rows:
        if not isinstance(row, dict) or not row.get("id"):
            continue
        sport = sport_for_filter(row["id"])
        entry = {"key": row["id"], "sport": sport,
                 "events": int(row.get("event_count") or 0),
                 "live_events": int(row.get("live_count") or 0),
                 "leagues": list(row.get("leagues") or [])}
        payload["sports"].append(entry)
        if sport == "other" and entry["events"]:
            payload["unsupported"][row["id"]] = entry["events"]
    return capabilities.store(db, SharpApiProvider.name, payload, ttl=ttl)


def sport_catalog_supported(payload: Dict) -> List[Dict]:
    """Catalog entries that map to a canonical sport with reachable events."""
    return [s for s in payload.get("sports", []) if s.get("sport") != "other" and s.get("events")]


def sport_for_league(slug: str) -> str:
    """The sport a SharpAPI league id belongs to (``spain_-_la_liga`` -> football).

    The provider cache uses this to say which sports a SharpAPI configuration is
    *supposed* to cover, so a silent soccer gap is reported instead of hidden.
    """
    key = re.sub(r"[^a-z0-9]+", "_", str(slug or "").lower()).strip("_")
    if key in LEAGUE_SPORTS:
        return LEAGUE_SPORTS[key]
    if key.startswith(NON_SOCCER_PREFIXES):
        return "other"
    if any(hint in key for hint in SOCCER_HINTS):
        return "football"
    return "other"


def sport_for_filter(value: str) -> str:
    """The bot's sport name for a SharpAPI ``sport=`` filter value.

    Unmapped filters (``darts``, ``cricket``, ``table_tennis``, ...) map to
    ``other`` so ``auto`` discovery reports them as discovered-unsupported
    instead of silently fetching feeds the model cannot price.
    """
    key = str(value or "").strip().lower()
    if not key:
        return "other"
    return _SPORT_ALIASES.get(key, SPORT_MAP.get(key, "other"))


SEGMENT_PERIOD = {
    "full_game": "full", "1st_half": "1h", "2nd_half": "2h", "1st_quarter": "1q", "2nd_quarter": "2q",
    "3rd_quarter": "3q", "4th_quarter": "4q",
    "1st_set": "1s", "2nd_set": "2s", "first_5_innings": "1i5", "1st_5_innings": "1i5",
}
_PERIOD_PREFIX = re.compile(r"^(1st_half|2nd_half|1st_quarter|2nd_quarter|3rd_quarter|4th_quarter|1st_set|2nd_set|first_5_innings|1st_5_innings)_")


class SharpApiProvider(BaseProvider):
    name = "sharpapi"

    def __init__(self, api_key: str, budget: Budget, leagues: List[str], min_interval: float = 5.5,
                 max_pages: int = 12, markets: Optional[List[str]] = None,
                 sports: Optional[List[str]] = None, events_per_target: int = 40,
                 price_mode: str = "median", main_lines_only: bool = False,
                 sport_markets: Optional[Dict[str, List[str]]] = None, monthly_limit: Optional[int] = None,
                 max_requests_per_generation: Optional[int] = None, league_priority: Optional[List[str]] = None):
        self.key = api_key
        #: ``is_main_line=true`` on every /odds request: alternate lines are ~70% of a match's rows
        self.main_lines_only = main_lines_only
        #: exact ``market=`` names per sport (they exclude the shots/fouls/corners/player rows that the
        #: ``spread``/``total`` aliases drag in); sports without an entry use ``markets``
        self.sport_markets = {k: list(v) for k, v in (sport_markets or {}).items() if v}
        #: SharpAPI documents no monthly quota (free tier: 12 requests/minute only); this is a
        #: self-imposed monthly budget, spread over the days left by Budget.monthly_daily_allowance
        self.monthly_limit = monthly_limit
        self.max_requests_per_generation = max_requests_per_generation
        self.league_priority = [x for x in (league_priority or []) if x]
        self.price_mode = price_mode
        self.budget = budget
        self.leagues = leagues
        self.markets = list(markets) if markets else list(DEFAULT_MARKETS)
        self.sports = sports or []
        self.max_pages = max(1, max_pages)
        self.events_per_target = max(0, events_per_target)
        self.db = getattr(budget, "db", None)      # capability cache storage
        # retries=0, matching the other providers' discipline: a retried request is
        # still a real upstream call, and budget.spend() is only charged once
        # per logical fetch here - a silent retry would understate real usage.
        # sleep_on_429=False: a 429 is handled by fetch_events (stop the walk, cool down), never slept through
        self.http = Http("https://api.sharpapi.io/api/v1", headers={"X-API-Key": api_key},
                         rate=RateLimiter(min_interval), retries=0, sleep_on_429=False)
        self.sport_status: Dict[str, Dict] = {}    # per-canonical-sport fetch outcome
        self.skipped_no_results: List[str] = []    # configured leagues with no results path (not fetched)
        self.parse_counts: Counter = Counter()     # row -> MarketLine rejection reasons

    def enabled(self) -> bool:
        return bool(self.key)

    def sport_catalog(self) -> Dict:
        """What this key can actually reach: GET /sports, cached in SQLite."""
        return discover_sport_catalog(self.http, self.db)

    def _targets(self) -> List[Dict[str, str]]:
        """One filtered feed per configured league (``SHARPAPI_LEAGUES``).

        Leagues are explicit: there is no ``auto`` expansion. A sport-level feed
        (``SHARPAPI_SPORTS``) is still honoured if configured, but it pulls every
        league of that sport through the page budget, so it is warned about."""
        # leagues nothing can settle are not fetched (chain.build_providers fills skipped_no_results)
        ordered = [lg for lg in self.league_priority if lg in self.leagues] + \
                  [lg for lg in self.leagues if lg not in self.league_priority]
        targets = [{"league": lg} for lg in ordered if lg not in self.skipped_no_results]
        sports = [sp for sp in self.sports if sp != "auto"]
        if "auto" in self.sports:
            log.warning("SharpAPI: SHARPAPI_SPORTS=auto is no longer supported - list leagues in SHARPAPI_LEAGUES")
        if sports:
            log.warning("SharpAPI: sport-level feed(s) %s configured; prefer explicit SHARPAPI_LEAGUES "
                        "(a sport feed spends the page budget on every league)", ", ".join(sports))
        targets += [{"sport": sp} for sp in sports]
        if not targets and not self.leagues and not sports:
            return [{}]                       # nothing configured at all: the unfiltered feed
        return targets

    @staticmethod
    def _target_sport(target: Dict[str, str]) -> str:
        if target.get("league"):
            return sport_for_league(target["league"])
        if target.get("sport"):
            return sport_for_filter(target["sport"])
        return "other"

    def request_budget(self) -> Optional[int]:
        """Requests this generation may spend: the daily share of ``monthly_limit`` (Budget keeps
        10% back) capped by ``max_requests_per_generation``; None = unbudgeted."""
        cap = self.max_requests_per_generation
        if self.monthly_limit:
            allowance = self.budget.monthly_daily_allowance(self.name, self.monthly_limit, reserve_frac=0.10)
            cap = allowance if cap is None else min(cap, allowance)
        return None if cap is None else max(0, int(cap))

    def _params(self, target: Dict[str, str]) -> Dict:
        params: Dict = {"is_live": "false", "limit": 200}
        params.update(target)
        markets = self.sport_markets.get(self._target_sport(target)) or self.markets
        if markets:
            params["market"] = ",".join(markets)
        if self.main_lines_only:
            params["is_main_line"] = "true"
        return params

    #: after a 429 SharpAPI is left alone at least this long (longer when its Retry-After says so), so the next
    #: scheduled attempt - not the same walk - asks again; persisted, so a restart does not hammer it either
    COOLDOWN_SECONDS = 120.0
    COOLDOWN_KEY = "sharpapi:cooldown_until"

    def _cooldown_left(self) -> float:
        until = getattr(self, "_cooldown_until", 0.0)
        if self.db is not None:
            try:
                until = max(until, float(self.db.get_kv(self.COOLDOWN_KEY) or 0))
            except (TypeError, ValueError):
                pass
        return max(0.0, until - time.time())

    def _start_cooldown(self, retry_after: Optional[float]) -> None:
        seconds = max(self.COOLDOWN_SECONDS, float(retry_after or 0))
        log.warning("SharpAPI: rate limited - stopping this walk and pausing SharpAPI for %.0fs "
                    "(data already fetched is kept)", seconds)
        self._cooldown_until = time.time() + seconds
        if self.db is not None:
            self.db.set_kv(self.COOLDOWN_KEY, str(self._cooldown_until))

    def fetch_events(self, start: datetime, end: datetime) -> List[Event]:
        self.last_fetch_complete = True
        self.fetch_counts = {}
        self.parse_counts = Counter()
        self.sport_status = {}
        target_status: Dict[str, set] = defaultdict(set)
        feeds: Counter = Counter()            # how many targets walked per sport
        rows: List[Dict] = []
        targets = self._targets()
        budget_left = self.request_budget()
        self.requests_budget = budget_left
        self.requests_spent = 0
        rate_limited = self._cooldown_left() > 0
        if rate_limited:
            log.warning("SharpAPI: cooling down after a rate limit for %.0fs more - no request this run",
                        self._cooldown_left())
        for index, target in enumerate(targets):
            label = target.get("league") or target.get("sport") or "all"
            sport = self._target_sport(target)
            feeds[sport] += 1
            # every feed still to come is owed one page, so a hungry feed cannot starve the rest
            ceiling = self.max_pages
            if budget_left is not None:
                owed = len(targets) - index - 1
                ceiling = min(ceiling, max(1 if budget_left >= 1 else 0, budget_left - owed))
            counts = {"requests": 0, "rows": 0, "matches_seen": 0, "matches_in_window": 0, "rows_per_match": 0.0,
                      "capped": False, "stop_reason": ""}
            self.fetch_counts[label] = counts
            if ceiling < 1:                   # checked BEFORE any request is made
                counts.update(capped=True, stop_reason="budget")
                self.last_fetch_complete = False
                target_status[sport].add("failed")
                continue
            if rate_limited:                  # one 429 stops the whole walk: no request per remaining league
                counts.update(capped=True, stop_reason="rate_limited")
                self.last_fetch_complete = False
                target_status[sport].add("failed")
                continue
            cursor = None
            pages = 0
            seen: set = set()
            in_window_ids: set = set()
            stop = "page_ceiling"
            while pages < ceiling:
                params = self._params(target)
                if cursor:
                    params["cursor"] = cursor
                try:
                    data = self.http.get("/odds", params)
                except ProviderError as e:
                    self.last_fetch_complete = False
                    target_status[sport].add("failed")
                    if getattr(e, "status", None) == 429:
                        rate_limited = True
                        self._start_cooldown(getattr(e, "retry_after", None))
                        stop = "rate_limited"
                        break
                    log.warning("SharpAPI fetch failed (%s): %s", label, e)
                    stop = "error"
                    break
                self.budget.spend(self.name, 1)
                pages += 1
                self.requests_spent += 1
                if budget_left is not None:
                    budget_left -= 1
                batch = data.get("data", [])
                rows.extend(batch)
                # Matches only: season outrights arrive without an away team.
                seen_starts, page_window = [], set()
                for r in batch:
                    if not r.get("away_team"):
                        continue
                    seen.add(str(r.get("event_id")))
                    st = parse_iso(r.get("event_start_time", ""))
                    if st:
                        seen_starts.append(st)
                        if start <= st < end:
                            page_window.add(str(r.get("event_id")))
                in_window_ids |= page_window
                pag = data.get("pagination") or {}
                counts["rows"] += len(batch)
                counts.update(requests=pages, matches_seen=len(seen), matches_in_window=len(in_window_ids),
                              rows_per_match=round(counts["rows"] / len(seen), 1) if seen else 0.0,
                              capped=bool(pag.get("has_more")))
                # legacy keys (tests, diagnostics)
                counts.update(pages=pages, match_events=len(seen), in_window=len(in_window_ids))
                if not pag.get("has_more") or not pag.get("next_cursor"):
                    stop = "no_more_data"
                    break
                # Kick-off ordered feed: once a page reaches past the window end every later
                # page starts after it, so there is nothing left for us.
                if seen_starts and max(seen_starts) > end:
                    stop = "window_end"
                    break
                if self.events_per_target and len(seen) >= self.events_per_target:
                    stop = "events_per_target"
                    break
                cursor = pag["next_cursor"]
            else:
                # page budget exhausted mid-walk: this sport's feed is truncated
                stop = "max_pages" if ceiling >= self.max_pages else "budget"
                target_status[sport].add("failed")
                self.last_fetch_complete = False
            counts["stop_reason"] = stop
            if stop in ("window_end", "events_per_target"):
                counts["capped"] = False                  # stopped on purpose: nothing we need is missing
            if pages:
                target_status[sport].add("fetched")
        events = self._rows_to_events(rows, start, end)
        sport_events = Counter(e.sport for e in events)
        # A sport is FAILED when every feed we walked for it failed, PARTIAL when
        # at least one feed failed but another answered, EMPTY when all feeds
        # answered with nothing in-window, SUCCESS otherwise.  Feeds we never
        # walked for a sport do not put that sport in the map.
        for sport, outcomes in target_status.items():
            if "failed" in outcomes and "fetched" not in outcomes:
                state = "failed"
            elif "failed" in outcomes:
                state = "partial"
            elif not sport_events.get(sport):
                state = "empty"
            else:
                state = "success"
            self.sport_status[sport] = {"status": state, "events": sport_events.get(sport, 0),
                                        "feeds": feeds[sport]}
        log.info("SharpAPI: %d rows -> %d events in window | parse rejections: %s | sport status: %s",
                 len(rows), len(events), dict(self.parse_counts), self.sport_status)
        return events

    # ------------------------------------------------------------------ parse
    def _rows_to_events(self, rows: List[Dict], start: datetime, end: datetime) -> List[Event]:
        counts = Counter()
        by_event: Dict[str, List[Dict]] = defaultdict(list)
        for r in rows:
            if r.get("is_active") is False or r.get("is_live"):
                continue
            by_event[str(r.get("event_id"))].append(r)
        events: List[Event] = []
        for eid, ers in by_event.items():
            r0 = ers[0]
            st = parse_iso(r0.get("event_start_time", ""))
            sport = SPORT_MAP.get(str(r0.get("sport", "")).lower(), "other")
            counts[sport + ":raw_events"] += 1
            if not st or not (start <= st < end):
                counts[sport + ":outside_window"] += 1
                continue
            if sport == "tennis" and is_doubles(r0.get("home_team", ""), r0.get("away_team", "")):
                counts[sport + ":doubles"] += 1
                self.parse_counts["DOUBLES_UNSUPPORTED"] += 1       # ESPN results are singles only: it could never settle
                continue
            counts[sport + ":in_window"] += 1
            e = Event(provider=self.name, provider_event_id=eid, sport=sport,
                      league=pretty_league(str(r0.get("league", ""))), league_id=str(r0.get("league", "")),
                      home=r0.get("home_team", ""), away=r0.get("away_team", ""), start=st, sources=[self.name])
            # consolidate the same selection across books -> best price + n_books
            merged: Dict[Tuple, MarketLine] = {}
            for r in ers:
                ml = self._row_to_line(r, sport)
                if not ml:
                    continue
                k = ml.key()
                if k in merged:
                    m = merged[k]
                    m.book_prices.append(ml.price)
                    m.n_books = len(m.book_prices)
                    for book, quoted in ml.book_quotes.items():
                        m.book_quotes.setdefault(book, quoted)
                    if ml.price > m.price:
                        m.price, m.bookmaker, m.provider_odd_id = ml.price, ml.bookmaker, ml.provider_odd_id
                else:
                    merged[k] = ml
            for m in merged.values():
                apply_price_mode(m, self.price_mode)
            e.markets = list(merged.values())
            if e.markets and e.home and e.away:
                counts[sport + ":normalized_events"] += 1
                events.append(e)
        self.normalization_counts = dict(counts)
        log.info("SharpAPI normalization: %s", self.normalization_counts)
        return events

    def _row_to_line(self, r: Dict, sport: str) -> Optional[MarketLine]:
        price = parse_decimal(r.get("odds_decimal"))
        if not price:
            self.parse_counts["MISSING_PRICE"] += 1
            return None
        mtype = str(r.get("market_type", "")).lower()
        stype = str(r.get("selection_type", "")).lower()
        line = r.get("line")
        try:
            line = float(line) if line is not None else None
        except (TypeError, ValueError):
            line = None
        period = SEGMENT_PERIOD.get(str(r.get("market_segment", "full_game")).lower(), "full")
        m = _PERIOD_PREFIX.match(mtype)
        if m:
            period = SEGMENT_PERIOD.get(m.group(1), period)
            mtype = mtype[m.end():]
        if r.get("is_player_prop") or mtype.startswith("player_"):
            self.parse_counts["PLAYER_PROP_UNSETTLEABLE"] += 1
            return None  # unsettleable from this provider's free tier
        # Alternate lines (is_alternate_line=True) are fine as candidates and
        # need no special handling here; they fall through to the normal
        # market parsing below like any other line.

        market: Optional[str] = None
        selection = stype
        stat, entity = "points", "all"

        if mtype in ("moneyline", "moneyline_3-way", "moneyline_3way", "3way_moneyline", "match_winner"):
            if stype not in ("home", "away", "draw"):
                return None
            # Soccer moneylines at US books are 3-way (a draw loses); every other
            # sport's moneyline is 2-way including overtime.
            market = "result3" if (sport == "football" or stype == "draw" or "3" in mtype) else "result2"
        elif mtype == "draw_no_bet":
            market = "dnb"
        elif mtype == "double_chance":
            market = "dc"
            selection = {"home_draw": "home_draw", "away_draw": "away_draw", "home_away": "home_away"}.get(stype)
            if not selection:
                return None
        elif mtype in ("both_teams_to_score", "btts"):
            market = "btts"
        elif "spread" in mtype or "handicap" in mtype or mtype in ("run_line", "puck_line"):
            if stype not in ("home", "away") or line is None:
                return None
            market = "handicap"
            if "corner" in mtype:
                stat = "corners"
            elif "card" in mtype:
                stat = "cards"
            elif "game" in mtype or "set" in mtype:
                stat = "games" if "game" in mtype else "sets"
        elif mtype in ("team_total", "team_total_goals"):
            if line is None:
                return None
            market = "total"
            side = r.get("team_side") or stype.split("_", 1)[0]
            ou = stype.split("_", 1)[1] if "_" in stype else stype
            if side not in ("home", "away") or ou not in ("over", "under"):
                return None
            entity, selection = side, ou
        elif mtype in ("match_result_total_goals", "result_total"):
            # Must be checked before the generic "total" in mtype fallback
            # below: "match_result_total_goals" contains the substring
            # "total", so that fallback would otherwise catch it first and
            # misfile this result+total combo market (selections like
            # "home_over") as a plain full-match Over/Under line, merging its
            # combo-market price into the real total's book_prices.
            if line is None or "_" not in stype:
                return None
            market = "result_total"
        elif "total" in mtype:
            if stype not in ("over", "under") or line is None:
                return None
            market = "total"
            if "corner" in mtype:
                stat = "corners"
            elif "card" in mtype:
                stat = "cards"
            elif "game" in mtype:
                stat = "games"
            elif "set" in mtype:
                stat = "sets"
            elif "round" in mtype:
                stat = "rounds"
        elif mtype in ("match_result_both_teams_to_score", "result_btts"):
            if "_" not in stype:
                return None
            market = "result_btts"
        elif mtype == "correct_score":
            if not re.match(r"^\d+_\d+$", stype):
                return None
            market, selection = "correct_score", stype.replace("_", "-")
        elif mtype in ("halftime_fulltime", "half_time_full_time", "ht_ft"):
            parts = stype.split("_")
            if len(parts) != 2 or not all(p in ("home", "draw", "away") for p in parts):
                return None
            market, selection = "ht_ft", "/".join(parts)
        elif "odd_even" in mtype or mtype.endswith("_parity"):
            if stype not in ("odd", "even"):
                return None
            market = "odd_even"
        elif mtype in ("clean_sheet", "win_to_nil", "team_to_score"):
            side = r.get("team_side")
            if side not in ("home", "away") or stype not in ("yes", "no"):
                return None
            market, entity = mtype, side
        elif mtype == "winning_margin":
            mm = re.match(r"^(home|away)_(\d+)(?:_(\d+|plus))?$", stype)
            if not mm:
                return None
            band = mm.group(2) + ("+" if mm.group(3) == "plus" else f"-{mm.group(3)}" if mm.group(3) else "")
            market, selection = "winning_margin", f"{mm.group(1)}_{band}"
        else:
            return None

        prob = r.get("odds_probability")
        try:
            prob = float(prob) if prob is not None else None
        except (TypeError, ValueError):
            prob = None

        return MarketLine(market=market, selection=selection, price=price, period=period, stat=stat,
                          entity=entity, line=line, bookmaker=str(r.get("sportsbook", "")), n_books=1,
                          provider_odd_id=str(r.get("id", "")), market_name=mtype.replace("_", " ").title(),
                          book_prices=[price], provider_prob=prob,
                          book_quotes={norm_book(r.get("sportsbook", "")): price} if r.get("sportsbook") else {})
