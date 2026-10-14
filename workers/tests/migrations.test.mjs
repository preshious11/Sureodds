import { DatabaseSync } from "node:sqlite";
import { readFileSync } from "node:fs";
import { describe, it } from "node:test";
import assert from "node:assert/strict";

// Regression coverage for the production D1 setup failure: 0001 succeeded,
// then 0002_update_retry.sql failed with "duplicate column name: status".
// Root cause: 0001_foundation.sql already declares telegram_updates'
// status/attempts/last_error/updated_at columns inline (its own comment
// says so — "0002 adds these on existing DBs; new DBs get them here").
// 0002 is an UPGRADE-ONLY migration for a database created from an older
// copy of 0001 that predated those inline columns; it must never run
// against a fresh database. This test is the "fresh local D1/SQLite
// database" check: the documented fresh-database sequence (0001, then
// 0003-0007, skipping 0002) must apply cleanly end to end, and the
// WRONG sequence (blindly including 0002) must fail exactly the way
// production did, so this stays caught here instead of in a live cutover.

function migrationPath(name) {
  return new URL(`../migrations/${name}`, import.meta.url);
}

const FRESH_DB_SEQUENCE = [
  "0001_foundation.sql",
  // 0002_update_retry.sql deliberately omitted — see header above.
  "0003_engine_state.sql",
  "0004_sync_state.sql",
  "0005_ticket_candidates.sql",
  "0006_custom_sessions.sql",
  "0007_custom_picks_conversation.sql",
  "0008_pinned_menu.sql",
];

describe("migration chain against a fresh database (workers/CUTOVER.md step 2)", () => {
  it("the documented fresh-database sequence (0001, 3-7) applies cleanly end to end", () => {
    const sql = new DatabaseSync(":memory:");
    for (const name of FRESH_DB_SEQUENCE) {
      assert.doesNotThrow(
        () => sql.exec(readFileSync(migrationPath(name), "utf8")),
        `migration ${name} must apply cleanly to a fresh database`,
      );
    }
    // telegram_updates must have the Stage-2 reliability columns from 0001
    // itself (not from 0002, which never ran) — confirms 0001 alone is
    // sufficient on a fresh database, matching its own header comment.
    const cols = sql.prepare("PRAGMA table_info(telegram_updates)").all().map((c) => c.name);
    for (const col of ["status", "attempts", "last_error", "updated_at"]) {
      assert.ok(cols.includes(col), `telegram_updates.${col} must exist after 0001 alone`);
    }
    // Spot-check a few tables from later migrations exist too, proving the
    // chain actually ran through to the end, not just 0001.
    const tables = sql.prepare("SELECT name FROM sqlite_master WHERE type='table'").all().map((t) => t.name);
    for (const table of ["custom_pick_candidates", "sync_state", "ticket_candidates", "custom_sessions", "custom_suggestions"]) {
      assert.ok(tables.includes(table), `${table} must exist after the full fresh-database sequence`);
    }
    const sessionCols = sql.prepare("PRAGMA table_info(custom_sessions)").all().map((c) => c.name);
    assert.ok(sessionCols.includes("request_json"), "0007's ALTER TABLE custom_sessions must have applied");
  });

  it("documents the exact failure production hit: including 0002 on a fresh database errors on 'duplicate column name'", () => {
    const sql = new DatabaseSync(":memory:");
    sql.exec(readFileSync(migrationPath("0001_foundation.sql"), "utf8"));
    assert.throws(
      () => sql.exec(readFileSync(migrationPath("0002_update_retry.sql"), "utf8")),
      /duplicate column name/i,
      "0002 must still fail this way on a fresh DB — if this ever stops throwing, 0001 and 0002 have silently diverged",
    );
  });

  it("0002 remains correct for its actual use case: upgrading a database from BEFORE those columns were inlined into 0001", () => {
    const sql = new DatabaseSync(":memory:");
    // The legacy shape 0001 had before it was amended to include the
    // Stage-2 columns inline (see 0001's own comment).
    sql.exec("CREATE TABLE telegram_updates (update_id INTEGER PRIMARY KEY, first_seen_at TEXT NOT NULL);");
    assert.doesNotThrow(() => sql.exec(readFileSync(migrationPath("0002_update_retry.sql"), "utf8")));
    const cols = sql.prepare("PRAGMA table_info(telegram_updates)").all().map((c) => c.name);
    for (const col of ["status", "attempts", "last_error", "updated_at"]) {
      assert.ok(cols.includes(col));
    }
  });
});
