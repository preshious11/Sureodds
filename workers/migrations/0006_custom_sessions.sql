-- Custom Picks session state. Production (predbot/custom_recovery.py) holds
-- this in plain in-memory dicts on one long-lived process; a Worker has no
-- memory shared across invocations, so the (user_id, chat_id) "in Custom
-- Picks mode" membership and the per-chat cooldown clock need a durable
-- substitute. Tickets themselves are still never persisted here (matching
-- production's "Tickets are never persisted" rule) — only session
-- membership and a cooldown timestamp are stored.
CREATE TABLE IF NOT EXISTS custom_sessions (
  user_id TEXT NOT NULL,
  chat_id TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  last_request_at TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (user_id, chat_id)
);
