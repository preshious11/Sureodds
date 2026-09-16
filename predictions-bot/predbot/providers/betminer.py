"""BetMiner (RapidAPI, betminer.p.rapidapi.com). Football only.

Requests used per daily run (BETMINER_DAILY_REQUEST_LIMIT, default 5):
  1 x /matches/{dateFrom}/{dateTo}   -> every league/country for the local day

Design notes
------------
* League filtering is done CLIENT-SIDE by normalised competition *name*.
  BetMiner's numeric league ids are NOT verified (the only documented id,
  Premier League = 39, collides with API-Football's own id, so assuming the
  two id spaces match would be a trap). Filtering by name sidesteps that risk
  entirely while still restricting output to the competitions this bot follows.
* Only genuinely priced selections become MarketLines. BetMiner also ships a
  `probabilities` / `predictions` block; those are stored on Event.extra as
  informational metadata ONLY and never feed the model - odds are never
  synthesised from a probability.
* NOT LIVE-VERIFIED: the presence/population of the `odds` block and the exact
  `kickoff` timestamp format come from documentation only. Both are handled
  defensively (missing odds -> no MarketLine; unparseable kickoff -> that match
  is skipped with a warning, never a crash).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from predbot.providers.base import BaseProvider, Event, MarketLine
from predbot.providers.budget import Budget
from predbot.util.http import Http, ProviderError, RateLimiter
from predbot.util.names import similarity
from predbot.util.odds import parse_decimal
from predbot.util.timeutil import parse_iso, to_local

log = logging.getLogger(__name__)

#: Competitions this bot follows (mirrors the head of
#: predbot.providers.api_football.PRIORITY_LEAGUES). Overridable with
#: BETMINER_LEAGUES. Matched by normalised name, never by provider league id.
DEFAULT_LEAGUES = [
    "Premier League", "La Liga", "Serie A", "Bundesliga", "Ligue 1",
    "Champions League", "Europa League", "Eredivisie", "Primeira Liga",
    "Belgian Pro League", "Turkish Super Lig", "MLS",
]

#: BetMiner odds field -> (market, selection, line)
ODDS_MAP = {
    "home_win": ("result3", "home", None),
    "draw": ("result3", "draw", None),
    "away_win": ("result3", "away", None),
    "btts_yes": ("btts", "yes", None),
    "btts_no": ("btts", "no", None),
    "over_15": ("total", "over", 1.5),
    "over_25": ("total", "over", 2.5),
    "over_35": ("total", "over", 3.5),
    "dc_1x": ("dc", "home_draw", None),
    "dc_12": ("dc", "home_away", None),
    "dc_x2": ("dc", "away_draw", None),
}

LEAGUE_MATCH_THRESHOLD = 0.90


class BetMinerProvider(BaseProvider):
    name = "betminer"
    sports = ["football"]

    def __init__(self, api_key: str, budget: Budget, host: str = "betminer.p.rapidapi.com",
                 leagues: Optional[List[str]] = None, daily_limit: int = 5,
                 timezone: str = "Africa/Lagos"):
        self.key = api_key
        self.budget = budget
        self.host = host
        self.leagues = [x for x in (leagues or []) if x] or list(DEFAULT_LEAGUES)
        self.daily_limit = daily_limit
        self.timezone = timezone
        headers = {"x-rapidapi-key": api_key, "x-rapidapi-host": host}
        self.http = Http(f"https://{host}", headers=headers, rate=RateLimiter(2.0), retries=0)

    def enabled(self) -> bool:
        return bool(self.key)

    # ---------------------------------------------------------------- helpers
    def _call(self, path: str, params: Optional[Dict] = None) -> Optional[Dict]:
        if self.budget.is_exhausted(self.name):
            return None
        if not self.budget.can_spend(self.name, 1, daily_limit=self.daily_limit):
            self.budget.mark_exhausted(self.name, "daily request limit reached")
            return None
        # Count every attempted HTTP request, including errors; no hidden retries.
        self.budget.spend(self.name, 1)
        return self.http.get(path, params)

    def _league_allowed(self, competition: Dict) -> bool:
        name = str((competition or {}).get("name") or "")
        if not name:
            return False
        return any(similarity(name, want) >= LEAGUE_MATCH_THRESHOLD for want in self.leagues)

    # ------------------------------------------------------------------ fetch
    def fetch_events(self, start: datetime, end: datetime) -> List[Event]:
        self.last_fetch_complete = True
        self.fetch_counts = {"raw_matches": 0, "league_rejected": 0, "bad_kickoff": 0,
                             "out_of_window": 0, "no_odds": 0, "events_with_markets": 0}
        d_from = to_local(start, self.timezone).date().strftime("%Y-%m-%d")
        # `end` is the exclusive upper bound (local midnight), so step back one
        # second before taking its local date.
        d_to = to_local(end - timedelta(seconds=1), self.timezone).date().strftime("%Y-%m-%d")
        data = self._call(f"/matches/{d_from}/{d_to}")
        if data is None:
            self.last_fetch_complete = False
            log.warning("BetMiner: skipped fetch (daily budget %d exhausted)", self.daily_limit)
            return []
        if not isinstance(data, dict) or not isinstance(data.get("data"), list):
            self.last_fetch_complete = False
            raise ProviderError("BetMiner: malformed response envelope")
        if data.get("success") is False:
            self.last_fetch_complete = False
            raise ProviderError("BetMiner: success=false")

        out: List[Event] = []
        for row in data["data"]:
            self.fetch_counts["raw_matches"] += 1
            if not isinstance(row, dict):
                continue
            if not self._league_allowed(row.get("competition") or {}):
                self.fetch_counts["league_rejected"] += 1
                continue
            ev = self._parse_match(row)
            if ev is None:
                continue
            if not (start <= ev.start < end):
                self.fetch_counts["out_of_window"] += 1
                continue
            if not ev.markets:
                self.fetch_counts["no_odds"] += 1
                continue
            out.append(ev)
        self.fetch_counts["events_with_markets"] = len(out)
        log.info("BetMiner: %d events with markets (used %d/%d today)",
                 len(out), self.budget.used_today(self.name), self.daily_limit)
        return out

    # ------------------------------------------------------------------ parse
    def _parse_match(self, row: Dict) -> Optional[Event]:
        mid = str(row.get("match_id") or "")
        home = str(((row.get("home_team") or {}).get("name")) or "")
        away = str(((row.get("away_team") or {}).get("name")) or "")
        if not mid or not home or not away:
            log.warning("BetMiner: match missing id/teams, skipped")
            return None
        # Kickoff format is documented as ISO-8601 but unverified against a live
        # response: parse defensively and drop (not crash) anything unparseable.
        start = parse_iso(str(row.get("kickoff") or ""))
        if start is None:
            self.fetch_counts["bad_kickoff"] += 1
            log.warning("BetMiner: unparseable kickoff for match %s, skipped", mid)
            return None
        comp = row.get("competition") or {}
        ev = Event(provider=self.name, provider_event_id=mid, sport="football",
                   league=str(comp.get("name") or ""), league_id=str(comp.get("id") or ""),
                   home=home, away=away, start=start, sources=[self.name])
        ev.extra["country"] = comp.get("country")
        ev.extra["status"] = row.get("status")
        # Informational only - never used for scoring / p_model.
        if isinstance(row.get("probabilities"), dict):
            ev.extra["betminer_probabilities"] = dict(row["probabilities"])
        if isinstance(row.get("predictions"), dict):
            ev.extra["betminer_predictions"] = dict(row["predictions"])
        ev.markets = self._parse_odds(row.get("odds"))
        return ev

    def _parse_odds(self, odds) -> List[MarketLine]:
        if not isinstance(odds, dict):
            return []
        out: List[MarketLine] = []
        for field, (market, selection, line) in ODDS_MAP.items():
            price = parse_decimal(odds.get(field))
            if not price:
                continue          # missing/null/<=1.0 -> no line, never fabricated
            out.append(MarketLine(market=market, selection=selection, price=price,
                                  period="full", stat="points", entity="all", line=line,
                                  bookmaker="betminer", n_books=1,
                                  provider_odd_id=field, book_prices=[price]))
        return out

    # ---------------------------------------------------------------- results
    # BetMiner settlement is not supported: fetch_result / find_result keep the
    # BaseProvider defaults (return None), like SharpAPI.
