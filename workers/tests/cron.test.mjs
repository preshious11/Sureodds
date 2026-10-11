import { describe, it } from "node:test";
import assert from "node:assert/strict";
import {
  TokenBucket,
  detectMissedGeneration,
  handleScheduled,
  lagosMinutes,
  parseHhmm,
  sweepDeliveries,
} from "../src/cron.ts";

// Lagos 09:30 window: 08:30 UTC. Before-gate: 08:00 UTC. Missed check: 10:00 UTC.
const AFTER_GATE = Date.UTC(2026, 9, 11, 8, 35);
const BEFORE_GATE = Date.UTC(2026, 9, 11, 7, 0);

function cronDb({ cache = null, subs = [], delivered = new Set() } = {}) {
  const jobs = {};
  return {
    jobs,
    prepare(sql) {
      const self = this;
      return {
        _sql: sql,
        _args: [],
        bind(...args) {
          this._args = args;
          return this;
        },
        async first() {
          if (this._sql.includes("FROM prediction_cache")) return cache;
          if (this._sql.includes("FROM job_state")) return null;
          return null;
        },
        async all() {
          if (this._sql.includes("FROM subscribers")) {
            const [limit, offset] = this._args;
            return { results: subs.slice(offset, offset + limit) };
          }
          if (this._sql.includes("FROM deliveries")) {
            return { results: [...delivered].map((c) => ({ chat_id: c })) };
          }
          if (this._sql.includes("FROM telegram_updates")) return { results: [] };
          return { results: [] };
        },
        async run() {
          if (this._sql.includes("INTO deliveries")) {
            delivered.add(this._args[2]);
          }
          if (this._sql.includes("UPDATE job_state")) {
            jobs.last = this._args[1];
          }
          return { meta: { changes: 1 } };
        },
      };
    },
  };
}

describe("Lagos gating (POST 09:00 / DELIVERY 09:30 / RECAP 07:45 preserved)", () => {
  it("parses HH:MM and maps Lagos minutes", () => {
    assert.equal(parseHhmm("09:30"), 570);
    assert.equal(parseHhmm("07:45"), 465);
    assert.equal(lagosMinutes(AFTER_GATE), 575);
  });
  it("no delivery before the Lagos gate (missed-run recovery only fires late)", async () => {
    const db = cronDb({ cache: { status: "COMPLETE", payload_json: "{}" }, subs: [{ chat_id: "1", timezone: "" }] });
    let calls = 0;
    const env = { DB: db, TIMEZONE: "Africa/Lagos", PAGE_SIZE: "5" };
    const res = await sweepDeliveries(env, "picks", BEFORE_GATE, async () => {
      calls += 1;
      return true;
    });
    assert.deepEqual(res, { sent: 0, skipped: 0, failed: 0 });
    assert.equal(calls, 0);
  });
});

describe("delivery sweep idempotency", () => {
  it("sends once per subscriber, skips receipted, never refetches providers", async () => {
    const db = cronDb({
      cache: { status: "COMPLETE", payload_json: "{}" },
      subs: [
        { chat_id: "1", timezone: "" },
        { chat_id: "2", timezone: "" },
      ],
      delivered: new Set(["2"]),
    });
    const sentTo = [];
    const env = { DB: db, TIMEZONE: "Africa/Lagos", PAGE_SIZE: "5" };
    const res = await sweepDeliveries(env, "picks", AFTER_GATE, async (_e, _m, p) => {
      sentTo.push(p.chat_id);
      return true;
    });
    assert.deepEqual(sentTo, ["1"]);
    assert.deepEqual(res, { sent: 1, skipped: 1, failed: 0 });
    // Second sweep (duplicate Cron fire) sends nothing.
    const res2 = await sweepDeliveries(env, "picks", AFTER_GATE, async () => {
      throw new Error("must not send");
    });
    assert.deepEqual(res2, { sent: 0, skipped: 2, failed: 0 });
  });
  it("failed sends leave no receipt (retried next sweep, never dropped)", async () => {
    const db = cronDb({
      cache: { status: "COMPLETE", payload_json: "{}" },
      subs: [{ chat_id: "9", timezone: "" }],
    });
    const env = { DB: db, TIMEZONE: "Africa/Lagos", PAGE_SIZE: "5" };
    const res = await sweepDeliveries(env, "picks", AFTER_GATE, async () => false);
    assert.deepEqual(res, { sent: 0, skipped: 0, failed: 1 });
  });
  it("no COMPLETE cache means waiting, not fabrication", async () => {
    const db = cronDb({ cache: null, subs: [{ chat_id: "1", timezone: "" }] });
    const env = { DB: db, TIMEZONE: "Africa/Lagos", PAGE_SIZE: "5" };
    let calls = 0;
    const res = await sweepDeliveries(env, "picks", AFTER_GATE, async () => {
      calls += 1;
      return true;
    });
    assert.equal(calls, 0);
    assert.deepEqual(res, { sent: 0, skipped: 0, failed: 0 });
  });
});

describe("missed-generation detection", () => {
  it("records MISS past delivery time without cache; silent before", async () => {
    const db = cronDb({ cache: null });
    const env = { DB: db, TIMEZONE: "Africa/Lagos", PAGE_SIZE: "5" };
    assert.equal(await detectMissedGeneration(env, AFTER_GATE), true);
    assert.match(db.jobs.last ?? "", /MISSED/);
    assert.equal(await detectMissedGeneration(env, BEFORE_GATE), false);
  });
  it("no miss when cache is COMPLETE", async () => {
    const db = cronDb({ cache: { status: "COMPLETE", payload_json: "{}" } });
    const env = { DB: db, TIMEZONE: "Africa/Lagos", PAGE_SIZE: "5" };
    assert.equal(await detectMissedGeneration(env, AFTER_GATE), false);
  });
});

describe("scheduler master switch", () => {
  it("does nothing unless STAGE3_CRON_ENABLED=1 (ships inactive)", async () => {
    const db = cronDb({ cache: { status: "COMPLETE", payload_json: "{}" }, subs: [{ chat_id: "1", timezone: "" }] });
    await handleScheduled({ DB: db }, "*/5 * * * *", AFTER_GATE);
    assert.equal(db.jobs.last, undefined);
  });
});

describe("token bucket rate limiting", () => {
  it("caps bursts and refills over time", () => {
    const b = new TokenBucket(25, 25);
    let allowed = 0;
    for (let i = 0; i < 30; i++) if (b.take(1000)) allowed += 1;
    assert.equal(allowed, 25);
    for (let i = 0; i < 25; i++) assert.equal(b.take(2000), true); // refilled 25 tokens in 1s
    assert.equal(b.take(2000), false);
  });
});
