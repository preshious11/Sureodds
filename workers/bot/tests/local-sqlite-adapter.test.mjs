// Unit + compatibility tests for LocalSqliteAdapter: the D1-compatible
// surface (prepare/bind/first/all/run), batch() as a real transaction, and
// applyPlan()'s idempotent-delivery guarantee (interrupted delivery,
// duplicate delivery). Also runs real functions from workers/src/db.ts
// against it, same as the old branch's d1-rest-adapter-worker-compat test -
// proving this is a correct drop-in for the exact functions the bot and
// cron scripts depend on, this time against a REAL sqlite file, not a mock.
import { readFileSync } from "node:fs";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, it, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";

import { LocalSqliteAdapter } from "../local-sqlite-adapter.ts";
import { tryClaimUpdate, completeUpdate, failUpdate, upsertSubscriber, getDayCache } from "../../src/db.ts";

// Same migration list (and 0002 omission) the Worker's own tests use - see
// workers/tests/access_and_result_gifs.test.mjs's world(): 0001_foundation.sql
// already has what 0002_update_retry.sql tries to ALTER TABLE ADD.
const MIGRATIONS = [
  "0001_foundation.sql", "0003_engine_state.sql", "0004_sync_state.sql", "0005_ticket_candidates.sql",
  "0006_custom_sessions.sql", "0007_custom_picks_conversation.sql", "0008_pinned_menu.sql", "0009_match_events.sql",
  "0010_sync_lease.sql", "0011_pipeline_dispatch.sql", "0012_result_gifs.sql", "0013_pro_picks.sql",
  "0014_issued_picks_ledger.sql", "0015_selection_rejections.sql", "0016_closing_capture_columns.sql",
];

let tmpDir;
let dbPath;
let adapter;

beforeEach(() => {
  tmpDir = mkdtempSync(join(tmpdir(), "sureodds-sqlite-test-"));
  dbPath = join(tmpDir, "test.db");
  adapter = new LocalSqliteAdapter(dbPath);
  const migrationsDir = new URL("../../migrations/", import.meta.url);
  for (const file of MIGRATIONS) {
    adapter.execRaw(readFileSync(new URL(file, migrationsDir), "utf8"));
  }
});

afterEach(() => {
  adapter.close();
  rmSync(tmpDir, { recursive: true, force: true });
});

describe("LocalSqliteAdapter: D1-compatible surface", () => {
  it("prepare() is synchronous; bind() is optional", async () => {
    const stmt = adapter.prepare("SELECT 1");
    assert.equal(typeof stmt.bind, "function");
    const result = await stmt.run();
    assert.equal(typeof result.meta.changes, "number");
  });

  it("first() returns a plain object (not sqlite's null-prototype row) or null", async () => {
    adapter.prepare("INSERT INTO subscribers (chat_id, subscribed_at, active) VALUES (?1, '2026-10-26T00:00:00Z', 1)").bind("42").run
      ? await adapter.prepare("INSERT INTO subscribers (chat_id, subscribed_at, active) VALUES (?1, '2026-10-26T00:00:00Z', 1)").bind("42").run()
      : null;
    const row = await adapter.prepare("SELECT chat_id, active FROM subscribers WHERE chat_id = ?1").bind("42").first();
    assert.equal(Object.getPrototypeOf(row), Object.prototype);
    assert.deepEqual(row, { chat_id: "42", active: 1 });

    const missing = await adapter.prepare("SELECT * FROM subscribers WHERE chat_id = ?1").bind("nope").first();
    assert.equal(missing, null);
  });

  it("all() returns { results: [...] }", async () => {
    await adapter.prepare("INSERT INTO subscribers (chat_id, subscribed_at, active) VALUES (?1, '2026-10-26T00:00:00Z', 1)").bind("1").run();
    await adapter.prepare("INSERT INTO subscribers (chat_id, subscribed_at, active) VALUES (?1, '2026-10-26T00:00:00Z', 1)").bind("2").run();
    const page = await adapter.prepare("SELECT chat_id FROM subscribers ORDER BY chat_id").all();
    assert.deepEqual(page.results, [{ chat_id: "1" }, { chat_id: "2" }]);
  });

  it("run() surfaces meta.changes - load-bearing for tryClaimUpdate's dedup logic", async () => {
    const result = await adapter.prepare("INSERT INTO subscribers (chat_id, subscribed_at, active) VALUES (?1, '2026-10-26T00:00:00Z', 1)").bind("99").run();
    assert.equal(result.meta.changes, 1);
    const noop = await adapter
      .prepare("UPDATE subscribers SET active = 1 WHERE chat_id = ?1")
      .bind("no-such-row")
      .run();
    assert.equal(noop.meta.changes, 0);
  });
});

describe("LocalSqliteAdapter.batch(): atomic like the real D1 binding", () => {
  it("commits every statement together", async () => {
    await adapter.batch([
      { sql: "INSERT INTO subscribers (chat_id, subscribed_at, active) VALUES (?, '2026-10-26T00:00:00Z', 1)", params: ["a"] },
      { sql: "INSERT INTO subscribers (chat_id, subscribed_at, active) VALUES (?, '2026-10-26T00:00:00Z', 1)", params: ["b"] },
    ]);
    const page = await adapter.prepare("SELECT chat_id FROM subscribers ORDER BY chat_id").all();
    assert.deepEqual(page.results, [{ chat_id: "a" }, { chat_id: "b" }]);
  });

  it("rolls back every statement if one fails - none partially applied", async () => {
    await assert.rejects(() =>
      adapter.batch([
        { sql: "INSERT INTO subscribers (chat_id, subscribed_at, active) VALUES (?, '2026-10-26T00:00:00Z', 1)", params: ["c"] },
        { sql: "INSERT INTO no_such_table (x) VALUES (1)", params: [] },
      ]),
    );
    const page = await adapter.prepare("SELECT chat_id FROM subscribers").all();
    assert.deepEqual(page.results, [], "the first insert must not survive the second statement's failure");
  });
});

describe("LocalSqliteAdapter.applyPlan(): idempotent delivery", () => {
  it("applies a new plan's statements in one transaction and records the plan id", () => {
    const outcome = adapter.applyPlan("plan-001", [
      "INSERT INTO subscribers (chat_id, subscribed_at, active) VALUES ('s1', '2026-10-26T00:00:00Z', 1)",
      "INSERT INTO subscribers (chat_id, subscribed_at, active) VALUES ('s2', '2026-10-26T00:00:00Z', 1)",
    ]);
    assert.equal(outcome, "applied");
    const rows = adapter.queryRaw("SELECT chat_id FROM subscribers ORDER BY chat_id");
    assert.deepEqual(rows.map((r) => r.chat_id), ["s1", "s2"]);
  });

  it("duplicate delivery of the SAME plan id is a no-op - does not re-run statements", () => {
    adapter.applyPlan("plan-002", ["INSERT INTO subscribers (chat_id, subscribed_at, active) VALUES ('dup', '2026-10-26T00:00:00Z', 1)"]);
    const outcome = adapter.applyPlan("plan-002", ["INSERT INTO subscribers (chat_id, subscribed_at, active) VALUES ('dup', '2026-10-26T00:00:00Z', 1)"]);
    assert.equal(outcome, "already_applied");
    // If it had re-run, this INSERT (no uniqueness constraint on chat_id
    // alone being violated here - subscribers' PK IS chat_id, so a real
    // re-run would throw a constraint violation, not just insert a
    // duplicate row) would have thrown. It didn't - because it never ran.
    const rows = adapter.queryRaw("SELECT chat_id FROM subscribers WHERE chat_id = 'dup'");
    assert.equal(rows.length, 1);
  });

  it("simulated interrupted delivery: a transaction that fails partway leaves nothing applied and the plan unrecorded, so a retry re-applies cleanly", () => {
    assert.throws(() =>
      adapter.applyPlan("plan-003", [
        "INSERT INTO subscribers (chat_id, subscribed_at, active) VALUES ('i1', '2026-10-26T00:00:00Z', 1)",
        "INSERT INTO no_such_table (x) VALUES (1)", // simulates the interruption: a statement that fails mid-batch
      ]),
    );
    // Nothing from the failed attempt survived.
    const rows = adapter.queryRaw("SELECT chat_id FROM subscribers WHERE chat_id = 'i1'");
    assert.equal(rows.length, 0);

    // The plan was never recorded as applied, so a retry with the CORRECTED
    // statement list succeeds normally - this is what "interrupted
    // delivery" recovers to in practice (the relay re-sends the same plan
    // id after fixing/retrying the transfer).
    const retryOutcome = adapter.applyPlan("plan-003", ["INSERT INTO subscribers (chat_id, subscribed_at, active) VALUES ('i1', '2026-10-26T00:00:00Z', 1)"]);
    assert.equal(retryOutcome, "applied");
    const after = adapter.queryRaw("SELECT chat_id FROM subscribers WHERE chat_id = 'i1'");
    assert.equal(after.length, 1);
  });

  it("two different plan ids both apply - idempotency is per plan id, not global", () => {
    adapter.applyPlan("plan-A", ["INSERT INTO subscribers (chat_id, subscribed_at, active) VALUES ('onlyA', '2026-10-26T00:00:00Z', 1)"]);
    adapter.applyPlan("plan-B", ["INSERT INTO subscribers (chat_id, subscribed_at, active) VALUES ('onlyB', '2026-10-26T00:00:00Z', 1)"]);
    const rows = adapter.queryRaw("SELECT chat_id FROM subscribers ORDER BY chat_id");
    assert.deepEqual(rows.map((r) => r.chat_id), ["onlyA", "onlyB"]);
  });
});

describe("workers/src/db.ts functions against LocalSqliteAdapter", () => {
  it("tryClaimUpdate: new -> duplicate (pending) -> duplicate (done)", async () => {
    assert.equal(await tryClaimUpdate(adapter, 5001), "new");
    assert.equal(await tryClaimUpdate(adapter, 5001), "duplicate");
    await completeUpdate(adapter, 5001);
    assert.equal(await tryClaimUpdate(adapter, 5001), "duplicate");
  });

  it("tryClaimUpdate: a failed update (under the attempt cap) can be reclaimed as 'retry'", async () => {
    assert.equal(await tryClaimUpdate(adapter, 5002), "new");
    await failUpdate(adapter, 5002, "boom");
    assert.equal(await tryClaimUpdate(adapter, 5002), "retry");
  });

  it("upsertSubscriber: insert then idempotent re-upsert with identical fields", async () => {
    await upsertSubscriber(adapter, "777", { username: "bob", firstName: "Bob" });
    await upsertSubscriber(adapter, "777", { username: "bob", firstName: "Bob" });
    const row = await adapter.prepare("SELECT username, first_name, active FROM subscribers WHERE chat_id = ?1").bind("777").first();
    assert.deepEqual(row, { username: "bob", first_name: "Bob", active: 1 });
  });

  it("getDayCache: null when there is no cache row for the date", async () => {
    assert.equal(await getDayCache(adapter, "2026-10-26", "Africa/Lagos"), null);
  });
});
