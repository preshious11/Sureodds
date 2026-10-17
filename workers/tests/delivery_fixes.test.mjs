// Covers the three production delivery-failure fixes:
//  - root cause 3 (recap can lock in unsettled results): postGroupRecap's
//    pending-settlement wait and its DEFAULT_RECAP_DEADLINE fallback.
//  - bug 4 (owner gets nothing when TELEGRAM_CHAT_ID is unset): postGroupPicks
//    and postGroupRecap both still reach the owner leg.
//  - root cause 2 (10-minute match-start window too short): decideMatchEvent
//    at the new MATCH_START_WINDOW_MIN=30 boundary.
// Minimal in-file D1 stub, in the style of tests/cron.test.mjs's cronDb.
import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { decideMatchEvent, detectMissedGeneration, postGroupPicks, postGroupRecap } from "../src/cron.ts";

// RECAP_TIME 07:45 Lagos = 06:45 UTC; RECAP_DEADLINE 08:45 Lagos = 07:45 UTC;
// DELIVERY_TIME 09:30 Lagos = 08:30 UTC. (Lagos is UTC+1, no DST.)
const RECAP_AFTER_GATE_BEFORE_DEADLINE = Date.UTC(2026, 9, 11, 6, 50); // 07:50 Lagos
const RECAP_AT_DEADLINE = Date.UTC(2026, 9, 11, 7, 45); // 08:45 Lagos
const PICKS_AFTER_GATE = Date.UTC(2026, 9, 11, 8, 35); // 09:35 Lagos

function stubDb({ cache = null, statuses = {} } = {}) {
  // Models the real `posts` table the same way cronDb does: a row with
  // message_id === null is an unconfirmed claim, non-null is confirmed.
  const postsTable = new Map();
  return {
    prepare(sql) {
      return {
        _sql: sql,
        _args: [],
        bind(...args) {
          this._args = args;
          return this;
        },
        async first() {
          if (this._sql.includes("FROM prediction_cache")) return cache;
          if (this._sql.includes("FROM posts")) {
            const [kind, postDate] = this._args;
            return postsTable.has(`${kind}|${postDate}`) ? { one: 1 } : null;
          }
          return null;
        },
        async all() {
          // getRecapStatuses: "SELECT p.status AS status FROM picks p JOIN
          // prediction_memberships m ..." — distinguished from the picks-page
          // query (getAllPicksForCollection) by its unique "AS status" select.
          if (this._sql.includes("AS status FROM picks")) {
            const [, collection] = this._args;
            return { results: (statuses[collection] ?? []).map((status) => ({ status })) };
          }
          // getAllPicksForCollection (renderGroupCard): no fixtures needed for
          // these tests — an empty collection still renders a valid (empty) card.
          return { results: [] };
        },
        async run() {
          if (this._sql.includes("INTO posts") && this._sql.includes("DO UPDATE")) {
            // claimPost
            const [kind, postDate] = this._args;
            const key = `${kind}|${postDate}`;
            if (!postsTable.has(key)) {
              postsTable.set(key, { message_id: null });
              return { meta: { changes: 1 } };
            }
            return { meta: { changes: 0 } };
          }
          if (this._sql.startsWith("UPDATE posts SET message_id")) {
            // confirmPosted
            const [kind, postDate, messageId] = this._args;
            const row = postsTable.get(`${kind}|${postDate}`);
            if (row) row.message_id = messageId;
          }
          if (this._sql.startsWith("DELETE FROM posts")) {
            // releaseClaim
            const [kind, postDate] = this._args;
            const key = `${kind}|${postDate}`;
            const row = postsTable.get(key);
            if (row && row.message_id === null) postsTable.delete(key);
          }
          return { meta: { changes: 1 } };
        },
      };
    },
  };
}

function recorder() {
  const sent = [];
  const fn = async (_env, _method, params) => {
    sent.push({ chat: String(params.chat_id), text: params.text });
    return { ok: true, messageId: sent.length };
  };
  return { sent, fn };
}

const COMPLETE_CACHE = { status: "COMPLETE" };

describe("recap waits for settlement, but never skips the day entirely", () => {
  it('returns "waiting" and sends nothing at 07:50 Lagos when yesterday has a pending status', async () => {
    const db = stubDb({ statuses: { top: ["pending"], daily: ["won"] } });
    const env = { DB: db, TELEGRAM_CHAT_ID: "-100", TELEGRAM_ADMIN_IDS: "999" };
    const r = recorder();
    const outcome = await postGroupRecap(env, RECAP_AFTER_GATE_BEFORE_DEADLINE, r.fn);
    assert.equal(outcome, "waiting");
    assert.equal(r.sent.length, 0);
  });

  it("sends to the group and the owner when everything is settled", async () => {
    const db = stubDb({ statuses: { top: ["won", "lost"], daily: ["lost"] } });
    const env = { DB: db, TELEGRAM_CHAT_ID: "-100", TELEGRAM_ADMIN_IDS: "999" };
    const r = recorder();
    const outcome = await postGroupRecap(env, RECAP_AFTER_GATE_BEFORE_DEADLINE, r.fn);
    assert.equal(outcome, "sent");
    assert.deepEqual(r.sent.map((s) => s.chat).sort(), ["-100", "999"]);
  });

  it("skips silently (no placeholder) and marks the day handled when yesterday has no picks at all", async () => {
    const db = stubDb({ statuses: { top: [], daily: [] } });
    const env = { DB: db, TELEGRAM_CHAT_ID: "-100", TELEGRAM_ADMIN_IDS: "999" };
    const r = recorder();
    const outcome = await postGroupRecap(env, RECAP_AFTER_GATE_BEFORE_DEADLINE, r.fn);
    assert.equal(outcome, "skipped");
    assert.equal(r.sent.length, 0, 'must never post the "No results yet" placeholder');
  });

  it("sends at 08:45 Lagos even with pending legs (never skipped entirely)", async () => {
    const db = stubDb({ statuses: { top: ["pending"], daily: ["pending"] } });
    const env = { DB: db, TELEGRAM_CHAT_ID: "-100", TELEGRAM_ADMIN_IDS: "999" };
    const r = recorder();
    const outcome = await postGroupRecap(env, RECAP_AT_DEADLINE, r.fn);
    assert.equal(outcome, "sent");
    assert.deepEqual(r.sent.map((s) => s.chat).sort(), ["-100", "999"]);
  });
});

describe("the owner leg is independent of TELEGRAM_CHAT_ID (bug 4)", () => {
  it("postGroupPicks with only TELEGRAM_ADMIN_IDS sends to the owner only and returns no-chat", async () => {
    const db = stubDb({ cache: COMPLETE_CACHE });
    const env = { DB: db, TELEGRAM_ADMIN_IDS: "999" }; // no TELEGRAM_CHAT_ID
    const r = recorder();
    const outcome = await postGroupPicks(env, "daily", PICKS_AFTER_GATE, r.fn);
    assert.equal(outcome, "no-chat");
    assert.deepEqual(r.sent.map((s) => s.chat), ["999"]);
  });

  it("postGroupRecap with only TELEGRAM_ADMIN_IDS sends to the owner only and returns no-chat", async () => {
    const db = stubDb({ statuses: { top: ["won"], daily: ["lost"] } });
    const env = { DB: db, TELEGRAM_ADMIN_IDS: "999" }; // no TELEGRAM_CHAT_ID
    const r = recorder();
    const outcome = await postGroupRecap(env, RECAP_AT_DEADLINE, r.fn);
    assert.equal(outcome, "no-chat");
    assert.deepEqual(r.sent.map((s) => s.chat), ["999"]);
  });

  it('still returns "no-chat" and sends nothing when neither a chat nor an admin id is configured (existing behaviour unchanged)', async () => {
    const db = stubDb({ cache: COMPLETE_CACHE });
    const env = { DB: db };
    const r = recorder();
    assert.equal(await postGroupPicks(env, "top", PICKS_AFTER_GATE, r.fn), "no-chat");
    assert.equal(await postGroupRecap(env, RECAP_AT_DEADLINE, r.fn), "no-chat");
    assert.equal(r.sent.length, 0);
  });
});

describe("missed generation also DMs the owner once per day (bug 7)", () => {
  it("DMs the owner when generation is missed, guarded by its own receipt", async () => {
    const db = stubDb(); // no cache -> missed
    const env = { DB: db, TELEGRAM_ADMIN_IDS: "999" };
    const r = recorder();
    const missed = await detectMissedGeneration(env, PICKS_AFTER_GATE, r.fn);
    assert.equal(missed, true);
    assert.deepEqual(r.sent.map((s) => s.chat), ["999"]);
    // A second invocation that same day must not DM again.
    const r2 = recorder();
    await detectMissedGeneration(env, PICKS_AFTER_GATE + 5 * 60_000, r2.fn);
    assert.equal(r2.sent.length, 0);
  });

  it("never DMs when generation actually landed", async () => {
    const db = stubDb({ cache: COMPLETE_CACHE });
    const env = { DB: db, TELEGRAM_ADMIN_IDS: "999" };
    const r = recorder();
    const missed = await detectMissedGeneration(env, PICKS_AFTER_GATE, r.fn);
    assert.equal(missed, false);
    assert.equal(r.sent.length, 0);
  });
});

describe("decideMatchEvent at the widened MATCH_START_WINDOW_MIN=30 (root cause 2)", () => {
  const ctx = { kickoff_utc: null, live_status: "in_progress", settlement_done: 0, published_open: 1, published_total: 1 };
  const evt = { pick_date: "2026-10-11", fixture_key: "football|x", event_type: "start", text: "t", created_at: "" };
  const env = { MATCH_START_WINDOW_MIN: "30" };

  it("kickoff 20 minutes ago still sends", () => {
    const now = Date.now();
    const decision = decideMatchEvent(env, evt, { ...ctx, kickoff_utc: new Date(now - 20 * 60_000).toISOString() }, now);
    assert.deepEqual(decision, { action: "send" });
  });

  it("kickoff 31 minutes ago has expired", () => {
    const now = Date.now();
    const decision = decideMatchEvent(env, evt, { ...ctx, kickoff_utc: new Date(now - 31 * 60_000).toISOString() }, now);
    assert.equal(decision.action, "expire");
  });
});
