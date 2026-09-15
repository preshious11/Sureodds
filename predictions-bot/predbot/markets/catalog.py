"""Canonical market catalogue.

Every market the three APIs expose is mapped onto one of these keys. Markets
that can be *settled from a final score* are graded by predbot.markets.settlement;
markets that need a provider-supplied stat value (player props, first scorer,
cards...) are graded from the provider's `score` field when it exists.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

from predbot.providers.base import MarketLine


@dataclass(frozen=True)
class MarketSpec:
    key: str
    name: str
    ways: int                 # 2 or 3 (or 0 = many-way, e.g. correct score)
    group: str                # result | totals | handicap | btts | double_chance | exotic | props
    modelable: bool           # can the score model price it?
    score_settled: bool       # can it be graded from period scores / team stats?


CATALOG: Dict[str, MarketSpec] = {
    "result3":        MarketSpec("result3", "Match Result", 3, "result", True, True),
    "result2":        MarketSpec("result2", "Moneyline", 2, "result", True, True),
    "dnb":            MarketSpec("dnb", "Draw No Bet", 2, "result", True, True),
    "dc":             MarketSpec("dc", "Double Chance", 3, "double_chance", True, True),
    "btts":           MarketSpec("btts", "Both Teams To Score", 2, "btts", True, True),
    "total":          MarketSpec("total", "Over/Under", 2, "totals", True, True),
    "handicap":       MarketSpec("handicap", "Handicap", 2, "handicap", True, True),
    "odd_even":       MarketSpec("odd_even", "Odd/Even", 2, "exotic", True, True),
    "ht_ft":          MarketSpec("ht_ft", "Half Time / Full Time", 0, "exotic", True, True),
    "correct_score":  MarketSpec("correct_score", "Correct Score", 0, "exotic", True, True),
    "result_total":   MarketSpec("result_total", "Result & Over/Under", 0, "exotic", True, True),
    "result_btts":    MarketSpec("result_btts", "Result & BTTS", 0, "exotic", True, True),
    "clean_sheet":    MarketSpec("clean_sheet", "Clean Sheet", 2, "exotic", True, True),
    "win_to_nil":     MarketSpec("win_to_nil", "Win To Nil", 2, "exotic", True, True),
    "winning_margin": MarketSpec("winning_margin", "Winning Margin", 0, "exotic", True, True),
    "team_to_score":  MarketSpec("team_to_score", "Team To Score", 2, "exotic", True, True),
    "prop":           MarketSpec("prop", "Prop", 2, "props", False, False),
}

# selection "families" used for contradiction / diversity checks
OPPOSITE = {
    "home": "away", "away": "home",
    "over": "under", "under": "over",
    "yes": "no", "no": "yes",
    "even": "odd", "odd": "even",
}

STAT_NAMES = {
    "points": "", "goals": "Goals", "corners": "Corners", "cards": "Cards", "sets": "Sets",
    "games": "Games", "rounds": "Rounds", "hits": "Hits", "shots": "Shots",
}

PERIOD_NAMES = {
    "full": "", "reg": "Regulation", "1h": "1st Half", "2h": "2nd Half",
    "1q": "1st Qtr", "2q": "2nd Qtr", "3q": "3rd Qtr", "4q": "4th Qtr",
    "1p": "1st Period", "2p": "2nd Period", "3p": "3rd Period",
    "1s": "1st Set", "2s": "2nd Set", "1i5": "First 5 Innings",
}

SPORT_POINTS_WORD = {
    "football": "Goals", "hockey": "Goals", "handball": "Goals",
    "basketball": "Points", "american_football": "Points", "baseball": "Runs",
    "tennis": "Games", "mma": "Rounds",
}


def spec(market: str) -> MarketSpec:
    return CATALOG.get(market, CATALOG["prop"])


def _fmt_line(line: Optional[float], signed: bool = False) -> str:
    if line is None:
        return ""
    if abs(line - round(line)) < 1e-9:
        s = f"{int(round(line))}"
    else:
        s = f"{line:g}"
    if signed and line > 0:
        s = "+" + s
    return s


def selection_label(ml: MarketLine, home: str, away: str, sport: str = "football") -> str:
    """Human readable, e.g. 'Arsenal Win', 'Over 2.5 Goals', 'Lakers -4.5', 'BTTS Yes'."""
    if ml.label and ml.market == "prop":
        return ml.label
    m, s = ml.market, ml.selection
    team = {"home": home, "away": away}
    pts = SPORT_POINTS_WORD.get(sport, "Points")
    stat_word = STAT_NAMES.get(ml.stat, ml.stat.title()) or pts
    per = PERIOD_NAMES.get(ml.period, ml.period)
    suffix = f" ({per})" if per else ""

    if m in ("result3", "result2"):
        core = "Draw" if s == "draw" else f"{team.get(s, s)} Win"
    elif m == "dnb":
        core = f"{team.get(s, s)} (DNB)"
    elif m == "dc":
        core = {"home_draw": f"{home} or Draw", "away_draw": f"{away} or Draw",
                "home_away": f"{home} or {away}"}.get(s, s)
    elif m == "btts":
        core = f"BTTS {s.title()}"
    elif m == "total":
        ent = "" if ml.entity == "all" else f"{team.get(ml.entity, ml.entity)} "
        core = f"{ent}{s.title()} {_fmt_line(ml.line)} {stat_word}".strip()
    elif m == "handicap":
        core = f"{team.get(s, s)} {_fmt_line(ml.line, signed=True)}"
        if ml.stat not in ("points",):
            core += f" {stat_word}"
    elif m == "odd_even":
        core = f"{stat_word} {s.title()}"
    elif m == "ht_ft":
        a, b = (s.split("/") + [""])[:2]
        name = {"home": home, "draw": "Draw", "away": away}
        core = f"HT {name.get(a, a)} / FT {name.get(b, b)}"
    elif m == "correct_score":
        core = f"Correct Score {s}"
    elif m == "result_total":
        t, ou = s.split("_", 1) if "_" in s else (s, "")
        core = f"{team.get(t, 'Draw' if t == 'draw' else t)} & {ou.title()} {_fmt_line(ml.line)}"
    elif m == "result_btts":
        t, yn = s.split("_", 1) if "_" in s else (s, "")
        core = f"{team.get(t, 'Draw' if t == 'draw' else t)} & BTTS {yn.title()}"
    elif m == "clean_sheet":
        core = f"{team.get(ml.entity, ml.entity)} Clean Sheet {s.title()}"
    elif m == "win_to_nil":
        core = f"{team.get(ml.entity, ml.entity)} Win To Nil {s.title()}"
    elif m == "team_to_score":
        core = f"{team.get(ml.entity, ml.entity)} To Score {s.title()}"
    elif m == "winning_margin":
        t, band = s.split("_", 1) if "_" in s else (s, "")
        core = f"{team.get(t, t)} by {band.replace('_', '-')}"
    else:
        core = ml.label or f"{ml.market_name} {s}"
    return f"{core}{suffix}"


def short_selection(ml: MarketLine, home: str = "", away: str = "", sport: str = "football") -> str:
    """Compact outcome code for the picks card: '1', 'X2', 'Over 2.5 Goals', 'BTTS Yes'."""
    if ml.label and ml.market == "prop":
        return ml.label
    m, s = ml.market, ml.selection
    team = {"home": home, "away": away}
    pts = SPORT_POINTS_WORD.get(sport, "Points")
    stat_word = STAT_NAMES.get(ml.stat, ml.stat.title()) or pts
    per = PERIOD_NAMES.get(ml.period, ml.period)
    suffix = f" ({per})" if per and ml.period not in ("full", "reg") else ""

    if m in ("result3", "result2"):
        core = {"home": "1", "draw": "X", "away": "2"}.get(s, s)
    elif m == "dnb":
        core = {"home": "1", "away": "2"}.get(s, s) + " DNB"
    elif m == "dc":
        core = {"home_draw": "1X", "away_draw": "X2", "home_away": "12"}.get(s, s)
    elif m == "btts":
        core = f"BTTS {s.title()}"
    elif m == "total":
        if ml.line is not None:
            ent = f"{team.get(ml.entity, ml.entity)} " if ml.entity != "all" else ""
            core = f"{ent}{s.title()} {_fmt_line(ml.line)} {stat_word}".strip()
        else:
            core = ml.label or s.title()  # fallback: use label or selection if no line
    elif m == "handicap" and ml.line is not None:
        core = f"{team.get(s, s)} {_fmt_line(ml.line, signed=True)}".strip()
        if ml.stat not in ("points",):
            core += f" {stat_word}"
    elif m == "odd_even":
        core = f"{stat_word} {s.title()}"
    elif m == "ht_ft":
        a, b = (s.split("/") + [""])[:2]
        name = {"home": "1", "draw": "X", "away": "2"}
        core = f"HT {name.get(a, a)} / FT {name.get(b, b)}"
    elif m == "correct_score":
        core = f"Score {s}" if s else "Correct Score"
    elif m == "result_total":
        t, ou = s.split("_", 1) if "_" in s else (s, "")
        code = {"home": "1", "draw": "X", "away": "2"}.get(t, t)
        core = f"{code} & {ou.title()} {_fmt_line(ml.line)}".strip()
    elif m == "result_btts":
        t, yn = s.split("_", 1) if "_" in s else (s, "")
        code = {"home": "1", "draw": "X", "away": "2"}.get(t, t)
        core = f"{code} & BTTS {yn.title()}"
    elif m == "clean_sheet":
        core = f"{team.get(ml.entity, ml.entity)} Clean Sheet {s.title()}"
    elif m == "win_to_nil":
        core = f"{team.get(ml.entity, ml.entity)} Win To Nil {s.title()}"
    elif m == "team_to_score":
        core = f"{team.get(ml.entity, ml.entity)} To Score {s.title()}"
    elif m == "winning_margin":
        t, band = s.split("_", 1) if "_" in s else (s, "")
        core = f"{team.get(t, t)} by {band.replace('_', '-')}"
    else:
        core = ml.label or f"{ml.market_name or ml.market} {s}".strip() or s
    return f"{core}{suffix}"


def selection_family(ml: MarketLine) -> str:
    """Coarse class of the outcome, used to stop the bot leaning on one outcome
    type (e.g. 20 home wins). Values: home, away, draw, over, under, yes, no,
    other."""
    s = ml.selection
    if s in ("home", "away", "draw", "over", "under", "yes", "no"):
        return s
    if s.startswith("home"):
        return "home"
    if s.startswith("away"):
        return "away"
    return "other"


def contradicts(a: MarketLine, b: MarketLine) -> bool:
    """True when two lines on the same event cannot both win and are the same
    market (e.g. Over 2.5 & Under 2.5, Home & Away)."""
    if a.market_key() != b.market_key():
        return False
    if a.selection == b.selection:
        return False
    if OPPOSITE.get(a.selection) == b.selection:
        return True
    if a.market in ("result3", "result2", "dnb", "correct_score", "ht_ft"):
        return True  # different selections of a one-winner market
    if a.market == "dc":
        return False  # DC selections overlap; not strictly contradictory
    return False


def is_equivalent(a: MarketLine, b: MarketLine) -> bool:
    """Same outcome expressed twice (e.g. result2 home == dnb home == handicap home 0)."""
    if a.key() == b.key():
        return True
    norm_a, norm_b = _normalize_equiv(a), _normalize_equiv(b)
    return norm_a is not None and norm_a == norm_b


def _normalize_equiv(ml: MarketLine):
    if ml.market in ("result2", "dnb"):
        return ("win2", ml.period, ml.selection)
    if ml.market == "handicap" and ml.line is not None and abs(ml.line) < 1e-9:
        return ("win2", ml.period, ml.selection)
    if ml.market == "total" and ml.stat == "points":
        return ("total", ml.period, ml.entity, round(ml.line or 0, 2), ml.selection)
    return None
