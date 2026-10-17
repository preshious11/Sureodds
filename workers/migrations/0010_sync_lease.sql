-- A2: the shared D1-write serialization lease (sync_write), used by every
-- production writer workflow (generation, live monitor, settlement) so
-- they never apply to D1 at the same time — plus job_state tracking for
-- the Worker's own on-demand live-monitor dispatch (bug 6).
--
-- INSERT OR IGNORE: the UPDATE-only lease-acquire pattern (mirroring the
-- existing 'generate' lease in stage5-acceptance-production.yml - an
-- UPDATE ... WHERE name='generate' with no matching INSERT) does nothing
-- if its row doesn't already exist, which is exactly why 'generate' is
-- pre-seeded in 0001_foundation.sql. sync_write and livemon_dispatch need
-- the same seeding to be lease-acquirable/recordable from a fresh DB.
-- 'settle' is already seeded in 0001_foundation.sql; included here too
-- (harmlessly, via IGNORE) since this migration's job is "ensure all three
-- exist", not "assume which already do".
INSERT OR IGNORE INTO job_state (name, due_at, updated_at) VALUES
  ('sync_write', NULL, strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  ('settle', NULL, strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  ('livemon_dispatch', NULL, strftime('%Y-%m-%dT%H:%M:%SZ','now'));
