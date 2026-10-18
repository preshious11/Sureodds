-- WHY: reusable Telegram file ids avoid uploads; unique ids deduplicate inline GIFs too.
CREATE TABLE IF NOT EXISTS result_gifs (
    kind TEXT NOT NULL CHECK(kind IN ('won','lost')),
    file_id TEXT NOT NULL,
    file_unique_id TEXT NOT NULL UNIQUE,
    added_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_result_gifs_kind ON result_gifs(kind);
