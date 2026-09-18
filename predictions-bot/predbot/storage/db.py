"""SQLite persistence. One file, WAL mode, safe for the single-process worker."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import logging
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS prediction_cache (
    cache_key TEXT PRIMARY KEY,
    local_date TEXT NOT NULL,
    timezone TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    sport_status_json TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    retry_after TEXT,
    cache_version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS provider_cache (
    cache_key TEXT PRIMARY KEY,
    local_date TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    cache_version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS picks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pick_date TEXT NOT NULL,             -- local date the pick was published for (YYYY-MM-DD)
    tier TEXT NOT NULL,                  -- top | daily
    rank INTEGER NOT NULL,
    provider TEXT NOT NULL,
    provider_event_id TEXT NOT NULL,
    sport TEXT NOT NULL,
    league TEXT,
    home TEXT NOT NULL,
    away TEXT NOT NULL,
    start_utc TEXT NOT NULL,
    market TEXT NOT NULL,
    period TEXT NOT NULL,
    stat TEXT NOT NULL,
    entity TEXT NOT NULL,
    line REAL,
    selection TEXT NOT NULL,
    label TEXT NOT NULL,
    price REAL NOT NULL,
    prob REAL NOT NULL,
    ev REAL NOT NULL,
    p_market REAL,
    sources TEXT,                        -- json {source: prob}
    provider_odd_id TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    settled_at TEXT,
    result_json TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_picks_status ON picks(status);
CREATE INDEX IF NOT EXISTS idx_picks_date ON picks(pick_date);

CREATE TABLE IF NOT EXISTS ratings (
    sport TEXT NOT NULL,
    team TEXT NOT NULL,
    rating REAL NOT NULL,
    games INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (sport, team)
);

CREATE TABLE IF NOT EXISTS results_seen (
    key TEXT PRIMARY KEY,                -- sport|home|away|date
    home_score REAL, away_score REAL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS api_budget (
    provider TEXT NOT NULL,
    bucket TEXT NOT NULL,                -- YYYY-MM-DD (daily) or YYYY-MM (monthly)
    used INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (provider, bucket)
);

CREATE TABLE IF NOT EXISTS calibration (
    scope TEXT PRIMARY KEY,              -- 'global' or sport
    a REAL NOT NULL, b REAL NOT NULL, n INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,                  -- picks | recap
    post_date TEXT NOT NULL,
    message_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(kind, post_date)
);

CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT
);

CREATE TABLE IF NOT EXISTS subscribers (
    chat_id TEXT PRIMARY KEY,            -- DM chat with the user
    username TEXT,
    first_name TEXT,
    timezone TEXT NOT NULL DEFAULT '',   -- auto-detected IANA zone ('' = fall back to config TIMEZONE)
    subscribed_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS deliveries (
    kind TEXT NOT NULL,                  -- picks | recap
    post_date TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(kind, post_date, chat_id)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Database:
    _prediction_locks = {}
    _prediction_locks_guard = threading.Lock()

    def __init__(self, path: str):
        self.path = path
        logging.getLogger(__name__).info("SQLite database path: %s | exists=%s",
                                        os.path.abspath(path), os.path.isfile(path))
        with self._prediction_locks_guard:
            key = os.path.normcase(os.path.abspath(path)) if path != ':memory:' else id(self)
            self.prediction_lock = self._prediction_locks.setdefault(key, threading.RLock())
        self._transaction_depth = 0
        d = os.path.dirname(os.path.abspath(path))
        os.makedirs(d, exist_ok=True)
        self._lock = threading.RLock()
        # timeout=30s: the in-process lock only coordinates threads within one
        # process: a manual `cli.py` command and the scheduler's `main.py`
        # worker are separate processes sharing this file, so SQLite's own
        # busy-wait is the only thing standing between them and an immediate
        # "database is locked" error during a longer write (e.g. picks
        # generation blocked on a provider API call).
        self.conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        with self._lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()
            self._migrate()

    def _migrate(self) -> None:
        """Additive migrations for databases created by older versions."""
        cols = {str(r["name"]) for r in self.query("PRAGMA table_info(subscribers)")}
        if "timezone" not in cols:                      # per-subscriber local delivery zone
            self.conn.execute("ALTER TABLE subscribers ADD COLUMN timezone TEXT NOT NULL DEFAULT ''")
            self.conn.commit()

    # ---- generic ----
    def execute(self, sql: str, params: Iterable = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self.conn.execute(sql, tuple(params))
            if not self._transaction_depth:
                self.conn.commit()
            return cur

    @contextmanager
    def transaction(self):
        with self._lock:
            outer = self._transaction_depth == 0
            if outer:
                self.conn.execute('BEGIN IMMEDIATE')
            self._transaction_depth += 1
            try:
                yield
                if outer:
                    self.conn.commit()
            except BaseException:
                if outer:
                    self.conn.rollback()
                raise
            finally:
                self._transaction_depth -= 1

    def close(self):
        with self._lock:
            self.conn.close()

    def get_prediction_cache(self, cache_key):
        rows = self.query('SELECT * FROM prediction_cache WHERE cache_key=?', (cache_key,))
        return dict(rows[0]) if rows else None

    def upsert_prediction_cache(self, row):
        columns = ('cache_key', 'local_date', 'timezone', 'status', 'payload_json',
                   'sport_status_json', 'generated_at', 'updated_at', 'expires_at',
                   'retry_after', 'cache_version')
        with self.transaction():
            self.execute('INSERT INTO prediction_cache (' + ','.join(columns) + ') VALUES (' +
                         ','.join('?' for _ in columns) + ') ON CONFLICT(cache_key) DO UPDATE SET ' +
                         ','.join(c + '=excluded.' + c for c in columns[1:]),
                         [row.get(c) for c in columns])

    def delete_prediction_cache(self, cache_key):
        self.execute('DELETE FROM prediction_cache WHERE cache_key=?', (cache_key,))

    def defer_prediction_retry(self, cache_key, retry_after):
        self.execute('UPDATE prediction_cache SET retry_after=? WHERE cache_key=?',
                     (retry_after, cache_key))

    def has_pick_selection(self, date, tier, scored):
        e, m = scored.event, scored.line
        return bool(self.query('SELECT id FROM picks WHERE pick_date=? AND tier=? AND '
                              'provider=? AND provider_event_id=? AND market=? AND period=? '
                              'AND stat=? AND entity=? AND line IS ? AND selection=? LIMIT 1',
                              (date, tier, e.provider, e.provider_event_id, m.market, m.period,
                               m.stat, m.entity, m.line, m.selection)))

    def prune_prediction_cache(self, before_date):
        with self.transaction():
            self.execute('DELETE FROM prediction_cache WHERE local_date < ?', (before_date,))
            self.execute('DELETE FROM provider_cache WHERE local_date < ?', (before_date,))

    def get_provider_cache(self, cache_key):
        rows = self.query('SELECT * FROM provider_cache WHERE cache_key=?', (cache_key,))
        return dict(rows[0]) if rows else None

    def upsert_provider_cache(self, cache_key, local_date, payload, metadata, expires_at, version=1):
        self.execute('INSERT INTO provider_cache VALUES(?,?,?,?,?,?) ON CONFLICT(cache_key) '
                     'DO UPDATE SET local_date=excluded.local_date,payload_json=excluded.payload_json,'
                     'metadata_json=excluded.metadata_json,expires_at=excluded.expires_at,'
                     'cache_version=excluded.cache_version',
                     (cache_key, local_date, json.dumps(payload, sort_keys=True, allow_nan=False),
                      json.dumps(metadata, sort_keys=True, allow_nan=False), expires_at, version))

    def query(self, sql: str, params: Iterable = ()) -> List[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, tuple(params)).fetchall()

    def get_kv(self, k: str, default: Optional[str] = None) -> Optional[str]:
        rows = self.query("SELECT v FROM kv WHERE k=?", (k,))
        return rows[0]["v"] if rows else default

    def set_kv(self, k: str, v: str) -> None:
        self.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))

    # ---- budget ----
    def budget_used(self, provider: str, bucket: str) -> int:
        rows = self.query("SELECT used FROM api_budget WHERE provider=? AND bucket=?", (provider, bucket))
        return int(rows[0]["used"]) if rows else 0

    def budget_add(self, provider: str, bucket: str, n: int = 1) -> int:
        self.execute(
            "INSERT INTO api_budget(provider,bucket,used) VALUES(?,?,?) "
            "ON CONFLICT(provider,bucket) DO UPDATE SET used=used+excluded.used",
            (provider, bucket, n))
        return self.budget_used(provider, bucket)

    # ---- picks ----
    def insert_pick(self, row: Dict[str, Any]) -> int:
        row = dict(row)
        row.setdefault("created_at", _now())
        row.setdefault("status", "pending")
        if isinstance(row.get("sources"), dict):
            row["sources"] = json.dumps(row["sources"])
        cols = ",".join(row.keys())
        qs = ",".join("?" for _ in row)
        cur = self.execute(f"INSERT INTO picks({cols}) VALUES({qs})", list(row.values()))
        return int(cur.lastrowid)

    def picks_for_date(self, pick_date: str) -> List[sqlite3.Row]:
        return self.query("SELECT * FROM picks WHERE pick_date=? ORDER BY tier DESC, rank ASC", (pick_date,))

    def latest_pick_date(self) -> str:
        """The most recent pick_date in the store ('' when nothing is published)."""
        rows = self.query("SELECT MAX(pick_date) AS d FROM picks")
        return str(rows[0]["d"] or "") if rows and rows[0]["d"] else ""

    def has_picks_for_date(self, pick_date: str) -> bool:
        return bool(self.query("SELECT 1 FROM picks WHERE pick_date=? LIMIT 1", (pick_date,)))

    def pending_picks(self, before_utc: Optional[str] = None) -> List[sqlite3.Row]:
        if before_utc:
            return self.query("SELECT * FROM picks WHERE status='pending' AND start_utc<=? ORDER BY start_utc", (before_utc,))
        return self.query("SELECT * FROM picks WHERE status='pending' ORDER BY start_utc")

    def settle_pick(self, pick_id: int, status: str, result: Optional[Dict] = None) -> None:
        self.execute("UPDATE picks SET status=?, settled_at=?, result_json=? WHERE id=?",
                     (status, _now(), json.dumps(result) if result else None, pick_id))

    def settled_picks_since(self, since_date: str) -> List[sqlite3.Row]:
        return self.query(
            "SELECT * FROM picks WHERE pick_date>=? AND status IN ('won','lost','push','half_won','half_lost') "
            "ORDER BY pick_date, tier DESC, rank", (since_date,))

    def all_settled_for_calibration(self, limit: int = 3000) -> List[sqlite3.Row]:
        return self.query(
            "SELECT prob, status, price FROM picks WHERE status IN ('won','lost','half_won','half_lost') "
            "ORDER BY id DESC LIMIT ?", (limit,))

    def recent_pick_keys(self, days: int = 1) -> List[sqlite3.Row]:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        return self.query("SELECT provider_event_id, market, period, stat, entity, line, selection, home, away FROM picks WHERE pick_date>=?", (since,))

    # ---- ratings ----
    def get_rating(self, sport: str, team: str):
        rows = self.query("SELECT rating, games FROM ratings WHERE sport=? AND team=?", (sport, team))
        return (float(rows[0]["rating"]), int(rows[0]["games"])) if rows else (None, 0)

    def set_rating(self, sport: str, team: str, rating: float, games: int) -> None:
        self.execute(
            "INSERT INTO ratings(sport,team,rating,games,updated_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(sport,team) DO UPDATE SET rating=excluded.rating, games=excluded.games, updated_at=excluded.updated_at",
            (sport, team, rating, games, _now()))

    def result_seen(self, key: str) -> bool:
        return bool(self.query("SELECT 1 FROM results_seen WHERE key=?", (key,)))

    def mark_result_seen(self, key: str, h: float, a: float) -> None:
        self.execute("INSERT OR IGNORE INTO results_seen(key,home_score,away_score,created_at) VALUES(?,?,?,?)",
                     (key, h, a, _now()))

    # ---- calibration ----
    def get_calibration(self, scope: str = "global"):
        rows = self.query("SELECT a,b,n FROM calibration WHERE scope=?", (scope,))
        return (float(rows[0]["a"]), float(rows[0]["b"]), int(rows[0]["n"])) if rows else None

    def set_calibration(self, scope: str, a: float, b: float, n: int) -> None:
        self.execute(
            "INSERT INTO calibration(scope,a,b,n,updated_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(scope) DO UPDATE SET a=excluded.a,b=excluded.b,n=excluded.n,updated_at=excluded.updated_at",
            (scope, a, b, n, _now()))

    # ---- posts ----
    def post_done(self, kind: str, post_date: str) -> bool:
        return bool(self.query("SELECT 1 FROM posts WHERE kind=? AND post_date=?", (kind, post_date)))

    def mark_post(self, kind: str, post_date: str, message_id: Optional[str]) -> None:
        self.execute("INSERT OR IGNORE INTO posts(kind,post_date,message_id,created_at) VALUES(?,?,?,?)",
                     (kind, post_date, message_id, _now()))

    # ---- subscribers / deliveries ----
    def subscriber_add(self, chat_id: str, username: str = "", first_name: str = "",
                       timezone: str = "") -> None:
        """Register (or re-activate) a subscriber; non-empty fields are kept/overwritten.

        ``subscribed_at`` marks when the CURRENT subscription started, because the
        delivery gate uses it to decide whether today's card was already due when
        this person joined (see ``broadcast._joined_before_target``).  It is
        refreshed only when the row was inactive - i.e. on a genuine (re)subscribe.
        An already-active user sending /start again keeps their original instant,
        so that cannot cost them the card that was already owed to them.
        """
        self.execute(
            "INSERT INTO subscribers(chat_id,username,first_name,timezone,subscribed_at,active) "
            "VALUES(?,?,?,?,?,1) ON CONFLICT(chat_id) DO UPDATE SET active=1, "
            "username=CASE WHEN excluded.username!='' THEN excluded.username ELSE subscribers.username END, "
            "first_name=CASE WHEN excluded.first_name!='' THEN excluded.first_name ELSE subscribers.first_name END, "
            "subscribed_at=CASE WHEN subscribers.active=0 THEN excluded.subscribed_at "
            "ELSE subscribers.subscribed_at END, "
            "timezone=CASE WHEN excluded.timezone!='' THEN excluded.timezone ELSE subscribers.timezone END",
            (str(chat_id), str(username or ""), str(first_name or ""), str(timezone or ""), _now()))

    def subscriber_remove(self, chat_id: str) -> None:
        self.execute("UPDATE subscribers SET active=0 WHERE chat_id=?", (str(chat_id),))

    def subscribers_active(self) -> List[sqlite3.Row]:
        return self.query("SELECT * FROM subscribers WHERE active=1 ORDER BY subscribed_at")

    def subscriber_timezone(self, chat_id: str) -> str:
        """The subscriber's auto-detected IANA zone ('' when unknown)."""
        rows = self.query("SELECT timezone FROM subscribers WHERE chat_id=?", (str(chat_id),))
        return str(rows[0]["timezone"] or "") if rows else ""

    def subscriber_set_timezone(self, chat_id: str, timezone: str) -> None:
        """Store the IANA zone used for subscriber-local prediction display.

        Creates the subscriber when the chat is unknown, so a zone detected
        before /start is never silently dropped.
        """
        self.execute(
            "INSERT INTO subscribers(chat_id,timezone,subscribed_at,active) VALUES(?,?,?,1) "
            "ON CONFLICT(chat_id) DO UPDATE SET timezone=excluded.timezone",
            (str(chat_id), str(timezone or ""), _now()))

    def delivery_done(self, kind: str, post_date: str, chat_id: str) -> bool:
        return bool(self.query("SELECT 1 FROM deliveries WHERE kind=? AND post_date=? AND chat_id=?",
                               (kind, post_date, str(chat_id))))

    def mark_delivery(self, kind: str, post_date: str, chat_id: str) -> None:
        self.execute("INSERT OR IGNORE INTO deliveries(kind,post_date,chat_id,created_at) VALUES(?,?,?,?)",
                     (kind, post_date, str(chat_id), _now()))
