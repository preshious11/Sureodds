"""Sharp fair probabilities and SportyBet edge. Pure functions: no I/O, no network."""
from __future__ import annotations

from typing import Dict, Optional, Tuple

from predbot.model.devig import shin_devig

#: Sharp books in preference order. Pinnacle first; Matchbook and Betfair Exchange are fallbacks.
SHARP_ORDER: Tuple[str, ...] = ("pinnacle", "matchbook", "betfair_ex_eu")
OUTCOMES: Tuple[str, ...] = ("Home", "Draw", "Away")


def fair_1x2_shin(prices: Dict[str, float]) -> Dict[str, float]:
    """Shin de-vig of one book's 1X2 decimal prices. ``prices`` maps Home/Draw/Away to decimal odds."""
    if set(prices) != set(OUTCOMES):
        raise ValueError("fair_1x2_shin needs exactly Home, Draw and Away")
    for k, v in prices.items():
        if not (isinstance(v, (int, float)) and v > 1.0):
            raise ValueError(f"invalid decimal price for {k}: {v!r}")
    fair = shin_devig(prices)
    if set(fair) != set(OUTCOMES):
        raise ValueError("Shin de-vig did not return all three outcomes")
    return {k: fair[k] for k in OUTCOMES}


def edge(sporty_price: float, fair_probability: float) -> float:
    """Edge = SportyBet price x fair probability - 1."""
    return sporty_price * fair_probability - 1.0


def sharp_books_1x2(book_prices: Dict[str, Dict[str, float]]) -> Dict[str, Dict[str, float]]:
    """Fair 1X2 for every sharp book present with a complete 1X2 quote, keyed by book.

    ``book_prices`` maps book key -> {Home, Draw, Away} decimal prices. Books missing any outcome are skipped."""
    out: Dict[str, Dict[str, float]] = {}
    for book in SHARP_ORDER:
        prices = book_prices.get(book)
        if prices and set(OUTCOMES) <= set(prices):
            out[book] = fair_1x2_shin({k: float(prices[k]) for k in OUTCOMES})
    return out


def primary_fair(fairs: Dict[str, Dict[str, float]]) -> Optional[Tuple[str, Dict[str, float]]]:
    """The first sharp book in preference order with a fair price, or None when no sharp price exists."""
    for book in SHARP_ORDER:
        if book in fairs:
            return book, fairs[book]
    return None
