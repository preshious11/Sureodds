"""Canonical market catalogue.

Every market the three APIs expose is mapped onto one of these keys. Markets
that can be *settled from a final score* are graded by predbot.markets.settlement;
markets that need a provider-supplied stat value (player props, first scorer,
cards...) are graded from the provider's `score` field when it exists.
"""
from __future__ import annotations

import re
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
    "1s": "1st Set", "2s": "2nd Set", "1i5": "First 5 Innings",
}

SPORT_POINTS_WORD = {
    "football": "Goals",
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


# Raw provider stat ids a punter should never have to decode ("Batting_Hits",
# "totalBases", "player_hits_over_under"...) mapped to plain-English words.
PROP_STAT_WORDS: Dict[str, str] = {
    "hits": "Hits", "batting_hits": "Hits", "total_bases": "Total Bases",
    "totalbases": "Total Bases", "runs": "Runs", "rbi": "RBIs",
    "runs_batted_in": "RBIs", "home_runs": "Home Runs", "homeruns": "Home Runs",
    "strikeouts": "Strikeouts", "strikeouts_thrown": "Strikeouts",
    "pitcher_strikeouts": "Strikeouts", "batter_strikeouts": "Strikeouts",
    "walks": "Walks", "doubles": "Doubles", "triples": "Triples",
    "rbi": "RBIs", "rbis": "RBIs",
    "earned_runs": "Earned Runs", "points": "Points", "rebounds": "Rebounds",
    "assists": "Assists", "three_pointers": "Three-Pointers",
    "threepointers": "Three-Pointers", "three_pointers_made": "Three-Pointers",
    "steals": "Steals", "blocks": "Blocks", "goals": "Goals", "shots": "Shots",
    "shots_on_target": "Shots on Target", "corners": "Corners", "cards": "Cards",
    "saves": "Saves", "touchdowns": "Touchdowns", "passing_yards": "Passing Yards",
    "rushing_yards": "Rushing Yards", "receiving_yards": "Receiving Yards",
    "receptions": "Receptions", "passing_tds": "Passing TDs",
    "field_goals": "Field Goals", "made_field_goals": "Field Goals",
}


def readable_stat(stat: str) -> str:
    """A raw stat id as readable text: 'Batting_Hits' -> 'Hits',
    'totalBases' -> 'Total Bases'. Unknown ids are split into words, never
    shown with underscores or camelCase."""
    s = str(stat or "").strip()
    if not s:
        return ""
    key = re.sub(r"[\s\-]+", "_", s).lower()
    if key in PROP_STAT_WORDS:
        return PROP_STAT_WORDS[key]
    if key in STAT_NAMES and STAT_NAMES[key]:
        return STAT_NAMES[key]
    words = re.sub(r"(?<!^)(?=[A-Z])", " ", s).replace("_", " ").split()
    out = " ".join(w for w in words if w).strip()
    return out.title() if out else s


_ACRONYMS = {"uefa", "fifa", "nba", "nfl", "mlb", "ncaab", "ncaaf", "wnba", "atp", "wta", "mls", "ufc", "efl",
             "uk", "usa", "pga", "itf", "cs2"}


def _words(text: str) -> str:
    out = []
    for w in re.split(r"[\s_]+", text.strip()):
        if not w:
            continue
        out.append(w.upper() if w.lower() in _ACRONYMS or len(w) == 1 else w.capitalize())
    return " ".join(out)


def pretty_league(slug: str, country: str = "") -> str:
    """A provider league slug as a card label: ``england_-_premier_league`` ->
    ``England • Premier League``; bare ids stay short (``nba`` -> ``NBA``).

    ``country`` (``event.extra["country"]``) wins over the slug's own prefix when
    the provider supplies one. Never leaves underscores or lower-case slugs."""
    raw = str(slug or "").strip()
    if not raw:
        return _words(country) if country else ""
    if "_-_" in raw:
        prefix, name = raw.split("_-_", 1)
    else:
        prefix, name = "", raw
    name_text = _words(name)
    country_text = _words(country) if country else _words(prefix)
    return f"{country_text} \u2022 {name_text}" if country_text and name_text else (name_text or country_text)


def prop_outcome(ml: MarketLine, home: str = "", away: str = "") -> str:
    """ONE canonical outcome builder for prop markets.

    Player props carry the player's name; team props carry the team; both end
    with the explicit side, line and stat: 'Alec Bohm Over 1.5 Hits',
    'Los Angeles Dodgers Over 8.5 Team Hits'. Returns '' when the canonical
    fields cannot produce an explicit outcome - an ambiguous candidate is
    skipped by the renderers, never shown as a bare 'Over'/'Yes'.
    """
    s = " ".join(str(ml.selection or "").split()).lower()
    team = {"home": home, "away": away}
    entity = str(ml.entity or "").strip()
    who = team.get(entity, entity)
    word = readable_stat(ml.stat)
    if s in ("over", "under"):
        if ml.line is None or not word or not who:
            return ""
        if entity in ("home", "away") and word.lower() == "hits":
            word = "Team Hits"          # a team's hit total, not a single player's
        return f"{who} {s.title()} {_fmt_line(ml.line)} {word}".strip()
    if s in ("yes", "no"):
        if not word and ml.label:
            word = readable_stat(ml.label)
        if not word or not who:
            return ""
        return f"{who} {word} {s.title()}"
    if s in ("even", "odd") and word and who:
        return f"{who} {word} {s.title()}"
    return ""


def selection_label(ml: MarketLine, home: str, away: str, sport: str = "football") -> str:
    """Human readable, e.g. 'Arsenal to Win', 'Over 2.5 Goals', 'Lakers -4.5', 'BTTS Yes'."""
    if ml.market == "prop":
        # Built from the canonical fields; the provider's market name is only a
        # last-resort STORAGE fallback, never an ambiguous user-facing outcome.
        return prop_outcome(ml, home, away) or str(ml.label or ml.market_name or "").strip()
    m, s = ml.market, ml.selection
    team = {"home": home, "away": away}
    pts = SPORT_POINTS_WORD.get(sport, "Points")
    stat_word = STAT_NAMES.get(ml.stat, readable_stat(ml.stat)) or pts
    per = PERIOD_NAMES.get(ml.period, ml.period)
    suffix = f" ({per})" if per else ""

    if m in ("result3", "result2"):
        core = "Draw" if s == "draw" else f"{team.get(s, s)} to Win"
        if ml.stat not in ("points",):
            # "Corners 1X2" / "Cards 1X2" settle on a count, not the score -
            # a bare "X to Win" would read as the match result.
            core += f" ({stat_word})"
    elif m == "dnb":
        core = f"{team.get(s, s)} (DNB)"
        if ml.stat not in ("points",):
            core += f" ({stat_word})"
    elif m == "dc":
        core = {"home_draw": f"{home} or Draw", "away_draw": f"{away} or Draw",
                "home_away": f"{home} or {away}"}.get(s, s)
    elif m == "btts":
        core = f"BTTS {s.title()}"
    elif m == "total":
        if ml.line is None or ml.entity not in ("all", "home", "away"):
            return ""  # no line, or an entity we can't attribute -> skip, never guess
        if ml.entity == "all":
            # "Over 0.5 Goals" alone doesn't say whose goals - a match total
            # needs to say so explicitly, same as a team total names the team.
            core = f"{s.title()} {_fmt_line(ml.line)} Total {stat_word}".strip()
        else:
            core = f"{team[ml.entity]} {s.title()} {_fmt_line(ml.line)} {stat_word}".strip()
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
    """Compact outcome code for the picks card: '1', 'X2', 'Over 2.5 Goals', 'BTTS Yes'.

    Props are ALWAYS built from the canonical fields here (never the provider's
    market name); an ambiguous prop - no line, no stat, no entity - returns ''
    so the renderer can skip it instead of showing a bare 'Over'/'Under'.
    """
    if ml.market == "prop":
        return prop_outcome(ml, home, away)
    m, s = ml.market, ml.selection
    team = {"home": home, "away": away}
    pts = SPORT_POINTS_WORD.get(sport, "Points")
    stat_word = STAT_NAMES.get(ml.stat, readable_stat(ml.stat)) or pts
    per = PERIOD_NAMES.get(ml.period, ml.period)
    suffix = f" ({per})" if per and ml.period not in ("full", "reg") else ""

    if m in ("result3", "result2"):
        core = {"home": "1", "draw": "X", "away": "2"}.get(s, s)
        if ml.stat not in ("points",):
            core += f" {stat_word}"
    elif m == "dnb":
        core = {"home": "1", "away": "2"}.get(s, s) + " DNB"
        if ml.stat not in ("points",):
            core += f" {stat_word}"
    elif m == "dc":
        core = {"home_draw": "1X", "away_draw": "X2", "home_away": "12"}.get(s, s)
    elif m == "btts":
        core = f"BTTS {s.title()}"
    elif m == "total":
        if ml.line is None or ml.entity not in ("all", "home", "away"):
            return ""  # no line, or an entity we can't attribute -> skip, never guess
        if ml.entity == "all":
            core = f"{s.title()} {_fmt_line(ml.line)} Total {stat_word}".strip()
        else:
            core = f"{team[ml.entity]} {s.title()} {_fmt_line(ml.line)} {stat_word}".strip()
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


#: Match-result markets whose selection implies a subset of {home, draw, away}
#: - used to catch cross-market contradictions (e.g. "Arsenal to Win" and
#: "Chelsea or Draw" on the same match) that same-market_key comparison alone
#: cannot see because result3/result2/dnb/dc are different markets.
_RESULT_OUTCOMES = {
    "result3": {"home": {"home"}, "draw": {"draw"}, "away": {"away"}},
    "result2": {"home": {"home"}, "away": {"away"}},
    "dnb": {"home": {"home"}, "away": {"away"}},
    "dc": {"home_draw": {"home", "draw"}, "away_draw": {"away", "draw"}, "home_away": {"home", "away"}},
}


def _implied_result(ml: MarketLine) -> Optional[set]:
    return _RESULT_OUTCOMES.get(ml.market, {}).get(ml.selection)


def contradicts(a: MarketLine, b: MarketLine) -> bool:
    """True when two lines on the same event cannot both win.

    Same-market cases (e.g. Over 2.5 & Under 2.5, Home & Away) are compared by
    market_key(); result3/result2/dnb/dc selections are additionally compared
    across markets via their implied {home, draw, away} outcome set, since a
    one-winner result and a double-chance pick can contradict even though they
    are technically different markets.
    """
    if a.market_key() == b.market_key():
        if a.selection == b.selection:
            return False
        if OPPOSITE.get(a.selection) == b.selection:
            return True
        if a.market in ("result3", "result2", "dnb", "correct_score", "ht_ft"):
            return True  # different selections of a one-winner market
        return False  # DC-vs-DC (same market) selections overlap; not strictly contradictory
    if a.period == b.period and a.entity == b.entity:
        oa, ob = _implied_result(a), _implied_result(b)
        if oa is not None and ob is not None:
            return not (oa & ob)
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
    return None
