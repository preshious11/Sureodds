// Ground rule: every Worker scheduled invocation must stay at or below 40
// D1 queries in the worst case (the free plan allows 50 per invocation).
// Counts real D1Database.prepare() calls against real SQLite + real
// migrations, under an adversarial (not average-case) fixture for each of
// the four Cron Trigger patterns.
import { DatabaseSync } from "node:sqlite";
import { readFileSync } from "node:fs";
import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { CRON_DELIVER_SWEEP, CRON_POST_SWEEP, CRON_RECAP_SWEEP, CRON_WATCH, handleScheduled } from "../src/cron.ts";

const BUDGET = 40;
const DAY = "2026-10-17";
const NOW = Date.UTC(2026, 9, 17, 8, 35); // 09:35 Lagos - past every gate (DELIVERY_TIME, RECAP_TIME, RECAP_DEADLINE)
const iso = (ms) => new Date(ms).toISOString();

function countingDb(sql) {
  let count = 0;
  const db = {
    get count() {
      return count;
    },
    prepare(query) {
      count += 1;
      return {
        _args: [],
        bind(...args) {
          this._args = args;
          return this;
        },
        async first() {
          return sql.prepare(query).get(...this._args) ?? null;
        },
        async all() {
          return { results: sql.prepare(query).all(...this._args) };
        },
        async run() {
          const info = sql.prepare(query).run(...this._args);
          return { meta: { changes: info.changes } };
        },
      };
    },
  };
  return db;
}

function world() {
  const sql = new DatabaseSync(":memory:");
  for (const file of [
    "0001_foundation.sql",
    "0003_engine_state.sql",
    "0006_custom_sessions.sql",
    "0007_custom_picks_conversation.sql",
    "0009_match_events.sql",
  ]) {
    sql.exec(readFileSync(new URL(`../migrations/${file}`, import.meta.url), "utf8"));
  }
  return { sql, DB: countingDb(sql) };
}

function insertPick(sql, { id, tier, status, startMs }) {
  sql.prepare(
    `INSERT INTO picks (id, pick_date, tier, rank, provider, provider_event_id, sport, home, away, start_utc,
       market, period, stat, entity, selection, label, price, prob, ev, status, created_at, fixture_key)
     VALUES (?,?,?,1,'p','e','football','H','A',?,'1x2','full','points','all','home','H',1.8,0.6,0.1,?,?,?)`,
  ).run(id, DAY, tier, iso(startMs), status, iso(startMs), `fx-${id}`);
  sql.prepare("INSERT INTO prediction_memberships VALUES (?,?,1)").run(id, tier);
}

async function noopFetch() {
  return { ok: true, status: 204, json: async () => ({}) };
}

describe("query budget: every scheduled invocation stays at or below 40 D1 queries", () => {
  it("CRON_POST_SWEEP: unclaimed Top AND Daily, both needing a full group+owner send", async () => {
    const { sql, DB } = world();
    sql.prepare(
      "INSERT INTO prediction_cache (cache_key, local_date, timezone, status, payload_json, generated_at, updated_at, expires_at) VALUES ('k',?,?,'COMPLETE','{}',?,?,?)",
    ).run(DAY, "Africa/Lagos", iso(NOW), iso(NOW), iso(NOW + 86_400_000));
    insertPick(sql, { id: 1, tier: "top", status: "pending", startMs: NOW + 3_600_000 });
    insertPick(sql, { id: 2, tier: "daily", status: "pending", startMs: NOW + 3_600_000 });
    const env = { DB, TELEGRAM_CHAT_ID: "-100", TELEGRAM_ADMIN_IDS: "999", TIMEZONE: "Africa/Lagos", STAGE3_CRON_ENABLED: "1" };
    const realFetch = globalThis.fetch;
    globalThis.fetch = async () => ({ ok: true, status: 200, json: async () => ({ ok: true, result: { message_id: 1 } }) });
    try {
      await handleScheduled(env, CRON_POST_SWEEP, NOW);
    } finally {
      globalThis.fetch = realFetch;
    }
    assert.ok(DB.count <= BUDGET, `CRON_POST_SWEEP used ${DB.count} queries, budget is ${BUDGET}`);
  });

  it("CRON_RECAP_SWEEP: unclaimed recap, yesterday fully settled (non-pending), full group+owner send", async () => {
    const { sql, DB } = world();
    const yesterday = "2026-10-16";
    for (const [id, tier, status] of [[1, "top", "won"], [2, "daily", "lost"]]) {
      insertPick(sql, { id, tier, status, startMs: Date.UTC(2026, 9, 16, 18, 0) });
    }
    const env = { DB, TELEGRAM_CHAT_ID: "-100", TELEGRAM_ADMIN_IDS: "999", TIMEZONE: "Africa/Lagos", STAGE3_CRON_ENABLED: "1" };
    const realFetch = globalThis.fetch;
    globalThis.fetch = async () => ({ ok: true, status: 200, json: async () => ({ ok: true, result: { message_id: 1 } }) });
    try {
      await handleScheduled(env, CRON_RECAP_SWEEP, NOW);
    } finally {
      globalThis.fetch = realFetch;
    }
    assert.ok(DB.count <= BUDGET, `CRON_RECAP_SWEEP used ${DB.count} queries, budget is ${BUDGET}`);
  });

  it("CRON_DELIVER_SWEEP: a backlog of 300 already-delivered events plus 5 fresh ones needing both legs, plus live-monitor dispatch", async () => {
    const { sql, DB } = world();
    sql.exec("ALTER TABLE telegram_updates ADD COLUMN x INTEGER"); // no-op, keeps table present for getFailedUpdates
    // 300 fully-delivered (group+owner+receipts) old events - must not cost
    // anything once getUndeliveredMatchEvents filters them out in its own
    // single query (bug 2's whole point).
    for (let i = 0; i < 300; i++) {
      const key = `old-${i}`;
      sql.prepare("INSERT INTO match_events VALUES (?,?,?,?,?)").run(DAY, key, "start", "t", iso(NOW - 3_600_000 - i * 1000));
      sql.prepare("INSERT INTO posts (kind, post_date, message_id, created_at) VALUES (?,?,?,?)").run(`evt_${key}_start`, DAY, "1", iso(NOW));
      sql.prepare("INSERT INTO posts (kind, post_date, message_id, created_at) VALUES (?,?,?,?)").run(`evt_${key}_start_owner`, DAY, "1", iso(NOW));
    }
    // 5 fresh events, each eligible to send (published, in_progress, inside the kickoff window).
    for (let i = 0; i < 5; i++) {
      const key = `new-${i}`;
      insertPick(sql, { id: 100 + i, tier: "top", status: "pending", startMs: NOW - 60_000 });
      sql.prepare("UPDATE picks SET fixture_key=? WHERE id=?").run(key, 100 + i);
      sql.prepare("DELETE FROM prediction_memberships WHERE prediction_id=?").run(100 + i);
      sql.prepare("INSERT INTO prediction_memberships VALUES (?,?,1)").run(100 + i, "top");
      sql.prepare("INSERT INTO live_fixture_state (pick_date, fixture_key, status, updated_at) VALUES (?,?,?,?)").run(DAY, key, "in_progress", iso(NOW));
      sql.prepare("INSERT INTO match_events VALUES (?,?,?,?,?)").run(DAY, key, "start", "t", iso(NOW - 30_000));
    }
    sql.prepare(
      "INSERT INTO prediction_cache (cache_key, local_date, timezone, status, payload_json, generated_at, updated_at, expires_at) VALUES ('k',?,?,'COMPLETE','{}',?,?,?)",
    ).run(DAY, "Africa/Lagos", iso(NOW), iso(NOW), iso(NOW + 86_400_000));
    const env = {
      DB,
      TELEGRAM_CHAT_ID: "-100",
      TELEGRAM_ADMIN_IDS: "999",
      TIMEZONE: "Africa/Lagos",
      STAGE3_CRON_ENABLED: "1",
      GITHUB_REPO: "owner/repo",
      GITHUB_DISPATCH_TOKEN: "t",
    };
    const realFetch = globalThis.fetch;
    globalThis.fetch = async (url) => {
      if (String(url).includes("api.github.com")) return noopFetch();
      return { ok: true, status: 200, json: async () => ({ ok: true, result: { message_id: 1 } }) };
    };
    try {
      await handleScheduled(env, CRON_DELIVER_SWEEP, NOW);
    } finally {
      globalThis.fetch = realFetch;
    }
    assert.ok(DB.count <= BUDGET, `CRON_DELIVER_SWEEP used ${DB.count} queries, budget is ${BUDGET}`);
  });

  // Bug 2's explicit regression case: a 300-event backlog must not prevent
  // (or even meaningfully cost extra to reach) delivering ONE new event.
  it("a 300-event backlog plus 1 new one: the new one still gets delivered", async () => {
    const { sql, DB } = world();
    for (let i = 0; i < 300; i++) {
      const key = `old-${i}`;
      sql.prepare("INSERT INTO match_events VALUES (?,?,?,?,?)").run(DAY, key, "start", "t", iso(NOW - 3_600_000 - i * 1000));
      sql.prepare("INSERT INTO posts (kind, post_date, message_id, created_at) VALUES (?,?,?,?)").run(`evt_${key}_start`, DAY, "1", iso(NOW));
    }
    insertPick(sql, { id: 1, tier: "top", status: "pending", startMs: NOW - 60_000 });
    sql.prepare("UPDATE picks SET fixture_key='new-fx' WHERE id=1").run();
    sql.prepare("DELETE FROM prediction_memberships WHERE prediction_id=1").run();
    sql.prepare("INSERT INTO prediction_memberships VALUES (1,'top',1)").run();
    sql.prepare("INSERT INTO live_fixture_state (pick_date, fixture_key, status, updated_at) VALUES (?,?,?,?)").run(DAY, "new-fx", "in_progress", iso(NOW));
    sql.prepare("INSERT INTO match_events VALUES (?,?,?,?,?)").run(DAY, "new-fx", "start", "t", iso(NOW - 30_000));
    sql.prepare(
      "INSERT INTO prediction_cache (cache_key, local_date, timezone, status, payload_json, generated_at, updated_at, expires_at) VALUES ('k',?,?,'COMPLETE','{}',?,?,?)",
    ).run(DAY, "Africa/Lagos", iso(NOW), iso(NOW), iso(NOW + 86_400_000));
    const env = { DB, TELEGRAM_CHAT_ID: "-100", STAGE3_CRON_ENABLED: "1", TIMEZONE: "Africa/Lagos" };
    const sent = [];
    const realFetch = globalThis.fetch;
    globalThis.fetch = async (_url, init) => {
      sent.push(JSON.parse(init.body));
      return { ok: true, status: 200, json: async () => ({ ok: true, result: { message_id: 1 } }) };
    };
    try {
      await handleScheduled(env, CRON_DELIVER_SWEEP, NOW);
    } finally {
      globalThis.fetch = realFetch;
    }
    assert.ok(sent.some((s) => s.chat_id === "-100"), "the new event's group leg was sent despite the 300-event backlog");
    assert.ok(DB.count <= BUDGET, `used ${DB.count} queries, budget is ${BUDGET}`);
  });

  it("CRON_WATCH: a missed generation alert plus the full daily cleanup sweep", async () => {
    const { sql, DB } = world();
    // No COMPLETE cache for today -> missed. Seed some old rows for cleanupOldRows to find.
    sql.prepare("INSERT INTO telegram_updates (update_id, status, updated_at) VALUES (1,'done',?)").run(iso(NOW - 10 * 86_400_000));
    sql.prepare(
      "INSERT INTO custom_suggestions (token, user_id, chat_id, label, request_json, ticket_json, created_at, expires_at) VALUES ('t','u','c','l','{}','{}',?,?)",
    ).run(iso(NOW - 86_400_000), iso(NOW - 1000));
    sql.prepare("INSERT INTO custom_sessions (user_id, chat_id, active, updated_at) VALUES ('u','c',0,?)").run(iso(NOW - 3 * 86_400_000));
    sql.prepare("INSERT INTO match_events VALUES (?,?,?,?,?)").run(DAY, "old", "start", "t", iso(NOW - 31 * 86_400_000));
    const env = { DB, TELEGRAM_ADMIN_IDS: "999", TIMEZONE: "Africa/Lagos", STAGE3_CRON_ENABLED: "1" };
    const realFetch = globalThis.fetch;
    globalThis.fetch = async () => ({ ok: true, status: 200, json: async () => ({ ok: true, result: { message_id: 1 } }) });
    try {
      await handleScheduled(env, CRON_WATCH, NOW);
    } finally {
      globalThis.fetch = realFetch;
    }
    assert.ok(DB.count <= BUDGET, `CRON_WATCH used ${DB.count} queries, budget is ${BUDGET}`);
  });
});
