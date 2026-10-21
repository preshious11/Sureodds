-- Pro Picks (admin-only preview, no paywall yet): a "mode" on the existing
-- Custom Picks session row. Entry is gated entirely by isAdmin() in the
-- Worker code, not by anything in this schema — `mode` just lets respond()
-- know which behavior (leg cap, confidence floor) applies to THIS session.
ALTER TABLE custom_sessions ADD COLUMN mode TEXT NOT NULL DEFAULT 'free';
