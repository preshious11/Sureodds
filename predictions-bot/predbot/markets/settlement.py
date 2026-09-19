"""Grade a pick against a final result.

Outcomes: won | lost | push | half_won | half_lost | void | pending
`pending` means the data needed is not available yet (e.g. corner counts).
"""
from __future__ import annotations

import logging
from typing import Callable, Dict, Optional, Set, Tuple

from predbot.providers.base import MarketLine, MatchResult
from predbot.providers.football_data import competition_for

log = logging.getLogger(__name__)

WON, LOST, PUSH, HALF_WON, HALF_LOST, VOID, PENDING = (
    "won", "lost", "push", "half_won", "half_lost", "void", "pending")


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

    if m in ("result3", "result2", "dnb", "dc", "ht_ft", "correct_score",
             "result_total", "result_btts", "clean_sheet", "win_to_nil", "winning_margin",
             "btts", "team_to_score") or (m in ("total", "handicap", "odd_even") and ml.stat == "points"):
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
        if ml.selection not in ("home", "away"):
            return VOID
        sel, opp = (h, a) if ml.selection == "home" else (a, h)
        return _handicap(sel, opp, ml.line)
    if m == "odd_even":
        st = (h, a) if ml.stat == "points" else res.stat(ml.stat, period)
        if st is None:
            return PENDING
        if ml.entity not in ("all", "home", "away"):
            return VOID
        tot = int(st[0] + st[1]) if ml.entity == "all" else int(st[0] if ml.entity == "home" else st[1])
        return WON if (tot % 2 == 0) == (ml.selection == "even") else LOST
    if m == "ht_ft":
        ht = res.score("1h")
        if ht is None:
            return PENDING
        parts = ml.selection.split("/")
        if len(parts) != 2:
            return VOID
        want_ht, want_ft = parts
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
        if ml.entity not in ("home", "away"):
            return VOID
        conceded = a if ml.entity == "home" else h
        cs = conceded == 0
        return WON if cs == (ml.selection == "yes") else LOST
    if m == "win_to_nil":
        if ml.entity not in ("home", "away"):
            return VOID
        won_nil = (h > a and a == 0) if ml.entity == "home" else (a > h and h == 0)
        return WON if won_nil == (ml.selection == "yes") else LOST
    if m == "team_to_score":
        if ml.entity not in ("home", "away"):
            return VOID
        scored = (h if ml.entity == "home" else a) > 0
        return WON if scored == (ml.selection == "yes") else LOST
    if m == "winning_margin":
        parts = ml.selection.split("_", 1)
        if len(parts) != 2:
            return VOID
        t, band = parts
        r = _result_of(h, a)
        margin = abs(h - a)
        if t == "draw":
            return WON if r == "draw" else LOST
        if r != t:
            return LOST
        if not _band_is_valid(band):
            return VOID
        return WON if _in_band(margin, band) else LOST
    return PENDING


def _band_is_valid(band: str) -> bool:
    band = band.replace("plus", "+")
    try:
        if band.endswith("+"):
            float(band[:-1])
        elif "-" in band:
            lo, hi = band.split("-", 1)
            float(lo)
            float(hi)
        else:
            float(band)
        return True
    except ValueError:
        return False


def _in_band(margin: float, band: str) -> bool:
    band = band.replace("plus", "+")
    if band.endswith("+"):
        return margin >= float(band[:-1])
    if "-" in band:
        lo, hi = band.split("-", 1)
        return float(lo) <= margin <= float(hi)
    return margin == float(band)


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


# ------------------------------------------------------------ results-path gate
#: Odds API sport-key prefixes whose /scores endpoint returns results.
ODDS_API_SCORES_PREFIXES = ("soccer_",)

#: Sports SportsGameOdds can look results up for (SPORT_TO_SGO) by teams + time.
SGO_RESULT_SPORTS = ("football", "basketball", "american_football", "baseball")

#: SharpAPI league slugs -> SportsGameOdds league ids, for leagues whose ids differ.
SHARP_TO_SGO_LEAGUE = {
    "nba": "NBA", "nfl": "NFL", "mlb": "MLB", "ncaab": "NCAAB", "ncaaf": "NCAAF", "mls": "MLS",
    "uefa_-_champions_league": "UEFA_CHAMPIONS_LEAGUE",
}


def _sgo_covers(event, sgo) -> bool:
    if getattr(event, "sport", "") not in SGO_RESULT_SPORTS:
        return False
    available = getattr(sgo, "_available_leagues", None)
    covered = set(available) if available else set(getattr(sgo, "leagues", None) or [])
    league_id = str(getattr(event, "league_id", "") or "")
    candidates = {league_id, league_id.upper(), SHARP_TO_SGO_LEAGUE.get(league_id.lower(), "")}
    return bool(covered & {c for c in candidates if c})


def results_path(event, cfg, providers: Dict) -> Optional[str]:
    """Which provider can settle a pick on this event, or None when none can.

    The single source of truth for "will this ever be gradable?" - the picker uses
    it to refuse candidates up front (``NO_RESULTS_PATH``) and the resolver uses it
    to explain a pick that stays pending. ``event`` only needs ``sport``, ``league``,
    ``league_id``, ``provider``, ``sources`` and ``extra`` (a stored pick works too).
    """
    providers = providers or {}
    sources: Set[str] = set(getattr(event, "sources", None) or []) | {getattr(event, "provider", "")}
    extra = getattr(event, "extra", None) or {}
    sgo = providers.get("sportsgameodds")
    if sgo is not None and "sportsgameodds" in sources:
        return "sportsgameodds"
    odds_api = providers.get("oddsapi")
    if odds_api is not None and "oddsapi" in sources \
            and str(extra.get("sport_key") or "").startswith(ODDS_API_SCORES_PREFIXES):
        return "oddsapi"
    football_data = providers.get("football_data")
    if football_data is not None and getattr(event, "sport", "") == "football" \
            and competition_for(str(getattr(event, "league_id", "") or ""), str(getattr(event, "league", "") or "")):
        return "football_data"
    if sgo is not None and _sgo_covers(event, sgo):
        return "sportsgameodds"
    return None


def make_results_gate(cfg, providers: Dict) -> Callable:
    """``gate(event) -> Optional[str]`` around results_path that logs each league
    without a results path once per run (per gate instance)."""
    logged: Set[str] = set()

    def gate(event) -> Optional[str]:
        path = results_path(event, cfg, providers)
        if path is None:
            league = str(getattr(event, "league_id", "") or getattr(event, "league", "") or "?")
            if league not in logged:
                logged.add(league)
                log.info("NO_RESULTS_PATH: no provider can settle %s (%s) - its picks are skipped",
                         league, getattr(event, "sport", "?"))
        return path

    return gate
