"""Bet of the Day input for the shadow comparison, read from the run's candidate archive.

The archive is the ``ticket_candidates`` table the generation run writes. It is opened READ-ONLY (sqlite ``mode=ro``),
so nothing here can change live output. Ranking is the production ``select_top``, called with no reject sink, so the
result is the same Top the live run would pick from the same archive. Shadow-only: the result never reaches a
publisher."""
from __future__ import annotations

import json
import pathlib
import sqlite3
from typing import Dict, List, Optional, Set

from predbot.pro_value.matching import match_key
from predbot.selection.top import select_top
from predbot.util.timeutil import parse_iso


def latest_pick_date(db_path: str) -> Optional[str]:
    """Most recent pick_date in the archive (read-only)."""
    con = _connect_ro(db_path)
    try:
        row = con.execute("SELECT MAX(pick_date) FROM ticket_candidates").fetchone()
        return row[0] if row else None
    finally:
        con.close()


def load_archive_rows(db_path: str, pick_date: str) -> List[Dict]:
    """Candidate rows for one pick date, shaped the way PredictionPool reads them (read-only, no writes)."""
    con = _connect_ro(db_path)
    try:
        rows = []
        for candidate_id, payload in con.execute(
                "SELECT candidate_id, payload_json FROM ticket_candidates WHERE pick_date=? ORDER BY candidate_id",
                (pick_date,)):
            row = json.loads(payload)
            row.update(id=candidate_id, status="pending")
            if isinstance(row.get("sources"), dict):
                row["sources"] = json.dumps(row["sources"])
            rows.append(row)
        return rows
    finally:
        con.close()


def event_key(row: Dict) -> str:
    return match_key(row["home"], row["away"], int(parse_iso(row["start_utc"]).timestamp()))


def bet_of_the_day(rows: List[Dict], cfg, now_utc: str, exclude: Optional[Set[str]] = None) -> List[Dict]:
    """Top legs in rank order as {match_key, outcome}. ``exclude`` removes whole events (the Pro exclusion)."""
    pool = [r for r in rows if not exclude or event_key(r) not in exclude]
    top, _diag = select_top(pool, cfg, now_utc)
    return [{"match_key": event_key(r), "outcome": r.get("selection")} for r in top]


def _connect_ro(db_path: str) -> sqlite3.Connection:
    uri = pathlib.Path(db_path).resolve().as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True)


__all__ = ["latest_pick_date", "load_archive_rows", "bet_of_the_day", "event_key"]
