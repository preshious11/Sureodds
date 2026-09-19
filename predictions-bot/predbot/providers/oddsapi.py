"""Odds API (the-odds-api.com) provider.

Fetches real-time and historical odds for football (soccer) and tennis only.
Converts the Odds API shape into the existing MarketLine prediction format
so the rest of the bot (picker, settlement, UI, cache) stays untouched.

API shape: https://the-odds-api.com/liveapi/guides/v4/

Quota (free tier ~= 500 requests / month, 1 req / sec):
  - /v4/sports                   once per startup (sport list + ids)
  - /v4/sports/{id}/odds         per sport per day window, paginated by bookmaker
  - /v4/sports/{id}/events       for historical results
"""
from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from predbot.providers.base import BaseProvider, Event, MarketLine, MatchResult, apply_price_mode
from predbot.providers.budget import Budget
from predbot.util.http import Http, ProviderError, RateLimiter
from predbot.util.names import is_doubles, same_team
from predbot.util.odds import parse_decimal
from predbot.util.timeutil import parse_iso, utcnow

log = logging.getLogger(__name__)


def sport_key_to_canonical(key: str) -> str:
    """Map an Odds API ``sport_key`` (``<group>_<league>``, e.g. ``soccer_epl``,
    ``tennis_atp_aus_open_singles``, ``americanfootball_nfl``) to a canonical
    sport name. Matched on the group prefix only, never a substring of the
    whole key, so ``americanfootball_nfl`` never matches "football"."""
    prefix = key.lower().strip().split("_", 1)[0]
    if prefix == "soccer":
        return "football"
    if prefix == "tennis":
        return "tennis"
    return "other"


#: Major football leagues fetched first when more in-season leagues exist than
#: ``max_leagues_per_sport`` allows; anything else discovered is appended after
#: these, so a quiet day still reaches whatever football is actually on.
FOOTBALL_LEAGUE_PRIORITY = [
    "soccer_epl", "soccer_spain_la_liga", "soccer_italy_serie_a", "soccer_germany_bundesliga",
    "soccer_france_ligue_one", "soccer_uefa_champs_league", "soccer_uefa_europa_league",
    "soccer_uefa_europa_conference_league", "soccer_usa_mls", "soccer_netherlands_eredivisie",
    "soccer_portugal_primeira_liga", "soccer_efl_champ", "soccer_brazil_campeonato",
    "soccer_mexico_ligamx", "soccer_belgium_first_div", "soccer_turkey_super_league",
    "soccer_conmebol_copa_libertadores", "soccer_conmebol_copa_sudamericana",
]


def _prioritized(keys: List[str], priority: List[str]) -> List[str]:
    rank = {k: i for i, k in enumerate(priority)}
    return sorted(keys, key=lambda k: (rank.get(k, len(priority)), k))


class OddsApiProvider(BaseProvider):
    name = "oddsapi"
    sports = ["football", "tennis"]

    def __init__(self, api_key: str, budget: Budget, min_interval: float = 1.1,
                 bookmakers: Optional[List[str]] = None, odds_format: str = "decimal",
                 regions: Optional[List[str]] = None, max_leagues_per_sport: int = 8,
                 monthly_limit: int = 500, max_credits_per_cycle: int = 15,
                 reserve_credits: int = 50, min_keys_per_sport: int = 1,
                 league_allowlist: Optional[List[str]] = None, price_mode: str = "median"):
        self.key = api_key
        self.price_mode = price_mode
        self.budget = budget
        self.min_interval = min_interval
        self.odds_format = odds_format
        # None = accept every bookmaker the requested regions return; the
        # Odds API's bookmaker set drifts (new books, per-locale variants like
        # "winamax_de"/"winamax_fr") so an allowlist would silently starve markets.
        self.bookmakers = bookmakers
        self.regions = regions or ["eu"]
        # The markets we actually use: h2h -> result markets, spreads ->
        # handicaps, totals -> over/under. Credit cost is 1 x regions x markets
        # per odds request, so one region and these three = 3 credits per key.
        self.markets = ["h2h", "spreads", "totals"]
        self.max_leagues_per_sport = max(1, max_leagues_per_sport)
        self.max_credits_per_cycle = max(1, max_credits_per_cycle)
        # Football league keys allowed at discovery; empty/None = every in-season key.
        self.league_allowlist = [k.strip() for k in (league_allowlist or []) if k and k.strip()]
        # Every allowed sport with events in the window gets at least this many
        # odds keys per cycle before leftover credits are ranked by event count.
        self.min_keys_per_sport = max(0, min_keys_per_sport)
        # Authoritative quota signal: x-requests-remaining from every response,
        # persisted so the gate survives restarts. The bot never spends below
        # reserve_credits of the remaining balance.
        self.reserve_credits = max(0, reserve_credits)
        self.requests_remaining: Optional[int] = None
        self.last_refusal: Optional[str] = None
        self.requests_used: Optional[int] = None
        # Free tier is ~500 requests/month; enforced here (not just paced by
        # RateLimiter) so a busy day can't burn the whole month's quota.
        self.monthly_limit = monthly_limit
        self.db = getattr(budget, "db", None)
        # retries=0: a retried request is still a real upstream call, and
        # Budget.spend() is only charged once per logical fetch - a silent
        # retry would let real usage drift ahead of what's being tracked.
        self.http = Http("https://api.the-odds-api.com/v4",
                         rate=RateLimiter(min_interval), retries=0)
        self.sport_keys: Dict[str, List[str]] = {}  # canonical -> in-season league keys
        self.parse_counts: Counter = Counter()
        self.sport_status: Dict[str, Dict] = {}
        # /scores responses cached per sport key for the duration of one settle.run
        self._scores_cache: Dict[str, Optional[Dict[str, Dict]]] = {}
        self.skipped_no_results: List[str] = []   # keys with no results path (not fetched)

    def begin_run(self) -> None:
        """Forget cached /scores responses: results change between settlement runs."""
        self._scores_cache = {}

    def enabled(self) -> bool:
        return bool(self.key)

    def _get(self, path: str, params: Optional[Dict] = None):
        """The Odds API authenticates via an ``apiKey`` query parameter, not a header."""
        merged = {"apiKey": self.key}
        merged.update(params or {})
        try:
            return self.http.get(path, merged)
        finally:
            self._track_quota(path)

    # ------------------------------------------------------------ quota headers
    def _track_quota(self, path: str = "") -> None:
        """Read x-requests-remaining / x-requests-used from the response just
        made, log them, and persist the latest values.

        These headers are the authoritative quota signal: the local budget
        counter only sees this process's spends, so it drifts from reality
        (dev cycles, restarts, other tools sharing the key)."""
        headers = getattr(self.http, "last_headers", {}) or {}
        rem, used = headers.get("x-requests-remaining"), headers.get("x-requests-used")
        if rem is None and used is None:
            return
        try:
            if rem is not None:
                self.requests_remaining = int(rem)
            if used is not None:
                self.requests_used = int(used)
        except (TypeError, ValueError):
            return
        log.info("OddsAPI quota | %s | x-requests-remaining=%s x-requests-used=%s "
                 "reserve=%d local_month=%d", path or "-", self.requests_remaining,
                 self.requests_used, self.reserve_credits,
                 self.budget.used_month(self.name) if self.budget else -1)
        if self.db is not None:
            try:
                if self.requests_remaining is not None:
                    self.db.set_kv("oddsapi:requests_remaining", str(self.requests_remaining))
                if self.requests_used is not None:
                    self.db.set_kv("oddsapi:requests_used", str(self.requests_used))
            except Exception:  # noqa: BLE001 - telemetry must never break a fetch
                log.debug("OddsAPI: could not persist quota headers", exc_info=True)

    def _remaining_credits(self) -> Optional[int]:
        """Latest known x-requests-remaining: in-memory, else persisted, else
        None when this key has never answered (gate then falls back to the
        local monthly counter)."""
        if self.requests_remaining is not None:
            return self.requests_remaining
        if self.db is not None:
            raw = self.db.get_kv("oddsapi:requests_remaining")
            if raw not in (None, ""):
                try:
                    self.requests_remaining = int(raw)
                except ValueError:
                    return None
        return self.requests_remaining

    def can_spend_credits(self, n: int) -> bool:
        """True when ``n`` credits may be spent without dipping below
        ODDSAPI_RESERVE_CREDITS of the remaining balance (or, before any
        header has been seen, without breaking the local monthly limit)."""
        remaining = self._remaining_credits()
        if remaining is None:
            return self.budget.can_spend(self.name, n, monthly_limit=self.monthly_limit)
        if remaining - n < self.reserve_credits:
            log.warning("OddsAPI: x-requests-remaining=%d, reserve=%d - refusing to spend %d credits",
                        remaining, self.reserve_credits, n)
            self.last_refusal = (f"x-requests-remaining={remaining}, reserve={self.reserve_credits} "
                                 f"- refused to spend {n} credits")
            return False
        return True

    def quota_state(self) -> Dict:
        """What the quota gate saw on the last fetch - stored with the provider
        report so it can be read back next to the slate it produced."""
        return {"requests_remaining": self._remaining_credits(), "requests_used": self.requests_used,
                "reserve_credits": self.reserve_credits, "refused_to_spend": self.last_refusal is not None,
                "refusal": self.last_refusal}

    @property
    def credits_per_key(self) -> int:
        """Odds API pricing: an odds request costs 1 credit x regions x markets."""
        return len(self.regions) * len(self.markets)

    def _discover_sports(self) -> Dict[str, List[str]]:
        """In-season football/tennis league keys from GET /sports.

        Per the Odds API docs this endpoint does NOT count against the usage
        quota, so it is not charged to the budget here. Football is
        prioritized by FOOTBALL_LEAGUE_PRIORITY, tennis by whatever is active
        (tournaments rotate through the year). Every other sport reported by
        this key is dropped: football/tennis only.
        """
        if self.sport_keys:
            return self.sport_keys
        try:
            data = self._get("/sports")
        except ProviderError as e:
            log.warning("OddsAPI: sport discovery failed (%s)", e)
            return {}

        football_keys, tennis_keys = [], []
        for row in data if isinstance(data, list) else []:
            if not isinstance(row, dict):
                continue
            if not row.get("active", True):
                continue
            key = row.get("key", "").strip()
            sport = sport_key_to_canonical(key)
            if self.league_allowlist and key not in self.league_allowlist:
                continue
            if key in self.skipped_no_results:
                continue                                  # nothing can settle it
            if sport == "football":
                football_keys.append(key)
            elif sport == "tennis":
                tennis_keys.append(key)

        out: Dict[str, List[str]] = {}
        if football_keys:
            out["football"] = _prioritized(football_keys, FOOTBALL_LEAGUE_PRIORITY)
        if tennis_keys:
            out["tennis"] = sorted(tennis_keys)

        self.sport_keys = out
        log.info("OddsAPI: discovered in-season leagues: %s", self.sport_keys)
        return out

    def fetch_events(self, start: datetime, end: datetime) -> List[Event]:
        self.parse_counts = Counter()
        self.sport_status = {}
        self.last_refusal = None
        sports = self._discover_sports()
        if not sports:
            log.warning("OddsAPI: no football/tennis leagues discovered in season")
            return []
        in_season = {key for keys in sports.values() for key in keys}
        canonical_of = {key: canonical for canonical, keys in sports.items() for key in keys}

        # ---- free enumeration (GET /sports/{key}/events costs 0 credits per
        #      the Odds API docs; the odds endpoint is the paid one)
        rows_by_key: Dict[str, List[Dict]] = {}
        for key in sorted(in_season):
            try:
                data = self._get(f"/sports/{key}/events", {"dateFormat": "iso"})
            except ProviderError as e:
                log.warning("OddsAPI: event listing failed for %s (%s)", key, e)
                continue
            n = 0
            for row in data if isinstance(data, list) else []:
                if not isinstance(row, dict):
                    continue
                start_ts = parse_iso(row.get("commence_time", ""))
                if start_ts and start <= start_ts < end:
                    rows_by_key.setdefault(key, []).append(row)
                    n += 1
            self.parse_counts["events_in_window"] += n
        self.parse_counts["keys_enumerated"] = len(in_season)

        # ---- spend credits only on keys that have events in the window, under
        #      a hard per-cycle credit cap. One odds request costs 1 credit x
        #      regions x markets, so one region + 3 markets = 3 credits per key
        #      -> cap 15 = up to 5 keys per cycle.
        #
        #      Allocation is per SPORT first, then by event count: every allowed
        #      sport that has >= 1 event in the window is guaranteed
        #      ``min_keys_per_sport`` keys before any leftover credit is ranked.
        #      Ranking all keys in one list let a busy football day take every
        #      slot and silently starve tennis.
        per_key = self.credits_per_key
        max_keys = max(1, self.max_credits_per_cycle // per_key)

        def rank(key: str) -> Tuple:
            # busiest first; football league priority, then name, break ties
            return (-len(rows_by_key[key]),
                    FOOTBALL_LEAGUE_PRIORITY.index(key) if key in FOOTBALL_LEAGUE_PRIORITY else 999, key)

        chosen: List[str] = []
        for canonical in self.sports:
            live = sorted((k for k in sports.get(canonical, []) if rows_by_key.get(k)), key=rank)
            chosen.extend(live[:self.min_keys_per_sport])
        chosen = chosen[:max_keys]
        rest = sorted((k for k in rows_by_key if k not in chosen), key=rank)
        chosen.extend(rest[:max_keys - len(chosen)])
        log.info("OddsAPI: %d in-season keys, %d with events in window, spending <=%d credits "
                 "(%d/key) on %d keys: %s",
                 len(in_season), len(rows_by_key), max_keys * per_key, per_key,
                 len(chosen), {k: len(rows_by_key[k]) for k in chosen})

        rows: List[Tuple[str, Dict]] = []
        spent = 0
        rows_by_sport: Counter = Counter()
        fetched_by_sport: Counter = Counter()
        for league_key in chosen:          # guaranteed per-sport keys come first
            canonical = canonical_of[league_key]
            if spent + per_key > self.max_credits_per_cycle:
                log.warning("OddsAPI: per-cycle credit cap %d reached - skipping %s",
                            self.max_credits_per_cycle, league_key)
                break
            if not self.can_spend_credits(per_key):
                log.warning("OddsAPI: quota/reserve gate stopped the odds fetch at %s "
                            "(remaining=%s, reserve=%d)", league_key,
                            self._remaining_credits(), self.reserve_credits)
                break
            try:
                data = self._get(f"/sports/{league_key}/odds", {
                    "oddsFormat": self.odds_format,
                    "regions": ",".join(self.regions),
                    "markets": ",".join(self.markets),
                })
                self.budget.spend(self.name, per_key)
                spent += per_key
            except ProviderError as e:
                log.warning("OddsAPI: fetch failed for %s (%s)", league_key, e)
                continue
            batch = data if isinstance(data, list) else []
            rows.extend((canonical, e) for e in batch)
            rows_by_sport[canonical] += len(batch)
            fetched_by_sport[canonical] += 1
        for canonical in self.sports:
            keys = sports.get(canonical, [])
            ok = fetched_by_sport[canonical]
            self.sport_status[canonical] = {
                "status": "success" if ok else ("empty" if keys else "failed"),
                "status_detail": "partial" if 0 < ok < len(keys) else None,
                "leagues_fetched": ok, "leagues_total": len(keys),
                "keys_with_events": sum(1 for k in keys if rows_by_key.get(k)),
                "events": rows_by_sport[canonical],
            }

        events = self._rows_to_events(rows, start, end)
        log.info("OddsAPI: %d odds rows -> %d events in window (%d credits spent, cap %d, "
                 "reserve %d, x-requests-remaining=%s) | parse rejections: %s | sport status: %s",
                 len(rows), len(events), spent, self.max_credits_per_cycle,
                 self.reserve_credits, self._remaining_credits(),
                 dict(self.parse_counts), self.sport_status)
        return events

    def _rows_to_events(self, rows: List[Tuple[str, Dict]], start: datetime, end: datetime) -> List[Event]:
        """Convert raw Odds API rows to Event objects with normalized MarketLine."""
        by_event: Dict[str, Tuple[str, Dict, List[Dict]]] = {}  # event_id -> (sport, event_row, bookmakers)

        for sport, row in rows:
            if not isinstance(row, dict):
                continue
            event_id = row.get("id", "").strip()
            if not event_id:
                continue
            # Group by event, keeping first occurrence metadata
            if event_id not in by_event:
                by_event[event_id] = (sport, row, [])
            # Accumulate bookmakers
            by_event[event_id][2].extend(row.get("bookmakers", []))

        events: List[Event] = []
        counts = Counter()

        for event_id, (sport, row, bookmakers) in by_event.items():
            start_str = row.get("commence_time", "")
            start_ts = parse_iso(start_str) if start_str else None

            counts[sport + ":raw_events"] += 1

            if not start_ts or not (start <= start_ts < end):
                counts[sport + ":outside_window"] += 1
                continue

            home = row.get("home_team", "").strip()
            away = row.get("away_team", "").strip()
            if not home or not away:
                counts[sport + ":missing_teams"] += 1
                continue

            if sport == "tennis" and is_doubles(home, away):
                counts[sport + ":doubles"] += 1
                self.parse_counts["DOUBLES_UNSUPPORTED"] += 1
                continue
            counts[sport + ":in_window"] += 1

            e = Event(provider=self.name, provider_event_id=event_id, sport=sport,
                      league=row.get("sport_title", "").strip() or sport.title(),
                      home=home, away=away, start=start_ts, sources=[self.name],
                      extra={"sport_key": str(row.get("sport_key") or "")} if row.get("sport_key") else {})

            # Merge bookmaker odds for same market/selection
            merged: Dict[Tuple, MarketLine] = {}
            for bm in bookmakers:
                if not isinstance(bm, dict):
                    continue
                if self.bookmakers is not None and bm.get("key") not in self.bookmakers:
                    continue
                bm_key = bm.get("key", "")
                for market in bm.get("markets", []):
                    if not isinstance(market, dict):
                        continue
                    ml = self._market_to_lines(market, bm_key, home, away)
                    for line in ml:
                        k = line.key()
                        if k in merged:
                            m = merged[k]
                            m.book_prices.append(line.price)
                            m.n_books = len(m.book_prices)
                            if line.price > m.price:
                                m.price, m.bookmaker, m.provider_odd_id = line.price, line.bookmaker, line.provider_odd_id
                        else:
                            merged[k] = line

            for m in merged.values():
                apply_price_mode(m, self.price_mode)
            e.markets = list(merged.values())
            if e.markets:
                counts[sport + ":normalized_events"] += 1
                events.append(e)

        self.normalization_counts = dict(counts)
        log.info("OddsAPI normalization: %s", self.normalization_counts)
        return events

    def _market_to_lines(self, market: Dict, bookmaker: str, home: str, away: str) -> List[MarketLine]:
        """Convert a single market into MarketLine objects.

        The Odds API names h2h/spreads outcomes by the actual team name
        (``"Arsenal"``, not ``"home"``), so every side has to be resolved back
        to home/away by matching against the event's own team names.
        """
        lines = []
        market_key = market.get("key", "").lower()
        outcomes = market.get("outcomes", [])

        if not outcomes or not isinstance(outcomes, list):
            self.parse_counts["NO_OUTCOMES"] += 1
            return []

        if market_key == "h2h":
            lines.extend(self._h2h_outcomes(outcomes, bookmaker, home, away))
        elif market_key == "spreads":
            lines.extend(self._spreads_outcomes(outcomes, bookmaker, home, away))
        elif market_key == "totals":
            lines.extend(self._totals_outcomes(outcomes, bookmaker))

        return lines

    def _side_for_team(self, name: str, home: str, away: str) -> Optional[str]:
        """Resolve an Odds API outcome's team name to 'home' or 'away'."""
        if same_team(name, home):
            return "home"
        if same_team(name, away):
            return "away"
        return None

    def _h2h_outcomes(self, outcomes: List[Dict], bookmaker: str, home: str, away: str) -> List[MarketLine]:
        """Convert head-to-head (moneyline) outcomes: team name / team name / 'Draw'."""
        lines = []
        has_draw = any(str(o.get("name", "")).strip().lower() == "draw" for o in outcomes)
        market = "result3" if has_draw else "result2"

        for outcome in outcomes:
            name = str(outcome.get("name", "")).strip()
            price = parse_decimal(outcome.get("price"))

            if not price:
                self.parse_counts["INVALID_PRICE"] += 1
                continue

            if name.lower() == "draw":
                selection = "draw"
            else:
                selection = self._side_for_team(name, home, away)
                if not selection:
                    self.parse_counts["UNKNOWN_H2H_OUTCOME"] += 1
                    continue

            lines.append(MarketLine(
                market=market, selection=selection, price=price,
                bookmaker=bookmaker, n_books=1,
                provider_odd_id=f"{bookmaker}:h2h:{selection}",
                book_prices=[price]
            ))

        return lines

    def _spreads_outcomes(self, outcomes: List[Dict], bookmaker: str, home: str, away: str) -> List[MarketLine]:
        """Convert point spread outcomes (outcome name is the team name)."""
        lines = []
        for outcome in outcomes:
            name = str(outcome.get("name", "")).strip()
            point = outcome.get("point")
            price = parse_decimal(outcome.get("price"))

            if not price:
                self.parse_counts["INVALID_PRICE"] += 1
                continue

            if point is None:
                self.parse_counts["MISSING_LINE"] += 1
                continue

            try:
                point = float(point)
            except (TypeError, ValueError):
                self.parse_counts["INVALID_LINE"] += 1
                continue

            side = self._side_for_team(name, home, away)
            if side == "home":
                selection, line = "home", point
            elif side == "away":
                selection, line = "away", point
            else:
                self.parse_counts["UNKNOWN_SPREAD_OUTCOME"] += 1
                continue

            lines.append(MarketLine(
                market="handicap", selection=selection, price=price, line=line,
                bookmaker=bookmaker, n_books=1,
                provider_odd_id=f"{bookmaker}:spread:{selection}:{point}",
                book_prices=[price]
            ))

        return lines

    def _totals_outcomes(self, outcomes: List[Dict], bookmaker: str) -> List[MarketLine]:
        """Convert over/under total outcomes."""
        lines = []
        for outcome in outcomes:
            name = outcome.get("name", "").lower().strip()
            point = outcome.get("point")
            price = parse_decimal(outcome.get("price"))

            if not price:
                self.parse_counts["INVALID_PRICE"] += 1
                continue

            if point is None:
                self.parse_counts["MISSING_LINE"] += 1
                continue

            try:
                point = float(point)
            except (TypeError, ValueError):
                self.parse_counts["INVALID_LINE"] += 1
                continue

            if not name.endswith(("over", "under")):
                self.parse_counts["UNKNOWN_TOTAL_OUTCOME"] += 1
                continue

            selection = name.split()[-1]  # "over" or "under"
            if selection not in ("over", "under"):
                self.parse_counts["UNKNOWN_TOTAL_OUTCOME"] += 1
                continue

            lines.append(MarketLine(
                market="total", selection=selection, price=price, line=point,
                bookmaker=bookmaker, n_books=1,
                provider_odd_id=f"{bookmaker}:total:{selection}:{point}",
                book_prices=[price]
            ))

        return lines

    #: GET /sports/{key}/scores costs 2 credits when ``daysFrom`` is used.
    SCORES_COST = 2

    def _scores_for(self, sport_key: str) -> Optional[Dict[str, Dict]]:
        """Recent scores for one sport key, by event id; ONE call (2 credits) per
        key per settlement run, and only while the quota gate allows it."""
        if sport_key in self._scores_cache:
            return self._scores_cache[sport_key]
        stored = self._stored_scores(sport_key)
        if stored is not None:                          # a run earlier today already paid for it
            self._scores_cache[sport_key] = stored
            return stored
        if not self.can_spend_credits(self.SCORES_COST):
            self._scores_cache[sport_key] = None
            return None
        try:
            data = self._get(f"/sports/{sport_key}/scores", {"daysFrom": 3, "dateFormat": "iso"})
        except ProviderError:
            self._scores_cache[sport_key] = None
            raise
        self.budget.spend(self.name, self.SCORES_COST)
        rows = {str(r.get("id")): r for r in (data if isinstance(data, list) else [])
                if isinstance(r, dict) and r.get("id")}
        self._scores_cache[sport_key] = rows
        self._store_scores(sport_key, rows)
        return rows

    # The paid /scores payload is kept in provider_cache for the rest of the UTC day,
    # so a second settlement run (or a restart) spends nothing.
    def _scores_cache_key(self, sport_key: str) -> str:
        return f"oddsapi:scores:{sport_key}"

    def _stored_scores(self, sport_key: str) -> Optional[Dict[str, Dict]]:
        if self.db is None:
            return None
        try:
            row = self.db.get_provider_cache(self._scores_cache_key(sport_key))
            if not row or row["local_date"] != utcnow().date().isoformat():
                return None
            rows = json.loads(row["payload_json"])
        except Exception as e:            # a bad cache row must never block settlement
            log.warning("OddsAPI scores cache unreadable for %s: %s", sport_key, e)
            return None
        return rows if isinstance(rows, dict) else None

    def _store_scores(self, sport_key: str, rows: Dict[str, Dict]) -> None:
        if self.db is None:
            return
        today = utcnow().date()
        try:
            self.db.upsert_provider_cache(self._scores_cache_key(sport_key), today.isoformat(), rows,
                                          {"sport_key": sport_key, "cost": self.SCORES_COST},
                                          (datetime.combine(today, datetime.min.time(), timezone.utc)
                                           + timedelta(days=1)).isoformat())
        except Exception as e:
            log.warning("OddsAPI scores cache write failed for %s: %s", sport_key, e)

    def fetch_result(self, provider_event_id: str, event_hint: Optional[Dict] = None) -> Optional[MatchResult]:
        """Result for a known Odds API event id from ``/sports/{key}/scores?daysFrom=3``.

        Needs the event's sport key (``event_hint["sport_key"]``, persisted with the
        pick); without it there is nothing to ask, so this returns None."""
        sport_key = str((event_hint or {}).get("sport_key") or "")
        if not sport_key or not provider_event_id:
            return None
        rows = self._scores_for(sport_key)
        row = (rows or {}).get(str(provider_event_id))
        return self._parse_score_row(row) if row else None

    def _parse_score_row(self, row: Dict) -> MatchResult:
        result = MatchResult(status="unknown", source=self.name)
        by_team = {}
        for s in row.get("scores") or []:
            try:
                by_team[str(s.get("name", "")).strip()] = float(s.get("score"))
            except (TypeError, ValueError, AttributeError):
                continue
        home, away = str(row.get("home_team", "")).strip(), str(row.get("away_team", "")).strip()
        pair = (by_team[home], by_team[away]) if home in by_team and away in by_team else None
        if row.get("completed") and pair:
            result.status = "final"
            result.scores["full"] = pair
        elif pair:
            result.status = "in_progress"
        return result

    def find_result(self, sport: str, home: str, away: str, start: datetime) -> Optional[MatchResult]:
        """Look up a result by teams/time (cross-provider settlement)."""
        # Odds API doesn't provide result endpoints on the free tier
        # Settlement will fall through to other providers
        return None
