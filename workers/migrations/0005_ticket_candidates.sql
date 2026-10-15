-- Separate All Predictions representatives from promoted alternate outcomes.
-- Existing forecast/settlement values are preserved; old rows remain representatives.
ALTER TABLE picks ADD COLUMN is_representative INTEGER NOT NULL DEFAULT 1;
DROP INDEX IF EXISTS uq_prediction_event_family_day;
CREATE UNIQUE INDEX IF NOT EXISTS uq_prediction_event_family_day
ON picks(pick_date, canonical_event_id, market)
WHERE canonical_event_id IS NOT NULL AND canonical_event_id<>'' AND is_representative=1;

CREATE TABLE IF NOT EXISTS ticket_candidates (
    pick_date TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY(pick_date, candidate_id)
);
