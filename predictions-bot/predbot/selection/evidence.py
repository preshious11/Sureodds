"""How much evidence stands behind a candidate - and a ranking score built from it.

Why this exists. ``p_model`` is almost always the soft market's own de-vigged probability: ranking on it picks
"the selection the bookmakers think is most likely", at a price that already carries their margin, out of ~1,000
noisy lines. That is the winner's curse. The ranking here instead asks, per candidate:

  * how well-supported is the line?        distinct books, providers, how closely the books agree
  * how reliable is its market class?      main / secondary / restricted (see ``classify``)
  * is the price actually good?            edge of the AVAILABLE price over a SHARP reference - only when a
                                           sharp book really quoted it; never manufactured

and ranks on the LOWER confidence bound of the probability, plus the sharp edge (both in probability points):

    rank = p_model - RANK_Z * se  +  EDGE_RANK_WEIGHT * clamp(p_fair - 1/best_available_soft_price)

``rank`` is a ranking score, NOT a probability: it is never shown and never stored as one (``prob`` is the
calibrated ``p_model``; the rank goes to ``rank_score`` next to it).

``se`` (uncertainty of the probability estimate, in probability points) combines independent error terms:
    market-class prior       main / secondary / restricted (config SE_*; judgement values, not fitted)
    unverified single book   SE_SINGLE_BOOK - one quote is not confirmation
    book disagreement        range of the books' implied probabilities -> sd (range / d2(n)) -> standard error
                             of their mean (sd / sqrt(n)); the standard small-sample estimator
    no sharp reference       SE_NO_SHARP - the estimate rests on soft prices alone
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, Optional

from predbot.selection.families import main_line_table

MAIN, SECONDARY, RESTRICTED = "main", "secondary", "restricted"
QUALITY_ORDER = {MAIN: 0, SECONDARY: 1, RESTRICTED: 2}
#: Order candidates are OFFERED to the Daily builder in. Main and secondary compete on the evidence-adjusted rank
#: (a well-covered +1.5 handicap at 86% rightly outranks a 58% win - its tier prior is already in the rank); only the
#: restricted class (first-half / corners / far alternates / exotics) is held back until nothing else is left.
OFFER_ORDER = {MAIN: 0, SECONDARY: 0, RESTRICTED: 1}

#: mean range of n standard-normal draws (d2): sd ~= range / d2(n)
_D2 = {2: 1.128, 3: 1.693, 4: 2.059, 5: 2.326, 6: 2.534, 7: 2.704, 8: 2.847}

#: how far (in goals / points) a total or handicap line may sit from the event's MAIN line and still count as main
MAIN_LINE_TOLERANCE = 0.5 + 1e-9
#: ...and as a secondary (alternate) line; beyond it the line is a far alternate (e.g. Over 0.5 goals)
ALT_LINE_TOLERANCE = 1.5 + 1e-9

_RESULT_MARKETS = ("result3", "result2", "dnb", "dc")


@dataclass(frozen=True)
class Evidence:
    books: int
    providers: int
    dispersion: Optional[float]
    quality: str
    se: float
    p_lcb: float
    edge: Optional[float]          # probability points of edge over the sharp reference; None = no reference
    has_sharp: bool
    rank: float

    def as_notes(self) -> Dict:
        """Compact, JSON-safe record stored with the pick (the data model for later calibration of these priors)."""
        return {"books": self.books, "providers": self.providers,
                "dispersion": None if self.dispersion is None else round(self.dispersion, 4),
                "quality": self.quality, "se": round(self.se, 4), "p_lcb": round(self.p_lcb, 4),
                "edge": None if self.edge is None else round(self.edge, 4), "sharp": self.has_sharp,
                "rank": round(self.rank, 4)}


def classify(s, tables: Optional[Dict[int, dict]] = None) -> str:
    """Market-quality tier of one candidate.

    main        match result / draw-no-bet / double chance, and the event's MAIN total or handicap line (within half
                a goal of the most balanced line), full match, on the score itself.
    secondary   BTTS, team totals, near-main alternate lines (within 1.5), tennis set / game totals.
    restricted  first-half and other period markets, corners / cards, far alternate lines, exotics and props.
    Thin markets are not deleted: a restricted line can still play a Daily if far more books back it."""
    ml = s.line
    if ml.market == "prop" or ml.market not in ("result3", "result2", "dnb", "dc", "btts", "total", "handicap"):
        return RESTRICTED                                     # props and exotics (correct score, result+total, ...)
    if ml.period not in ("full", "reg", "", None):
        return RESTRICTED                                     # 1H / 2H / quarters / sets / first five innings
    if ml.stat != "points":
        if getattr(s.event, "sport", "") == "tennis" and ml.stat in ("sets", "games") and ml.market in ("total", "handicap"):
            return SECONDARY
        return RESTRICTED                                     # corners, cards, ...
    if ml.market in _RESULT_MARKETS:
        return MAIN
    if ml.market == "btts":
        return SECONDARY
    if ml.market == "total" and ml.entity in ("home", "away"):
        return SECONDARY                                      # team totals
    if ml.line is None:
        return RESTRICTED
    tables = tables if tables is not None else {}
    table = tables.get(id(s.event))
    if table is None:
        table = tables[id(s.event)] = main_line_table(s.event)
    entity = "all" if ml.market == "handicap" and ml.entity in ("home", "away") else ml.entity
    main = table.get((ml.market, ml.period, ml.stat, entity))
    if main is None:
        return MAIN
    line = ml.line if (ml.market == "total" or ml.selection == "home") else -ml.line
    gap = abs(line - main)
    return MAIN if gap <= MAIN_LINE_TOLERANCE else SECONDARY if gap <= ALT_LINE_TOLERANCE else RESTRICTED


def book_standard_error(ml) -> float:
    """Standard error of the books' mean implied probability (0.0 when fewer than two books quoted)."""
    prices = [p for p in (list(ml.book_quotes.values()) or ml.book_prices or []) if p and p > 1.0]
    n = len(prices)
    if n < 2:
        return 0.0
    implied = [1.0 / p for p in prices]
    sd = (max(implied) - min(implied)) / _D2.get(n, 2.97)
    return sd / math.sqrt(n)


def uncertainty(ml, quality: str, has_sharp: bool, cfg) -> float:
    prior = {MAIN: cfg.se_main, SECONDARY: cfg.se_secondary, RESTRICTED: cfg.se_restricted}[quality]
    var = prior ** 2
    if ml.distinct_books() <= 1:
        var += cfg.se_single_book ** 2
    else:
        var += book_standard_error(ml) ** 2
    if not has_sharp:
        var += cfg.se_no_sharp ** 2
    return math.sqrt(var)


def edge_points(s) -> Optional[float]:
    """Settlement-aware return advantage divided by the reference offer price.
    Binary legacy rows use the equivalent fair-probability minus implied price.
    None when no sharp reference quoted the complete market.
    """
    if s.p_fair is None or s.edge_ev is None:
        return None
    if (s.notes.get('settlement') or {}).get('version') == 2:
        return s.edge_ev / s.notes['reference_price']
    return s.p_fair * s.edge_ev / (1.0 + s.edge_ev)


def assess(s, cfg, tables: Optional[Dict[int, dict]] = None) -> Evidence:
    ml = s.line
    quality = classify(s, tables)
    has_sharp = s.p_fair is not None and s.edge_ev is not None
    se = uncertainty(ml, quality, has_sharp, cfg)
    p_lcb = max(0.01, s.p_model - cfg.rank_z * se)
    edge = edge_points(s)
    rank = p_lcb + (cfg.edge_rank_weight * min(0.08, max(-0.06, edge)) if edge is not None else 0.0)
    return Evidence(books=ml.distinct_books(), providers=ml.provider_count(), dispersion=ml.price_dispersion(),
                    quality=quality, se=se, p_lcb=p_lcb, edge=edge, has_sharp=has_sharp, rank=rank)


def min_books(quality: str, cfg) -> int:
    """Distinct bookmakers a Daily leg of this market class needs."""
    return {MAIN: cfg.daily_min_books, SECONDARY: cfg.daily_secondary_min_books,
            RESTRICTED: cfg.daily_restricted_min_books}[quality]
