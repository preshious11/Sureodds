"""Shadow results store: a separate SQLite file. Refuses any path that names the production or bot database."""
from __future__ import annotations

import os
import sqlite3
from typing import Dict, Iterable, List, Optional

FORBIDDEN_NAMES = {"predbot.db", "bot.db", "sureodds_production.db"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS closing_spend (
  day TEXT PRIMARY KEY,
  calls INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS pro_value_rows (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL,
  run_at INTEGER NOT NULL,
  match_key TEXT NOT NULL,
  league TEXT,
  home TEXT,
  away TEXT,
  kickoff_ts INTEGER NOT NULL,
  outcome TEXT NOT NULL,
  sporty_odds REAL NOT NULL,
  sporty_fetched_at INTEGER,
  fair_probability REAL,
  fair_book TEXT,
  sharp_fetched_at INTEGER,
  edge REAL,
  second_book TEXT,
  second_edge REAL,
  status TEXT NOT NULL,
  reason TEXT,
  rank INTEGER,
  closing_fair_probability REAL,
  closing_fair_price REAL,
  closing_captured_at INTEGER,
  clv REAL
);
CREATE TABLE IF NOT EXISTS pro_value_botd_compare (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL,
  botd_match_key TEXT NOT NULL,
  lost_to_pro INTEGER NOT NULL,
  substitute_match_key TEXT
);
"""


class ShadowStore:
    def __init__(self, path: str):
        if not path:
            raise ValueError("a shadow database path is required")
        base = os.path.basename(path).lower()
        if base in FORBIDDEN_NAMES or "predbot.db" in path.replace("\\", "/").lower():
            raise ValueError("refusing to write shadow results into a production or bot database")
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def insert_rows(self, run_id: str, run_at: int, rows: Iterable[Dict]) -> int:
        n = 0
        for r in rows:
            self.conn.execute(
                "INSERT INTO pro_value_rows (run_id, run_at, match_key, league, home, away, kickoff_ts, outcome, "
                "sporty_odds, sporty_fetched_at, fair_probability, fair_book, sharp_fetched_at, edge, second_book, "
                "second_edge, status, reason, rank) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, run_at, r["match_key"], r.get("league"), r.get("home"), r.get("away"), r["kickoff_ts"],
                 r["outcome"], r["sporty_odds"], r.get("sporty_fetched_at"), r.get("fair_probability"),
                 r.get("fair_book"), r.get("sharp_fetched_at"), r.get("edge"), r.get("second_book"),
                 r.get("second_edge"), r["status"], r.get("reason"), r.get("rank")))
            n += 1
        self.conn.commit()
        return n

    def open_pending_picks(self, now_ts: int) -> List[sqlite3.Row]:
        """Selected shadow picks that have kicked off in the future and have no closing price yet."""
        return self.conn.execute(
            "SELECT * FROM pro_value_rows WHERE status='selected' AND closing_captured_at IS NULL "
            "AND kickoff_ts > ? ORDER BY kickoff_ts", (now_ts,)).fetchall()

    def set_closing(self, row_id: int, fair_prob: float, fair_price: float, captured_at: int, clv: float) -> None:
        self.conn.execute(
            "UPDATE pro_value_rows SET closing_fair_probability=?, closing_fair_price=?, closing_captured_at=?, "
            "clv=? WHERE id=?", (fair_prob, fair_price, captured_at, clv, row_id))
        self.conn.commit()

    def insert_botd_compare(self, run_id: str, botd_key: str, lost: bool, substitute: Optional[str]) -> None:
        self.conn.execute("INSERT INTO pro_value_botd_compare (run_id, botd_match_key, lost_to_pro, substitute_match_key) "
                          "VALUES (?,?,?,?)", (run_id, botd_key, 1 if lost else 0, substitute))
        self.conn.commit()

    def spent_on(self, day: str) -> int:
        """Closing-capture OddsAPI calls already made on ``day`` (UTC date), across runs."""
        row = self.conn.execute("SELECT calls FROM closing_spend WHERE day=?", (day,)).fetchone()
        return int(row["calls"]) if row else 0

    def add_spent(self, day: str, calls: int) -> None:
        self.conn.execute("INSERT INTO closing_spend (day, calls) VALUES (?,?) "
                          "ON CONFLICT(day) DO UPDATE SET calls = calls + excluded.calls", (day, int(calls)))
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
