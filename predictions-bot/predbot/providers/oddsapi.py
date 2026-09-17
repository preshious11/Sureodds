"""The Odds API provider for football and tennis odds."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from predbot.providers.base import BaseProvider, Event, MarketLine
from predbot.providers.budget import Budget
from predbot.util.http import Http, ProviderError

log = logging.getLogger(__name__)

SPORT_MAPPING = {
    # Football - Major European leagues
    "soccer_epl": "football",
    "soccer_france_ligue_one": "football",
    "soccer_germany_bundesliga": "football",
    "soccer_italy_serie_a": "football",
    "soccer_spain_la_liga": "football",
    # Football - European competitions
    "soccer_uefa_champs_league": "football",
    "soccer_uefa_europa_league": "football",
    # Football - Other major leagues
    "soccer_usa_mls": "football",
    "soccer_brazil_campeonato": "football",
    "soccer_mexico_ligamx": "football",
    "soccer_japan_j_league": "football",
    # Tennis
    "tennis_wta_guadalajara_open": "tennis",
}

BOOKMAKER_ODDS_KEYS = {
    "h2h": ("moneyline", {"home": "home", "away": "away", "draw": "draw"}),
    "spreads": ("handicap", {"home": "home", "away": "away"}),
    "totals": ("total", {"over": "over", "under": "under"}),
}


class OddsApiProvider(BaseProvider):
    """The Odds API provider: free tier, football + tennis only."""

    def __init__(
        self,
        api_key: str,
        budget: Budget,
        sports: Optional[List[str]] = None,
        min_interval: float = 0.0,
    ):
        self.api_key = api_key
        self.budget = budget
        self.requested_sports = sports or ["football", "tennis"]
        self.http = Http(
            "https://api.the-odds-api.com",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=20,
        )
        self.fetch_counts = {}
        self.normalization_counts = {}
        self.last_fetch_complete = True

    def enabled(self) -> bool:
        return bool(self.api_key)

    def fetch_events(self, start: datetime, end: datetime) -> List[Event]:
        """Fetch football and tennis events with odds."""
        self.fetch_counts = {}
        self.normalization_counts = {"raw_events": 0, "filtered_sports": 0, "in_window": 0}
        events = []

        for sport_key in sorted(SPORT_MAPPING.keys()):
            sport = SPORT_MAPPING[sport_key]
            if sport not in self.requested_sports:
                self.normalization_counts["filtered_sports"] += 1
                continue

            try:
                self._fetch_sport(sport_key, sport, start, end, events)
            except ProviderError as e:
                log.warning("OddsAPI %s failed: %s", sport_key, e)
                continue

        return events

    def _fetch_sport(
        self,
        sport_key: str,
        sport: str,
        start: datetime,
        end: datetime,
        events: List[Event],
    ) -> None:
        """Fetch events for one sport."""
        data = self.http.get(
            f"/v4/sports/{sport_key}/events",
            params={"apiKey": self.api_key, "dateFormat": "iso"},
        )

        raw_count = len(data) if isinstance(data, list) else 0
        self.fetch_counts[sport_key] = {"raw_events": raw_count}

        if not isinstance(data, list):
            return

        for ev_data in data:
            try:
                ev = self._normalize_event(ev_data, sport, start, end)
                if ev:
                    events.append(ev)
                    self.normalization_counts["in_window"] += 1
            except Exception as e:
                log.debug("OddsAPI normalization failed for %s: %s", sport_key, e)
                continue

        self.normalization_counts["raw_events"] = self.normalization_counts.get(
            "raw_events", 0
        ) + raw_count

    def _normalize_event(
        self,
        data: Dict,
        sport: str,
        start: datetime,
        end: datetime,
    ) -> Optional[Event]:
        """Normalize OddsAPI event to our Event model."""
        try:
            event_id = data.get("id", "")
            home_team = data.get("home_team", "")
            away_team = data.get("away_team", "")
            commence_time_str = data.get("commence_time", "")

            if not all([event_id, home_team, away_team, commence_time_str]):
                return None

            # Parse event time
            try:
                event_time = datetime.fromisoformat(commence_time_str.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                return None

            # Check window
            if not (start <= event_time < end):
                return None

            # Extract markets from bookmakers
            markets = self._extract_markets(data.get("bookmakers", []))
            if not markets:
                return None

            return Event(
                provider="oddsapi",
                provider_event_id=event_id,
                sport=sport,
                league=data.get("league", sport.title()),
                home=home_team,
                away=away_team,
                start=event_time,
                sources=["oddsapi"],
                markets=markets,
                extra={"country": ""},
            )
        except Exception as e:
            log.debug("Event normalization error: %s", e)
            return None

    def _extract_markets(self, bookmakers: List[Dict]) -> List[MarketLine]:
        """Extract market lines from bookmaker odds."""
        markets = []

        for bm in bookmakers:
            bm_key = bm.get("key", "")
            for market_data in bm.get("markets", []):
                try:
                    market_key = market_data.get("key", "")
                    if market_key not in BOOKMAKER_ODDS_KEYS:
                        continue

                    market_type, selection_map = BOOKMAKER_ODDS_KEYS[market_key]

                    for outcome in market_data.get("outcomes", []):
                        selection = outcome.get("name", "").lower()
                        if selection not in selection_map.values():
                            continue

                        price = outcome.get("price")
                        if not price or price <= 1.0:
                            continue

                        # Handle spreads and totals with point values
                        point = None
                        if market_type in ("handicap", "total"):
                            point = outcome.get("point")

                        markets.append(
                            MarketLine(
                                market=market_type,
                                selection=selection,
                                price=float(price),
                                n_books=1,
                                line=point,
                                bookmaker=bm_key,
                                provider_odd_id=f"{bm_key}_{market_key}_{selection}",
                            )
                        )
                except (KeyError, ValueError, TypeError):
                    continue

        return markets
