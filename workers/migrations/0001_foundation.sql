-- SureOdds D1 foundation schema (Stage 1).
-- Mirrors predictions-bot/predbot/storage/db.py table/column names so the
-- Python engine can read/write without logic changes in Stage 2.
-- D1 is SQLite-compatible; UNIQUE/ON CONFLICT semantics are preserved.
-- Production SQLite is NEVER altered by these scripts.

CREATE TABLE IF NOT EXISTS telegram_updates (
  update_id INTEGER PRIMARY KEY,
  first_seen_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  -- Stage 2 reliability lifecycle (0002 adds these on existing DBs; new DBs
  -- get them here so fresh staging works with a single file).
  status TEXT NOT NULL DEFAULT 'pending',
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_telegram_updates_retry ON telegram_updates(status, attempts, update_id);

CREATE TABLE IF NOT EXISTS subscribers (
  chat_id TEXT PRIMARY KEY,
  username TEXT,
  first_name TEXT,
  timezone TEXT NOT NULL DEFAULT '',
  subscribed_at TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS picks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  pick_date TEXT NOT NULL,
  tier TEXT NOT NULL,
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
  prob_raw REAL,
  sources TEXT,
  provider_odd_id TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  settled_at TEXT,
  result_json TEXT,
  created_at TEXT NOT NULL,
  -- Added by later Python migrations (predbot/storage/db.py); nullable so
  -- old and new engine versions interoperate. Never used for Stage-1 reads.
  p_fair REAL,
  edge_ev REAL,
  rank_score REAL,
  books INTEGER,
  quality TEXT,
  closing_price REAL,
  clv REAL,
  fixture_key TEXT,
  canonical_event_id TEXT,
  canonical_competition_id TEXT,
  home_id TEXT,
  away_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_picks_date_tier ON picks(pick_date, tier, rank);
CREATE INDEX IF NOT EXISTS idx_picks_status ON picks(status);

CREATE TABLE IF NOT EXISTS prediction_memberships (
  prediction_id INTEGER NOT NULL,
  collection TEXT NOT NULL CHECK(collection IN ('all','top','daily')),
  rank INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (prediction_id, collection)
);
CREATE INDEX IF NOT EXISTS idx_membership_collection ON prediction_memberships(collection, prediction_id);

CREATE TABLE IF NOT EXISTS prediction_cache (
  cache_key TEXT PRIMARY KEY,
  local_date TEXT NOT NULL,
  timezone TEXT NOT NULL,
  status TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  sport_status_json TEXT NOT NULL DEFAULT '{}',
  generated_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  retry_after TEXT,
  cache_version INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_prediction_cache_date ON prediction_cache(local_date, timezone);

CREATE TABLE IF NOT EXISTS posts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  post_date TEXT NOT NULL,
  message_id TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(kind, post_date)
);

CREATE TABLE IF NOT EXISTS deliveries (
  kind TEXT NOT NULL,
  post_date TEXT NOT NULL,
  chat_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(kind, post_date, chat_id)
);

-- Stage 2/3 job coordination: persistent due timestamps + leases replace the
-- in-memory DailyJob/IntervalJob/DynamicJob state. Seeded idempotently.
CREATE TABLE IF NOT EXISTS job_state (
  name TEXT PRIMARY KEY,
  due_at TEXT,
  lease_owner TEXT,
  lease_until TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  last_run_at TEXT,
  last_result TEXT,
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
INSERT OR IGNORE INTO job_state (name, due_at, updated_at) VALUES
  ('generate', NULL, strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  ('deliver', NULL, strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  ('settle', NULL, strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  ('recap', NULL, strftime('%Y-%m-%dT%H:%M:%SZ','now'));
