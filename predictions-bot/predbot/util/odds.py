"""Odds format conversions and implied probabilities."""
from __future__ import annotations

from typing import Optional


def american_to_decimal(a) -> Optional[float]:
    try:
        a = float(str(a).replace("+", ""))
    except (TypeError, ValueError):
        return None
    if a == 0:
        return None
    if a > 0:
        return round(1.0 + a / 100.0, 4)
    return round(1.0 + 100.0 / abs(a), 4)


def decimal_to_american(d: float) -> int:
    if d >= 2.0:
        return int(round((d - 1.0) * 100))
    return int(round(-100.0 / (d - 1.0)))


def implied_prob(decimal_odds: float) -> float:
    return 1.0 / decimal_odds if decimal_odds and decimal_odds > 1.0 else 0.0


def fair_decimal(p: float) -> float:
    p = min(max(p, 1e-6), 1 - 1e-6)
    return 1.0 / p


def parse_decimal(v) -> Optional[float]:
    """Parse a decimal odds value from str/float; returns None if invalid (<=1)."""
    try:
        d = float(v)
    except (TypeError, ValueError):
        return None
    return d if d > 1.0 else None
