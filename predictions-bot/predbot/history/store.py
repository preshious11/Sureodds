"""Persistent historical results, keyed by canonical competition and canonical participants.

One row per real match (sport, competition, kickoff date, home, away, final score) with the source that supplied it.
Writes are idempotent twice over: the same source/match id is stored once, and the same real fixture arriving from a
second source (same competition, same two canonical participants, same kickoff date) is stored once. An ingestion
ledger (``history_fetches``) records which (source, competition, season) has been fetched, so history is never
re-requested per user or per prediction run.
"""
from __future__ import annotations

import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional

from predbot.model.elo import Elo
from predbot.util.timeutil import iso, parse_iso, utcnow


@dataclass(frozen=True)
class HistoryMatch:
    id: int
    sport: str
    competition_id: str
    season: str
    kickoff: datetime
    home_id: str
    away_id: str
    home_goals: int
    away_goals: int
    source: str


def season_of(kickoff: datetime) -> str:
    """Start year of the (European-calendar) season a kickoff falls in: Jul 2024 .. Jun 2025 -> '2024'."""
    return str(kickoff.year if kickoff.month >= 7 else kickoff.year - 1)


def _row(r) -> HistoryMatch:
    return HistoryMatch(id=int(r["id"]), sport=r["sport"], competition_id=r["competition_id"], season=r["season"],
                        kickoff=parse_iso(r["kickoff_utc"]), home_id=r["home_id"], away_id=r["away_id"],
                        home_goals=int(r["home_goals"]), away_goals=int(r["away_goals"]), source=r["source"])


class HistoryStore:
    def __init__(self, db):
        self.db = db

    # ------------------------------------------------------------------ writes
    def add_result(self, *, sport: str, competition_id: str, kickoff: datetime, home_id: str, away_id: str,
                   home_goals: int, away_goals: int, source: str, source_match_id: str,
                   home_ht: Optional[int] = None, away_ht: Optional[int] = None,
                   odds: Optional[Dict[str, Dict[str, float]]] = None, now: Optional[datetime] = None) -> str:
        """Store one final result. Returns 'inserted' or 'duplicate'; ``ValueError`` for an unusable row."""
        if kickoff is None or kickoff.tzinfo is None:
            raise ValueError("kickoff must be timezone-aware")
        if not home_id or not away_id or home_id == away_id:
            raise ValueError("a result needs two distinct canonical participants")
        if int(home_goals) < 0 or int(away_goals) < 0 or int(home_goals) != home_goals or int(away_goals) != away_goals:
            raise ValueError("goals must be non-negative integers")
        if home_ht is not None and away_ht is not None and (home_ht > home_goals or away_ht > away_goals):
            raise ValueError("half-time score exceeds the final score: the provider's score is inconsistent")
        when = iso(kickoff)
        cur = self.db.execute(
            "INSERT OR IGNORE INTO match_results(sport,competition_id,season,kickoff_utc,home_id,away_id,home_goals,"
            "away_goals,home_ht,away_ht,source,source_match_id,ingested_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sport, competition_id, season_of(kickoff), when, home_id, away_id, int(home_goals), int(away_goals),
             home_ht, away_ht, source, str(source_match_id), iso(now or utcnow())))
        inserted = cur.rowcount == 1
        match_id = cur.lastrowid if inserted else self._existing_id(competition_id, home_id, away_id, when, source,
                                                                    source_match_id)
        for book, quote in (odds or {}).items():
            self.db.execute("INSERT OR IGNORE INTO match_odds VALUES(?,?,?,?,?)",
                            (match_id, book, quote["home"], quote["draw"], quote["away"]))
        return "inserted" if inserted else "duplicate"

    def _existing_id(self, competition_id, home_id, away_id, when, source, source_match_id) -> int:
        rows = self.db.query("SELECT id FROM match_results WHERE source=? AND source_match_id=?",
                             (source, str(source_match_id)))
        if not rows:
            rows = self.db.query("SELECT id FROM match_results WHERE competition_id=? AND home_id=? AND away_id=? "
                                 "AND substr(kickoff_utc,1,10)=?", (competition_id, home_id, away_id, when[:10]))
        return int(rows[0]["id"])

    def add_many(self, rows: Iterable[dict]) -> Counter:
        counts: Counter = Counter()
        with self.db.transaction():
            for row in rows:
                try:
                    counts[self.add_result(**row)] += 1
                except (ValueError, KeyError, TypeError):
                    counts["rejected"] += 1
        return counts

    # ------------------------------------------------------------------- reads
    def results(self, *, sport: str = "football", competition_id: Optional[str] = None,
                before: Optional[datetime] = None, since: Optional[datetime] = None) -> List[HistoryMatch]:
        """Results in kickoff order. ``before`` is strict: a match that kicked off AT ``before`` is excluded, so a
        prediction made for a given instant can never see that instant's (or any later) result."""
        where, params = ["sport=?"], [sport]
        if competition_id:
            where.append("competition_id=?")
            params.append(competition_id)
        if before is not None:
            where.append("kickoff_utc<?")
            params.append(iso(before))
        if since is not None:
            where.append("kickoff_utc>=?")
            params.append(iso(since))
        rows = self.db.query("SELECT * FROM match_results WHERE " + " AND ".join(where) +
                             " ORDER BY kickoff_utc, id", params)
        return [_row(r) for r in rows]

    def odds(self, match_id: int, book: Optional[str] = None) -> Dict[str, Dict[str, float]]:
        rows = self.db.query("SELECT * FROM match_odds WHERE match_id=?" + (" AND book=?" if book else ""),
                             (match_id, book) if book else (match_id,))
        return {r["book"]: {"home": r["odds_home"], "draw": r["odds_draw"], "away": r["odds_away"]} for r in rows}

    def add_odds_ext(self, match_id: int, book: str, market: str, kind: str, prices: tuple, columns: str) -> bool:
        """Store one raw reference quote. Idempotent: an existing quote is never overwritten."""
        o = list(prices) + [None] * (3 - len(prices))
        cur = self.db.execute("INSERT OR IGNORE INTO match_odds_ext VALUES(?,?,?,?,?,?,?,?)",
                              (match_id, book, market, kind, o[0], o[1], o[2], columns))
        return cur.rowcount == 1

    def odds_ext(self, match_id: int) -> Dict[tuple, tuple]:
        """{(book, market, kind): (o1, o2, o3)} for one match."""
        return {(r["book"], r["market"], r["kind"]): (r["o1"], r["o2"], r["o3"])
                for r in self.db.query("SELECT * FROM match_odds_ext WHERE match_id=?", (match_id,))}

    def coverage(self, sport: str = "football") -> Dict[str, dict]:
        """Per competition: matches, distinct teams, first/last kickoff, seasons, how many carry odds."""
        out: Dict[str, dict] = {}
        for r in self.db.query("SELECT competition_id, COUNT(*) AS n, MIN(kickoff_utc) AS first, MAX(kickoff_utc) AS last "
                               "FROM match_results WHERE sport=? GROUP BY competition_id ORDER BY competition_id", (sport,)):
            comp = r["competition_id"]
            teams = self.db.query("SELECT COUNT(*) AS n FROM (SELECT home_id AS t FROM match_results WHERE competition_id=? "
                                  "UNION SELECT away_id FROM match_results WHERE competition_id=?)", (comp, comp))[0]["n"]
            seasons = [x["season"] for x in self.db.query("SELECT DISTINCT season FROM match_results WHERE competition_id=? "
                                                          "ORDER BY season", (comp,))]
            with_odds = self.db.query("SELECT COUNT(DISTINCT m.id) AS n FROM match_results m JOIN match_odds o ON o.match_id=m.id "
                                      "WHERE m.competition_id=?", (comp,))[0]["n"]
            out[comp] = {"matches": r["n"], "teams": teams, "first": r["first"], "last": r["last"], "seasons": seasons,
                         "with_odds": with_odds}
        return out

    # ------------------------------------------------------------------ ledger
    def fetch_state(self, source: str, competition_id: str, season: str) -> Optional[dict]:
        rows = self.db.query("SELECT * FROM history_fetches WHERE source=? AND competition_id=? AND season=?",
                             (source, competition_id, season))
        return dict(rows[0]) if rows else None

    def mark_fetch(self, source: str, competition_id: str, season: str, status: str, rows: int,
                   now: Optional[datetime] = None) -> None:
        self.db.execute("INSERT OR REPLACE INTO history_fetches VALUES(?,?,?,?,?,?)",
                        (source, competition_id, season, status, rows, iso(now or utcnow())))

    def needs_fetch(self, source: str, competition_id: str, season: str, now: Optional[datetime] = None,
                    current_ttl: timedelta = timedelta(hours=24)) -> bool:
        """A completed past season is fetched once, ever. The current season is refreshed at most once per
        ``current_ttl``. A failed fetch is retried after a short back-off, never in a tight loop."""
        now = now or utcnow()
        state = self.fetch_state(source, competition_id, season)
        if state is None:
            return True
        age = now - parse_iso(state["fetched_at"])
        if state["status"] == "failed":
            return age > timedelta(hours=1)
        current = season == season_of(now)
        return current and age > current_ttl

    # --------------------------------------------------------------------- Elo
    def apply_pending_elo(self, sport: str = "football") -> int:
        """Feed every not-yet-applied result into the canonical-id Elo, oldest first, and mark it applied.
        Idempotent: a result updates ratings exactly once however often this runs."""
        elo, n = Elo(self.db), 0
        with self.db.transaction():
            for r in self.db.query("SELECT * FROM match_results WHERE sport=? AND elo_applied=0 "
                                   "ORDER BY kickoff_utc, id", (sport,)):
                elo.update(sport, "", "", r["home_goals"], r["away_goals"], home_id=r["home_id"], away_id=r["away_id"])
                self.db.execute("UPDATE match_results SET elo_applied=1 WHERE id=?", (r["id"],))
                n += 1
        return n
