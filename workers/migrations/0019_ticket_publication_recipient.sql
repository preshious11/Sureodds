-- Record the actual chat that received a successful group/owner ticket post.
-- Older receipts are backfilled only where legacy delivery history identifies
-- the community recipient; unknown recipients remain NULL (fail closed).
ALTER TABLE ticket_publications ADD COLUMN recipient_id TEXT;

UPDATE ticket_publications
SET recipient_id = (
  SELECT d.chat_id
  FROM deliveries d
  WHERE d.post_date = ticket_publications.pick_date
    AND d.kind = CASE WHEN ticket_publications.collection = 'top'
      THEN 'community_picks_top' ELSE 'community_picks_daily' END
  ORDER BY d.created_at DESC
  LIMIT 1
)
WHERE ticket_publications.destination = 'group'
  AND ticket_publications.recipient_id IS NULL
  AND EXISTS (
    SELECT 1
    FROM deliveries d
    WHERE d.post_date = ticket_publications.pick_date
      AND d.kind = CASE WHEN ticket_publications.collection = 'top'
        THEN 'community_picks_top' ELSE 'community_picks_daily' END
  );
