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
// 0003-0008, skipping 0002) must apply cleanly end to end, and the
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
  "0009_match_events.sql",
  "0010_sync_lease.sql",
  "0011_pipeline_dispatch.sql",
  "0012_result_gifs.sql",
  "0013_pro_picks.sql",
  "0014_issued_picks_ledger.sql",
  "0015_selection_rejections.sql",
  "0016_closing_capture_columns.sql",
  "0017_oracle_recovery_guards.sql",
  "0018_ticket_publication_revisions.sql",
  "0019_ticket_publication_recipient.sql",
];

describe("migration chain against a fresh database (workers/CUTOVER.md step 2)", () => {
  it("the documented fresh-database sequence (0001, 3-8) applies cleanly end to end", () => {
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
    for (const table of ["custom_pick_candidates", "sync_state", "ticket_candidates", "custom_sessions",
      "custom_suggestions", "ticket_revisions", "active_ticket_revisions", "ticket_publications"]) {
      assert.ok(tables.includes(table), `${table} must exist after the full fresh-database sequence`);
    }
    const pickCols = sql.prepare("PRAGMA table_info(picks)").all().map((c) => c.name);
    assert.ok(pickCols.includes("revision_id"));
    const publicationCols = sql.prepare("PRAGMA table_info(ticket_publications)").all().map((c) => c.name);
    assert.ok(publicationCols.includes("recipient_id"));
    const sessionCols = sql.prepare("PRAGMA table_info(custom_sessions)").all().map((c) => c.name);
    assert.ok(sessionCols.includes("request_json"), "0007's ALTER TABLE custom_sessions must have applied");
    // 0010 (A2): the shared D1-write lease and the live-monitor dispatch
    // tracking row must exist, pre-seeded the same way 'generate' already
    // is in 0001 - their lease-acquire/interval-check patterns are UPDATE-
    // only and do nothing against a row that was never inserted.
    const jobNames = sql.prepare("SELECT name FROM job_state ORDER BY name").all().map((r) => r.name);
    for (const name of ["generate", "deliver", "settle", "recap", "sync_write", "livemon_dispatch", "generation_dispatch", "settlement_dispatch"]) {
      assert.ok(jobNames.includes(name), `job_state must have a '${name}' row after the fresh-database sequence`);
    }
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

  it("0005's unique index is safely repeatable (IF NOT EXISTS)", () => {
    const sql = new DatabaseSync(":memory:");
    for (const name of FRESH_DB_SEQUENCE) {
      sql.exec(readFileSync(migrationPath(name), "utf8"));
    }
    // Only the index statement is re-run here: the file's ALTER TABLE is
    // inherently one-shot (as with every other migration), but the index
    // — the statement that previously broke re-application — must tolerate
    // a resumed/interrupted run.
    const indexStmt = readFileSync(migrationPath("0005_ticket_candidates.sql"), "utf8")
      .split(";")
      .find((s) => s.includes("uq_prediction_event_family_day") && s.includes("CREATE")) + ";";
    assert.doesNotThrow(
      () => sql.exec(indexStmt),
      "re-creating 0005's unique index must not error",
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

  it("0018 keeps existing forecasts, memberships, and receipts as active revision 1", () => {
    const sql = new DatabaseSync(":memory:");
    for (const name of FRESH_DB_SEQUENCE.slice(0, -2)) {
      sql.exec(readFileSync(migrationPath(name), "utf8"));
    }
    sql.exec(`INSERT INTO picks(id,pick_date,tier,rank,provider,provider_event_id,sport,home,away,start_utc,
      market,period,stat,entity,selection,label,price,prob,ev,status,created_at,fixture_key,canonical_event_id,
      is_representative) VALUES(7,'2026-11-01','all',1,'p','e','football','A','B',
      '2026-11-01T20:00:00Z','result3','full','points','all','home','A to win',1.44,.7,0,'pending',
      '2026-11-01T08:00:00Z','fixture-a','event-a',1);
      INSERT INTO prediction_memberships VALUES(7,'all',1),(7,'top',1);
      INSERT INTO posts(kind,post_date,message_id,created_at) VALUES
        ('top','2026-11-01','437','2026-11-01T08:30:00Z');`);
    const before = sql.prepare("SELECT price,created_at FROM picks WHERE id=7").get();
    sql.exec(readFileSync(migrationPath("0018_ticket_publication_revisions.sql"), "utf8"));
    assert.deepEqual(sql.prepare("SELECT price,created_at FROM picks WHERE id=7").get(), before);
    assert.equal(sql.prepare("SELECT collection FROM prediction_memberships WHERE prediction_id=7 AND collection='top'").get().collection, "top");
    assert.equal(sql.prepare("SELECT revision_id FROM active_ticket_revisions WHERE pick_date='2026-11-01'").get().revision_id, 1);
    assert.equal(sql.prepare("SELECT state FROM ticket_publications WHERE pick_date='2026-11-01' AND collection='top' AND destination='group'").get().state, "sent");
  });

  it("0019 adds the recipient column and backfills known legacy community recipients", () => {
    const sql = new DatabaseSync(":memory:");
    for (const name of FRESH_DB_SEQUENCE.slice(0, -1)) {
      sql.exec(readFileSync(migrationPath(name), "utf8"));
    }
    sql.exec(`INSERT INTO deliveries(kind,post_date,chat_id,created_at)
      VALUES('community_picks_top','2026-11-01','-100123','2026-11-01T08:30:00Z');
      INSERT INTO ticket_revisions VALUES('2026-11-01',1,'legacy','2026-11-01T08:00:00Z');
      INSERT INTO active_ticket_revisions VALUES('2026-11-01',1,'2026-11-01T08:00:00Z');
      INSERT INTO ticket_publications
        (pick_date,collection,revision_id,destination,state,created_at,updated_at)
      VALUES('2026-11-01','top',1,'group','sent','2026-11-01T08:30:00Z','2026-11-01T08:30:00Z');`);
    sql.exec(readFileSync(migrationPath("0019_ticket_publication_recipient.sql"), "utf8"));
    assert.equal(sql.prepare("SELECT recipient_id FROM ticket_publications").get().recipient_id, "-100123");
    sql.close();
  });
});
