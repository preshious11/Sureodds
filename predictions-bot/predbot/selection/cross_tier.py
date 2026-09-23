"""The cross-tier rule: the same match may appear in the Top ticket and in the Daily list, never with the same
tip. "Same tip" means the same selection, an equivalent one, or one NESTED inside the other (Arsenal to win /
Arsenal or draw / Arsenal DNB; Over 1.5 / Over 2.5 goals; the same side of a handicap at any line).

Every comparison is made on the Top leg's side of the fixture: when the two providers list home/away the other
way round, the candidate is mirrored first (home <-> away, a handicap line's sign, a team total's entity).
"""
from __future__ import annotations

import re
from dataclasses import replace
from typing import List, Optional, Tuple

from predbot.markets.catalog import contradicts, is_equivalent
from predbot.providers.base import MarketLine

DIFFERENT_OUTCOME = "different_outcome"
FORBID = "forbid"

DUPLICATE = "duplicate"
NESTED = "nested"
CONTRADICTS = "contradicts"

_SIDE = {"home": "away", "away": "home", "home_draw": "away_draw", "away_draw": "home_draw"}
_ENTITY = {"home": "away", "away": "home"}


def mirror(ml: MarketLine) -> MarketLine:
    """The same bet as the other provider would list it, with home and away exchanged (the handicap line
    stays on the same team - see ``MarketLine.mirrored``)."""
    return ml.mirrored()


def tip_signature(ml: MarketLine) -> Optional[Tuple]:
    """What the bet says once the line and the market wrapper are ignored - two lines with the same signature
    on one match are the same tip. ``None`` = no nesting notion for this market (only the exact-duplicate
    checks apply)."""
    sel = ml.selection
    if ml.market in ("result3", "result2", "dnb") and sel in ("home", "away"):
        return ("win", ml.period, sel)
    if ml.market == "dc" and sel in ("home_draw", "away_draw"):
        return ("win", ml.period, sel.split("_")[0])
    if ml.market == "handicap" and sel in ("home", "away"):
        return ("handicap", ml.period, ml.stat, ml.entity, sel)
    if ml.market == "total" and sel in ("over", "under"):
        return ("total", ml.period, ml.stat, ml.entity, sel)
    return None


def relation(top: MarketLine, cand: MarketLine, swapped: bool = False) -> Optional[str]:
    """How ``cand`` (a Daily candidate) relates to the Top leg ``top`` on the same match."""
    c = mirror(cand) if swapped else cand
    if top.key() == c.key() or is_equivalent(top, c):
        return DUPLICATE
    if contradicts(top, c):
        return CONTRADICTS
    sig = tip_signature(top)
    if sig is not None and sig == tip_signature(c):
        return NESTED
    return None


def violations(top: List, daily: List, links, mode: str) -> List[Tuple]:
    """(daily leg, top leg, reason) for every final-slate breach of the rule. ``links(event_a, event_b)``
    returns None (different matches), False (same match) or True (same match, sides swapped)."""
    out = []
    for d in daily:
        for t in top:
            swapped = links(t.event, d.event)
            if swapped is None:
                continue
            if mode == FORBID:
                out.append((d, t, DUPLICATE))
                continue
            rel = relation(t.line, d.line, swapped)
            if rel in (DUPLICATE, NESTED):
                out.append((d, t, rel))
    return out
