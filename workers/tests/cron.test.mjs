import { describe, it } from "node:test";
import assert from "node:assert/strict";
import {
  CRON_DELIVER_SWEEP,
  CRON_POST_SWEEP,
  CRON_RECAP_SWEEP,
  CRON_WATCH,
  TokenBucket,
  detectMissedGeneration,
  handleScheduled,
  lagosMinutes,
  parseHhmm,
  postGroupPicks,
  postGroupRecap,
  sweepDeliveries,
  sweepMatchEvents,
} from "../src/cron.ts";

// Lagos 09:30 window: 08:30 UTC. Before-gate: 08:00 UTC. Missed check: 10:00 UTC.
const AFTER_GATE = Date.UTC(2026, 9, 11, 8, 35);
const BEFORE_GATE = Date.UTC(2026, 9, 11, 7, 0);

function cronDb({ cache = null, subs = [], delivered = new Set(), posted = new Set(), events = [] } = {}) {
  const jobs = {};
  const state = { cache };
  return {
    jobs,
    posted,
    events,
    get cache() {
      return state.cache;
    },
    set cache(value) {
      state.cache = value;
    },
    prepare(sql) {
      return {
        _sql: sql,
        _args: [],
        bind(...args) {
          this._args = args;
          return this;
        },
        async first() {
          if (this._sql.includes("FROM prediction_cache")) return state.cache;
          if (this._sql.includes("FROM job_state")) return null;
          if (this._sql.includes("FROM posts")) {
            const [kind, postDate] = this._args;
            return posted.has(`${kind}|${postDate}`) ? { one: 1 } : null;
          }
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
          if (this._sql.includes("FROM match_events")) {
            const [sinceIso] = this._args;
            return { results: events.filter((e) => !e.created_at || e.created_at >= sinceIso) };
          }
          return { results: [] };
        },
        async run() {
          if (this._sql.includes("INTO deliveries")) {
            delivered.add(this._args[2]);
          }
          if (this._sql.includes("INTO posts")) {
            const [kind, postDate] = this._args;
            posted.add(`${kind}|${postDate}`);
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
  it("DELIVERY_TIME overrides the default 09:30 gate (missed-check follows it too)", async () => {
    // 10:00 Lagos: past the default gate, before a 12:00 custom gate.
    const tenOClock = Date.UTC(2026, 9, 11, 9, 0);
    const db = cronDb({ cache: { status: "COMPLETE", payload_json: "{}" }, subs: [{ chat_id: "1", timezone: "" }] });
    let calls = 0;
    const customEnv = { DB: db, TIMEZONE: "Africa/Lagos", PAGE_SIZE: "5", DELIVERY_TIME: "12:00" };
    const gated = await sweepDeliveries(customEnv, "picks", tenOClock, async () => {
      calls += 1;
      return { ok: true };
    });
    assert.deepEqual(gated, { sent: 0, skipped: 0, failed: 0 });
    assert.equal(calls, 0, "custom DELIVERY_TIME must gate the sweep");
    assert.equal(await detectMissedGeneration(customEnv, tenOClock), false, "missed-check must follow the same gate");
    // Same instant, default config: past 09:30, so delivery proceeds.
    const db2 = cronDb({ cache: { status: "COMPLETE", payload_json: "{}" }, subs: [{ chat_id: "1", timezone: "" }] });
    const defaultEnv = { DB: db2, TIMEZONE: "Africa/Lagos", PAGE_SIZE: "5" };
    const res = await sweepDeliveries(defaultEnv, "picks", tenOClock, async () => ({ ok: true }));
    assert.equal(res.sent, 1, "default 09:30 gate is preserved");
  });
  it("no delivery before the Lagos gate (missed-run recovery only fires late)", async () => {
    const db = cronDb({ cache: { status: "COMPLETE", payload_json: "{}" }, subs: [{ chat_id: "1", timezone: "" }] });
    let calls = 0;
    const env = { DB: db, TIMEZONE: "Africa/Lagos", PAGE_SIZE: "5" };
    const res = await sweepDeliveries(env, "picks", BEFORE_GATE, async () => {
      calls += 1;
      return { ok: true };
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
      return { ok: true };
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
    const res = await sweepDeliveries(env, "picks", AFTER_GATE, async () => ({ ok: false }));
    assert.deepEqual(res, { sent: 0, skipped: 0, failed: 1 });
  });
  it("no COMPLETE cache means waiting, not fabrication", async () => {
    const db = cronDb({ cache: null, subs: [{ chat_id: "1", timezone: "" }] });
    const env = { DB: db, TIMEZONE: "Africa/Lagos", PAGE_SIZE: "5" };
    let calls = 0;
    const res = await sweepDeliveries(env, "picks", AFTER_GATE, async () => {
      calls += 1;
      return { ok: true };
    });
    assert.equal(calls, 0);
    assert.deepEqual(res, { sent: 0, skipped: 0, failed: 0 });
  });
});

describe("group posting (production model: single group, never per-subscriber)", () => {
  it("posts once to TELEGRAM_CHAT_ID when cache is COMPLETE and past POST_TIME", async () => {
    const db = cronDb({ cache: { status: "COMPLETE", payload_json: "{}" } });
    const env = { DB: db, TIMEZONE: "Africa/Lagos", PAGE_SIZE: "5", TELEGRAM_CHAT_ID: "-100123" };
    const sent = [];
    const sender = async (_e, _m, p) => {
      sent.push(p.chat_id);
      return { ok: true, messageId: 42 };
    };
    const outcome = await postGroupPicks(env, "top", AFTER_GATE, sender);
    assert.equal(outcome, "sent");
    assert.deepEqual(sent, ["-100123"]);
    // Duplicate Cron fire (or overlap) must not send twice.
    const outcome2 = await postGroupPicks(env, "top", AFTER_GATE, async () => {
      throw new Error("must not send twice");
    });
    assert.equal(outcome2, "skipped");
  });
  it("waits (does not send) before POST_TIME or without a COMPLETE cache", async () => {
    const readyDb = cronDb({ cache: { status: "COMPLETE", payload_json: "{}" } });
    const env = { DB: readyDb, TIMEZONE: "Africa/Lagos", PAGE_SIZE: "5", TELEGRAM_CHAT_ID: "-100123" };
    assert.equal(
      await postGroupPicks(env, "daily", BEFORE_GATE, async () => {
        throw new Error("must not send before gate");
      }),
      "waiting",
    );
    const noCacheDb = cronDb({ cache: null });
    const env2 = { DB: noCacheDb, TIMEZONE: "Africa/Lagos", PAGE_SIZE: "5", TELEGRAM_CHAT_ID: "-100123" };
    assert.equal(
      await postGroupPicks(env2, "daily", AFTER_GATE, async () => {
        throw new Error("must not fabricate without a cache");
      }),
      "waiting",
    );
  });
  it("never sends without TELEGRAM_CHAT_ID configured", async () => {
    const db = cronDb({ cache: { status: "COMPLETE", payload_json: "{}" } });
    const env = { DB: db, TIMEZONE: "Africa/Lagos", PAGE_SIZE: "5" };
    const outcome = await postGroupPicks(env, "top", AFTER_GATE, async () => {
      throw new Error("must not send without a chat id");
    });
    assert.equal(outcome, "no-chat");
  });
  it("recap posts once to the group after RECAP_TIME, gated by its own receipt", async () => {
    const db = cronDb();
    const env = { DB: db, TIMEZONE: "Africa/Lagos", PAGE_SIZE: "5", TELEGRAM_CHAT_ID: "-100123" };
    const RECAP_AFTER = Date.UTC(2026, 9, 11, 6, 50); // 07:50 Lagos > 07:45 gate
    const sent = [];
    const texts = [];
    const outcome = await postGroupRecap(env, RECAP_AFTER, async (_e, _m, p) => {
      sent.push(p.chat_id);
      texts.push(p.text);
      return { ok: true, messageId: 7 };
    });
    assert.equal(outcome, "sent");
    assert.deepEqual(sent, ["-100123"]);
    // Matches predbot/jobs/recap.py's recap_message() title exactly
    // ("📊 RESULTS") — distinct from the interactive /yesterday button's
    // own "🗓 YESTERDAY" title (UI-parity audit, 2026-10-15).
    assert.match(texts[0], /^<b>📊 RESULTS<\/b>/);
    assert.doesNotMatch(texts[0], /YESTERDAY/);
    const outcome2 = await postGroupRecap(env, RECAP_AFTER, async () => {
      throw new Error("must not post recap twice");
    });
    assert.equal(outcome2, "skipped");
  });
  it("a failed send leaves no receipt so the next sweep retries", async () => {
    const db = cronDb({ cache: { status: "COMPLETE", payload_json: "{}" } });
    const env = { DB: db, TIMEZONE: "Africa/Lagos", PAGE_SIZE: "5", TELEGRAM_CHAT_ID: "-100123" };
    assert.equal(await postGroupPicks(env, "top", AFTER_GATE, async () => ({ ok: false })), "failed");
    assert.equal(db.posted.size, 0);
  });
});

describe("owner-private delivery (production repair, 2026-10-15: no subscriber DMs — group + owner only)", () => {
  it("posts to both the group and the owner's private chat, with independent receipts", async () => {
    const db = cronDb({ cache: { status: "COMPLETE", payload_json: "{}" } });
    const env = { DB: db, TIMEZONE: "Africa/Lagos", PAGE_SIZE: "5", TELEGRAM_CHAT_ID: "-100123", TELEGRAM_ADMIN_IDS: "999" };
    const sent = [];
    const outcome = await postGroupPicks(env, "top", AFTER_GATE, async (_e, _m, p) => {
      sent.push(p.chat_id);
      return { ok: true, messageId: 42 };
    });
    assert.equal(outcome, "sent");
    assert.deepEqual(sent, ["-100123", "999"]);
    assert.ok(db.posted.has("top|2026-10-11"));
    assert.ok(db.posted.has("top_owner|2026-10-11"));
  });
  it("a failed owner send never causes the group message to be resent, and retries independently next sweep", async () => {
    const db = cronDb({ cache: { status: "COMPLETE", payload_json: "{}" } });
    const env = { DB: db, TIMEZONE: "Africa/Lagos", PAGE_SIZE: "5", TELEGRAM_CHAT_ID: "-100123", TELEGRAM_ADMIN_IDS: "999" };
    const sent = [];
    await postGroupPicks(env, "top", AFTER_GATE, async (_e, _m, p) => {
      sent.push(p.chat_id);
      return { ok: p.chat_id !== "999" }; // group succeeds, owner fails
    });
    assert.deepEqual(sent, ["-100123", "999"]);
    assert.ok(db.posted.has("top|2026-10-11"), "the group's own receipt must still be recorded");
    assert.ok(!db.posted.has("top_owner|2026-10-11"), "the failed owner leg must have no receipt, so it retries");

    sent.length = 0;
    const outcome2 = await postGroupPicks(env, "top", AFTER_GATE, async (_e, _m, p) => {
      sent.push(p.chat_id);
      return { ok: true, messageId: 43 };
    });
    // Group is already posted (skipped) — only the owner leg is retried,
    // never a second group message.
    assert.equal(outcome2, "skipped");
    assert.deepEqual(sent, ["999"]);
    assert.ok(db.posted.has("top_owner|2026-10-11"));
  });
  it("a failed group send never attempts the owner leg that sweep (group goes first, then owner)", async () => {
    const db = cronDb({ cache: { status: "COMPLETE", payload_json: "{}" } });
    const env = { DB: db, TIMEZONE: "Africa/Lagos", PAGE_SIZE: "5", TELEGRAM_CHAT_ID: "-100123", TELEGRAM_ADMIN_IDS: "999" };
    const sent = [];
    const outcome = await postGroupPicks(env, "top", AFTER_GATE, async (_e, _m, p) => {
      sent.push(p.chat_id);
      return { ok: false };
    });
    assert.equal(outcome, "failed");
    assert.deepEqual(sent, ["-100123"]);
    assert.equal(db.posted.size, 0);
  });
  it("no owner configured: group delivery is entirely unaffected, no owner send attempted", async () => {
    const db = cronDb({ cache: { status: "COMPLETE", payload_json: "{}" } });
    const env = { DB: db, TIMEZONE: "Africa/Lagos", PAGE_SIZE: "5", TELEGRAM_CHAT_ID: "-100123" };
    const sent = [];
    const outcome = await postGroupPicks(env, "top", AFTER_GATE, async (_e, _m, p) => {
      sent.push(p.chat_id);
      return { ok: true, messageId: 42 };
    });
    assert.equal(outcome, "sent");
    assert.deepEqual(sent, ["-100123"]);
  });
  it("the recap also reaches the owner privately, independently of the group receipt", async () => {
    const db = cronDb();
    const env = { DB: db, TIMEZONE: "Africa/Lagos", PAGE_SIZE: "5", TELEGRAM_CHAT_ID: "-100123", TELEGRAM_ADMIN_IDS: "999" };
    const RECAP_AFTER = Date.UTC(2026, 9, 11, 6, 50);
    const sent = [];
    const outcome = await postGroupRecap(env, RECAP_AFTER, async (_e, _m, p) => {
      sent.push(p.chat_id);
      return { ok: true, messageId: 7 };
    });
    assert.equal(outcome, "sent");
    assert.deepEqual(sent, ["-100123", "999"]);
  });
});

describe("match-event delivery (GitHub Actions writes the event; only this sweep ever sends it)", () => {
  const EVENT = { pick_date: "2026-10-11", fixture_key: "fx-1", event_type: "start", text: "🟢 Match Started" };
  const eventKey = `evt_${EVENT.fixture_key}_${EVENT.event_type}`;

  it("delivers a new event to both the group and the owner, with independent receipts", async () => {
    const db = cronDb({ events: [EVENT] });
    const env = { DB: db, TELEGRAM_CHAT_ID: "-100123", TELEGRAM_ADMIN_IDS: "999" };
    const sent = [];
    const delivered = await sweepMatchEvents(env, Date.now(), async (_e, _m, p) => {
      sent.push(p.chat_id);
      return { ok: true, messageId: 5 };
    });
    assert.equal(delivered, 2);
    assert.deepEqual(sent.sort(), ["-100123", "999"]);
    assert.ok(db.posted.has(`${eventKey}|${EVENT.pick_date}`));
    assert.ok(db.posted.has(`${eventKey}_owner|${EVENT.pick_date}`));
  });

  it("a duplicate sweep for an already-delivered event sends nothing again", async () => {
    const db = cronDb({ events: [EVENT] });
    const env = { DB: db, TELEGRAM_CHAT_ID: "-100123", TELEGRAM_ADMIN_IDS: "999" };
    await sweepMatchEvents(env, Date.now(), async () => ({ ok: true, messageId: 5 }));
    const delivered = await sweepMatchEvents(env, Date.now(), async () => {
      throw new Error("must not re-send an already-delivered event");
    });
    assert.equal(delivered, 0);
  });

  it("a failed owner send never blocks the group, and retries independently next sweep", async () => {
    const db = cronDb({ events: [EVENT] });
    const env = { DB: db, TELEGRAM_CHAT_ID: "-100123", TELEGRAM_ADMIN_IDS: "999" };
    const sent = [];
    const delivered = await sweepMatchEvents(env, Date.now(), async (_e, _m, p) => {
      sent.push(p.chat_id);
      return { ok: p.chat_id !== "999" };
    });
    assert.equal(delivered, 1);
    assert.deepEqual(sent, ["-100123", "999"]);
    assert.ok(db.posted.has(`${eventKey}|${EVENT.pick_date}`));
    assert.ok(!db.posted.has(`${eventKey}_owner|${EVENT.pick_date}`));

    sent.length = 0;
    const delivered2 = await sweepMatchEvents(env, Date.now(), async (_e, _m, p) => {
      sent.push(p.chat_id);
      return { ok: true, messageId: 6 };
    });
    assert.equal(delivered2, 1);
    assert.deepEqual(sent, ["999"], "only the owner leg retries — the group is not re-sent");
  });

  it("no owner configured: the group still receives the event, no owner send attempted", async () => {
    const db = cronDb({ events: [EVENT] });
    const env = { DB: db, TELEGRAM_CHAT_ID: "-100123" };
    const sent = [];
    const delivered = await sweepMatchEvents(env, Date.now(), async (_e, _m, p) => {
      sent.push(p.chat_id);
      return { ok: true, messageId: 5 };
    });
    assert.equal(delivered, 1);
    assert.deepEqual(sent, ["-100123"]);
  });

  it("an unrelated event's receipt never blocks or is confused with another event's", async () => {
    const other = { pick_date: "2026-10-11", fixture_key: "fx-2", event_type: "end", text: "🏁 Match Ended" };
    const db = cronDb({ events: [EVENT, other], posted: new Set([`${eventKey}|${EVENT.pick_date}`, `${eventKey}_owner|${EVENT.pick_date}`]) });
    const env = { DB: db, TELEGRAM_CHAT_ID: "-100123", TELEGRAM_ADMIN_IDS: "999" };
    const sent = [];
    const delivered = await sweepMatchEvents(env, Date.now(), async (_e, _m, p) => {
      sent.push(p.chat_id);
      return { ok: true, messageId: 7 };
    });
    assert.equal(delivered, 2, "only fx-2's two legs are new; fx-1 was already fully delivered");
    assert.deepEqual(sent.sort(), ["-100123", "999"]);
  });

  // Regression (verification pass, 2026-10-15): the lookback window used to
  // be 48h, so an event surviving an outage longer than that would never
  // be picked up by getRecentMatchEvents again, even though the row (and
  // its receipts) never expire. 30 days makes this require an outage an
  // order of magnitude longer than anything else here tolerates.
  it("a delayed sweep still delivers an event from well beyond the old 48h boundary (up to 30 days)", async () => {
    const stale = { ...EVENT, created_at: new Date(Date.now() - 20 * 24 * 3_600_000).toISOString() }; // 20 days old
    const db = cronDb({ events: [stale] });
    const env = { DB: db, TELEGRAM_CHAT_ID: "-100123" };
    const sent = [];
    const delivered = await sweepMatchEvents(env, Date.now(), async (_e, _m, p) => {
      sent.push(p.chat_id);
      return { ok: true, messageId: 9 };
    });
    assert.equal(delivered, 1, "a 20-day-old undelivered event must still be delivered, not silently dropped");
    assert.deepEqual(sent, ["-100123"]);
  });
  it("an event genuinely older than the lookback window is no longer swept (the bound still has an edge)", async () => {
    const ancient = { ...EVENT, created_at: new Date(Date.now() - 45 * 24 * 3_600_000).toISOString() }; // 45 days old
    const db = cronDb({ events: [ancient] });
    const env = { DB: db, TELEGRAM_CHAT_ID: "-100123" };
    const delivered = await sweepMatchEvents(env, Date.now(), async () => {
      throw new Error("must not even attempt a send for an event outside the lookback window");
    });
    assert.equal(delivered, 0);
  });
});

describe("handleScheduled dispatches on the exact Cron Trigger pattern, never a substring tag", () => {
  it("only the configured pattern strings trigger anything; an unrelated pattern is a silent no-op", async () => {
    const db = cronDb({ cache: { status: "COMPLETE", payload_json: "{}" } });
    const env = { DB: db, TIMEZONE: "Africa/Lagos", PAGE_SIZE: "5", TELEGRAM_CHAT_ID: "-100123", STAGE3_CRON_ENABLED: "1" };
    // A real Cloudflare Cron Trigger pattern never contains the word "post" —
    // a dispatcher relying on cron.includes("post") would never fire.
    await handleScheduled(env, "0 8 * * *", AFTER_GATE); // looks post-ish, is NOT the registered pattern
    assert.equal(db.posted.size, 0);
    assert.equal(db.jobs.last, undefined);
  });
  it("CRON_POST_SWEEP posts top and daily to the group; CRON_RECAP_SWEEP posts group recap", async () => {
    const db = cronDb({ cache: { status: "COMPLETE", payload_json: "{}" } });
    const env = {
      DB: db,
      TIMEZONE: "Africa/Lagos",
      PAGE_SIZE: "5",
      TELEGRAM_CHAT_ID: "-100123",
      STAGE3_CRON_ENABLED: "1",
    };
    const realFetch = globalThis.fetch;
    let calls = 0;
    globalThis.fetch = async () => {
      calls += 1;
      return { ok: true, status: 200, json: async () => ({ result: { message_id: 1 } }) };
    };
    try {
      await handleScheduled(env, CRON_POST_SWEEP, AFTER_GATE);
      assert.equal(calls, 2); // top + daily, never a per-subscriber loop
      const RECAP_AFTER = Date.UTC(2026, 9, 11, 6, 50);
      await handleScheduled(env, CRON_RECAP_SWEEP, RECAP_AFTER);
      assert.equal(calls, 3);
      // CRON_DELIVER_SWEEP and CRON_WATCH are distinct patterns that must
      // not re-trigger a post even though they can fire in the same minute.
      await handleScheduled(env, CRON_DELIVER_SWEEP, AFTER_GATE);
      await handleScheduled(env, CRON_WATCH, AFTER_GATE);
      assert.equal(calls, 3);
    } finally {
      globalThis.fetch = realFetch;
    }
  });
  it("readiness sweep: a slow generation run still gets posted exactly once, not skipped for the whole day", async () => {
    // Simulates the real race this fixes: generation (GitHub Actions) and
    // posting (this Worker) run on separate infra with no shared lock. The
    // first few sweep fires land before the cache goes COMPLETE; a later
    // fire after it lands must still post; every fire after THAT must be a
    // no-op, never a duplicate.
    const db = cronDb({ cache: null });
    const env = { DB: db, TIMEZONE: "Africa/Lagos", PAGE_SIZE: "5", TELEGRAM_CHAT_ID: "-100123", STAGE3_CRON_ENABLED: "1" };
    const realFetch = globalThis.fetch;
    let calls = 0;
    globalThis.fetch = async () => {
      calls += 1;
      return { ok: true, status: 200, json: async () => ({ result: { message_id: 1 } }) };
    };
    try {
      // Two sweep fires while generation is still running (no COMPLETE cache yet).
      await handleScheduled(env, CRON_POST_SWEEP, AFTER_GATE);
      await handleScheduled(env, CRON_POST_SWEEP, AFTER_GATE + 5 * 60_000);
      assert.equal(calls, 0, "must not post stale/missing data");
      // Generation lands.
      db.cache = { status: "COMPLETE", payload_json: "{}" };
      await handleScheduled(env, CRON_POST_SWEEP, AFTER_GATE + 10 * 60_000);
      assert.equal(calls, 2, "posts top+daily exactly once generation is ready");
      // Further sweep fires that day must never duplicate.
      await handleScheduled(env, CRON_POST_SWEEP, AFTER_GATE + 15 * 60_000);
      assert.equal(calls, 2, "no duplicate group messages on later sweep fires");
    } finally {
      globalThis.fetch = realFetch;
    }
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
