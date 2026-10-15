-- Normalized match-start/match-end events, written by the GitHub Actions
-- live-monitoring workflow (predbot/jobs/live_monitor_d1.py, reusing the
-- existing tested Python live-tracking/settlement engine against a D1-synced
-- scratch copy) and consumed by the Worker's own Cron sweep, which is the
-- ONLY thing that ever sends these to Telegram (GitHub Actions never sends).
--
-- Deliberately keyed by the natural (pick_date, fixture_key, event_type)
-- triple rather than a surrogate autoincrement id: an autoincrement id
-- assigned in the GitHub Actions scratch SQLite copy has no relationship to
-- D1's own sequence, and syncing it across would risk silent id collisions
-- or require the same id-rebase machinery d1_sync.py's "rebase" step exists
-- for elsewhere. The natural key needs no rebasing and IS the duplicate-
-- processing guard: INSERT OR IGNORE on a repeat detection is a no-op.
CREATE TABLE IF NOT EXISTS match_events (
  pick_date TEXT NOT NULL,
  fixture_key TEXT NOT NULL,
  event_type TEXT NOT NULL CHECK(event_type IN ('start', 'end')),
  text TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (pick_date, fixture_key, event_type)
);
CREATE INDEX IF NOT EXISTS idx_match_events_created ON match_events(created_at);
