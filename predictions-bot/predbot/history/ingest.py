"""Historical football results -> the persistent store, resolved to canonical competitions and participants.

Two sources, both cheap:

* ``ingest_football_data`` - football-data.org v4 ``/competitions/{code}/matches?season=YYYY&status=FINISHED``: ONE
  request returns a whole competition-season (free tier: 10 requests/minute, the bot's existing key). Team ids are
  stable, so they become the participants' provider ids. The ledger (``history_fetches``) makes a completed past
  season a one-time fetch and rate-limits the current season to one refresh a day.
* ``ingest_csv`` - football-data.co.uk style CSV files (Div, Date, HomeTeam, AwayTeam, FTHG, FTAG, optional odds
  columns). No API at all: download once, import offline. Closing odds, when present, are stored for backtesting.

Nothing here runs from the bot's normal paths: history is an explicit, bounded operation (see ``cli.py history-*``).
"""
from __future__ import annotations

import csv
import io
import logging
from collections import Counter
from datetime import datetime, timezone
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from predbot.history.store import HistoryStore, season_of
from predbot.markets.competitions import COMPETITIONS
from predbot.storage.identity import IdentityRegistry
from predbot.util.names import canonical_team
from predbot.util.timeutil import parse_iso, utcnow

log = logging.getLogger(__name__)

SPORT = "football"
FD_CODE_TO_COMPETITION = {c.football_data: c for c in COMPETITIONS if c.football_data}
#: football-data.co.uk league divisions -> canonical competition ids
CSV_DIVISIONS = {"E0": "epl", "E1": "championship", "SP1": "la_liga", "I1": "serie_a", "D1": "bundesliga",
                 "F1": "ligue_1", "N1": "eredivisie", "P1": "primeira", "T1": "super_lig"}
REQUEST_SPACING_SECONDS = 6.5            # the free tier allows 10 requests/minute


def plan_football_data(db, codes: Iterable[str], seasons: Iterable[str], now: Optional[datetime] = None) -> List[Tuple[str, str]]:
    """The (code, season) pairs that would actually be requested - what a dry run reports. Nothing is fetched."""
    store, now = HistoryStore(db), now or utcnow()
    out = []
    for code in codes:
        comp = FD_CODE_TO_COMPETITION.get(code)
        if comp is None:
            continue
        for season in seasons:
            if store.needs_fetch("football_data", comp.id, str(season), now):
                out.append((code, str(season)))
    return out


def _fd_score(m: dict) -> Optional[Tuple[int, int, Optional[int], Optional[int]]]:
    """90-minute score of a FINISHED football-data match (fullTime only when the match ended in regulation)."""
    if str(m.get("status", "")).upper() not in ("FINISHED", "AWARDED"):
        return None
    score = m.get("score") or {}
    duration = str(score.get("duration", "REGULAR")).upper()
    block = score.get("fullTime") if duration == "REGULAR" else score.get("regularTime")
    if not isinstance(block, dict) or block.get("home") is None or block.get("away") is None:
        return None
    half = score.get("halfTime") if isinstance(score.get("halfTime"), dict) else {}
    try:
        ht = (None if half.get("home") is None else int(half["home"]), None if half.get("away") is None else int(half["away"]))
        return int(block["home"]), int(block["away"]), ht[0], ht[1]
    except (TypeError, ValueError):
        return None


def ingest_football_data(db, get: Callable[[str, dict], dict], codes: Iterable[str], seasons: Iterable[str],
                         now: Optional[datetime] = None, sleep: Callable[[float], None] = lambda s: None) -> Dict[str, dict]:
    """Backfill competition-seasons through ``get(path, params)`` (``FootballDataProvider.http.get``). Returns a
    per-(code, season) report. A fetched season is recorded in the ledger and never requested again (the current
    season at most daily); a failure is recorded and retried after an hour."""
    store, registry, now = HistoryStore(db), IdentityRegistry(db), now or utcnow()
    report: Dict[str, dict] = {}
    first = True
    for code in codes:
        comp = FD_CODE_TO_COMPETITION.get(code)
        if comp is None:
            report[code] = {"skipped": "no canonical competition for this football-data code"}
            continue
        for season in seasons:
            season = str(season)
            key = f"{code}:{season}"
            if not store.needs_fetch("football_data", comp.id, season, now):
                report[key] = {"skipped": "already fetched"}
                continue
            if not first:
                sleep(REQUEST_SPACING_SECONDS)
            first = False
            try:
                data = get(f"/competitions/{code}/matches", {"season": season, "status": "FINISHED"})
            except Exception as exc:                                            # noqa: BLE001 - recorded, retried later
                store.mark_fetch("football_data", comp.id, season, "failed", 0, now)
                report[key] = {"error": type(exc).__name__}
                continue
            counts: Counter = Counter()
            rows = []
            for m in (data.get("matches", []) if isinstance(data, dict) else []):
                parsed = _fd_score(m) if isinstance(m, dict) else None
                when = parse_iso(str(m.get("utcDate", ""))) if isinstance(m, dict) else None
                if parsed is None or when is None:
                    counts["unusable"] += 1
                    continue
                try:
                    ids = []
                    for side in ("homeTeam", "awayTeam"):
                        team = m[side]
                        cid = registry.participant("football_data", SPORT, team.get("name") or team.get("shortName"),
                                                   str(team.get("id") or ""))
                        for alias in (team.get("shortName"), team.get("name"), team.get("tla")):
                            if alias:
                                registry.add_alias(SPORT, alias, cid)
                        ids.append(cid)
                except (ValueError, KeyError, AttributeError):
                    counts["unresolved_participant"] += 1
                    continue
                hg, ag, hht, aht = parsed
                rows.append(dict(sport=SPORT, competition_id=comp.id, kickoff=when, home_id=ids[0], away_id=ids[1],
                                 home_goals=hg, away_goals=ag, home_ht=hht, away_ht=aht, source="football_data",
                                 source_match_id=str(m.get("id")), now=now))
            counts.update(store.add_many(rows))
            store.mark_fetch("football_data", comp.id, season, "complete", counts["inserted"] + counts["duplicate"], now)
            report[key] = dict(counts)
    return report


# ------------------------------------------------------------------------------------------------ CSV
_ODDS_COLUMNS = {"pinnacle": [("PSCH", "PSCD", "PSCA"), ("PSH", "PSD", "PSA")],
                 "bet365": [("B365CH", "B365CD", "B365CA"), ("B365H", "B365D", "B365A")],
                 "market_avg": [("AvgCH", "AvgCD", "AvgCA"), ("AvgH", "AvgD", "AvgA")]}


def _csv_kickoff(row: dict) -> Optional[datetime]:
    raw = (row.get("Date") or "").strip()
    for fmt in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            day = datetime.strptime(raw, fmt)
            break
        except ValueError:
            day = None
    if day is None:
        return None
    hh, mm = 12, 0
    t = (row.get("Time") or "").strip()
    if t and ":" in t:
        try:
            hh, mm = [int(x) for x in t.split(":")[:2]]
        except ValueError:
            hh, mm = 12, 0
    return day.replace(hour=hh, minute=mm, tzinfo=timezone.utc)     # the file gives local time; only the date matters here


def _csv_odds(row: dict) -> Dict[str, Dict[str, float]]:
    out = {}
    for book, options in _ODDS_COLUMNS.items():
        for cols in options:
            try:
                h, d, a = (float(row[c]) for c in cols)
            except (KeyError, TypeError, ValueError):
                continue
            if min(h, d, a) > 1.0:
                out[book] = {"home": h, "draw": d, "away": a}
                break
    return out


def ingest_csv(db, text: str, competition_id: Optional[str] = None, source: str = "csv:football-data.co.uk",
               now: Optional[datetime] = None) -> Counter:
    """Import a football-data.co.uk style CSV (as text). ``competition_id`` overrides the ``Div`` column for files
    without one. Idempotent: re-importing the same file inserts nothing."""
    store, registry, now = HistoryStore(db), IdentityRegistry(db), now or utcnow()
    counts: Counter = Counter()
    rows = []
    for row in csv.DictReader(io.StringIO(text.lstrip("﻿"))):
        comp = competition_id or CSV_DIVISIONS.get((row.get("Div") or "").strip())
        when = _csv_kickoff(row)
        home, away = (row.get("HomeTeam") or "").strip(), (row.get("AwayTeam") or "").strip()
        try:
            hg, ag = int(row["FTHG"]), int(row["FTAG"])
        except (KeyError, TypeError, ValueError):
            counts["unusable"] += 1
            continue
        if not comp or when is None or not home or not away:
            counts["unusable"] += 1
            continue
        try:
            h, a = registry.participant(source, SPORT, home), registry.participant(source, SPORT, away)
        except ValueError:
            counts["unresolved_participant"] += 1
            continue
        rows.append(dict(sport=SPORT, competition_id=comp, kickoff=when, home_id=h, away_id=a, home_goals=hg,
                         away_goals=ag, source=source, source_match_id=f"{comp}:{when.date()}:{canonical_team(home)}:{canonical_team(away)}",
                         odds=_csv_odds(row), now=now))
    counts.update(store.add_many(rows))
    return counts
