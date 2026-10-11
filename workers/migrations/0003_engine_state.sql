-- Stage 2: remaining engine-state tables, verbatim from
-- predbot/storage/db.py SCHEMA (as created by the Python migrations
-- on the live DB). Idempotent CREATE TABLE IF NOT EXISTS.

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

CREATE TABLE IF NOT EXISTS canonical_events (
    id TEXT PRIMARY KEY, competition_id TEXT NOT NULL, home_id TEXT NOT NULL,
    away_id TEXT NOT NULL, kickoff_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS canonical_participants (id TEXT PRIMARY KEY, sport TEXT NOT NULL, name TEXT NOT NULL);

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

CREATE TABLE IF NOT EXISTS history_fetches (
    source TEXT NOT NULL, competition_id TEXT NOT NULL, season TEXT NOT NULL,
    status TEXT NOT NULL, rows INTEGER NOT NULL DEFAULT 0, fetched_at TEXT NOT NULL,
    PRIMARY KEY(source, competition_id, season)
);

CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT
);

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

CREATE TABLE IF NOT EXISTS match_odds (
    match_id INTEGER NOT NULL REFERENCES match_results(id),
    book TEXT NOT NULL,
    odds_home REAL NOT NULL, odds_draw REAL NOT NULL, odds_away REAL NOT NULL,
    PRIMARY KEY(match_id, book)
);

CREATE TABLE IF NOT EXISTS match_odds_ext (
    match_id INTEGER NOT NULL REFERENCES match_results(id),
    book TEXT NOT NULL, market TEXT NOT NULL, kind TEXT NOT NULL,
    o1 REAL NOT NULL, o2 REAL NOT NULL, o3 REAL, columns TEXT NOT NULL,
    PRIMARY KEY(match_id, book, market, kind)
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

CREATE TABLE IF NOT EXISTS participant_aliases (
    sport TEXT NOT NULL, alias TEXT NOT NULL, canonical_id TEXT NOT NULL,
    PRIMARY KEY(sport,alias)
);

CREATE TABLE IF NOT EXISTS provider_cache (
    cache_key TEXT PRIMARY KEY,
    local_date TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    cache_version INTEGER NOT NULL
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

-- Indexes (idempotent).
CREATE INDEX IF NOT EXISTS idx_match_results_comp ON match_results(competition_id, kickoff_utc);
CREATE INDEX IF NOT EXISTS idx_membership_collection ON prediction_memberships(collection,prediction_id);
CREATE INDEX IF NOT EXISTS idx_picks_date ON picks(pick_date);
CREATE INDEX IF NOT EXISTS idx_picks_status ON picks(status);
CREATE INDEX IF NOT EXISTS idx_sgo_usage_day ON sgo_usage(day);
CREATE UNIQUE INDEX IF NOT EXISTS uq_match_results_fixture ON match_results(competition_id, home_id, away_id, substr(kickoff_utc,1,10));
CREATE UNIQUE INDEX IF NOT EXISTS uq_picks_fixture_selection ON picks(pick_date, fixture_key, market, period, stat, entity, COALESCE(line, -999999), selection);
CREATE UNIQUE INDEX IF NOT EXISTS uq_prediction_event_family_day ON picks(pick_date,canonical_event_id,market) WHERE canonical_event_id IS NOT NULL AND canonical_event_id<>'';
