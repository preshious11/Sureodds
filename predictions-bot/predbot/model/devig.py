"""Remove bookmaker margin (vig) from a set of prices.

Two standard estimators are averaged:
  * multiplicative  (normalise implied probabilities)
  * power           (p_i = q_i ** k with k solved so sum == 1) - closer to how
                    books shade longshots, so it is fairer to underdogs.
"""
from __future__ import annotations

import math
from typing import Dict, Optional

TYPICAL_MARGIN = {2: 0.045, 3: 0.065, 0: 0.12}


def _power_devig(q: Dict[str, float], iters: int = 40) -> Dict[str, float]:
    lo, hi = 0.5, 3.0
    for _ in range(iters):
        k = (lo + hi) / 2
        s = sum(v ** k for v in q.values())
        if s > 1:
            lo = k
        else:
            hi = k
    k = (lo + hi) / 2
    return {s: v ** k for s, v in q.items()}


def devig(prices: Dict[str, float]) -> Dict[str, float]:
    """prices: selection -> decimal odds for *all* outcomes of one market."""
    q = {s: 1.0 / p for s, p in prices.items() if p and p > 1.0}
    if not q:
        return {}
    tot = sum(q.values())
    if tot <= 1.0:  # arbitrage / incomplete market: trust as is, normalise softly
        return {s: v / tot for s, v in q.items()}
    mult = {s: v / tot for s, v in q.items()}
    powr = _power_devig(q)
    return {s: 0.5 * (mult[s] + powr[s]) for s in q}


def single_side_prob(price: float, ways: int = 2) -> float:
    """Best-effort fair probability when only one side of a market is known."""
    margin = TYPICAL_MARGIN.get(ways, 0.08)
    q = 1.0 / price
    return max(0.01, min(0.99, q / (1.0 + margin)))


def logit(p: float) -> float:
    p = min(max(p, 1e-6), 1 - 1e-6)
    return math.log(p / (1 - p))


def sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1 / (1 + z)
    z = math.exp(x)
    return z / (1 + z)


def blend_logit(probs: Dict[str, float], weights: Dict[str, float]) -> Optional[float]:
    num = 0.0
    den = 0.0
    for k, p in probs.items():
        w = weights.get(k, 0.0)
        if w <= 0 or p is None:
            continue
        num += w * logit(p)
        den += w
    if den == 0:
        return None
    return sigmoid(num / den)
