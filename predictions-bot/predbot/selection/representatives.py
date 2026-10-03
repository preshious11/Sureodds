"""One eligible representative per FIXTURE x MARKET FAMILY (the All Predictions pool).

A fixture may have several All Predictions, at most one per market family. Families do not compete for one slot, so no
probability is ever ranked against another family's. Eligibility follows the mathematical structure of each market:

  1X2              the MODAL outcome of home / draw / away. No probability floor: the best a three-way market can say.
                   With an incomplete 1X2 only a line above 50% qualifies (it is then necessarily the mode).
  Moneyline        the favourite side (more likely to win than to lose).
  Double Chance    the pair covering the TWO MOST LIKELY 1X2 outcomes (the excluded outcome is the least likely one).
                   Read from the fixture's 1X2 when it is complete, else from its three double-chance lines.
  Draw No Bet      the side more likely to win than to lose; a draw is void.
  BTTS             the likelier Yes/No side, and it must be above 50%.
  Over/Under       the MAIN LINE (below), then the likelier side of it.
  Handicap         the MAIN LINE, then the likelier side of it - kept only when it is a genuine margin line
                   (|line| >= 1) and can still win together with every other representative of the fixture.

Main line. Of the lines quoted on both sides, the one whose likelier side is closest to even (the market's own definition of
its central line; same principle as ``families.main_line_table``). A handicap's home line L and away line -L are ONE line
seen from either side. Lines quoted on one side only are used only when no line has both. A probability is the production
``p_model``; for a push line it already is ``P(win) + 0.5 P(push)``, so ``p > 0.5`` means exactly "win more likely than
lose", with or without pushes. A far line (Over 0.5, +2.5) is more probable but never "main", so it is never chosen for
being probable. A perfectly balanced main line has no likelier side and the family is omitted.

A family with no eligible representative is omitted for that fixture, with a recorded reason. Team totals, period markets,
corners / cards and props are outside this pool. Nothing here uses odds, books, EV, history or the independent model to
choose; bookmaker coverage is only a deterministic tie-break between otherwise identical candidates.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from predbot.markets.settlement import HALF_WON, WON, grade
from predbot.providers.base import MatchResult

#: stable display / storage order
FAMILIES = ("1X2", "Moneyline", "Double Chance", "Draw No Bet", "Handicap", "Over/Under", "BTTS")
FAMILY_OF_MARKET = {"result3": "1X2", "result2": "Moneyline", "dc": "Double Chance", "dnb": "Draw No Bet",
                    "handicap": "Handicap", "total": "Over/Under", "btts": "BTTS"}
FAMILY_ORDER = {market: i for i, market in enumerate(("result3", "result2", "dc", "dnb", "handicap", "total", "btts"))}

MIN_HANDICAP_LINE = 1.0                 # smaller handicap lines (0, +-0.5) restate draw-no-bet / 1X2 / double chance
_SCORE_GRID = [(h, a) for h in range(9) for a in range(9)]

# omission reasons
NO_ELIGIBLE_SIDE = "no_eligible_side"
INCOMPLETE_RESULT = "incomplete_result_market"
BALANCED_MAIN_LINE = "balanced_main_line"
HANDICAP_RESTATES_RESULT = "handicap_line_below_1"
HANDICAP_CANNOT_WIN_WITH = "handicap_cannot_win_with_other_prediction"


def in_pool(s) -> bool:
    """Candidates the representative pool considers: full match, on the score, one of the seven families; totals on the whole
    match (team totals are outside this pool); no player props."""
    ml = s.line
    if ml.market not in FAMILY_OF_MARKET or ml.period != "full" or ml.stat != "points" or ml.is_player_prop:
        return False
    if ml.market in ("total", "btts") and ml.entity != "all":
        return False
    return True


def _best(cands: Iterable):
    """Deterministic best of equals: probability, then bookmaker coverage, then the line key."""
    return max(cands, key=lambda s: (s.p_model, s.line.distinct_books(), str(s.line.key())))


def _result3(cands: Sequence) -> Tuple[Optional[object], str]:
    one = {s.line.selection: _best(c for c in cands if c.line.selection == s.line.selection)
           for s in cands if s.line.selection in ("home", "draw", "away")}
    best = max(one.values(), key=lambda s: (s.p_model, s.line.distinct_books(), str(s.line.key()))) if one else None
    if len(one) == 3:
        return best, "modal outcome"
    if best is not None and best.p_model > 0.5:
        return best, "modal outcome (above 50%, so necessarily the mode)"      # the other two outcomes sum to < 50%
    return None, INCOMPLETE_RESULT


def _double_chance(dc: Sequence, one_x_two: Dict[str, object]) -> Tuple[Optional[object], str]:
    excluded = {"home_draw": "away", "away_draw": "home", "home_away": "draw"}
    mine = {s.line.selection: _best(c for c in dc if c.line.selection == s.line.selection)
            for s in dc if s.line.selection in excluded}
    if not mine:
        return None, NO_ELIGIBLE_SIDE
    if len(one_x_two) == 3:
        least = min(one_x_two.values(), key=lambda s: (s.p_model, str(s.line.key()))).line.selection
        pick = [s for sel, s in mine.items() if excluded[sel] == least]
        return (pick[0], "covers the two most likely outcomes") if pick else (None, NO_ELIGIBLE_SIDE)
    if len(mine) == 3:                     # all three pairs quoted: the likeliest pair excludes the least likely outcome
        return max(mine.values(), key=lambda s: (s.p_model, str(s.line.key()))), "covers the two most likely outcomes"
    return None, INCOMPLETE_RESULT


def _likelier_side(cands: Sequence) -> Tuple[Optional[object], str]:
    """Binary market: the likelier side, which must be more likely than its opposite (p > 0.5)."""
    best = _best(cands)
    return (best, "likelier side") if best.p_model > 0.5 else (None, NO_ELIGIBLE_SIDE)


def _line_key(s) -> float:
    ml = s.line
    return ml.line if (ml.market == "total" or ml.selection == "home") else -ml.line


def _main_line(cands: Sequence, market: str) -> Tuple[Optional[object], str]:
    groups: Dict[float, List] = defaultdict(list)
    for s in cands:
        if s.line.line is not None:
            groups[round(_line_key(s), 2)].append(s)
    if not groups:
        return None, NO_ELIGIBLE_SIDE
    first, second = ("over", "under") if market == "total" else ("home", "away")
    both = {k: v for k, v in groups.items() if {first, second} <= {s.line.selection for s in v}}
    pool = both or groups

    def balance(item):
        k, v = item
        return (abs(max(s.p_model for s in v) - 0.5), -max(s.line.distinct_books() for s in v), abs(k), k)

    k, members = min(pool.items(), key=balance)
    best = _best(members)
    if best.p_model <= 0.5:
        return None, BALANCED_MAIN_LINE
    return best, f"main line {k:g}"


def _can_win_together(a, b, sport: str) -> bool:
    for h, aw in _SCORE_GRID:
        res = MatchResult(status="final", scores={"full": (h, aw)})
        if all(grade(s.line, res, sport) in (WON, HALF_WON) for s in (a, b)):
            return True
    return False


def _keep_handicap(handicap, others: Sequence, sport: str) -> Optional[str]:
    """Reason to drop the handicap representative, or None to keep it."""
    if abs(handicap.line.line) < MIN_HANDICAP_LINE:
        return HANDICAP_RESTATES_RESULT
    if sport == "football":
        for other in others:
            if not _can_win_together(handicap, other, sport):
                return HANDICAP_CANNOT_WIN_WITH
    return None


def event_representatives(cands: Sequence) -> Tuple[Dict[str, object], Dict[str, str]]:
    """({family: representative Scored}, {family: omission reason}) for ONE fixture's valid candidates."""
    pool = [s for s in cands if in_pool(s)]
    by_market: Dict[str, List] = defaultdict(list)
    for s in pool:
        by_market[s.line.market].append(s)
    reps: Dict[str, Tuple[object, str]] = {}
    omitted: Dict[str, str] = {}

    def record(market: str, outcome: Tuple[Optional[object], str]) -> None:
        rep, note = outcome
        family = FAMILY_OF_MARKET[market]
        if rep is None:
            omitted[family] = note
        else:
            reps[market] = (rep, note)

    one_x_two: Dict[str, object] = {}
    if "result3" in by_market:
        rep, note = _result3(by_market["result3"])
        record("result3", (rep, note))
        one_x_two = {sel: _best(c for c in by_market["result3"] if c.line.selection == sel)
                     for sel in ("home", "draw", "away") if any(c.line.selection == sel for c in by_market["result3"])}
    if "result2" in by_market:
        record("result2", _likelier_side(by_market["result2"]))
    if "dc" in by_market:
        record("dc", _double_chance(by_market["dc"], one_x_two))
    if "dnb" in by_market:
        record("dnb", _likelier_side(by_market["dnb"]))
    if "btts" in by_market:
        record("btts", _likelier_side(by_market["btts"]))
    if "total" in by_market:
        record("total", _main_line(by_market["total"], "total"))
    if "handicap" in by_market:
        record("handicap", _main_line(by_market["handicap"], "handicap"))
    if "handicap" in reps:
        h = reps["handicap"][0]
        why = _keep_handicap(h, [s for m, (s, _) in reps.items() if m != "handicap"], h.event.sport)
        if why:
            del reps["handicap"]
            omitted["Handicap"] = why
    out: Dict[str, object] = {}
    for market in sorted(reps, key=lambda m: FAMILY_ORDER[m]):
        s, note = reps[market]
        s.notes["representative"] = {"family": FAMILY_OF_MARKET[market], "rule": note}
        out[FAMILY_OF_MARKET[market]] = s
    return out, omitted


@dataclass
class Representatives:
    predictions: List = field(default_factory=list)
    by_family: Counter = field(default_factory=Counter)
    omitted: Dict[str, Counter] = field(default_factory=dict)          # family -> reason -> fixtures
    fixtures: int = 0
    without_representative: List[str] = field(default_factory=list)      # canonical event ids


def select_representatives(groups: Dict[str, Sequence]) -> Representatives:
    """``groups``: canonical_event_id -> that fixture's valid candidates. At most one prediction per (event, family),
    in fixture order and, within a fixture, the stable family order."""
    result = Representatives(fixtures=len(groups))
    for event_id, cands in groups.items():
        reps, omitted = event_representatives(cands)
        if not reps:
            result.without_representative.append(event_id)
        for family, s in reps.items():
            result.predictions.append(s)
            result.by_family[family] += 1
        for family, why in omitted.items():
            result.omitted.setdefault(family, Counter())[why] += 1
    return result
