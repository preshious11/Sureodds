"""One table of competitions and every provider's identifier for each.

Each row lists how a competition is called by every provider that can supply
fixtures/odds (SharpAPI slug, Odds API sport key, SportsGameOdds league id) and
by every provider that can supply a RESULT (football-data.org code, ESPN
``sport/league`` slug, EuroLeague competition code). The results gate, the
resolver and the startup coverage log all read this table, so adding a league or
a results source is a one-row change.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Set


@dataclass(frozen=True)
class Competition:
    id: str
    sport: str
    sharp: str = ""            # SharpAPI league slug
    oddsapi: str = ""          # The Odds API sport key
    sgo: str = ""              # SportsGameOdds leagueID
    football_data: str = ""    # football-data.org competition code
    espn: str = ""             # ESPN "<sport>/<league>"
    euroleague: str = ""       # EuroLeague API competition code (E = EuroLeague, U = EuroCup)
    espn_groups: str = ""      # extra ``groups`` filter ESPN needs for this competition


def _c(*a, **k) -> Competition:
    return Competition(*a, **k)


COMPETITIONS: List[Competition] = [
    # ---- football
    _c("epl", "football", "england_-_premier_league", "soccer_epl", "", "PL", "soccer/eng.1"),
    _c("championship", "football", "england_-_championship", "soccer_efl_champ", "", "ELC", "soccer/eng.2"),
    _c("la_liga", "football", "spain_-_la_liga", "soccer_spain_la_liga", "", "PD", "soccer/esp.1"),
    _c("serie_a", "football", "italy_-_serie_a", "soccer_italy_serie_a", "", "SA", "soccer/ita.1"),
    _c("bundesliga", "football", "germany_-_bundesliga", "soccer_germany_bundesliga", "", "BL1", "soccer/ger.1"),
    _c("ligue_1", "football", "france_-_ligue_1", "soccer_france_ligue_one", "FR_LIGUE_1", "FL1", "soccer/fra.1"),
    _c("eredivisie", "football", "netherlands_-_eredivisie", "soccer_netherlands_eredivisie", "", "DED", "soccer/ned.1"),
    _c("primeira", "football", "portugal_-_primeira_liga", "soccer_portugal_primeira_liga", "", "PPL", "soccer/por.1"),
    _c("super_lig", "football", "turkey_-_super_lig", "soccer_turkey_super_league", "", "", "soccer/tur.1"),
    _c("ucl", "football", "uefa_-_champions_league", "soccer_uefa_champs_league", "UEFA_CHAMPIONS_LEAGUE", "CL",
       "soccer/uefa.champions"),
    _c("uel", "football", "uefa_-_europa_league", "soccer_uefa_europa_league", "", "", "soccer/uefa.europa"),
    _c("uecl", "football", "uefa_-_europa_conference_league", "soccer_uefa_europa_conference_league", "", "",
       "soccer/uefa.europa.conf"),
    _c("nigeria_npfl", "football", "nigeria_-_premier_league", "", "", "", "soccer/nga.1"),
    _c("mls", "football", "", "", "MLS", "", "soccer/usa.1"),
    _c("world_cup", "football", "", "soccer_fifa_world_cup", "", "WC", ""),
    _c("brazil_serie_a", "football", "", "soccer_brazil_campeonato", "", "BSA", ""),
    # ---- basketball
    _c("nba", "basketball", "nba", "", "NBA", "", "basketball/nba"),
    _c("ncaab", "basketball", "ncaab", "", "NCAAB", "", "basketball/mens-college-basketball", "", "50"),
    _c("euroleague", "basketball", "euroleague", "", "", "", "", "E"),
    # ---- american football
    _c("nfl", "american_football", "nfl", "", "NFL", "", "football/nfl"),
    _c("ncaaf", "american_football", "ncaaf", "", "NCAAF", "", "football/college-football", "", "80"),
    # ---- baseball
    _c("mlb", "baseball", "mlb", "", "MLB", "", "baseball/mlb"),
    # ---- tennis
    _c("atp", "tennis", "atp", "", "", "", "tennis/atp"),
    _c("wta", "tennis", "wta", "", "", "", "tennis/wta"),
]

BY_ID: Dict[str, Competition] = {c.id: c for c in COMPETITIONS}


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").lower()).strip("_")


_INDEX: Dict[str, Competition] = {}
for _comp in COMPETITIONS:
    for _key in (_comp.sharp, _comp.oddsapi, _comp.sgo):
        if _key:
            _INDEX[_norm(_key)] = _comp


def lookup(*identifiers: str) -> Optional[Competition]:
    """The competition any provider identifier (slug, key, league id, display name) belongs to."""
    for raw in identifiers:
        if raw:
            found = _INDEX.get(_norm(raw))
            if found is not None:
                return found
    return None


def competition_for_event(event) -> Optional[Competition]:
    """Resolve the competition of an event (or a stored pick) from whatever the
    origin provider recorded: ``extra["sport_key"]`` (Odds API), ``league_id``
    (SharpAPI slug / SGO league id), then the display ``league``."""
    extra = getattr(event, "extra", None) or {}
    return lookup(str(extra.get("sport_key") or ""), str(getattr(event, "league_id", "") or ""),
                  str(getattr(event, "league", "") or ""))


def by_espn(slug: str) -> Optional[Competition]:
    return next((c for c in COMPETITIONS if c.espn == slug), None)


# ------------------------------------------------------------------ capabilities
#: Periods / stats a results path can grade. ``None`` = unrestricted.
FULL: Set[str] = {"full"}
HALVES_QUARTERS: Set[str] = {"full", "1h", "2h", "1q", "2q", "3q", "4q"}


def path_periods(path: str, competition: Optional[Competition]) -> Optional[Set[str]]:
    if path == "sportsgameodds":
        return None
    if path == "football_data":
        return {"full", "1h", "2h"}
    if path == "espn":
        if competition is not None and competition.sport in ("basketball", "american_football"):
            return set(HALVES_QUARTERS)
        return set(FULL)                      # soccer, baseball, tennis: no per-period scores in the scoreboard
    if path == "euroleague":
        return set(HALVES_QUARTERS)
    return set(FULL)                          # oddsapi /scores, thesportsdb


def path_stats(path: str, competition: Optional[Competition]) -> Optional[Set[str]]:
    if path == "sportsgameodds":
        return None
    if path == "espn" and competition is not None and competition.sport == "tennis":
        return {"points", "games", "sets"}
    return {"points"}


def normalise_period(period: str) -> str:
    return "full" if period in ("", "reg", None) else period


def union(caps: Iterable[Optional[Set[str]]]) -> Optional[Set[str]]:
    """Union of capability sets; ``None`` (unrestricted) wins."""
    out: Set[str] = set()
    for c in caps:
        if c is None:
            return None
        out |= c
    return out
