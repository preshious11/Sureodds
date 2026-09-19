"""Normalised data model. Every provider converts its own payload into these
objects so the model, picker and settlement code never touch raw API shapes.

Canonical vocabulary
--------------------
sport      : football (soccer), basketball, american_football, baseball,
             tennis, mma, other
period     : full  (whole match incl. overtime where the sport has it, or 90'
             for football), reg (regulation only), 1h, 2h, 1q..4q, 1s..
stat       : points (goals/points/runs), corners, cards, sets, games, or a
             provider-specific stat id for props (e.g. "player:points")
entity     : all | home | away | <player name>
market     : see predbot.markets.catalog
selection  : home | draw | away | over | under | yes | no | even | odd |
             home_draw | away_draw | home_away | home_over ... | "2-1" |
             "home/draw" (ht_ft) ...
line       : numeric line for total/handicap markets, applied to *the selected
             side* for handicaps (home -0.5 => line=-0.5 on selection=home)
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple


@dataclass
class MarketLine:
    market: str
    selection: str
    price: float                      # decimal odds actually available (best/consensus)
    period: str = "full"
    stat: str = "points"
    entity: str = "all"
    line: Optional[float] = None
    fair_price: Optional[float] = None   # provider de-vigged price when supplied
    provider_prob: Optional[float] = None   # provider-supplied probability (0..1) when present, e.g. SharpAPI odds_probability
    bookmaker: str = ""
    n_books: int = 1
    provider_odd_id: str = ""            # SGO oddID / SharpAPI row id
    label: str = ""                      # human readable selection label
    market_name: str = ""                # human readable market name
    is_player_prop: bool = False
    book_prices: List[float] = field(default_factory=list)  # all book prices seen

    def key(self) -> Tuple:
        return (self.market, self.period, self.stat, self.entity,
                None if self.line is None else round(self.line, 2), self.selection)

    def market_key(self) -> Tuple:
        """Identifies the market (all selections share it)."""
        return (self.market, self.period, self.stat, self.entity,
                None if self.line is None else round(abs(self.line), 2))


def apply_price_mode(ml: MarketLine, mode: str = "median") -> None:
    """Set ``ml.price`` from ``ml.book_prices`` according to PRICE_MODE.

    ``median`` (default) prices a line at what a typical book offers, so a single
    outlier book cannot inflate the card; ``best`` keeps the highest price and the
    bookmaker that offered it. With a single book the price and its name are kept
    either way. Idempotent: it only ever reads ``book_prices``."""
    prices = [p for p in (ml.book_prices or []) if p and p > 1.0] or [ml.price]
    if len(prices) < 2:
        return
    if mode == "best":
        ml.price = max(prices)
    else:
        ml.price = round(float(statistics.median(prices)), 4)
        ml.bookmaker = "median"


@dataclass
class Event:
    provider: str
    provider_event_id: str
    sport: str
    league: str
    home: str
    away: str
    start: datetime
    markets: List[MarketLine] = field(default_factory=list)
    league_id: str = ""            # provider league identifier
    extra: Dict = field(default_factory=dict)
    sources: List[str] = field(default_factory=list)  # providers merged into this event

    @property
    def name(self) -> str:
        return f"{self.home} vs {self.away}"

    def dedupe_key(self) -> str:
        from predbot.util.names import normalize
        stamp = self.start.strftime("%Y%m%d%H") if self.start else ""
        return f"{self.sport}|{normalize(self.home)}|{normalize(self.away)}|{stamp}"


@dataclass
class MatchResult:
    status: str                                   # final | in_progress | postponed | cancelled | unknown
    scores: Dict[str, Tuple[float, float]] = field(default_factory=dict)   # period -> (home, away) points
    stats: Dict[Tuple[str, str], Tuple[float, float]] = field(default_factory=dict)  # (stat, period) -> (home, away)
    odd_scores: Dict[str, float] = field(default_factory=dict)   # provider odd id -> settled stat value
    source: str = ""

    def score(self, period: str = "full") -> Optional[Tuple[float, float]]:
        if period in self.scores:
            return self.scores[period]
        if period == "reg" and "full" in self.scores:
            return self.scores["full"]
        if period == "full" and "reg" in self.scores:
            return self.scores["reg"]
        return None

    def stat(self, stat: str, period: str = "full") -> Optional[Tuple[float, float]]:
        if stat == "points":
            return self.score(period)
        v = self.stats.get((stat, period))
        if v is None and period == "full":
            v = self.stats.get((stat, "reg"))
        return v


class BaseProvider:
    name: str = "base"
    sports: List[str] = []

    def enabled(self) -> bool:
        return True

    def fetch_events(self, start: datetime, end: datetime) -> List[Event]:
        raise NotImplementedError

    def fetch_result(self, provider_event_id: str, event_hint: Optional[Dict] = None) -> Optional[MatchResult]:
        """Return a MatchResult for a *known* provider event id, or None."""
        return None

    def find_result(self, sport: str, home: str, away: str, start: datetime) -> Optional[MatchResult]:
        """Look a result up by teams/time (cross-provider settlement). Optional."""
        return None
