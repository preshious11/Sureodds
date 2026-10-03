"""Daily ranking families: rank each market family against ITS OWN peers.

``_daily_score`` is 0.60 x p_model plus small terms, so on any match the safest line
(+1.5, Over 1.5, double chance) always outscores the straight win or the real Over 2.5,
and "one pick per match" then always keeps the safe line. Nothing here changes a gate
or a cap: it only changes the ORDER in which the Daily builder is offered candidates,
by z-scoring the score within a family (win, main-line total, alternate handicap, ...).
"""
from __future__ import annotations

import logging
from collections import Counter, defaultdict
from statistics import mean, pstdev
from typing import Dict, List, Optional, Tuple

from predbot.markets.catalog import spec

log = logging.getLogger(__name__)

FAMILIES = ("win", "total_main", "total_alt", "handicap_main", "handicap_alt", "safety", "btts",
            "team_total", "period", "exotic", "prop", "other")

MIN_FAMILY_SIZE = 3          # smaller families borrow the whole pool's mean / std
MIN_STD = 0.02               # a family whose scores are all equal must not blow z up


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


def daily_family(s, tables: Dict[int, dict]) -> str:
    """The ranking family of one candidate (``tables`` caches main_line_table per event)."""
    ml = s.line
    if ml.period not in ("full", "reg", "", None):
        return "period"
    group = spec(ml.market).group
    if group == "exotic":
        return "exotic"
    if group == "props" or ml.market == "prop":
        return "prop"
    if ml.market in ("result3", "result2"):
        return "win"
    if ml.market in ("dc", "dnb"):
        return "safety"
    if ml.market == "btts":
        return "btts"
    if ml.market in ("total", "handicap"):
        if ml.market == "total" and ml.entity in ("home", "away"):
            return "team_total"
        if ml.stat != "points" or ml.line is None:
            return "other"
        table = tables.get(id(s.event))
        if table is None:
            table = tables[id(s.event)] = main_line_table(s.event)
        entity = "all" if ml.market == "handicap" and ml.entity in ("home", "away") else ml.entity
        key = (ml.market, ml.period, ml.stat, entity)
        line = ml.line if (ml.market == "total" or ml.selection == "home") else -ml.line
        main = table.get(key)
        return f"{ml.market}_{'main' if main is None or line == main else 'alt'}"
    return "other"


def order_by_family_z(pool: List, score) -> Tuple[List, Dict[str, int]]:
    """Return ``pool`` ordered by (z within family, score) descending, tagging each
    candidate's ``notes`` with its family and z. Also returns the family counts."""
    tables: Dict[int, dict] = {}
    families = [daily_family(s, tables) for s in pool]
    scores = [score(s) for s in pool]
    by_family: Dict[str, List[float]] = defaultdict(list)
    for fam, sc in zip(families, scores):
        by_family[fam].append(sc)
    everyone = (mean(scores), pstdev(scores)) if scores else (0.0, 0.0)
    stats = {fam: ((mean(v), pstdev(v)) if len(v) >= MIN_FAMILY_SIZE else everyone) for fam, v in by_family.items()}
    ranked = []
    for s, fam, sc in zip(pool, families, scores):
        mu, sd = stats[fam]
        z = (sc - mu) / max(sd, MIN_STD)
        s.notes["family"], s.notes["z"] = fam, round(z, 3)
        ranked.append((z, sc, s))
    ranked.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return [s for _, _, s in ranked], dict(Counter(families))


def family_counts(picks: List, tables: Optional[Dict[int, dict]] = None) -> Dict[str, int]:
    tables = tables if tables is not None else {}
    return dict(Counter(daily_family(s, tables) for s in picks))
