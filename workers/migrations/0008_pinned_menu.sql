-- Worker-only operational state for the /pinmenu admin command: which
-- message in the prediction group is the currently pinned community menu,
-- so a repeat /pinmenu edits that same message in place (no duplicate
-- post, never touches any other message or another admin's pin) instead
-- of blindly posting+pinning a new one every time. Not prediction/engine
-- data — same category as custom_sessions (0006).
CREATE TABLE IF NOT EXISTS pinned_menu (
  chat_id TEXT PRIMARY KEY,
  message_id INTEGER NOT NULL,
  pinned_by TEXT NOT NULL,
  pinned_at TEXT NOT NULL
);
