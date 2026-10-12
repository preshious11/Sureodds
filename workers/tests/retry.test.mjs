import { describe, it } from "node:test";
import assert from "node:assert/strict";
import {
  completeUpdate,
  failUpdate,
  tryClaimUpdate,
  MAX_UPDATE_ATTEMPTS,
} from "../src/db.ts";

// Faithful in-memory D1 for the telegram_updates lifecycle (conditions honored).
function updateDb() {
  const rows = new Map();
  const nowIso = () => new Date().toISOString().replace(/\.\d+Z$/, "Z");
  return {
    rows,
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
          if (this._sql.startsWith("INSERT OR IGNORE")) {
            const id = this._args[0];
            if (self.rows.has(id)) return { meta: { changes: 0 } };
            self.rows.set(id, { status: "pending", attempts: 0, updated_at: nowIso() });
            return { meta: { changes: 1 } };
          }
          if (this._sql.includes("AND status = 'failed'")) {
            const r = self.rows.get(this._args[0]);
            if (r && r.status === "failed") {
              r.status = "pending";
              r.updated_at = nowIso();
              return { meta: { changes: 1 } };
            }
            return { meta: { changes: 0 } };
          }
          if (this._sql.includes("AND status = 'pending' AND updated_at <")) {
            const r = self.rows.get(this._args[0]);
            const cutoff = this._args[1];
            if (r && r.status === "pending" && r.updated_at < cutoff) {
              r.updated_at = nowIso();
              return { meta: { changes: 1 } };
            }
            return { meta: { changes: 0 } };
          }
          if (this._sql.includes("SET status = 'done'")) {
            const r = self.rows.get(this._args[0]);
            if (r && r.status !== "done") {
              r.status = "done";
              r.updated_at = nowIso();
              return { meta: { changes: 1 } };
            }
            return { meta: { changes: 0 } };
          }
          if (this._sql.includes("SET status = 'failed'")) {
            const r = self.rows.get(this._args[0]);
            r.status = "failed";
            r.attempts += 1;
            r.last_error = this._args[1];
            r.updated_at = nowIso();
            return { meta: { changes: 1 } };
          }
          throw new Error("unexpected SQL: " + this._sql);
        },
        async first() {
          const r = self.rows.get(this._args[0]);
          return r ? { ...r } : null;
        },
      };
    },
  };
}

describe("update lifecycle: never silently discarded", () => {
  it("first delivery is new, redelivery while done is duplicate", async () => {
    const db = updateDb();
    assert.equal(await tryClaimUpdate(db, 1), "new");
    // Simulate success path finishing concurrently: mark done, retry arrives.
    await completeUpdate(db, 1);
    assert.equal(await tryClaimUpdate(db, 1), "duplicate");
  });

  it("failure leaves the update retryable with attempt count and error", async () => {
    const db = updateDb();
    assert.equal(await tryClaimUpdate(db, 7), "new");
    await failUpdate(db, 7, "D1 timeout");
    const row = db.rows.get(7);
    assert.equal(row.status, "failed");
    assert.equal(row.attempts, 1);
    assert.equal(row.last_error, "D1 timeout");
    // Telegram retry (or Stage-3 sweeper) reaps it.
    assert.equal(await tryClaimUpdate(db, 7), "retry");
    await completeUpdate(db, 7);
    assert.equal(await tryClaimUpdate(db, 7), "duplicate");
  });

  it("a crashed worker (pending, no completion) is retried once stale, never lost", async () => {
    const db = updateDb();
    assert.equal(await tryClaimUpdate(db, 9), "new");
    // Owner still (presumably) working: a duplicate delivery backs off.
    assert.equal(await tryClaimUpdate(db, 9), "duplicate");
    // Owner crashed long ago: the next redelivery steals the stale claim.
    db.rows.get(9).updated_at = "2020-01-01T00:00:00Z";
    assert.equal(await tryClaimUpdate(db, 9), "retry");
    await completeUpdate(db, 9);
    assert.equal(await tryClaimUpdate(db, 9), "duplicate");
  });

  it("two concurrent deliveries of the same pending update never both process", async () => {
    const db = updateDb();
    const [first, second] = [await tryClaimUpdate(db, 21), await tryClaimUpdate(db, 21)];
    assert.equal(first, "new");
    assert.equal(second, "duplicate"); // in-flight elsewhere: no double side effects
  });

  it("poison updates stop after max attempts", async () => {
    const db = updateDb();
    assert.equal(await tryClaimUpdate(db, 11), "new");
    for (let i = 0; i < MAX_UPDATE_ATTEMPTS; i++) {
      await failUpdate(db, 11, `boom ${i}`);
      const outcome = await tryClaimUpdate(db, 11);
      if (i < MAX_UPDATE_ATTEMPTS - 1) assert.equal(outcome, "retry");
      else assert.equal(outcome, "duplicate");
    }
    assert.equal(db.rows.get(11).attempts, MAX_UPDATE_ATTEMPTS);
  });

  it("non-numeric update ids are never processed", async () => {
    const db = updateDb();
    assert.equal(await tryClaimUpdate(db, NaN), "duplicate");
  });
});
