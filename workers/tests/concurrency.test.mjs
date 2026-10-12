import { describe, it, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";
import { QuotaExhausted } from "../src/db.ts";
import { handleUpdate } from "../src/index.ts";

const QUOTA_MSG =
  "D1_ERROR: Your account has exceeded D1's free tier daily row write limit. [code: 7500]";

/// D1 stub with a quota switch; records billed row-writes.
function stagedDb() {
  const state = { quotaBlocked: false, updates: new Map(), subs: new Map(), billed: 0 };
  const db = {
    state,
    prepare(sql) {
      return {
        _sql: sql,
        _args: [],
        bind(...args) {
          this._args = args;
          return this;
        },
        async run() {
          if (/^\s*(INSERT|UPDATE|DELETE)/i.test(this._sql)) {
            if (state.quotaBlocked) throw new Error(QUOTA_MSG);
            state.billed += 1;
          }
          if (this._sql.includes("INTO telegram_updates")) {
            const id = this._args[0];
            if (state.updates.has(id)) return { meta: { changes: 0 } };
            state.updates.set(id, { status: "pending", attempts: 0, updated_at: new Date().toISOString() });
            return { meta: { changes: 1 } };
          }
          if (this._sql.includes("SET status = 'done'")) {
            const r = state.updates.get(this._args[0]);
            if (r) r.status = "done";
            return { meta: { changes: 1 } };
          }
          if (this._sql.includes("SET status = 'failed'")) {
            const r = state.updates.get(this._args[0]);
            if (r) {
              r.status = "failed";
              r.attempts += 1;
            }
            return { meta: { changes: 1 } };
          }
          if (this._sql.includes("SET status = 'pending'")) {
            const r = state.updates.get(this._args[0]);
            if (this._sql.includes("AND status = 'failed'")) {
              const ok = !!r && r.status === "failed";
              if (ok) r.status = "pending";
              return { meta: { changes: ok ? 1 : 0 } };
            }
            const ok = !!r && r.status === "pending" && r.updated_at < this._args[1];
            if (ok) r.updated_at = new Date().toISOString();
            return { meta: { changes: ok ? 1 : 0 } };
          }
          if (this._sql.includes("INTO subscribers")) {
            state.subs.set(this._args[0], { active: 1 });
            return { meta: { changes: 1 } };
          }
          return { meta: { changes: 0 } };
        },
        async first() {
          if (this._sql.includes("FROM telegram_updates")) {
            return state.updates.get(this._args[0]) ?? null;
          }
          if (this._sql.includes("FROM subscribers")) {
            return state.subs.get(this._args[0]) ?? null;
          }
          if (this._sql.includes("FROM prediction_cache")) return null;
          return null;
        },
        async all() {
          return { results: [] };
        },
      };
    },
  };
  return db;
}

function testEnv(db) {
  return {
    DB: db,
    TELEGRAM_BOT_TOKEN: "t",
    TELEGRAM_WEBHOOK_SECRET: "s",
    TIMEZONE: "Africa/Lagos",
    PAGE_SIZE: "5",
  };
}

function startUpdate(id) {
  return {
    update_id: id,
    message: {
      chat: { id: "7", type: "private" },
      from: { id: 7, first_name: "Ada" },
      text: "/start",
    },
  };
}

let realFetch;
let sends;
beforeEach(() => {
  sends = [];
  realFetch = globalThis.fetch;
  globalThis.fetch = async (url, opts) => {
    sends.push(JSON.parse(opts.body));
    return { ok: true, status: 200, json: async () => ({ ok: true, result: { message_id: 1 } }) };
  };
});
afterEach(() => {
  globalThis.fetch = realFetch;
});

describe("failed claim never processes or acks", () => {
  it("quota-blocked INSERT: no send, no row, QuotaExhausted propagates (500, redeliver)", async () => {
    const db = stagedDb();
    db.state.quotaBlocked = true;
    await assert.rejects(handleUpdate(testEnv(db), startUpdate(70)), QuotaExhausted);
    assert.equal(sends.length, 0); // routeUpdate never ran
    assert.equal(db.state.updates.has(70), false); // nothing recorded
  });
});

describe("post-send completion failure: at-least-once, never lost", () => {
  it("send accepted then done-write fails -> redelivery re-sends, then done exactly once", async () => {
    const db = stagedDb();
    const env = testEnv(db);
    // First delivery: claim ok, send ok, completion hits quota.
    db.state.quotaBlocked = false;
    const origRun = db.prepare.bind(db);
    let failNextComplete = true;
    db.prepare = (sql) => {
      const stmt = origRun(sql);
      const origStmtRun = stmt.run.bind(stmt);
      stmt.run = async function () {
        if (failNextComplete && stmt._sql.includes("SET status = 'done'")) {
          throw new Error(QUOTA_MSG);
        }
        return origStmtRun();
      };
      return stmt;
    };
    await assert.rejects(handleUpdate(env, startUpdate(71)), QuotaExhausted);
    assert.equal(sends.length, 1); // the user WAS messaged...
    assert.equal(db.state.updates.get(71).status, "pending"); // ...but not marked done
    // Immediate redelivery backs off (owner presumed in-flight): still 1 send.
    failNextComplete = false;
    await handleUpdate(env, startUpdate(71));
    assert.equal(sends.length, 1);
    // Redelivery after the claim goes stale re-sends (documented duplicate)
    // and completes; a third delivery is then quiet.
    db.state.updates.get(71).updated_at = "2020-01-01T00:00:00Z";
    await handleUpdate(env, startUpdate(71));
    assert.equal(sends.length, 2); // at-least-once: one duplicate DM, never a loss
    assert.equal(db.state.updates.get(71).status, "done");
    await handleUpdate(env, startUpdate(71)); // fourth delivery: quiet
    assert.equal(sends.length, 2);
  });
});

describe("one invocation processes an update exactly once", () => {
  it("single delivery -> single send, done row", async () => {
    const db = stagedDb();
    await handleUpdate(testEnv(db), startUpdate(72));
    assert.equal(sends.length, 1);
    assert.equal(db.state.updates.get(72).status, "done");
  });
});
