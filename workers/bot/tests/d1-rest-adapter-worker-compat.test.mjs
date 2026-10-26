// Compatibility test: real functions from workers/src/db.ts, called with a
// D1RestAdapter in place of env.DB, backed by a fetch mock that executes the
// SQL against a real in-memory sqlite database running the actual
// migrations. This proves the adapter is a correct drop-in for the exact
// functions bot.ts depends on - not just that it satisfies a made-up
// contract (see d1-rest-adapter.test.mjs for that).
//
// What this does NOT prove: that Cloudflare's real D1 REST API behaves
// identically to this mock in every edge case (error wording, timing,
// concurrency). That requires the staging-D1 integration test in Phase 7,
// against real Cloudflare credentials, which this environment does not
// have. This test is the offline half of condition 2's requirement; the
// online half is still pending.
import { DatabaseSync } from "node:sqlite";
import { readFileSync } from "node:fs";
import { describe, it, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";

import { D1RestAdapter } from "../d1-rest-adapter.ts";
import { tryClaimUpdate, completeUpdate, failUpdate, upsertSubscriber, getDayCache, QuotaExhausted } from "../../src/db.ts";

const ACCOUNT_ID = "test-account";
const DB_ID = "test-db-id";
const TOKEN = "test-token";

// Same migration list (and 0002 omission) the Worker's own tests use - see
// workers/tests/access_and_result_gifs.test.mjs's world(): 0001_foundation.sql
// already has what 0002_update_retry.sql tries to ALTER TABLE ADD, so
// running both back to back is a duplicate-column error, not a real gap.
const MIGRATIONS = [
  "0001_foundation.sql", "0003_engine_state.sql", "0004_sync_state.sql", "0005_ticket_candidates.sql",
  "0006_custom_sessions.sql", "0007_custom_picks_conversation.sql", "0008_pinned_menu.sql", "0009_match_events.sql",
  "0010_sync_lease.sql", "0011_pipeline_dispatch.sql", "0012_result_gifs.sql", "0013_pro_picks.sql",
  "0014_issued_picks_ledger.sql", "0015_selection_rejections.sql", "0016_closing_capture_columns.sql",
];

let sql;
let originalFetch;

function freshDatabase() {
  const db = new DatabaseSync(":memory:");
  const migrationsDir = new URL("../../migrations/", import.meta.url);
  for (const file of MIGRATIONS) {
    db.exec(readFileSync(new URL(file, migrationsDir), "utf8"));
  }
  return db;
}

/** Mimics Cloudflare's D1 REST response envelope, executing against the real sqlite instance. */
function installFetchMock(db) {
  originalFetch = globalThis.fetch;
  globalThis.fetch = async (_url, init) => {
    const { sql: querySql, params } = JSON.parse(init.body);
    try {
      const isSelect = /^\s*select/i.test(querySql);
      if (isSelect) {
        const rows = db.prepare(querySql).all(...params);
        return jsonResponse(200, { success: true, result: [{ success: true, results: rows, meta: {} }] });
      }
      const info = db.prepare(querySql).run(...params);
      return jsonResponse(200, {
        success: true,
        result: [{ success: true, results: [], meta: { changes: info.changes, last_row_id: Number(info.lastInsertRowid) } }],
      });
    } catch (err) {
      return jsonResponse(200, { success: false, errors: [{ message: String(err?.message ?? err) }] });
    }
  };
}

function jsonResponse(status, body) {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

beforeEach(() => {
  sql = freshDatabase();
  installFetchMock(sql);
});

afterEach(() => {
  globalThis.fetch = originalFetch;
  sql.close();
});

describe("tryClaimUpdate against D1RestAdapter", () => {
  it("claims a brand new update as 'new'", async () => {
    const db = new D1RestAdapter(ACCOUNT_ID, DB_ID, TOKEN);
    const outcome = await tryClaimUpdate(db, 1001);
    assert.equal(outcome, "new");
  });

  it("a second claim of the same pending update (before completion) is 'duplicate'", async () => {
    const db = new D1RestAdapter(ACCOUNT_ID, DB_ID, TOKEN);
    assert.equal(await tryClaimUpdate(db, 1002), "new");
    assert.equal(await tryClaimUpdate(db, 1002), "duplicate");
  });

  it("a completed update can never be reclaimed - 'duplicate' forever", async () => {
    const db = new D1RestAdapter(ACCOUNT_ID, DB_ID, TOKEN);
    assert.equal(await tryClaimUpdate(db, 1003), "new");
    await completeUpdate(db, 1003);
    assert.equal(await tryClaimUpdate(db, 1003), "duplicate");
  });

  it("a failed update (under the attempt cap) can be reclaimed as 'retry'", async () => {
    const db = new D1RestAdapter(ACCOUNT_ID, DB_ID, TOKEN);
    assert.equal(await tryClaimUpdate(db, 1004), "new");
    await failUpdate(db, 1004, "boom");
    assert.equal(await tryClaimUpdate(db, 1004), "retry");
  });
});

describe("upsertSubscriber against D1RestAdapter", () => {
  it("inserts a new subscriber and is idempotent on identical fields", async () => {
    const db = new D1RestAdapter(ACCOUNT_ID, DB_ID, TOKEN);
    await upsertSubscriber(db, "555", { username: "alice", firstName: "Alice" });
    const row = sql.prepare("SELECT username, first_name, active FROM subscribers WHERE chat_id = ?").get("555");
    assert.deepEqual({ ...row }, { username: "alice", first_name: "Alice", active: 1 });

    // Second call with the same fields must not throw and must leave the row intact.
    await upsertSubscriber(db, "555", { username: "alice", firstName: "Alice" });
    const again = sql.prepare("SELECT username, first_name, active FROM subscribers WHERE chat_id = ?").get("555");
    assert.deepEqual({ ...again }, { username: "alice", first_name: "Alice", active: 1 });
  });
});

describe("getDayCache against D1RestAdapter", () => {
  it("returns null when there is no cache row for the date", async () => {
    const db = new D1RestAdapter(ACCOUNT_ID, DB_ID, TOKEN);
    const cache = await getDayCache(db, "2026-10-26", "Africa/Lagos");
    assert.equal(cache, null);
  });
});

describe("D1RestAdapter surfaces a D1 quota error as QuotaExhausted", () => {
  it("tryClaimUpdate's insert throws QuotaExhausted on D1's quota wording, same as the real binding would", async () => {
    globalThis.fetch = async () =>
      jsonResponse(200, { success: false, errors: [{ message: "D1_ERROR: row write limit exceeded for this database" }] });
    const db = new D1RestAdapter(ACCOUNT_ID, DB_ID, TOKEN);
    await assert.rejects(() => tryClaimUpdate(db, 2001), (err) => {
      assert.ok(err instanceof QuotaExhausted, `expected QuotaExhausted, got ${err?.constructor?.name}: ${err?.message}`);
      return true;
    });
  });
});
