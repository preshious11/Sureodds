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


_SIDE_SWAP = {"home": "away", "away": "home", "home_draw": "away_draw", "away_draw": "home_draw"}

#: Markets whose selections ARE the sides of one bet (home/draw/away, a handicap's two teams). Their ``entity`` can
#: only repeat the selected side - SportsGameOdds tags each side with its own team where other providers say
#: "all" - and must not split one market into two: ``market_key`` groups every selection of a market for
#: de-vigging, the sharp reference and the margin model, so a per-side entity left each side alone in its group.
SIDE_MARKETS = frozenset({"result2", "result3", "dnb", "dc", "handicap"})
#: Markets whose selection is "<side>_<rest>" (result & total, result & BTTS, a side's winning-margin band): mirroring
#: swaps the side prefix and keeps the rest ("home_over" -> "away_over", "draw_yes" stays).
_COMPOSITE_MARKETS = frozenset({"result_total", "result_btts", "winning_margin"})


def norm_book(name: str) -> str:
    """One spelling per bookmaker across providers: ``"Betfair Exchange"`` / ``"betfair_exchange"`` agree."""
    import re
    return re.sub(r"[^a-z0-9]+", "_", str(name or "").strip().lower()).strip("_")


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
    #: bookmaker key (see ``norm_book``) -> that book's price. ``book_prices`` loses WHO quoted; this keeps it, so a
    #: sharp book can be told apart from a soft one and the same book seen through two providers counts once.
    #: Empty for lines parsed before this field existed (cached rows): consumers then fall back to ``n_books``.
    book_quotes: Dict[str, float] = field(default_factory=dict)
    providers: List[str] = field(default_factory=list)      # providers whose feed carried this line
    provider_market_id: str = ""       # raw/composite provider market identifier used to normalize this line

    def __post_init__(self):
        # One canonical representation for side markets however a provider tags them (also for cached rows
        # restored with MarketLine(**row)); the side itself is the ``selection``.
        if self.market in SIDE_MARKETS and self.entity in ("home", "away"):
            self.entity = "all"

    def distinct_books(self) -> int:
        """How many DIFFERENT bookmakers quote this line: ``book_quotes`` when the provider supplied them,
        else the legacy ``n_books`` count."""
        return len(self.book_quotes) if self.book_quotes else max(1, int(self.n_books or 1))

    def provider_count(self) -> int:
        return max(1, len(self.providers))

    def price_dispersion(self) -> Optional[float]:
        """Spread (max - min) of the books' IMPLIED probabilities, in probability points; ``None`` with fewer
        than two prices. A wide spread means the books disagree (a stale or outlier quote)."""
        prices = [p for p in (list(self.book_quotes.values()) or self.book_prices or []) if p and p > 1.0]
        if len(prices) < 2:
            return None
        implied = [1.0 / p for p in prices]
        return max(implied) - min(implied)

    def key(self) -> Tuple:
        return (self.market, self.period, self.stat, self.entity,
                None if self.line is None else round(self.line, 2), self.selection)

    def mirrored(self) -> "MarketLine":
        """The same bet as a provider that lists home and away the other way round would state it: sides
        exchanged (selection, a team total's entity, a correct score's digits). A handicap's ``line`` is
        applied to the SELECTED side (see the module docstring, and settlement's ``_handicap(sel, opp, line)``),
        so it does NOT change sign when the label changes: "their home -1.5" is "our away -1.5" - the same
        team keeps the same line. (Negating it would describe the opposite bet.)"""
        import re
        from dataclasses import replace
        sel = self.selection
        if self.market == "correct_score" and re.fullmatch(r"\d+-\d+", sel or ""):
            a, b = sel.split("-")
            sel = f"{b}-{a}"
        elif self.market == "ht_ft" and "/" in (sel or ""):
            sel = "/".join(_SIDE_SWAP.get(part, part) for part in sel.split("/"))      # "home/draw" -> "away/draw"
        elif self.market in _COMPOSITE_MARKETS and "_" in (sel or ""):
            side, rest = sel.split("_", 1)                                               # "home_over" -> "away_over"
            sel = f"{_SIDE_SWAP.get(side, side)}_{rest}"
        else:
            sel = _SIDE_SWAP.get(sel, sel)
        return replace(self, selection=sel, entity=_SIDE_SWAP.get(self.entity, self.entity))

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


def consolidate_lines(markets: List["MarketLine"]) -> List["MarketLine"]:
    """One line per bet: lines with an identical key (SportsGameOdds lists a football market under both its
    ``game`` and ``reg`` period, which we map to the same ``full``) collapse into the one quoted by most books,
    keeping its price and adding the others' books. Order of first appearance is kept."""
    groups: Dict[Tuple, List["MarketLine"]] = {}
    for m in markets:
        groups.setdefault(m.key(), []).append(m)
    out: List["MarketLine"] = []
    for group in groups.values():
        primary = group[0] if len(group) == 1 else max(group, key=lambda m: m.n_books)   # ties: first seen
        if len(group) > 1:
            primary.book_prices = [p for m in group for p in (m.book_prices or [m.price])]
            primary.n_books = len(primary.book_prices)
            for m in group:
                for book, price in m.book_quotes.items():
                    primary.book_quotes.setdefault(book, price)
        out.append(primary)
    return out


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
    canonical_event_id: str = ""
    canonical_competition_id: str = ""
    home_id: str = ""
    away_id: str = ""

    @property
    def name(self) -> str:
        return f"{self.home} vs {self.away}"

    def dedupe_key(self) -> str:
        from predbot.util.names import normalize
        stamp = self.start.strftime("%Y%m%d%H") if self.start else ""
        return f"{self.sport}|{normalize(self.home)}|{normalize(self.away)}|{stamp}"


@dataclass
class MatchResult:
    status: str                                   # scheduled | in_progress | final | postponed | cancelled | unknown
    scores: Dict[str, Tuple[float, float]] = field(default_factory=dict)   # period -> (home, away) points
    stats: Dict[Tuple[str, str], Tuple[float, float]] = field(default_factory=dict)  # (stat, period) -> (home, away)
    odd_scores: Dict[str, float] = field(default_factory=dict)   # provider odd id -> settled stat value
    source: str = ""
    #: whether the game went beyond regulation (overtime / extra innings): True, False, or None = the source did
    #: not say. ``full`` includes overtime in every sport that has it (football's ``full`` is the 90 minutes).
    overtime: Optional[bool] = None

    def score(self, period: str = "full") -> Optional[Tuple[float, float]]:
        """The score of ``period``. ``reg`` (regulation only) and ``full`` (overtime included) are only substituted
        for each other when that is provably the same score - never guessed:

        * ``reg`` from ``full`` only when the source says there was no overtime;
        * ``full`` from ``reg`` when there was no overtime, or when regulation did not end level (overtime is only
          ever played after a tie, so a decided regulation score IS the final score).

        Otherwise None: a regulation-time bet is left unresolved rather than graded on an overtime-inclusive score.
        (Football never reaches this: settlement grades its ``reg`` on ``full``, which is the 90 minutes.)"""
        if period in self.scores:
            return self.scores[period]
        if period == "reg" and "full" in self.scores and self.overtime is False:
            return self.scores["full"]
        if period == "full" and "reg" in self.scores:
            reg = self.scores["reg"]
            if self.overtime is False or (self.overtime is None and reg[0] != reg[1]):
                return reg
        return None

    def stat(self, stat: str, period: str = "full") -> Optional[Tuple[float, float]]:
        if stat == "points":
            return self.score(period)
        v = self.stats.get((stat, period))
        if v is None and period == "full" and self.overtime is False:
            v = self.stats.get((stat, "reg"))
        if v is None and period == "reg" and self.overtime is False:
            v = self.stats.get((stat, "full"))
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

    def find_result(self, sport: str, home: str, away: str, start: datetime,
                    event_hint: Optional[Dict] = None) -> Optional[MatchResult]:
        """Look a result up by teams/time (cross-provider settlement). Optional."""
        return None
