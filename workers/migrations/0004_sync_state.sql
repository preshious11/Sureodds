-- Stage 3: sync epoch for stale-snapshot protection.
-- Fresh installs apply 0001 + 0003 + 0004. The single row advances on every
-- accepted engine push; a push built on an older epoch is refused.
CREATE TABLE IF NOT EXISTS sync_state (
  id INTEGER PRIMARY KEY CHECK(id = 1),
  epoch INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
INSERT OR IGNORE INTO sync_state (id, epoch) VALUES (1, 0);
