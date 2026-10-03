"""SQLite persistence. One file, WAL mode, safe for the single-process worker."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import logging
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
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
    prob_raw REAL,                       -- model probability BEFORE calibration (what refit trains on)
    sources TEXT,                        -- json {source: prob}
    provider_odd_id TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    settled_at TEXT,
    result_json TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_picks_status ON picks(status);
CREATE INDEX IF NOT EXISTS idx_picks_date ON picks(pick_date);
CREATE TABLE IF NOT EXISTS prediction_memberships (
    prediction_id INTEGER NOT NULL REFERENCES picks(id),
    collection TEXT NOT NULL CHECK(collection IN ('all','top','daily')),
    rank INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(prediction_id,collection)
);
CREATE INDEX IF NOT EXISTS idx_membership_collection ON prediction_memberships(collection,prediction_id);
CREATE TABLE IF NOT EXISTS canonical_participants (id TEXT PRIMARY KEY, sport TEXT NOT NULL, name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS canonical_events (
    id TEXT PRIMARY KEY, competition_id TEXT NOT NULL, home_id TEXT NOT NULL,
    away_id TEXT NOT NULL, kickoff_utc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS match_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sport TEXT NOT NULL,
    competition_id TEXT NOT NULL,
    season TEXT NOT NULL DEFAULT '',
    kickoff_utc TEXT NOT NULL,
    home_id TEXT NOT NULL,
    away_id TEXT NOT NULL,
    home_goals INTEGER NOT NULL,
    away_goals INTEGER NOT NULL,
    home_ht INTEGER,
    away_ht INTEGER,
    source TEXT NOT NULL,
    source_match_id TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    elo_applied INTEGER NOT NULL DEFAULT 0,
    UNIQUE(source, source_match_id)
);
CREATE INDEX IF NOT EXISTS idx_match_results_comp ON match_results(competition_id, kickoff_utc);
CREATE UNIQUE INDEX IF NOT EXISTS uq_match_results_fixture ON match_results(competition_id, home_id, away_id, substr(kickoff_utc,1,10));
CREATE TABLE IF NOT EXISTS match_odds (
    match_id INTEGER NOT NULL REFERENCES match_results(id),
    book TEXT NOT NULL,
    odds_home REAL NOT NULL, odds_draw REAL NOT NULL, odds_away REAL NOT NULL,
    PRIMARY KEY(match_id, book)
);
-- reference bookmaker prices for EVALUATION only (never a model input): raw decimal odds exactly as published.
-- market '1x2' = (home, draw, away); 'ou25' = (over 2.5, under 2.5). kind 'pre' = the pre-match collection columns,
-- 'close' = the closing columns. ``columns`` names the source CSV columns, so provenance is never lost.
CREATE TABLE IF NOT EXISTS match_odds_ext (
    match_id INTEGER NOT NULL REFERENCES match_results(id),
    book TEXT NOT NULL, market TEXT NOT NULL, kind TEXT NOT NULL,
    o1 REAL NOT NULL, o2 REAL NOT NULL, o3 REAL, columns TEXT NOT NULL,
    PRIMARY KEY(match_id, book, market, kind)
);
CREATE TABLE IF NOT EXISTS history_fetches (
    source TEXT NOT NULL, competition_id TEXT NOT NULL, season TEXT NOT NULL,
    status TEXT NOT NULL, rows INTEGER NOT NULL DEFAULT 0, fetched_at TEXT NOT NULL,
    PRIMARY KEY(source, competition_id, season)
);
CREATE TABLE IF NOT EXISTS participant_aliases (
    sport TEXT NOT NULL, alias TEXT NOT NULL, canonical_id TEXT NOT NULL,
    PRIMARY KEY(sport,alias)
);
CREATE TABLE IF NOT EXISTS provider_identity (
    provider TEXT NOT NULL, kind TEXT NOT NULL, provider_id TEXT NOT NULL,
    scope TEXT NOT NULL, canonical_id TEXT NOT NULL,
    PRIMARY KEY(provider,kind,provider_id,scope)
);

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

-- Every SportsGameOdds request, with WHY it was made. Internal only (never shown to subscribers); no credentials or
-- account identifiers are ever stored here.
CREATE TABLE IF NOT EXISTS sgo_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    day TEXT NOT NULL,                   -- UTC date
    reason TEXT NOT NULL,                -- generation | settlement | fallback | diagnostic | manual | catalog | quota_probe
    endpoint TEXT NOT NULL,              -- /events | /leagues | /account/usage
    sport TEXT,
    league TEXT,
    event_id TEXT,
    http_attempts INTEGER NOT NULL DEFAULT 1,   -- HTTP requests made (retries included)
    objects_billed INTEGER NOT NULL DEFAULT 0,  -- what SGO bills: objects returned, minimum 1 per answered request
    status TEXT NOT NULL,                -- ok | rate_limited | error | skipped
    note TEXT
);
CREATE INDEX IF NOT EXISTS idx_sgo_usage_day ON sgo_usage(day);
CREATE TABLE IF NOT EXISTS live_fixture_state (
    pick_date TEXT NOT NULL,
    fixture_key TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT,
    final_at TEXT,
    last_checked_at TEXT,
    result_source TEXT,
    result_json TEXT,
    settlement_done INTEGER NOT NULL DEFAULT 0,
    notifications_done INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (pick_date, fixture_key)
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
        #: instrumentation: statements executed / commits issued (see tools/replay.py)
        self.stats = {"executes": 0, "commits": 0}
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
        pick_cols = {str(r["name"]) for r in self.query("PRAGMA table_info(picks)")}
        if pick_cols and "prob_raw" not in pick_cols:   # pre-calibration probability
            self.conn.execute("ALTER TABLE picks ADD COLUMN prob_raw REAL")
            self.conn.commit()
        #: evidence recorded WITH each pick, so accuracy can later be sliced by it (edge at selection time, book
        #: coverage, market quality, the ranking score) and closing line value filled in once a closing price exists
        for col, kind in (("p_fair", "REAL"), ("edge_ev", "REAL"), ("rank_score", "REAL"), ("books", "INTEGER"),
                          ("quality", "TEXT"), ("closing_price", "REAL"), ("clv", "REAL")):
            if pick_cols and col not in pick_cols:
                self.conn.execute(f"ALTER TABLE picks ADD COLUMN {col} {kind}")
                self.conn.commit()
        if pick_cols:
            self._migrate_fixture_key("fixture_key" in pick_cols)
        self._migrate_split_picks_kind()
        for col in ("canonical_event_id", "canonical_competition_id", "home_id", "away_id"):
            if col not in pick_cols:
                self.conn.execute(f"ALTER TABLE picks ADD COLUMN {col} TEXT")
        self._migrate_prediction_uniqueness()
        if self.get_kv('migration:prediction_memberships:v1') != 'done':
            self.conn.execute("INSERT OR IGNORE INTO prediction_memberships SELECT id,'all',rank FROM picks")
            self.conn.execute("INSERT OR IGNORE INTO prediction_memberships SELECT id,tier,rank FROM picks WHERE tier IN ('top','daily')")
            self.conn.execute("INSERT OR REPLACE INTO kv VALUES('migration:prediction_memberships:v1','done')")
        self.conn.commit()

    def _migrate_prediction_uniqueness(self) -> None:
        """One stored prediction per (day, canonical event, market FAMILY) - was one per (day, canonical event).

        ``market`` is the canonical family key (result3, result2, dc, dnb, handicap, total, btts), never a display string.
        The old index is only dropped, never any row, so every stored and settled prediction survives; the new, looser
        index can always be built because the old rule already implied it. Idempotent."""
        self.conn.execute("DROP INDEX IF EXISTS uq_prediction_event_day")
        self.conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_prediction_event_family_day "
                          "ON picks(pick_date,canonical_event_id,market) "
                          "WHERE canonical_event_id IS NOT NULL AND canonical_event_id<>''")

    def _migrate_split_picks_kind(self) -> None:
        """The card went out as ONE ``picks`` message; it is now two (``picks_top``, ``picks_daily``). A ``picks`` row
        already recorded counts as both, so nothing that was sent is sent again. Idempotent."""
        for kind in ("picks_top", "picks_daily"):
            self.conn.execute("INSERT OR IGNORE INTO deliveries(kind,post_date,chat_id,created_at) "
                              "SELECT ?, post_date, chat_id, created_at FROM deliveries WHERE kind='picks'", (kind,))
            self.conn.execute("INSERT OR IGNORE INTO posts(kind,post_date,message_id,created_at) "
                              "SELECT ?, post_date, message_id, created_at FROM posts WHERE kind='picks'", (kind,))
        self.conn.commit()

    def _migrate_fixture_key(self, has_column: bool) -> None:
        """``picks.fixture_key`` + a unique index that forbids the same selection on the same match twice on
        one date whatever the tier. Old rows are backfilled from their own columns; a legacy duplicate keeps
        its row but gets a distinct key suffix, so the index can always be built."""
        if not has_column:
            self.conn.execute("ALTER TABLE picks ADD COLUMN fixture_key TEXT")
        rows = self.query("SELECT * FROM picks WHERE fixture_key IS NULL OR fixture_key='' ORDER BY id")
        if rows:
            from predbot.util.fixtures import row_fixture_key
            seen = {(r["pick_date"], r["fixture_key"], r["market"], r["period"], r["stat"], r["entity"], r["line"], r["selection"])
                    for r in self.query("SELECT pick_date, fixture_key, market, period, stat, entity, line, selection "
                                        "FROM picks WHERE fixture_key IS NOT NULL AND fixture_key<>''")}
            for r in rows:
                key = row_fixture_key(r)
                ident = (r["pick_date"], key, r["market"], r["period"], r["stat"], r["entity"], r["line"], r["selection"])
                if ident in seen:
                    key = f"{key}#dup{r['id']}"
                    ident = (r["pick_date"], key, r["market"], r["period"], r["stat"], r["entity"], r["line"], r["selection"])
                seen.add(ident)
                self.conn.execute("UPDATE picks SET fixture_key=? WHERE id=?", (key, r["id"]))
        self.conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_picks_fixture_selection ON picks("
                          "pick_date, fixture_key, market, period, stat, entity, COALESCE(line, -999999), selection)")
        self.conn.commit()

    # ---- generic ----
    def execute(self, sql: str, params: Iterable = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self.conn.execute(sql, tuple(params))
            self.stats["executes"] += 1
            if not self._transaction_depth:
                self.conn.commit()
                self.stats["commits"] += 1
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
                    self.stats["commits"] += 1
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

    def latest_prediction_cache_row(self):
        """The most recently generated snapshot row, whatever its day (or None)."""
        rows = self.query("SELECT * FROM prediction_cache ORDER BY generated_at DESC LIMIT 1")
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

    def deliveries_exist(self, kind: str, post_date: str) -> bool:
        """True once ANY subscriber has been sent ``kind`` for ``post_date`` (``picks`` = either of its two messages)."""
        kinds = ("picks", "picks_top", "picks_daily") if kind == "picks" else (kind,)
        marks = ",".join("?" for _ in kinds)
        return bool(self.query(f"SELECT 1 FROM deliveries WHERE kind IN ({marks}) AND post_date=? LIMIT 1", (*kinds, post_date)))

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

    # ---- SportsGameOdds attribution ----
    def sgo_usage_add(self, reason: str, endpoint: str, status: str, *, sport: str = "", league: str = "",
                      event_id: str = "", http_attempts: int = 1, objects_billed: int = 0, note: str = "") -> None:
        ts = _now()
        self.execute("INSERT INTO sgo_usage(ts,day,reason,endpoint,sport,league,event_id,http_attempts,objects_billed,"
                     "status,note) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                     (ts, ts[:10], reason, endpoint, sport or None, league or None, event_id or None,
                      int(http_attempts), int(objects_billed), status, note or None))

    def sgo_usage_summary(self, since_day: str) -> List[sqlite3.Row]:
        """Per (day, reason, endpoint, status): requests, HTTP attempts, billed objects."""
        return self.query("SELECT day, reason, endpoint, status, COUNT(*) AS requests, SUM(http_attempts) AS attempts, "
                          "SUM(objects_billed) AS objects FROM sgo_usage WHERE day>=? "
                          "GROUP BY day, reason, endpoint, status ORDER BY day, reason", (since_day,))

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
        if not row.get("fixture_key"):
            from predbot.util.fixtures import row_fixture_key
            row["fixture_key"] = row_fixture_key(row)
        if isinstance(row.get("sources"), dict):
            row["sources"] = json.dumps(row["sources"])
        cols = ",".join(row.keys())
        qs = ",".join("?" for _ in row)
        cur = self.execute(f"INSERT INTO picks({cols}) VALUES({qs})", list(row.values()))
        pid = int(cur.lastrowid)
        self.add_membership(pid, 'all', row.get('rank', 0))
        if row.get('tier') in ('top', 'daily'):
            self.add_membership(pid, row['tier'], row.get('rank', 0))
        return pid

    def add_membership(self, prediction_id, collection, rank=0):
        self.execute("INSERT OR IGNORE INTO prediction_memberships VALUES(?,?,?)", (prediction_id, collection, rank))

    def collection_predictions(self, collection='all', pick_date=None, settled_only=False):
        if collection not in ('all', 'top', 'daily'):
            raise ValueError('unknown prediction collection')
        where, params = ['m.collection=?'], [collection]
        if pick_date is not None:
            where.append('p.pick_date=?')
            params.append(pick_date)
        if settled_only:
            where.append("p.status<>'pending'")
        return self.query("SELECT p.*,m.collection AS collection,m.rank AS collection_rank FROM picks p "
                          "JOIN prediction_memberships m ON m.prediction_id=p.id WHERE " + ' AND '.join(where) +
                          " ORDER BY p.pick_date DESC,m.rank,p.id", params)

    def store_prediction(self, row):
        """Immutable first prediction for a fixture x market family on a day, irrespective of collection.
        A fixture may hold several predictions (one per family); a stored one is never replaced or updated.

        "Same fixture x family" is recognised two ways: by canonical event id, and - for rows stored before canonical
        ids existed (NULL) or under another provider's event id - by ``fixture_key`` and the family's own coordinates
        (market, period, stat, entity). Without the second, such a row slips past the first check and the insert then
        collides with ``uq_picks_fixture_selection`` (the exact-duplicate guard on fixture_key)."""
        existing = self.query("SELECT id FROM picks WHERE pick_date=? AND canonical_event_id=? AND market=?",
                              (row['pick_date'], row['canonical_event_id'], row['market']))
        if not existing and row.get('fixture_key'):
            existing = self.query("SELECT id FROM picks WHERE pick_date=? AND fixture_key=? AND market=? AND period=? "
                                  "AND stat=? AND entity=? ORDER BY id LIMIT 1",
                                  (row['pick_date'], row['fixture_key'], row['market'], row.get('period', 'full'),
                                   row.get('stat', 'points'), row.get('entity', 'all')))
        if existing:
            return int(existing[0]['id'])
        try:
            return self.insert_pick(dict(row, tier='all'))
        except sqlite3.IntegrityError:                  # lost a race / an index we did not anticipate: first write wins
            winner = self.query("SELECT id FROM picks WHERE pick_date=? AND fixture_key=? AND market=? AND period=? "
                                "AND stat=? AND entity=? AND selection=? ORDER BY id LIMIT 1",
                                (row['pick_date'], row.get('fixture_key') or '', row['market'], row.get('period', 'full'),
                                 row.get('stat', 'points'), row.get('entity', 'all'), row['selection']))
            if not winner:
                raise
            return int(winner[0]['id'])

    def reset_prediction_day(self, day, timezone, now_utc, dry_run=False):
        """Invalidate only the shared snapshot and the unpublished future pending pool for ``day``.

        Provider raw data/metadata, settled records, started fixtures, history and quota accounting survive,
        so the next read regenerates once from cached provider data without spending quota. A published day is
        refused rather than altering cards subscribers already hold. ``dry_run`` reports without deleting."""
        date.fromisoformat(day)
        if self.deliveries_exist('picks', day) or self.query("SELECT 1 FROM posts WHERE post_date=? "
                                                          "AND kind IN ('picks','picks_top','picks_daily')", (day,)):
            raise ValueError('cannot reset a published prediction day')
        key = f'daily_predictions:{timezone}:{day}'
        with self.prediction_lock, self.transaction():
            ids = [r['id'] for r in self.query("SELECT id FROM picks WHERE pick_date=? AND status='pending' "
                                               "AND start_utc>?", (day, now_utc))]
            kept = self.query("SELECT COUNT(*) AS n FROM picks WHERE pick_date=?", (day,))[0]['n'] - len(ids)
            if not dry_run:
                for pid in ids:
                    self.execute('DELETE FROM prediction_memberships WHERE prediction_id=?', (pid,))
                    self.execute('DELETE FROM picks WHERE id=?', (pid,))
                self.delete_prediction_cache(key)
        return {'snapshot_key': key, 'pending_predictions_removed': len(ids), 'dry_run': dry_run,
                'predictions_kept_for_day': kept, 'provider_cache_preserved': True, 'history_preserved': True}

    _WITH_COLLECTIONS = (
        "SELECT p.*, "
        "(SELECT group_concat(collection) FROM prediction_memberships m WHERE m.prediction_id=p.id) AS collections, "
        "(SELECT rank FROM prediction_memberships m WHERE m.prediction_id=p.id AND m.collection='top') AS top_rank, "
        "(SELECT rank FROM prediction_memberships m WHERE m.prediction_id=p.id AND m.collection='daily') AS daily_rank "
        "FROM picks p ")

    def picks_where(self, where: str = "1=1", params=(), published_only: bool = False) -> List[Dict]:
        """Canonical prediction rows with their collection memberships attached.

        ``collections`` is the set of memberships (all/top/daily); ``tier`` is derived for display code that
        distinguishes published picks - 'top' if the prediction is in Top Picks, else 'daily' if in Daily Picks, else
        'all' - and ``rank`` is its rank in that collection. One row per prediction, whatever it belongs to."""
        out = []
        for r in self.query(self._WITH_COLLECTIONS + "WHERE " + where, tuple(params)):
            d = dict(r)
            cols = set(str(d.pop("collections") or "").split(",")) - {""}
            top_rank, daily_rank = d.pop("top_rank"), d.pop("daily_rank")
            d["collections"] = cols
            d["tier"] = "top" if "top" in cols else "daily" if "daily" in cols else "all"
            if d["tier"] == "top":
                d["rank"] = top_rank
            elif d["tier"] == "daily":
                d["rank"] = daily_rank
            if published_only and d["tier"] == "all":
                continue
            out.append(d)
        order = {"top": 0, "daily": 1, "all": 2}
        out.sort(key=lambda d: (d["pick_date"], order[d["tier"]], d["rank"] or 0, d["id"]))
        return out

    def picks_for_date(self, pick_date: str, published_only: bool = False) -> List[Dict]:
        return self.picks_where("p.pick_date=?", (pick_date,), published_only)

    def has_picks_for_date(self, pick_date: str) -> bool:
        return bool(self.query("SELECT 1 FROM picks WHERE pick_date=? LIMIT 1", (pick_date,)))

    def pending_picks(self, before_utc: Optional[str] = None) -> List[sqlite3.Row]:
        if before_utc:
            return self.query("SELECT * FROM picks WHERE status='pending' AND start_utc<=? ORDER BY start_utc", (before_utc,))
        return self.query("SELECT * FROM picks WHERE status='pending' ORDER BY start_utc")

    def live_picks(self, earliest: str, latest: str) -> List[Dict]:
        """All predictions on due pending fixtures that reached Top or Daily, plus durable unfinished lifecycle work.

        Fixtures only in All Predictions are settled by the ordinary settlement job, not polled live.

        Terminal delivery retries are not tied to pending picks or the stale watch
        window. Nonterminal watches expire with the ordinary settlement safety net.
        """
        return self.picks_where("""
            EXISTS (    -- live status is only worth provider calls for fixtures a subscriber was actually sent
                SELECT 1 FROM prediction_memberships pm JOIN picks pp ON pp.id=pm.prediction_id
                WHERE pp.pick_date=p.pick_date AND pp.fixture_key=p.fixture_key
                AND pm.collection IN ('top','daily')
            ) AND (EXISTS (
                SELECT 1 FROM picks q WHERE q.pick_date=p.pick_date
                AND q.fixture_key=p.fixture_key AND q.status='pending'
                AND q.start_utc BETWEEN ? AND ?
            ) OR EXISTS (
                SELECT 1 FROM live_fixture_state s WHERE s.pick_date=p.pick_date
                AND s.fixture_key=p.fixture_key AND s.notifications_done=0
                AND (s.status IN ('final','postponed','cancelled') OR p.start_utc BETWEEN ? AND ?)
            ))
        """, (earliest, latest, earliest, latest))

    def live_state(self, pick_date: str, fixture_key: str):
        rows = self.query("SELECT * FROM live_fixture_state WHERE pick_date=? AND fixture_key=?",
                          (pick_date, fixture_key))
        return rows[0] if rows else None

    def save_live_result(self, pick_date: str, fixture_key: str, result, checked_at: str) -> None:
        """Persist the complete result, including tuple-keyed stats, for crash recovery."""
        payload = {"status": result.status, "scores": result.scores, "source": result.source,
                   "stats": [[stat, period, list(value)] for (stat, period), value in result.stats.items()],
                   "odd_scores": result.odd_scores}
        self.execute("""
            INSERT INTO live_fixture_state
                (pick_date,fixture_key,status,started_at,final_at,last_checked_at,result_source,result_json,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(pick_date,fixture_key) DO UPDATE SET
                status=excluded.status,
                started_at=COALESCE(live_fixture_state.started_at,excluded.started_at),
                final_at=COALESCE(live_fixture_state.final_at,excluded.final_at),
                last_checked_at=excluded.last_checked_at,result_source=excluded.result_source,
                result_json=excluded.result_json,updated_at=excluded.updated_at
        """, (pick_date, fixture_key, result.status,
              checked_at if result.status == "in_progress" else None,
              checked_at if result.status == "final" else None,
              checked_at, result.source, json.dumps(payload), checked_at))

    def complete_live_settlement(self, pick_date: str, fixture_key: str) -> None:
        self.execute("UPDATE live_fixture_state SET settlement_done=1,updated_at=? "
                     "WHERE pick_date=? AND fixture_key=?", (_now(), pick_date, fixture_key))

    def complete_live_notifications(self, pick_date: str, fixture_key: str) -> None:
        self.execute("UPDATE live_fixture_state SET notifications_done=1,updated_at=? "
                     "WHERE pick_date=? AND fixture_key=?", (_now(), pick_date, fixture_key))

    def settle_pick(self, pick_id: int, status: str, result: Optional[Dict] = None) -> None:
        self.execute("UPDATE picks SET status=?, settled_at=?, result_json=? WHERE id=? AND status='pending'",
                     (status, _now(), json.dumps(result) if result else None, pick_id))

    def all_settled_for_calibration(self, limit: int = 3000) -> List[sqlite3.Row]:
        return self.query(
            "SELECT prob, prob_raw, status, price FROM picks WHERE status IN ('won','lost','half_won','half_lost') "
            "ORDER BY id DESC LIMIT ?", (limit,))

    def previous_pick_fixtures(self, before_date: str, lookback_days: int = 2) -> List[sqlite3.Row]:
        """sport/home/away/start_utc of picks stored on the ``lookback_days``
        days strictly before ``before_date`` - used to keep a late-evening
        fixture from being posted on two consecutive days. Bounded on purpose:
        an all-time lookup would also drop a genuine rematch months later."""
        since = (date.fromisoformat(before_date) - timedelta(days=lookback_days)).isoformat()
        return self.query("SELECT DISTINCT sport, home, away, start_utc FROM picks "
                          "WHERE pick_date<? AND pick_date>=?", (before_date, since))

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

    def deliveries_done(self, kind: str, post_date: str) -> set:
        """chat ids already sent ``kind`` for ``post_date`` - one query instead of one per subscriber."""
        return {str(r["chat_id"]) for r in self.query("SELECT chat_id FROM deliveries WHERE kind=? AND post_date=?",
                                                     (kind, post_date))}

    def mark_delivery(self, kind: str, post_date: str, chat_id: str) -> None:
        self.execute("INSERT OR IGNORE INTO deliveries(kind,post_date,chat_id,created_at) VALUES(?,?,?,?)",
                     (kind, post_date, str(chat_id), _now()))
