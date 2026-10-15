-- Stage 2: webhook reliability UPGRADE-ONLY migration.
-- Apply ONLY to D1 databases created from the Stage-1 0001_foundation.sql
-- (whose telegram_updates had just update_id + first_seen_at).
-- Fresh databases must apply 0001 + 0003 through 0008 and SKIP this file:
-- 0001 already declares these columns, and re-applying this file errors
-- with `duplicate column name` (verified in the Stage-2 drill). The CI
-- schema check reads all migration files and accepts either path.
ALTER TABLE telegram_updates ADD COLUMN status TEXT NOT NULL DEFAULT 'pending';
ALTER TABLE telegram_updates ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE telegram_updates ADD COLUMN last_error TEXT;
ALTER TABLE telegram_updates ADD COLUMN updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'));
CREATE INDEX IF NOT EXISTS idx_telegram_updates_retry ON telegram_updates(status, attempts, update_id);
