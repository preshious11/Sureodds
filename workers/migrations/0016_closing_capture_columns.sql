-- Closing-price capture columns on picks (predictions-bot/predbot/jobs/closing_price.py).
--
-- closing_fair_prob: the sharp-book fair probability at close (Shin de-vig, same method as
--   predbot/model/ensemble.py's _sharp_fair). NULL when no sharp book quoted the whole market.
-- closing_captured_at: when the closing snapshot was taken (ISO-8601 UTC).
--
-- These join closing_price and clv (0001_foundation.sql) as the four columns the capture job
-- owns. workers/scripts/d1_sync.py's write_policy() fills them once, only while D1's
-- closing_price is NULL, and excludes all four from the settlement guard's must-match set.
-- The capture job never writes picks.sources.
--
-- Mirrors predictions-bot/predbot/storage/db.py, which adds the same columns to a local
-- SQLite copy on open.
--
-- Rollback: SQLite/D1 cannot drop these without a table rebuild. They are nullable, unread by
-- the Worker and by selection, so leaving them in place is the rollback; stop the job instead.

ALTER TABLE picks ADD COLUMN closing_fair_prob REAL;
ALTER TABLE picks ADD COLUMN closing_captured_at TEXT;
