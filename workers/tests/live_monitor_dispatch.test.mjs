// Real SQLite, real migrations — maybeDispatchLiveMonitor (bug 6) decides
// its own "is there work?" via one real SQL query, so it's exercised
// against the real schema rather than a hand-rolled mock.
import { DatabaseSync } from "node:sqlite";
import { readFileSync } from "node:fs";
import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { maybeDispatchLiveMonitor } from "../src/cron.ts";

function stubDb(sql) {
  return {
    prepare(query) {
      return {
        _args: [],
        bind(...args) {
          this._args = args;
          return this;
        },
        async first() {
          return sql.prepare(query).get(...this._args) ?? null;
        },
        async run() {
          const info = sql.prepare(query).run(...this._args);
          return { meta: { changes: info.changes } };
        },
      };
    },
  };
}

function world() {
  const sql = new DatabaseSync(":memory:");
  for (const file of ["0001_foundation.sql", "0003_engine_state.sql"]) {
    sql.exec(readFileSync(new URL(`../migrations/${file}`, import.meta.url), "utf8"));
  }
  return { sql, DB: stubDb(sql) };
}

const NOW = Date.UTC(2026, 9, 17, 12, 0);
const iso = (ms) => new Date(ms).toISOString();

function insertPick(sql, { id, startMs, status = "pending" }) {
  sql.prepare(
    `INSERT INTO picks (id, pick_date, tier, rank, provider, provider_event_id, sport, home, away, start_utc,
       market, period, stat, entity, selection, label, price, prob, ev, status, created_at)
     VALUES (?,'2026-10-17','top',1,'p','e','football','H','A',?,'1x2','full','points','all','home','H',1.8,0.6,0.1,?,?)`,
  ).run(id, iso(startMs), status, iso(startMs));
}

function seedPendingPublishedPick(sql, { startMs }) {
  insertPick(sql, { id: 1, startMs });
  sql.prepare("INSERT INTO prediction_memberships VALUES (1,'top',1)").run();
  sql.prepare(
    "INSERT INTO prediction_cache (cache_key, local_date, timezone, status, payload_json, generated_at, updated_at, expires_at) VALUES ('k','2026-10-17','Africa/Lagos','COMPLETE','{}',?,?,?)",
  ).run(iso(NOW), iso(NOW), iso(NOW + 86_400_000));
}

function githubFetchStub(calls, ok = true) {
  return async (url, init) => {
    calls.push({ url, init });
    return { ok, status: ok ? 204 : 500, json: async () => ({}) };
  };
}

describe("maybeDispatchLiveMonitor (bug 6: dispatch the live monitor on demand)", () => {
  it("dispatches when work exists and config is present", async () => {
    const { sql, DB } = world();
    seedPendingPublishedPick(sql, { startMs: NOW + 5 * 60_000 }); // kicks off in 5 minutes
    const env = { DB, GITHUB_REPO: "owner/repo", GITHUB_DISPATCH_TOKEN: "ghp_x" };
    const calls = [];
    await maybeDispatchLiveMonitor(env, NOW, githubFetchStub(calls));
    assert.equal(calls.length, 1);
    assert.match(calls[0].url, /repos\/owner\/repo\/actions\/workflows\/stage8-live-monitor-production\.yml\/dispatches/);
    assert.equal(calls[0].init.headers.Authorization, "Bearer ghp_x");
    assert.equal(calls[0].init.headers.Accept, "application/vnd.github+json");
    assert.equal(calls[0].init.headers["X-GitHub-Api-Version"], "2022-11-28");
    assert.ok(calls[0].init.headers["User-Agent"], "GitHub requires a User-Agent");
    const body = JSON.parse(calls[0].init.body);
    assert.deepEqual(body, { ref: "main", inputs: { dry_run: "false" } });
    const job = sql.prepare("SELECT last_run_at, last_result FROM job_state WHERE name='livemon_dispatch'").get();
    assert.ok(job.last_run_at);
    assert.match(job.last_result, /OK/);
  });

  it("respects GITHUB_REF when set", async () => {
    const { sql, DB } = world();
    seedPendingPublishedPick(sql, { startMs: NOW + 5 * 60_000 });
    const env = { DB, GITHUB_REPO: "owner/repo", GITHUB_DISPATCH_TOKEN: "t", GITHUB_REF: "staging" };
    const calls = [];
    await maybeDispatchLiveMonitor(env, NOW, githubFetchStub(calls));
    assert.equal(JSON.parse(calls[0].init.body).ref, "staging");
  });

  it("does not dispatch again before LIVE_MONITOR_INTERVAL_MIN has passed", async () => {
    const { sql, DB } = world();
    seedPendingPublishedPick(sql, { startMs: NOW + 5 * 60_000 });
    const env = { DB, GITHUB_REPO: "owner/repo", GITHUB_DISPATCH_TOKEN: "t", LIVE_MONITOR_INTERVAL_MIN: "10" };
    const calls = [];
    await maybeDispatchLiveMonitor(env, NOW, githubFetchStub(calls));
    assert.equal(calls.length, 1);
    await maybeDispatchLiveMonitor(env, NOW + 5 * 60_000, githubFetchStub(calls)); // only 5 min later
    assert.equal(calls.length, 1, "too soon - no second dispatch");
    await maybeDispatchLiveMonitor(env, NOW + 11 * 60_000, githubFetchStub(calls)); // 11 min after the first
    assert.equal(calls.length, 2, "interval has passed - dispatches again");
  });

  it("does not dispatch at all when there is no work", async () => {
    const { DB } = world(); // no picks, no live_fixture_state rows
    const env = { DB, GITHUB_REPO: "owner/repo", GITHUB_DISPATCH_TOKEN: "t" };
    const calls = [];
    await maybeDispatchLiveMonitor(env, NOW, githubFetchStub(calls));
    assert.equal(calls.length, 0);
  });

  it("a live_fixture_state row needing settlement/notification is also 'work', even with no pending pick", async () => {
    const { sql, DB } = world();
    sql.prepare("INSERT INTO live_fixture_state (pick_date, fixture_key, status, settlement_done, notifications_done, updated_at) VALUES ('2026-10-17','fx','final',1,0,?)").run(iso(NOW));
    const env = { DB, GITHUB_REPO: "owner/repo", GITHUB_DISPATCH_TOKEN: "t" };
    const calls = [];
    await maybeDispatchLiveMonitor(env, NOW, githubFetchStub(calls));
    assert.equal(calls.length, 1);
  });

  it("an unpublished pending pick (no posts receipt, no COMPLETE cache) is NOT work", async () => {
    const { sql, DB } = world();
    insertPick(sql, { id: 1, startMs: NOW + 5 * 60_000 });
    sql.prepare("INSERT INTO prediction_memberships VALUES (1,'top',1)").run();
    // Deliberately no posts receipt and no COMPLETE prediction_cache row.
    const env = { DB, GITHUB_REPO: "owner/repo", GITHUB_DISPATCH_TOKEN: "t" };
    const calls = [];
    await maybeDispatchLiveMonitor(env, NOW, githubFetchStub(calls));
    assert.equal(calls.length, 0);
  });

  it("DMs the owner once per Lagos day when work exists but config is missing, never a GitHub call", async () => {
    const { sql, DB } = world();
    seedPendingPublishedPick(sql, { startMs: NOW + 5 * 60_000 });
    const env = { DB, TELEGRAM_ADMIN_IDS: "999" }; // no GITHUB_REPO/TOKEN
    const sent = [];
    const sender = async (_e, _m, params) => {
      sent.push(params);
      return { ok: true, messageId: sent.length };
    };
    const calls = [];
    await maybeDispatchLiveMonitor(env, NOW, githubFetchStub(calls), sender);
    assert.equal(calls.length, 0, "never a GitHub call without config");
    assert.equal(sent.length, 1);
    assert.equal(sent[0].chat_id, "999");
    await maybeDispatchLiveMonitor(env, NOW + 60_000, githubFetchStub(calls), sender);
    assert.equal(sent.length, 1, "only once per day, receipt-guarded");
  });

  it("never alerts when there is no owner configured either", async () => {
    const { sql, DB } = world();
    seedPendingPublishedPick(sql, { startMs: NOW + 5 * 60_000 });
    const env = { DB }; // no owner, no GITHUB_REPO/TOKEN
    const sent = [];
    const sender = async (_e, _m, params) => {
      sent.push(params);
      return { ok: true, messageId: 1 };
    };
    await maybeDispatchLiveMonitor(env, NOW, githubFetchStub([]), sender);
    assert.equal(sent.length, 0);
  });
});
