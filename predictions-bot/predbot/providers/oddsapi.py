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

import logging
from collections import Counter
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from predbot.providers.base import BaseProvider, Event, MarketLine, MatchResult
from predbot.providers.budget import Budget
from predbot.util.http import Http, ProviderError, RateLimiter
from predbot.util.names import same_team
from predbot.util.odds import parse_decimal
from predbot.util.timeutil import parse_iso

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
                 monthly_limit: int = 500):
        self.key = api_key
        self.budget = budget
        self.min_interval = min_interval
        self.odds_format = odds_format
        # None = accept every bookmaker the requested regions return; the
        # Odds API's bookmaker set drifts (new books, per-locale variants like
        # "winamax_de"/"winamax_fr") so an allowlist would silently starve markets.
        self.bookmakers = bookmakers
        self.regions = regions or ["us", "eu"]
        self.max_leagues_per_sport = max(1, max_leagues_per_sport)
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

    def enabled(self) -> bool:
        return bool(self.key)

    def _get(self, path: str, params: Optional[Dict] = None):
        """The Odds API authenticates via an ``apiKey`` query parameter, not a header."""
        merged = {"apiKey": self.key}
        merged.update(params or {})
        return self.http.get(path, merged)

    def _discover_sports(self) -> Dict[str, List[str]]:
        """In-season football/tennis league keys from GET /sports (default
        ``all=false``, i.e. skips leagues that are out of season), football
        prioritized by FOOTBALL_LEAGUE_PRIORITY, tennis by whatever is active
        (there is no "the" tennis league - tournaments rotate through the year).
        Every other sport reported by this key is dropped: football/tennis only.
        """
        if self.sport_keys:
            return self.sport_keys
        if not self.budget.can_spend(self.name, 1, monthly_limit=self.monthly_limit):
            log.warning("OddsAPI: monthly quota exhausted, skipping sport discovery")
            return {}
        try:
            data = self._get("/sports")
            self.budget.spend(self.name, 1)
        except ProviderError as e:
            log.warning("OddsAPI: sport discovery failed (%s)", e)
            return {}

        football_keys, tennis_keys = [], []
        for row in data if isinstance(data, list) else []:
            if not isinstance(row, dict):
                continue
            key = row.get("key", "").strip()
            sport = sport_key_to_canonical(key)
            if sport == "football":
                football_keys.append(key)
            elif sport == "tennis":
                tennis_keys.append(key)

        out: Dict[str, List[str]] = {}
        if football_keys:
            out["football"] = _prioritized(football_keys, FOOTBALL_LEAGUE_PRIORITY)[:self.max_leagues_per_sport]
        if tennis_keys:
            out["tennis"] = sorted(tennis_keys)[:self.max_leagues_per_sport]

        self.sport_keys = out
        log.info("OddsAPI: discovered in-season leagues: %s", self.sport_keys)
        return out

    def fetch_events(self, start: datetime, end: datetime) -> List[Event]:
        self.parse_counts = Counter()
        self.sport_status = {}
        sports = self._discover_sports()
        if not sports:
            log.warning("OddsAPI: no football/tennis leagues discovered in season")
            return []

        rows: List[Dict] = []
        for canonical, league_keys in sports.items():
            leagues_ok, events_seen = 0, 0
            for league_key in league_keys:
                if not self.budget.can_spend(self.name, 1, monthly_limit=self.monthly_limit):
                    log.warning("OddsAPI: monthly quota exhausted, stopping league fetch early")
                    break
                try:
                    data = self._get(f"/sports/{league_key}/odds", {
                        "oddsFormat": self.odds_format,
                        "regions": ",".join(self.regions),
                        "markets": "h2h,spreads,totals",
                    })
                    self.budget.spend(self.name, 1)
                    batch = data if isinstance(data, list) else []
                    rows.extend([(canonical, e) for e in batch])
                    events_seen += len(batch)
                    leagues_ok += 1
                except ProviderError as e:
                    log.warning("OddsAPI: fetch failed for %s (%s)", league_key, e)
            self.sport_status[canonical] = {
                "status": "success" if leagues_ok else "failed",
                "status_detail": "partial" if 0 < leagues_ok < len(league_keys) else None,
                "leagues_fetched": leagues_ok, "leagues_total": len(league_keys),
                "events": events_seen,
            }

        events = self._rows_to_events(rows, start, end)
        log.info("OddsAPI: %d odds rows -> %d events in window | "
                 "parse rejections: %s | sport status: %s",
                 len(rows), len(events), dict(self.parse_counts), self.sport_status)
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

            counts[sport + ":in_window"] += 1

            e = Event(provider=self.name, provider_event_id=event_id, sport=sport,
                      league=row.get("sport_title", "").strip() or sport.title(),
                      home=home, away=away, start=start_ts, sources=[self.name])

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

    def fetch_result(self, provider_event_id: str, event_hint: Optional[Dict] = None) -> Optional[MatchResult]:
        """Fetch a result for a known Odds API event id."""
        # Odds API doesn't provide easy result endpoints on the free tier
        # Settlement will rely on cross-provider lookups (API-Football, SportsGameOdds)
        return None

    def find_result(self, sport: str, home: str, away: str, start: datetime) -> Optional[MatchResult]:
        """Look up a result by teams/time (cross-provider settlement)."""
        # Odds API doesn't provide result endpoints on the free tier
        # Settlement will fall through to other providers
        return None
