-- Fail loudly instead of silently ignoring stale integer-ID collisions.
CREATE TRIGGER IF NOT EXISTS guard_prediction_id_identity
BEFORE INSERT ON picks
WHEN EXISTS (
  SELECT 1 FROM picks p WHERE p.id=NEW.id AND (
    p.pick_date IS NOT NEW.pick_date OR p.fixture_key IS NOT NEW.fixture_key OR
    p.market IS NOT NEW.market OR p.period IS NOT NEW.period OR
    p.stat IS NOT NEW.stat OR p.entity IS NOT NEW.entity OR
    p.selection IS NOT NEW.selection OR p.line IS NOT NEW.line OR p.price IS NOT NEW.price
  )
)
BEGIN
  SELECT RAISE(ABORT, 'prediction ID belongs to a different immutable contract; refresh the Oracle snapshot');
END;

-- A time limit is not an authoritative cancellation or result.
CREATE TRIGGER IF NOT EXISTS guard_unverified_timeout_settlement
BEFORE UPDATE OF status ON picks
WHEN NEW.status='void' AND json_valid(NEW.result_json)
  AND json_extract(NEW.result_json,'$.reason')='settlement deadline expired'
BEGIN
  SELECT RAISE(ABORT, 'unverified timeout cannot settle a prediction');
END;
