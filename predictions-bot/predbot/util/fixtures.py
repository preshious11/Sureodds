"""One real-world match, however many providers describe it.

``fixture_orientation`` decides whether two events are the same match (and whether one provider lists
home/away the other way round); ``fixture_key`` is the string identity of a match used by the picker,
``store_picks`` and the ``picks`` table's unique index.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Optional

from predbot.markets.competitions import competition_for_event
from predbot.util.names import canonical_team, same_team

KICKOFF_WINDOW_SECONDS = 3 * 3600


def _competition_id(event) -> str:
    comp = competition_for_event(event)
    return comp.id if comp else ""


def fixture_orientation(a, b, comp_a: Optional[str] = None, comp_b: Optional[str] = None) -> Optional[str]:
    """``None`` when ``a`` and ``b`` are different matches, ``"same"`` when they are one match listed with
    the same home/away order, ``"swapped"`` when the two providers list the sides the other way round.

    Same sport, kickoffs within 3 h, both teams matching (fuzzy ``same_team`` with the sport's rules). When
    both events resolve to a known competition and it is not the same one, they are never linked (a
    Premier League match cannot be a Championship one however alike the names)."""
    if a.sport != b.sport or not a.start or not b.start:
        return None
    if abs((a.start - b.start).total_seconds()) > KICKOFF_WINDOW_SECONDS:
        return None
    ca = _competition_id(a) if comp_a is None else comp_a
    cb = _competition_id(b) if comp_b is None else comp_b
    if ca and cb and ca != cb:
        return None
    sport = a.sport
    if same_team(a.home, b.home, sport=sport) and same_team(a.away, b.away, sport=sport):
        return "same"
    if same_team(a.home, b.away, sport=sport) and same_team(a.away, b.home, sport=sport):
        return "swapped"
    return None


def competition_id(event) -> str:
    return _competition_id(event)


def _key(sport: str, competition: str, home: str, away: str, start) -> str:
    pair = sorted((canonical_team(home, sport), canonical_team(away, sport)))
    stamp = (start + timedelta(minutes=30)).strftime("%Y%m%d%H") if start else ""     # nearest hour
    return f"{sport}|{competition}|{pair[0]}|{pair[1]}|{stamp}"


def fixture_key(event, competition: Optional[str] = None) -> str:
    """sport | competition (when known) | sorted canonical team pair | kickoff rounded to the hour.
    Identical for the two providers' views of one match whenever their names reduce to the same teams;
    ``fixture_orientation`` remains the authority for anything looser."""
    comp = _competition_id(event) if competition is None else competition
    return _key(event.sport, comp, event.home, event.away, event.start)
