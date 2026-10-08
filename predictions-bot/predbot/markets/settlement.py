"""Grade a pick against a final result.

Outcomes: won | lost | push | half_won | half_lost | void | pending
`pending` means the data needed is not available yet (e.g. corner counts). A pick still pending at its settlement
deadline is stored as VOID with reason "settlement deadline expired" and reported as UNRESOLVED (jobs/stats.py).

Quarter lines (x.25 / x.75, totals, handicaps and the total leg of result & total) split into two half bets, so the
outcome can be half_won / half_lost; stats count a half as its side for the hit rate and pay half the stake in ROI.

Regulation vs overtime. ``full`` includes overtime / extra innings in every sport that has it; ``reg`` (a
regulation-only market) is graded only on a score known to be regulation (``MatchResult.score``):

* ESPN: ``full`` includes OT; ``reg`` and the overtime flag come from the period linescores (4 quarters, 2 halves in
  NCAAB, 9 innings). Without complete linescores regulation is unknown.
* EuroLeague: ``full`` includes OT; ``reg`` is the sum of the four quarters.
* SportsGameOdds: reports ``reg`` and ``game`` separately; ``full`` is derived from ``reg`` only when regulation did
  not end level (overtime only follows a tie).
* The Odds API ``/scores``: final score only - a regulation-only market cannot be graded from it.
* football-data.org / history: football only, where ``full`` is the 90 minutes and ``reg`` is graded on it.

A regulation market without a regulation score stays pending and, at its deadline, is reported unresolved - never
graded on an overtime-inclusive score.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import Callable, Dict, List, Optional, Set, Tuple

from predbot.util.names import is_doubles
from predbot.providers.base import MarketLine, MatchResult
from predbot.markets.competitions import (BY_ID, competition_for_event, lookup, normalise_period, path_periods,
                                          path_stats)

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

    if sport == "tennis" and ml.market in ("total", "handicap") and ml.stat == "points":
        ml = replace(ml, stat="games")            # a tennis total / handicap line is in GAMES, not sets
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
        if _result_of(h, a) != t:
            return LOST
        # the result leg is won: the bet is exactly the total leg - including a quarter line's half win / half
        # loss (it used to be flattened to a full LOSS) and a whole line's push
        return _cmp_line(h + a, ml.line, ou)
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
#: Sports SportsGameOdds can look results up for (SPORT_TO_SGO) by teams + time.
SGO_RESULT_SPORTS = ("football", "basketball", "american_football", "baseball")


@dataclass(frozen=True)
class ResultPath:
    """One way to settle a pick, with what it can grade. ``periods`` / ``stats`` of
    ``None`` mean unrestricted."""
    name: str
    periods: Optional[frozenset] = None
    stats: Optional[frozenset] = None

    def allows_period(self, period: str) -> bool:
        return self.periods is None or normalise_period(period) in self.periods

    def allows_stat(self, stat: str) -> bool:
        return self.stats is None or (stat or "points") in self.stats


def _path(name: str, comp) -> ResultPath:
    periods, stats = path_periods(name, comp), path_stats(name, comp)
    return ResultPath(name, None if periods is None else frozenset(periods),
                      None if stats is None else frozenset(stats))


def _sgo_covers(event, sgo, comp) -> bool:
    if getattr(event, "sport", "") not in SGO_RESULT_SPORTS or comp is None or not comp.sgo:
        return False
    available = getattr(sgo, "_available_leagues", None)
    covered = set(available) if available else set(getattr(sgo, "leagues", None) or [])
    return comp.sgo in covered


def result_paths(event, cfg, providers: Dict) -> List[ResultPath]:
    """Every provider that can settle a pick on this event, cheapest and most reliable
    first, each with the periods/stats it can grade. Empty = nothing can settle it.

    ``event`` only needs ``sport``, ``league``, ``league_id``, ``provider``, ``sources``
    and ``extra`` (a stored pick works too). Order: the origin SportsGameOdds id,
    football-data.org, ESPN, EuroLeague, The Odds API ``/scores`` (costs credits, so
    last), then a SportsGameOdds team+time lookup.
    """
    providers = providers or {}
    if getattr(event, "sport", "") == "tennis" and is_doubles(getattr(event, "home", ""), getattr(event, "away", "")):
        return []                             # ESPN's tennis parser grades singles only; nothing can settle doubles
    comp = competition_for_event(event)
    sources: Set[str] = set(getattr(event, "sources", None) or []) | {getattr(event, "provider", "")}
    extra = getattr(event, "extra", None) or {}
    out: List[ResultPath] = []
    sgo = providers.get("sportsgameodds")
    if sgo is not None and "sportsgameodds" in sources:
        out.append(_path("sportsgameodds", comp))
    if providers.get("football_data") is not None and getattr(event, "sport", "") == "football"             and comp is not None and comp.football_data:
        out.append(_path("football_data", comp))
    if providers.get("espn") is not None and comp is not None and comp.espn:
        out.append(_path("espn", comp))
    if providers.get("euroleague") is not None and comp is not None and comp.euroleague:
        out.append(_path("euroleague", comp))
    if providers.get("oddsapi") is not None and "oddsapi" in sources             and str(extra.get("sport_key") or "").startswith("soccer_"):
        out.append(_path("oddsapi", comp))
    if sgo is not None and not any(p.name == "sportsgameodds" for p in out) and _sgo_covers(event, sgo, comp):
        out.append(_path("sportsgameodds", comp))
    return out


def results_path(event, cfg, providers: Dict) -> Optional[str]:
    """Name of the first (preferred) path that can settle the event, or None."""
    paths = result_paths(event, cfg, providers)
    return paths[0].name if paths else None


def unsettleable_reason(paths, period: str, stat: str) -> Optional[str]:
    """``"period"`` / ``"stat"`` when no path can grade this market's period / stat, else None.
    ``paths`` may be ResultPaths or plain names (a name is taken as unrestricted)."""
    graders = [p for p in (paths or []) if isinstance(p, ResultPath)]
    if not graders or len(graders) < len(paths):
        return None
    if not any(p.allows_period(period) for p in graders):
        return "period"
    if not any(p.allows_period(period) and p.allows_stat(stat) for p in graders):
        return "stat"
    return None


def make_results_gate(cfg, providers: Dict) -> Callable:
    """``gate(event) -> Optional[List[ResultPath]]`` around result_paths that logs each
    league without any path once per run (per gate instance)."""
    logged: Set[str] = set()

    def gate(event):
        paths = result_paths(event, cfg, providers)
        if not paths:
            league = str(getattr(event, "league_id", "") or getattr(event, "league", "") or "?")
            if league not in logged:
                logged.add(league)
                log.info("NO_RESULTS_PATH: no provider can settle %s (%s) - its picks are skipped",
                         league, getattr(event, "sport", "?"))
            return None
        return paths

    return gate


def league_has_results(competition_id_or_comp, providers: Dict) -> bool:
    """Would an event of this competition (from its own fixture provider) have a results path?"""
    comp = competition_id_or_comp
    if isinstance(comp, str):
        comp = BY_ID.get(comp)
    if comp is None:
        return False
    for origin, ident in (("sharpapi", comp.sharp), ("oddsapi", comp.oddsapi), ("sportsgameodds", comp.sgo)):
        if not ident:
            continue
        event = SimpleNamespace(sport=comp.sport, league="", league_id=ident, provider=origin,
                                sources=[origin], extra={"sport_key": comp.oddsapi} if origin == "oddsapi" else {})
        if result_paths(event, None, providers):
            return True
    return False


def coverage_table(cfg, providers: Dict) -> List[Tuple[str, str, List[str]]]:
    """(provider, configured league identifier, path names) for every configured league."""
    rows: List[Tuple[str, str, List[str]]] = []
    lists = (("sharpapi", getattr(cfg, "sharpapi_leagues", []) or []),
             ("oddsapi", getattr(cfg, "odds_api_league_allowlist", []) or []),
             ("sportsgameodds", getattr(cfg, "sgo_leagues", []) or []))
    for origin, idents in lists:
        for ident in idents:
            comp = lookup(ident)
            event = SimpleNamespace(sport=comp.sport if comp else "", league="", league_id=ident, provider=origin,
                                    sources=[origin], extra={"sport_key": ident} if origin == "oddsapi" else {})
            rows.append((origin, ident, [p.name for p in result_paths(event, cfg, providers)]))
    return rows


def coverage_lines(cfg, providers: Dict) -> List[str]:
    """The startup results-coverage table: every configured league -> the paths that can
    settle it, or NONE (a config smell: picks there are rejected NO_RESULTS_PATH and the
    league is not fetched)."""
    lines = ["results coverage (configured league -> settlement path, in order):"]
    for origin, ident, paths in coverage_table(cfg, providers):
        lines.append(f"  {origin:<14} {ident:<38} -> {' > '.join(paths) if paths else 'NONE'}")
    return lines


def log_coverage(cfg, providers: Dict) -> None:
    for line in coverage_lines(cfg, providers):
        (log.warning if line.rstrip().endswith("NONE") else log.info)(line)
