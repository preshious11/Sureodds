"""SQLite persistence. One file, WAL mode, safe for the single-process worker."""
from __future__ import annotations

import json
import hashlib
import math
import os
import sqlite3
import threading
import logging
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS custom_pick_candidates (
    prediction_date TEXT NOT NULL,
    timezone TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    canonical_event_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    PRIMARY KEY (timezone, prediction_date, candidate_id)
);
CREATE TABLE IF NOT EXISTS custom_pick_pools (
    prediction_date TEXT NOT NULL,
    timezone TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('COMPLETE','PARTIAL')),
    diagnostics_json TEXT NOT NULL,
    PRIMARY KEY (timezone, prediction_date)
);
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
CREATE TABLE IF NOT EXISTS ticket_candidates (
    pick_date TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY(pick_date, candidate_id)
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
    _publication_locks = {}
    _prediction_locks_guard = threading.Lock()

    def __init__(self, path: str):
        self.path = path
        logging.getLogger(__name__).info("SQLite database path: %s | exists=%s",
                                        os.path.abspath(path), os.path.isfile(path))
        with self._prediction_locks_guard:
            key = os.path.normcase(os.path.abspath(path)) if path != ':memory:' else id(self)
            self.prediction_lock = self._prediction_locks.setdefault(key, threading.RLock())
            self.publication_lock = self._publication_locks.setdefault(key, threading.RLock())
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
        if 'is_representative' not in pick_cols:
            self.conn.execute('ALTER TABLE picks ADD COLUMN is_representative INTEGER NOT NULL DEFAULT 1')
        self._migrate_prediction_uniqueness()
        if self.get_kv('migration:prediction_memberships:v1') != 'done':
            self.conn.execute("INSERT OR IGNORE INTO prediction_memberships SELECT id,'all',rank FROM picks")
            self.conn.execute("INSERT OR IGNORE INTO prediction_memberships SELECT id,tier,rank FROM picks WHERE tier IN ('top','daily')")
            self.conn.execute("INSERT OR REPLACE INTO kv VALUES('migration:prediction_memberships:v1','done')")
        self.conn.commit()

    def _migrate_prediction_uniqueness(self) -> None:
        """One representative per fixture/family; ticket-only alternates may coexist.
        Existing rows default to representatives. No forecast/settlement is rewritten.
        Exact-outcome uniqueness still prevents duplicate bets across collections.
        """
        self.conn.execute("DROP INDEX IF EXISTS uq_prediction_event_day")
        self.conn.execute("DROP INDEX IF EXISTS uq_prediction_event_family_day")
        self.conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_prediction_event_family_day "
                          "ON picks(pick_date,canonical_event_id,market) "
                          "WHERE canonical_event_id IS NOT NULL AND canonical_event_id<>'' AND is_representative=1")

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

    def store_custom_pick_candidates(self, prediction_date, timezone, candidates, *, generated_at,
                                     complete, diagnostics):
        """Atomic, non-destructive daily union. Latest evaluation wins per bet.

        Missing candidates are never deleted, including on an empty/failed retry.
        Each record retains its evaluation time; this is an archive, not fresh odds.
        Called inside the normal prediction publication transaction. No providers.
        """
        date.fromisoformat(prediction_date)
        if not timezone:
            raise ValueError('candidate archive requires the prediction timezone')

        def clean(value):
            # Optional nonfinite evaluated/provider fields cannot be JSON numbers.
            # Required price/probability were already validated by the engine.
            if isinstance(value, float) and not math.isfinite(value):
                return None
            if isinstance(value, dict):
                return {k: clean(v) for k, v in value.items()}
            if isinstance(value, (list, tuple)):
                return [clean(v) for v in value]
            return value

        with self.transaction():
            for candidate in candidates:
                coordinates = [candidate['canonical_event_id'], candidate['market'], candidate['period'],
                               candidate['stat'], candidate['entity'],
                               None if candidate['line'] is None else float(candidate['line']) or 0.0,
                               candidate['selection'], candidate.get('home_id') or '',
                               candidate.get('away_id') or '', candidate.get('home') or '',
                               candidate.get('away') or '']
                identity = json.dumps(coordinates, separators=(',', ':'), ensure_ascii=True, allow_nan=False)
                candidate_id = hashlib.sha256(identity.encode('utf-8')).hexdigest()
                payload = json.dumps(clean(candidate), sort_keys=True, allow_nan=False)
                self.execute(
                    'INSERT INTO custom_pick_candidates VALUES(?,?,?,?,?,?,?,?) '
                    'ON CONFLICT(timezone,prediction_date,candidate_id) DO UPDATE SET '
                    'payload_json=excluded.payload_json,generated_at=excluded.generated_at,'
                    'schema_version=excluded.schema_version '
                    'WHERE excluded.generated_at>=custom_pick_candidates.generated_at',
                    (prediction_date, timezone, candidate_id, candidate['canonical_event_id'], payload,
                     generated_at, generated_at, 1))
            # An empty failed refresh must not overwrite usable pool provenance.
            if candidates or complete or not self.get_custom_pick_pool(prediction_date, timezone):
                self.execute(
                    'INSERT INTO custom_pick_pools VALUES(?,?,?,?,?) '
                    'ON CONFLICT(timezone,prediction_date) DO UPDATE SET generated_at=excluded.generated_at,'
                    'status=excluded.status,diagnostics_json=excluded.diagnostics_json '
                    'WHERE excluded.generated_at>=custom_pick_pools.generated_at',
                    (prediction_date, timezone, generated_at, 'COMPLETE' if complete else 'PARTIAL',
                     json.dumps(clean(diagnostics), sort_keys=True, allow_nan=False)))
            counts = self.query('SELECT COUNT(*) AS candidates,COUNT(DISTINCT canonical_event_id) AS fixtures '
                                'FROM custom_pick_candidates WHERE prediction_date=? AND timezone=?',
                                (prediction_date, timezone))
            return dict(counts[0])

    def get_custom_pick_candidates(self, prediction_date, timezone):
        """SQLite SELECT only. Missing dates return []; never generate or refresh.

        Returned dictionaries are detached copies, scoped to the application date
        AND timezone. They are archived evaluations, not settlement predictions.
        """
        date.fromisoformat(prediction_date)
        rows = self.query('SELECT * FROM custom_pick_candidates WHERE prediction_date=? AND timezone=? '
                          'ORDER BY canonical_event_id,candidate_id', (prediction_date, timezone))
        return [dict(json.loads(r['payload_json']), prediction_date=r['prediction_date'], timezone=r['timezone'],
                     candidate_id=r['candidate_id'], first_seen_at=r['first_seen_at'], generated_at=r['generated_at'],
                     archive_schema_version=r['schema_version']) for r in rows]

    def get_custom_pick_pool(self, prediction_date, timezone):
        """SQLite-only provenance/coverage read; None means no captured pool."""
        date.fromisoformat(prediction_date)
        rows = self.query('SELECT * FROM custom_pick_pools WHERE prediction_date=? AND timezone=?',
                          (prediction_date, timezone))
        if not rows:
            return None
        row = dict(rows[0])
        row['diagnostics'] = json.loads(row.pop('diagnostics_json'))
        return row

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
        # Self-atomic: the pick row and its memberships commit together, so a
        # failure between the writes cannot leave a pick without its ``all``
        # membership (invisible to every JOIN-based read). Safe under an outer
        # ``transaction()``: the context manager nests via _transaction_depth and
        # only the outermost level commits/rolls back.
        with self.transaction():
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
            if row.get('is_representative', 1):
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
        with self.transaction():
            return self._store_representative(row)

    def _store_representative(self, row):
        """Immutable first prediction for a fixture x market family on a day, irrespective of collection.
        A fixture may hold several predictions (one per family); a stored one is never replaced or updated.

        "Same fixture x family" is recognised two ways: by canonical event id, and - for rows stored before canonical
        ids existed (NULL) or under another provider's event id - by ``fixture_key`` and the family's own coordinates
        (market, period, stat, entity). Without the second, such a row slips past the first check and the insert then
        collides with ``uq_picks_fixture_selection`` (the exact-duplicate guard on fixture_key)."""
        existing = self.query("SELECT id FROM picks WHERE pick_date=? AND canonical_event_id=? AND market=? AND EXISTS (SELECT 1 FROM prediction_memberships WHERE prediction_id=picks.id AND collection='all')",
                              (row['pick_date'], row['canonical_event_id'], row['market']))
        if not existing and row.get('fixture_key'):
            existing = self.query("SELECT id FROM picks WHERE pick_date=? AND fixture_key=? AND market=? AND period=? "
                                  "AND stat=? AND entity=? AND EXISTS (SELECT 1 FROM prediction_memberships WHERE prediction_id=picks.id AND collection='all') ORDER BY id LIMIT 1",
                                  (row['pick_date'], row['fixture_key'], row['market'], row.get('period', 'full'),
                                   row.get('stat', 'points'), row.get('entity', 'all')))
        if existing:
            return int(existing[0]['id'])
        exact = self._exact_prediction(row)
        if exact:
            # A previously promoted alternate can become this family's All
            # representative. Reuse its immutable forecast; never rewrite it.
            pid = int(exact[0]['id'])
            self.add_membership(pid, 'all', row.get('rank', 0))
            return pid
        if row.get('is_representative', 1) and row.get('canonical_event_id'):
            # Mirror of uq_prediction_event_family_day: a representative row
            # for this (day, event, market) already exists but carries no 'all'
            # membership (partial import or legacy write), so none of the
            # checks above could see it while the unique index still forbids a
            # second one. First write wins: keep the stored forecast, adopt it
            # into 'all' so it becomes visible, and never rewrite history.
            clash = self.query("SELECT id, selection, line, period, stat, entity, status FROM picks "
                               "WHERE pick_date=? AND canonical_event_id=? AND market=? "
                               "AND is_representative=1 ORDER BY id LIMIT 1",
                               (row['pick_date'], row['canonical_event_id'], row['market']))
            if clash:
                pid = int(clash[0]['id'])
                logging.getLogger(__name__).warning(
                    "representative collision kept first write | day=%s event=%s market=%s "
                    "kept=%s dropped_selection=%s",
                    row['pick_date'], row['canonical_event_id'], row['market'],
                    dict(clash[0]), row.get('selection'))
                self.add_membership(pid, 'all', row.get('rank', 0))
                return pid
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

    @staticmethod
    def _candidate_identity(row):
        coords = [row['canonical_event_id'], row['market'], row['period'], row['stat'],
                  row['entity'], row['line'], row['selection']]
        return hashlib.sha256(json.dumps(coords, sort_keys=True).encode()).hexdigest()

    def archive_ticket_candidate(self, row):
        """Refresh UNUSED evaluations before publication; issued forecasts are
        first-write-wins. This pool is not settlement history. A partial retry
        may add coverage for a bet that has never been recorded as a forecast.
        """
        identity = self._candidate_identity(row)
        frozen = bool(self._exact_prediction(row) or self.deliveries_exist('picks', row['pick_date'])
                      or self.query("SELECT 1 FROM posts WHERE post_date=? AND kind IN ('picks','picks_top','picks_daily')",
                                    (row['pick_date'],)))
        clause = '' if frozen else 'ON CONFLICT(pick_date,candidate_id) DO UPDATE SET payload_json=excluded.payload_json'
        self.execute('INSERT OR IGNORE INTO ticket_candidates VALUES(?,?,?) '+clause,
                     (row['pick_date'], identity, json.dumps(row, sort_keys=True, allow_nan=False)))

    def ticket_candidate_rows(self, day):
        rows = []
        for item in self.query('SELECT * FROM ticket_candidates WHERE pick_date=? ORDER BY candidate_id', (day,)):
            row = json.loads(item['payload_json'])
            row.update(id=item['candidate_id'], status='pending')
            if isinstance(row['sources'], dict):
                row['sources'] = json.dumps(row['sources'])
            # An already issued forecast is immutable, including across retries.
            existing = self._exact_prediction(row)
            if existing:
                stored = dict(existing[0])
                incoming_meta = json.loads(row['sources']).get('_notes') or {}
                stored_meta = json.loads(stored.get('sources') or '{}').get('_notes') or {}
                if incoming_meta.get('probability_basis') == 'strict_win_v2' and stored_meta.get('probability_basis') != 'strict_win_v2':
                    # Preserve the legacy forecast, but do not rank conditional
                    # legacy confidence against corrected strict-win candidates.
                    continue
                row = stored
            rows.append(row)
        return rows

    def _exact_prediction(self, row):
        return self.query('SELECT * FROM picks WHERE pick_date=? AND canonical_event_id=? '
                          'AND market=? AND period=? AND stat=? AND entity=? AND line IS ? AND selection=?',
                          (row['pick_date'], row['canonical_event_id'], row['market'], row['period'],
                           row['stat'], row['entity'], row['line'], row['selection']))

    def store_ticket_prediction(self, row):
        existing = self._exact_prediction(row)
        if existing:
            return existing[0]['id']
        values = dict(row)
        values.pop('id', None)
        values.pop('status', None)
        values['is_representative'] = 0
        return self.insert_pick(values)

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
                self.execute("DELETE FROM ticket_candidates WHERE pick_date=? AND json_extract(payload_json, '$.start_utc')>?", (day, now_utc))
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

    #: A prediction is PUBLISHED when it belongs to the Top or Daily collection. All-only predictions are generated
    #: and browsable (/all) but are never tracked, settled, announced or counted in results.
    _PUBLISHED = ("EXISTS (SELECT 1 FROM prediction_memberships pm WHERE pm.prediction_id=picks.id "
                  "AND pm.collection IN ('top','daily'))")

    def pending_picks(self, before_utc: Optional[str] = None, published_only: bool = False) -> List[sqlite3.Row]:
        extra = f" AND {self._PUBLISHED}" if published_only else ""
        if before_utc:
            return self.query(f"SELECT * FROM picks WHERE status='pending' AND start_utc<=?{extra} ORDER BY start_utc", (before_utc,))
        return self.query(f"SELECT * FROM picks WHERE status='pending'{extra} ORDER BY start_utc")

    def live_picks(self, earliest: str, latest: str, require_receipt: bool = False) -> List[Dict]:
        """All predictions on due pending fixtures that reached Top or Daily, plus durable unfinished lifecycle work.

        Fixtures only in All Predictions are settled by the ordinary settlement job, not polled live.
        Only predictions that are themselves Top/Daily members are returned (never an All-only sibling on the same
        fixture). ``require_receipt`` (the D1 path) additionally demands official publication of the day: a ``posts``
        receipt for the collection OR a COMPLETE prediction cache. Selection alone is not publication.

        Terminal delivery retries are not tied to pending picks or the stale watch
        window. Nonterminal watches expire with the ordinary settlement safety net.
        """
        # Publication = the prediction itself is a Top/Daily member AND its day was officially published: the card
        # was posted (a `posts` receipt) OR the day's cache is COMPLETE (what /top and /daily serve). A failed
        # Telegram send must not permanently stop legitimate Top/Daily fixtures being tracked.
        published = ("AND (EXISTS (SELECT 1 FROM posts po WHERE po.post_date=p.pick_date "
                     "AND po.kind IN (pm.collection, 'picks_' || pm.collection)) "
                     "OR EXISTS (SELECT 1 FROM prediction_cache pc WHERE pc.local_date=p.pick_date "
                     "AND pc.status='COMPLETE'))") if require_receipt else ""
        return self.picks_where(f"""
            EXISTS (    -- this prediction itself is a published selection (never an All-only sibling)
                SELECT 1 FROM prediction_memberships pm
                WHERE pm.prediction_id=p.id AND pm.collection IN ('top','daily') {published}
            ) AND (EXISTS (
                SELECT 1 FROM picks q WHERE q.pick_date=p.pick_date
                AND q.fixture_key=p.fixture_key AND q.status='pending'
                AND q.start_utc BETWEEN ? AND ?
                AND EXISTS (SELECT 1 FROM prediction_memberships qm WHERE qm.prediction_id=q.id AND qm.collection IN ('top','daily'))
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
                   "odd_scores": result.odd_scores, "overtime": getattr(result, "overtime", None)}
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
            "SELECT prob, prob_raw, status, price, sources, sport, market, period, line FROM picks WHERE status IN ('won','lost','half_won','half_lost','push') "
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
