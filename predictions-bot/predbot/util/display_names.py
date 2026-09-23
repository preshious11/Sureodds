"""Display-only city names for the US-team abbreviations SharpAPI uses ('BOS Red Sox', 'ARI Diamondbacks').

Applied at RENDER time only (pick blocks, /yesterday, the recap's best pick) - never in matching, ``fixture_key``,
storage or settlement, which keep working on the provider's own spelling. The table is per sport, so a football
club that merely starts with capitals (``PSV Eindhoven``, ``RCD Espanyol``, ``LA Galaxy``, ``DC United``) is never
touched.

Verification against the repo's data (see BROADCAST_REPORT.md): ARI, BOS, TOR (baseball) and PHO (basketball) appear
in ``tests/fixtures`` / the replay day; TEX in CROSS_TIER_REPORT.md. The rest are the standard SharpAPI/ESPN codes
and are NOT confirmed against any fixture in this repo.
"""
from __future__ import annotations

import re
from typing import Dict

_MLB: Dict[str, str] = {
    "ARI": "Arizona", "ATL": "Atlanta", "BAL": "Baltimore", "BOS": "Boston", "CHC": "Chicago", "CHI": "Chicago",
    "CWS": "Chicago", "CIN": "Cincinnati", "CLE": "Cleveland", "COL": "Colorado", "DET": "Detroit", "HOU": "Houston",
    "KC": "Kansas City", "LA": "Los Angeles", "LAA": "Los Angeles", "LAD": "Los Angeles", "MIA": "Miami",
    "MIL": "Milwaukee", "MIN": "Minnesota", "NY": "New York", "NYM": "New York", "NYY": "New York", "OAK": "Oakland",
    "PHI": "Philadelphia", "PIT": "Pittsburgh", "SD": "San Diego", "SEA": "Seattle", "SF": "San Francisco",
    "STL": "St. Louis", "TB": "Tampa Bay", "TEX": "Texas", "TOR": "Toronto", "WAS": "Washington", "WSH": "Washington",
}
_NBA: Dict[str, str] = {
    "ATL": "Atlanta", "BKN": "Brooklyn", "BOS": "Boston", "CHA": "Charlotte", "CHI": "Chicago", "CLE": "Cleveland",
    "DAL": "Dallas", "DEN": "Denver", "DET": "Detroit", "GS": "Golden State", "GSW": "Golden State", "HOU": "Houston",
    "IND": "Indiana", "LA": "Los Angeles", "LAC": "Los Angeles", "LAL": "Los Angeles", "MEM": "Memphis", "MIA": "Miami",
    "MIL": "Milwaukee", "MIN": "Minnesota", "NO": "New Orleans", "NOP": "New Orleans", "NY": "New York",
    "NYK": "New York", "OKC": "Oklahoma City", "ORL": "Orlando", "PHI": "Philadelphia", "PHO": "Phoenix",
    "PHX": "Phoenix", "POR": "Portland", "SA": "San Antonio", "SAC": "Sacramento", "SAS": "San Antonio",
    "TOR": "Toronto", "UTA": "Utah", "WAS": "Washington", "WSH": "Washington",
}
_NFL: Dict[str, str] = {
    "ARI": "Arizona", "ATL": "Atlanta", "BAL": "Baltimore", "BUF": "Buffalo", "CAR": "Carolina", "CHI": "Chicago",
    "CIN": "Cincinnati", "CLE": "Cleveland", "DAL": "Dallas", "DEN": "Denver", "DET": "Detroit", "GB": "Green Bay",
    "HOU": "Houston", "IND": "Indianapolis", "JAX": "Jacksonville", "KC": "Kansas City", "LA": "Los Angeles",
    "LAC": "Los Angeles", "LAR": "Los Angeles", "LV": "Las Vegas", "MIA": "Miami", "MIN": "Minnesota",
    "NE": "New England", "NO": "New Orleans", "NY": "New York", "NYG": "New York", "NYJ": "New York",
    "PHI": "Philadelphia", "PIT": "Pittsburgh", "SEA": "Seattle", "SF": "San Francisco", "TB": "Tampa Bay",
    "TEN": "Tennessee", "WAS": "Washington", "WSH": "Washington",
}

#: sport key (``Event.sport`` / ``picks.sport``) -> code table. Any sport not listed is never touched.
CITY_CODES: Dict[str, Dict[str, str]] = {"baseball": _MLB, "basketball": _NBA, "american_football": _NFL}

_LEADING_CODE = re.compile(r"^([A-Z]{2,3}) (\S.*)$")


def display_name(name: str, sport: str) -> str:
    """'BOS Red Sox' -> 'Boston Red Sox' for a code in the sport's table; anything else comes back unchanged."""
    table = CITY_CODES.get(sport)
    if not table or not name:
        return name
    m = _LEADING_CODE.match(name)
    if not m or m.group(1) not in table:
        return name
    return f"{table[m.group(1)]} {m.group(2)}"


def display_text(text: str, home: str, away: str, sport: str) -> str:
    """``text`` (an outcome like 'BOS Red Sox to Win') with each raw team name replaced by its display name."""
    for raw in (home, away):
        shown = display_name(raw, sport)
        if shown != raw and raw:
            text = text.replace(raw, shown)
    return text
