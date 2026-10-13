-- PREPARED ONLY. Run against an explicitly confirmed database/configuration.
-- All statements inspect data/schema; no DDL, DML, migration or sync commands.
-- Requires the existing foundation/engine/sync schema; inspect the catalog first.
SELECT type,name,tbl_name,sql FROM sqlite_master
WHERE name IN ('picks','prediction_memberships','prediction_cache','ticket_candidates',
               'job_state','sync_state','d1_migrations','uq_prediction_event_family_day')
ORDER BY type,name;
PRAGMA table_info('picks');
PRAGMA table_info('ticket_candidates');
PRAGMA foreign_key_list('prediction_memberships');
PRAGMA foreign_key_check;

SELECT id,epoch,updated_at FROM sync_state;
SELECT name,due_at,lease_owner,lease_until,attempts,last_run_at,last_result,updated_at
FROM job_state ORDER BY name;
SELECT local_date,timezone,status,generated_at,cache_version
FROM prediction_cache ORDER BY local_date DESC LIMIT 20;
SELECT pick_date,status,COUNT(*) AS predictions FROM picks
WHERE pick_date >= date('now','-30 days') GROUP BY pick_date,status ORDER BY pick_date DESC,status;
SELECT p.pick_date,m.collection,COUNT(*) AS legs
FROM picks p JOIN prediction_memberships m ON m.prediction_id=p.id
WHERE p.pick_date >= date('now','-30 days') GROUP BY p.pick_date,m.collection
ORDER BY p.pick_date DESC,m.collection;
SELECT p.pick_date,m.collection,COALESCE(NULLIF(p.canonical_event_id,''),p.fixture_key) AS fixture,
       COUNT(*) AS duplicates
FROM picks p JOIN prediction_memberships m ON m.prediction_id=p.id
WHERE m.collection IN ('top','daily') AND p.pick_date >= date('now','-30 days')
GROUP BY p.pick_date,m.collection,COALESCE(NULLIF(p.canonical_event_id,''),p.fixture_key)
HAVING COUNT(*)>1;
SELECT COUNT(*) AS orphan_memberships FROM prediction_memberships m
LEFT JOIN picks p ON p.id=m.prediction_id WHERE p.id IS NULL;
-- Partial legacy metadata or intentionally stale tier? Correlate with immutable
-- publication snapshots/receipts; do not repair or infer memberships blindly.
SELECT p.id,p.pick_date,p.tier FROM picks p
WHERE p.tier IN ('top','daily')
  AND EXISTS (SELECT 1 FROM prediction_memberships m WHERE m.prediction_id=p.id)
  AND NOT EXISTS (SELECT 1 FROM prediction_memberships m
                  WHERE m.prediction_id=p.id AND m.collection=p.tier)
ORDER BY p.pick_date DESC,p.id LIMIT 100;
SELECT sport,COUNT(*) AS overdue_pending FROM picks
WHERE status='pending' AND julianday(start_utc)<julianday('now','-12 hours')
GROUP BY sport;
SELECT kind,post_date,COUNT(*) AS receipts FROM posts
WHERE post_date>=date('now','-7 days') GROUP BY kind,post_date ORDER BY post_date DESC;
SELECT kind,post_date,COUNT(*) AS deliveries FROM deliveries
WHERE post_date>=date('now','-7 days') GROUP BY kind,post_date ORDER BY post_date DESC;
