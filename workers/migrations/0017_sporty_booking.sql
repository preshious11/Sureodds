-- Optional request claims only; never apply as part of an experiment.
-- No timeout reclaim: an ambiguous POST must never automatically repeat.
CREATE TABLE IF NOT EXISTS sporty_booking_requests (
    request_key TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK(status IN ('processing','verified','unverified')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
