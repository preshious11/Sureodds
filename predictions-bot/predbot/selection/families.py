"""Main-line detection: of a fixture's quoted total / handicap lines, the one nearest to even probability.

Used by the evidence tiers (a line near the main line is a MAIN-quality market). The All Predictions representatives use the
same principle on the model's own probabilities (``selection.representatives``).
"""
from __future__ import annotations

from collections import defaultdict
from typing import Dict, Tuple


def _devig2(price_a: float, price_b: float) -> float:
    """De-vigged probability of side A in a two-way market."""
    a, b = 1.0 / price_a, 1.0 / price_b
    return a / (a + b)


def main_line_table(event) -> Dict[Tuple[str, str, str, str], float]:
    """(market, period, stat, entity) -> the MAIN line of that market on this event.

    Main = the line whose two sides' de-vigged probabilities are closest to 0.5; a lone
    line is the main line. Handicaps are keyed by the HOME line (an away +1.5 is the same
    line as a home -1.5)."""
    groups: Dict[tuple, Dict[float, Dict[str, float]]] = defaultdict(lambda: defaultdict(dict))
    for m in event.markets:
        if m.line is None or m.price is None or m.price <= 1.0:
            continue
        if m.market == "total" and m.selection in ("over", "under"):
            groups[("total", m.period, m.stat, m.entity)][m.line][m.selection] = m.price
        elif m.market == "handicap" and m.selection in ("home", "away"):
            home_line = m.line if m.selection == "home" else -m.line
            entity = "all" if m.entity in ("home", "away") else m.entity
            groups[("handicap", m.period, m.stat, entity)][home_line][m.selection] = m.price
    table: Dict[tuple, float] = {}
    for key, lines in groups.items():
        first, second = ("over", "under") if key[0] == "total" else ("home", "away")
        both = [(ln, sides) for ln, sides in lines.items() if first in sides and second in sides]
        if both:
            table[key] = min(both, key=lambda x: abs(_devig2(x[1][first], x[1][second]) - 0.5))[0]
        else:
            # one-sided lines only: the one priced nearest evens is the closest to the middle
            table[key] = min(lines, key=lambda ln: abs(1.0 / next(iter(lines[ln].values())) - 0.5))
    return table
