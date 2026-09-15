"""Grade a pick against a final result.

Outcomes: won | lost | push | half_won | half_lost | void | pending
`pending` means the data needed is not available yet (e.g. corner counts).
"""
from __future__ import annotations

from typing import Optional, Tuple

from predbot.providers.base import MarketLine, MatchResult

WON, LOST, PUSH, HALF_WON, HALF_LOST, VOID, PENDING = (
    "won", "lost", "push", "half_won", "half_lost", "void", "pending")

FINAL_OUTCOMES = {WON, LOST, PUSH, HALF_WON, HALF_LOST, VOID}


def _cmp_line(value: float, line: float, side: str) -> str:
    """Grade an over/under-style comparison including Asian quarter lines."""
    # quarter lines (x.25 / x.75) split the stake between two neighbouring lines
    frac = abs(line - int(line)) if line >= 0 else abs(line - int(line))
    frac = round(frac, 2)
    if frac in (0.25, 0.75):
        lo = line - 0.25
        hi = line + 0.25
        r1 = _cmp_line(value, lo, side)
        r2 = _cmp_line(value, hi, side)
        return _combine_half(r1, r2)
    if side == "over":
        if value > line:
            return WON
        if value == line:
            return PUSH
        return LOST
    # under
    if value < line:
        return WON
    if value == line:
        return PUSH
    return LOST


def _combine_half(r1: str, r2: str) -> str:
    pair = {r1, r2}
    if pair == {WON}:
        return WON
    if pair == {LOST}:
        return LOST
    if pair == {WON, PUSH}:
        return HALF_WON
    if pair == {LOST, PUSH}:
        return HALF_LOST
    if pair == {PUSH}:
        return PUSH
    return LOST if LOST in pair else WON


def _handicap(sel_score: float, opp_score: float, line: float) -> str:
    """Selected team + line vs opponent (Asian handicap semantics)."""
    frac = round(abs(line - int(line)), 2)
    if frac in (0.25, 0.75):
        step = 0.25
        lo, hi = line - step, line + step
        return _combine_half(_handicap(sel_score, opp_score, lo), _handicap(sel_score, opp_score, hi))
    adj = sel_score + line
    if adj > opp_score:
        return WON
    if adj == opp_score:
        return PUSH
    return LOST


def _result_of(h: float, a: float) -> str:
    return "home" if h > a else "away" if a > h else "draw"


def grade(ml: MarketLine, res: MatchResult, sport: str = "football") -> str:
    if res.status in ("postponed", "cancelled"):
        return VOID
    if res.status != "final":
        return PENDING

    # Provider-supplied settled value (SportsGameOdds `score`) wins for props/exotics.
    if ml.provider_odd_id and ml.provider_odd_id in res.odd_scores:
        v = res.odd_scores[ml.provider_odd_id]
        out = _grade_from_value(ml, v, res)
        if out is not None:
            return out

    m = ml.market
    period = ml.period if ml.period != "reg" or sport != "football" else "full"

    if m in ("result3", "result2", "dnb", "dc", "handicap", "ht_ft", "correct_score",
             "result_total", "result_btts", "clean_sheet", "win_to_nil", "winning_margin",
             "btts", "odd_even", "team_to_score") or (m == "total" and ml.stat == "points"):
        sc = res.score(period)
        if sc is None:
            return PENDING
        h, a = sc

    if m == "result3":
        return WON if _result_of(h, a) == ml.selection else LOST
    if m in ("result2", "dnb"):
        r = _result_of(h, a)
        if r == "draw":
            # 2-way markets over a period that can end level push; full-game
            # markets in OT sports are graded on the 'full' score already.
            return PUSH
        return WON if r == ml.selection else LOST
    if m == "dc":
        r = _result_of(h, a)
        ok = {"home_draw": r in ("home", "draw"), "away_draw": r in ("away", "draw"),
              "home_away": r in ("home", "away")}.get(ml.selection)
        return WON if ok else LOST
    if m == "btts":
        both = h > 0 and a > 0
        return WON if (both and ml.selection == "yes") or (not both and ml.selection == "no") else LOST
    if m == "total":
        if ml.stat == "points":
            val = {"all": h + a, "home": h, "away": a}.get(ml.entity)
        else:
            st = res.stat(ml.stat, period)
            if st is None:
                return PENDING
            val = {"all": st[0] + st[1], "home": st[0], "away": st[1]}.get(ml.entity)
        if val is None or ml.line is None:
            return VOID
        return _cmp_line(val, ml.line, ml.selection)
    if m == "handicap":
        if ml.line is None:
            return VOID
        if ml.stat != "points":
            st = res.stat(ml.stat, period)
            if st is None:
                return PENDING
            h, a = st
        sel, opp = (h, a) if ml.selection == "home" else (a, h)
        return _handicap(sel, opp, ml.line)
    if m == "odd_even":
        st = (h, a) if ml.stat == "points" else res.stat(ml.stat, period)
        if st is None:
            return PENDING
        tot = int(st[0] + st[1]) if ml.entity == "all" else int(st[0] if ml.entity == "home" else st[1])
        return WON if (tot % 2 == 0) == (ml.selection == "even") else LOST
    if m == "ht_ft":
        ht = res.score("1h")
        if ht is None:
            return PENDING
        want_ht, want_ft = (ml.selection.split("/") + [""])[:2]
        return WON if _result_of(*ht) == want_ht and _result_of(h, a) == want_ft else LOST
    if m == "correct_score":
        try:
            wh, wa = [int(x) for x in ml.selection.replace(":", "-").split("-")]
        except ValueError:
            return VOID
        return WON if (int(h), int(a)) == (wh, wa) else LOST
    if m == "result_total":
        t, ou = ml.selection.split("_", 1)
        if ml.line is None:
            return VOID
        r_ok = _result_of(h, a) == t
        t_res = _cmp_line(h + a, ml.line, ou)
        if not r_ok or t_res == LOST:
            return LOST
        return t_res if t_res in (WON, PUSH) else LOST
    if m == "result_btts":
        t, yn = ml.selection.split("_", 1)
        both = h > 0 and a > 0
        return WON if _result_of(h, a) == t and both == (yn == "yes") else LOST
    if m == "clean_sheet":
        conceded = a if ml.entity == "home" else h
        cs = conceded == 0
        return WON if cs == (ml.selection == "yes") else LOST
    if m == "win_to_nil":
        won_nil = (h > a and a == 0) if ml.entity == "home" else (a > h and h == 0)
        return WON if won_nil == (ml.selection == "yes") else LOST
    if m == "team_to_score":
        scored = (h if ml.entity == "home" else a) > 0
        return WON if scored == (ml.selection == "yes") else LOST
    if m == "winning_margin":
        t, band = ml.selection.split("_", 1)
        r = _result_of(h, a)
        margin = abs(h - a)
        if t == "draw":
            return WON if r == "draw" else LOST
        if r != t:
            return LOST
        return WON if _in_band(margin, band) else LOST
    return PENDING


def _in_band(margin: float, band: str) -> bool:
    band = band.replace("plus", "+")
    if band.endswith("+"):
        return margin >= float(band[:-1])
    if "-" in band:
        lo, hi = band.split("-", 1)
        return float(lo) <= margin <= float(hi)
    try:
        return margin == float(band)
    except ValueError:
        return False


def _grade_from_value(ml: MarketLine, v: float, res: MatchResult) -> Optional[str]:
    """Grade using a provider-settled stat value for *this* selection's stat entity."""
    if ml.market == "total" or (ml.market == "prop" and ml.selection in ("over", "under") and ml.line is not None):
        return _cmp_line(v, ml.line, ml.selection)
    if ml.market == "odd_even" or (ml.market == "prop" and ml.selection in ("even", "odd")):
        return WON if (int(v) % 2 == 0) == (ml.selection == "even") else LOST
    if ml.market == "prop" and ml.selection in ("yes", "no"):
        return WON if (v > 0) == (ml.selection == "yes") else LOST
    # moneyline/handicap style props need the opposing value; fall through to score-based grading
    return None
