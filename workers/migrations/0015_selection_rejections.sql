-- Durable rejection log for select_top()/select_daily() (rejection-logging investigation, 2026-10-23).
-- Previously a rejected candidate was only counted into an in-memory Counter (predbot/selection/top.py's
-- ``rejected``) that was discarded after the diagnostics dict was built. This table makes it durable so a
-- rejected candidate can later be joined to its real match result (via canonical_event_id ->
-- canonical_events -> match_results, the identity space predbot/storage/identity.py and
-- predbot/history/store.py already use) for hypothetical "what if we'd picked it" grading.
--
-- Mirrors predictions-bot/predbot/storage/db.py's local-sqlite ``selection_rejections`` table
-- column-for-column (same convention as every other table shared between D1 and the Python engine).
--
-- Volume control (predbot/selection/rejections.py): full-detail rows for every rejection reason except
-- below_probability / disabled_market / insufficient_books. Those three get one summary row per
-- (pick_date, product, reason) with count = that day's total, plus at most ``rejection_sample_per_reason``
-- full-detail rows per (pick_date, product, reason), chosen by a deterministic hash of candidate identity,
-- so they stay gradeable without storing every candidate.
--
-- Duplicates: every generation run before publication re-selects, so one day can produce several runs'
-- rejections for the same candidate. The unique index keeps the FIRST write per
-- (pick_date, product, reason, candidate_id) and the sync uses INSERT OR IGNORE, so repeated runs never
-- duplicate rows. run_id records the run that first wrote each row. Note this is the run that first
-- OBSERVED the rejection; if a later run published that candidate instead, the row is stale. Analysis must
-- exclude any rejected candidate that also appears among the day's published picks.
--
-- Selection output itself is never affected by this table - see predbot/selection/top.py and daily.py's
-- optional ``reject_sink`` parameter and the byte-identical-output test in tests/test_rejection_logging.py.
--
-- Rollback: purely additive; `DROP TABLE selection_rejections;` has no knock-on effect.

CREATE TABLE IF NOT EXISTS selection_rejections (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  pick_date TEXT NOT NULL,
  product TEXT NOT NULL CHECK(product IN ('top', 'daily')),
  -- Same identity as ticket_candidates/picks: a sha256 of
  -- (canonical_event_id, market, period, stat, entity, line, selection) - see
  -- predbot/storage/db.py's ``_candidate_identity``. NULL for a volume-controlled
  -- summary row (no single candidate to name).
  candidate_id TEXT,
  canonical_event_id TEXT,
  market TEXT,
  period TEXT,
  selection TEXT,
  line REAL,
  price REAL,
  prob REAL,
  ev REAL,
  rank_score REAL,
  quality TEXT,
  books INTEGER,
  reason TEXT NOT NULL,
  -- Only set when reason='duplicate_event': the candidate_id of the row that WON
  -- that fixture's slot instead (same identity scheme as candidate_id above).
  duplicate_winner_candidate_id TEXT,
  -- 1 for a full-detail row; the day's total for a summary row (candidate_id NULL).
  count INTEGER NOT NULL DEFAULT 1,
  -- 1 when a full-detail row is part of the deterministic sample of a volume-controlled reason.
  sampled INTEGER NOT NULL DEFAULT 0,
  -- Generation run that first wrote this row: "<GITHUB_RUN_ID or local>-<random8>".
  run_id TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_selection_rejections_date ON selection_rejections(pick_date, product, reason);
CREATE INDEX IF NOT EXISTS idx_selection_rejections_event ON selection_rejections(canonical_event_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_selection_rejections_key
  ON selection_rejections(pick_date, product, reason, COALESCE(candidate_id, ''));
