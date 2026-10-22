-- Issued-pick tracking for Custom Picks and Pro Picks (Top/Daily already
-- have this via picks/prediction_memberships + settlement — this is the
-- equivalent for the two features that never had one).
--
-- Applied to production D1. recordIssuedTicket() is wired live into
-- enterProPicks/postProPicksToAdmin (cron.ts) and the Custom Picks send
-- path (index.ts) — see workers/src/ledger.ts's own header comment for
-- current wiring status, including the one piece still outstanding
-- (automatic settlement is not yet invoked from a scheduled job).
--
-- Rollback: both tables are purely additive and read by nothing else in
-- the schema (no foreign keys point INTO them from pre-existing tables),
-- so a rollback is `DROP TABLE issued_ticket_legs; DROP TABLE
-- issued_tickets;` with no knock-on effect on Top/Daily/Custom Picks'
-- own existing tables or settlement.

CREATE TABLE IF NOT EXISTS issued_tickets (
  id TEXT PRIMARY KEY,                 -- content-derived, see ledger.ts ticketId()
  feature TEXT NOT NULL CHECK(feature IN ('custom', 'pro')),
  user_id TEXT NOT NULL,
  chat_id TEXT NOT NULL,
  prediction_date TEXT NOT NULL,       -- Africa/Lagos local date (YYYY-MM-DD)
  timezone TEXT NOT NULL,
  legs_key TEXT NOT NULL,              -- deterministic digest of the actual legs, see ledger.ts legsKey()
  published_at TEXT NOT NULL,          -- ISO 8601 UTC, when the ticket was actually sent
  combined_odds REAL NOT NULL,
  leg_count INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'issued' CHECK(status IN ('issued', 'settled')),
  created_at TEXT NOT NULL
);
-- Idempotency boundary: (feature, user, chat, day, THE ACTUAL LEGS). NOT
-- just (feature, user, chat, day) - Custom Picks is conversational, and one
-- user can legitimately request several genuinely different tickets in the
-- same day (e.g. "3 picks football" then later "5 picks basketball"); those
-- must both be recorded, never collapsed into one. What this index actually
-- guards against is a RETRY of the exact same send (a retried webhook, an
-- overlapping cron tick) producing the exact same legs twice - that, and
-- only that, is a no-op (INSERT OR IGNORE). Pro Picks' own one-per-day
-- discipline is already enforced at the send layer (cron.ts's claimPost),
-- so this naturally reduces to one row per day there without needing a
-- separate rule here.
CREATE UNIQUE INDEX IF NOT EXISTS idx_issued_tickets_identity
  ON issued_tickets(feature, user_id, chat_id, prediction_date, legs_key);

CREATE TABLE IF NOT EXISTS issued_ticket_legs (
  ticket_id TEXT NOT NULL REFERENCES issued_tickets(id),
  leg_index INTEGER NOT NULL,
  canonical_event_id TEXT NOT NULL,
  sport TEXT NOT NULL,
  home TEXT NOT NULL,
  away TEXT NOT NULL,
  market TEXT NOT NULL,
  period TEXT NOT NULL,
  stat TEXT NOT NULL,
  entity TEXT NOT NULL,
  selection TEXT NOT NULL,
  line REAL,
  price REAL NOT NULL,                 -- decimal odds at issue time
  probability REAL NOT NULL,           -- p_model at issue time
  probability_basis TEXT NOT NULL DEFAULT 'p_model',  -- what the probability number actually is (see predbot/model/payoff.py Payoff.fair_probability vs raw p_model — never conflated)
  bookmaker_count INTEGER NOT NULL DEFAULT 0,
  -- Settlement: mirrors predbot/markets/settlement.py's own outcome
  -- vocabulary exactly (won|lost|push|half_won|half_lost|void|pending) —
  -- never a second, contradictory grading scheme.
  settlement_status TEXT NOT NULL DEFAULT 'pending'
    CHECK(settlement_status IN ('pending', 'won', 'lost', 'push', 'half_won', 'half_lost', 'void')),
  payout_multiplier REAL,              -- this leg's realized return per unit stake (0 for lost/void, 1 for push/void... see ledger.ts legReturn())
  settled_at TEXT,
  PRIMARY KEY (ticket_id, leg_index)
);
CREATE INDEX IF NOT EXISTS idx_issued_ticket_legs_event ON issued_ticket_legs(canonical_event_id);
