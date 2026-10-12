import { describe, it } from "node:test";
import assert from "node:assert/strict";
import {
  QuotaExhausted,
  completeUpdate,
  failUpdate,
  isQuotaError,
  tryClaimUpdate,
  upsertSubscriber,
} from "../src/db.ts";

const QUOTA_MSG =
  "D1_ERROR: Your account has exceeded D1's free tier daily row write limit. [code: 7500]";

/// In-memory D1 stub: optional quota outage flag, per-table rows, write log.
function quotaDb({ quotaBlocked = false, updates = new Map(), subs = new Map() } = {}) {
  const writes = [];
  const maybeQuota = () => {
    if (quotaBlocked) {
      const err = new Error(QUOTA_MSG);
      throw err;
    }
  };
  return {
    writes,
    updates,
    subs,
    /// Row-writes D1 would actually bill (UPDATEs that change nothing bill nothing).
    billed() {
      return writes.reduce((n, w) => n + w.changes, 0);
    },
    prepare(sql) {
      const self = this;
      return {
        _sql: sql,
        _args: [],
        bind(...args) {
          this._args = args;
          return this;
        },
        async run() {
          const isWrite = /^\s*(INSERT|UPDATE|DELETE)/i.test(this._sql);
          if (!isWrite) return { meta: { changes: 0 } };
          const entry = { sql: this._sql.split("(")[0].trim(), changes: 0 };
          const done = (changes) => {
            entry.changes = changes;
            writes.push(entry);
            return { meta: { changes } };
          };
          try {
            maybeQuota();
          } catch (e) {
            throw e; // blocked writes record nothing and change nothing
          }
          if (this._sql.includes("INTO telegram_updates")) {
            const id = this._args[0];
            if (self.updates.has(id)) return done(0);
            self.updates.set(id, { status: "pending", attempts: 0 });
            return done(1);
          }
          if (this._sql.includes("SET status = 'pending'")) {
            const r = self.updates.get(this._args[0]);
            if (r && r.status === "failed") {
              r.status = "pending";
              return done(1);
            }
            return done(0);
          }
          if (this._sql.includes("SET status = 'done'")) {
            const r = self.updates.get(this._args[0]);
            if (r && r.status !== "done") {
              r.status = "done";
              return done(1);
            }
            return done(0);
          }
          if (this._sql.includes("SET status = 'failed'")) {
            const r = self.updates.get(this._args[0]);
            if (r) {
              r.status = "failed";
              r.attempts += 1;
              return done(1);
            }
            return done(0);
          }
          if (this._sql.includes("INTO subscribers")) {
            self.subs.set(this._args[0], {
              username: this._args[1],
              first_name: this._args[2],
              active: 1,
            });
            return done(1);
          }
          return done(0);
        },
        async first() {
          if (this._sql.includes("FROM telegram_updates")) {
            const r = self.updates.get(this._args[0]);
            return r ? { ...r } : null;
          }
          if (this._sql.includes("FROM subscribers")) {
            const r = self.subs.get(this._args[0]);
            return r ? { ...r } : null;
          }
          return null;
        },
      };
    },
  };
}

describe("quota error classification", () => {
  it("recognizes D1 free-tier exhaustion", () => {
    assert.equal(isQuotaError(new Error(QUOTA_MSG)), true);
    assert.equal(isQuotaError(new Error("boom")), false);
    assert.equal(isQuotaError(null), false);
  });
});

describe("webhook under quota outage: fail closed, zero amplification", () => {
  it("INSERT blocked -> QuotaExhausted, no row, redelivery later succeeds once", async () => {
    const db = quotaDb({ quotaBlocked: true });
    await assert.rejects(tryClaimUpdate(db, 50), QuotaExhausted);
    assert.equal(db.updates.has(50), false); // nothing recorded
    // Two more redeliveries while blocked: blocked statements bill nothing.
    await assert.rejects(tryClaimUpdate(db, 50), QuotaExhausted);
    await assert.rejects(tryClaimUpdate(db, 50), QuotaExhausted);
    assert.equal(db.updates.has(50), false);
    assert.equal(db.billed(), 0); // zero row-writes spent during the outage
    // Quota recovers: the update is still new (never acked, never done)...
    db.writes.length = 0;
    const fresh = quotaDb({ updates: db.updates, subs: db.subs });
    assert.equal(await tryClaimUpdate(fresh, 50), "new");
    await completeUpdate(fresh, 50);
    assert.equal(fresh.updates.get(50).status, "done");
    assert.equal(await tryClaimUpdate(fresh, 50), "duplicate"); // exactly-once
  });

  it("pending redelivery costs zero writes (no state flip)", async () => {
    const db = quotaDb();
    assert.equal(await tryClaimUpdate(db, 61), "new"); // crash before complete
    const before = db.billed();
    assert.equal(await tryClaimUpdate(db, 61), "new"); // redelivery
    assert.equal(db.billed(), before); // SELECT only
    assert.equal(db.writes.length, 2); // two INSERT statements, one billed row
  });

  it("failed redelivery reaps with one conditional write", async () => {
    const db = quotaDb();
    assert.equal(await tryClaimUpdate(db, 62), "new");
    await failUpdate(db, 62, "timeout");
    const billedBefore = db.billed();
    assert.equal(await tryClaimUpdate(db, 62), "retry");
    assert.equal(db.billed(), billedBefore + 1); // exactly one row rewritten
  });

  it("complete on an already-done row bills nothing", async () => {
    const db = quotaDb();
    assert.equal(await tryClaimUpdate(db, 63), "new");
    await completeUpdate(db, 63);
    const before = db.billed();
    await completeUpdate(db, 63);
    assert.equal(db.billed(), before);
  });

  it("failUpdate under quota surfaces QuotaExhausted (never a fake failure row)", async () => {
    const db = quotaDb({ quotaBlocked: true });
    db.updates.set(64, { status: "pending", attempts: 0 });
    await assert.rejects(failUpdate(db, 64, "x"), QuotaExhausted);
    assert.equal(db.updates.get(64).status, "pending"); // untouched
    assert.equal(db.updates.get(64).attempts, 0);
  });
});

describe("subscriber upsert writes only on change", () => {
  it("identical visit costs zero writes; real change costs one", async () => {
    const db = quotaDb();
    await upsertSubscriber(db, "9", { username: "u", firstName: "F" });
    const afterFirst = db.writes.length;
    assert.equal(afterFirst, 1);
    await upsertSubscriber(db, "9", { username: "u", firstName: "F" });
    assert.equal(db.writes.length, afterFirst); // no-op: read-only
    await upsertSubscriber(db, "9", { username: "u2", firstName: "F" });
    assert.equal(db.writes.length, afterFirst + 1);
  });
});
