-- Custom Picks conversation refinement + alternative-suggestion tokens.
-- Mirrors predbot/custom_recovery.py's in-memory Recovery.conversations and
-- Recovery.ready dicts, durable here because a Worker invocation shares no
-- memory with the next one. Tickets themselves still aren't persisted
-- beyond a short TTL window needed to serve a button tap.
ALTER TABLE custom_sessions ADD COLUMN request_json TEXT;
ALTER TABLE custom_sessions ADD COLUMN request_expires_at TEXT;

CREATE TABLE IF NOT EXISTS custom_suggestions (
  token TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  chat_id TEXT NOT NULL,
  label TEXT NOT NULL,
  request_json TEXT NOT NULL,
  ticket_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_custom_suggestions_expiry ON custom_suggestions(expires_at);
