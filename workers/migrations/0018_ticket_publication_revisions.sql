-- Explicit, immutable daily ticket revisions. Legacy contracts default to revision 1;
-- the active pointer selects what /top, /daily, and group posting read.
ALTER TABLE picks ADD COLUMN revision_id INTEGER NOT NULL DEFAULT 1;

DROP INDEX IF EXISTS uq_prediction_event_family_day;
CREATE UNIQUE INDEX IF NOT EXISTS uq_prediction_event_family_day
ON picks(pick_date, revision_id, canonical_event_id, market)
WHERE canonical_event_id IS NOT NULL AND canonical_event_id<>'' AND is_representative=1;

CREATE UNIQUE INDEX IF NOT EXISTS uq_picks_fixture_selection
ON picks(pick_date, revision_id, fixture_key, market, period, stat, entity,
         COALESCE(line, -999999), selection);

CREATE TABLE IF NOT EXISTS ticket_revisions (
  pick_date TEXT NOT NULL,
  revision_id INTEGER NOT NULL,
  evaluation_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(pick_date, revision_id),
  UNIQUE(pick_date, evaluation_id)
);

CREATE TABLE IF NOT EXISTS active_ticket_revisions (
  pick_date TEXT PRIMARY KEY,
  revision_id INTEGER NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(pick_date, revision_id) REFERENCES ticket_revisions(pick_date, revision_id)
);

CREATE TABLE IF NOT EXISTS ticket_publications (
  pick_date TEXT NOT NULL,
  collection TEXT NOT NULL CHECK(collection IN ('top','daily')),
  revision_id INTEGER NOT NULL,
  destination TEXT NOT NULL CHECK(destination IN ('group','owner')),
  state TEXT NOT NULL CHECK(state IN ('sending','failed','sent','uncertain')),
  message_id TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(pick_date, collection, revision_id, destination)
);

-- Preserve the existing day-level memberships and receipts as revision 1.
INSERT OR IGNORE INTO ticket_revisions(pick_date, revision_id, evaluation_id, created_at)
SELECT p.pick_date, 1, 'legacy', MIN(p.created_at)
FROM picks p
JOIN prediction_memberships m ON m.prediction_id=p.id
WHERE m.collection IN ('top','daily')
GROUP BY p.pick_date;

INSERT OR IGNORE INTO active_ticket_revisions(pick_date, revision_id, updated_at)
SELECT pick_date, revision_id, created_at FROM ticket_revisions;

INSERT OR IGNORE INTO ticket_publications
  (pick_date, collection, revision_id, destination, state, message_id, created_at, updated_at)
SELECT post_date,
       CASE WHEN kind IN ('picks_top','top','top_owner') THEN 'top' ELSE 'daily' END,
       1,
       CASE WHEN kind IN ('top_owner','daily_owner') THEN 'owner' ELSE 'group' END,
       CASE WHEN message_id IS NULL THEN 'uncertain' ELSE 'sent' END,
       message_id, created_at, created_at
FROM posts
WHERE kind IN ('picks_top','picks_daily','top','daily','top_owner','daily_owner');
