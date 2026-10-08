"""One real-world match, however many providers describe it.

``fixture_orientation`` decides whether two events are the same match (and whether one provider lists
home/away the other way round); ``fixture_key`` is the string identity of a match used by the picker,
``store_picks`` and the ``picks`` table's unique index.
"""
from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace
from typing import Optional

from predbot.markets.competitions import competition_for_event
from predbot.util.names import canonical_team, same_participant

KICKOFF_WINDOW_SECONDS = 3 * 3600
#: Sports where the same two teams can meet twice in one day (a baseball doubleheader's second game starts about
#: three hours after the first): two providers' kickoffs for ONE game differ by minutes, so the tolerance there is
#: kept well below the gap between two games.
SPORT_KICKOFF_WINDOW_SECONDS = {"baseball": 2 * 3600}
#: When one provider itself lists the same pairing more than once around that time (a doubleheader / series), a
#: kickoff must match this closely to be the same game.
SERIES_KICKOFF_WINDOW_SECONDS = 45 * 60


def kickoff_window(sport: str, series: bool = False) -> int:
    """Seconds two kickoffs of ONE real fixture may differ by across providers."""
    if series:
        return SERIES_KICKOFF_WINDOW_SECONDS
    return SPORT_KICKOFF_WINDOW_SECONDS.get(sport, KICKOFF_WINDOW_SECONDS)


def _competition_id(event) -> str:
    comp = competition_for_event(event)
    return comp.id if comp else ""


def fixture_orientation(a, b, comp_a: Optional[str] = None, comp_b: Optional[str] = None) -> Optional[str]:
    """``None`` when ``a`` and ``b`` are different matches, ``"same"`` when they are one match listed with
    the same home/away order, ``"swapped"`` when the two providers list the sides the other way round.

    Same sport, kickoffs within the sport's window (``kickoff_window``), both teams the same participant under
    the STRICT identity test (``same_participant``, scoped to the competition) - a merely similar name is a
    different team. When both events resolve to a known competition and it is not the same one, they are never
    linked (a Premier League match cannot be a Championship one however alike the names)."""
    if a.sport != b.sport or not a.start or not b.start:
        return None
    if abs((a.start - b.start).total_seconds()) > kickoff_window(a.sport):
        return None
    ca = _competition_id(a) if comp_a is None else comp_a
    cb = _competition_id(b) if comp_b is None else comp_b
    if ca and cb and ca != cb:
        return None
    sport, comp = a.sport, ca or cb or ""
    def same(x, y):
        return same_participant(x, y, sport, comp)
    if same(a.home, b.home) and same(a.away, b.away):
        return "same"
    if same(a.home, b.away) and same(a.away, b.home):
        return "swapped"
    return None


def competition_id(event) -> str:
    return _competition_id(event)


def _key(sport: str, competition: str, home: str, away: str, start) -> str:
    pair = sorted((canonical_team(home, sport, competition), canonical_team(away, sport, competition)))
    stamp = (start + timedelta(minutes=30)).strftime("%Y%m%d%H") if start else ""     # nearest hour
    return f"{sport}|{competition}|{pair[0]}|{pair[1]}|{stamp}"


def fixture_key(event, competition: Optional[str] = None) -> str:
    """sport | competition (when known) | sorted canonical team pair | kickoff rounded to the hour.
    Identical for the two providers' views of one match whenever their names reduce to the same teams;
    ``fixture_orientation`` remains the authority for anything looser."""
    comp = _competition_id(event) if competition is None else competition
    return _key(event.sport, comp, event.home, event.away, event.start)


def row_fixture_key(row) -> str:
    """The fixture_key of a stored ``picks`` row (sport/league/home/away/start_utc, plus the league id and
    Odds API sport key kept in its ``sources`` JSON). A row whose kickoff cannot be read still gets a key
    from its raw columns."""
    from predbot.util.timeutil import parse_iso

    def col(name):
        try:
            return row[name]
        except (KeyError, IndexError):
            return None

    meta = {}
    raw = col("sources")
    if isinstance(raw, str) and raw:
        try:
            meta = json.loads(raw) or {}
        except ValueError:
            meta = {}
    elif isinstance(raw, dict):
        meta = raw
    start = parse_iso(col("start_utc")) if col("start_utc") else None
    event = SimpleNamespace(sport=col("sport") or "", league=col("league") or "", league_id=meta.get("_league_id") or "",
                            extra={"sport_key": meta.get("_sport_key") or ""}, home=col("home") or "",
                            away=col("away") or "", start=start)
    return fixture_key(event)





def same_fixture(a, b):
    return fixture_orientation(a, b) is not None
