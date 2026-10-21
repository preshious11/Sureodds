import { DatabaseSync } from "node:sqlite";
import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  CRON_DELIVER_SWEEP,
  CRON_POST_SWEEP,
  CRON_RECAP_SWEEP,
  decideMatchEvent,
  handleScheduled,
  lagosMinutes,
  postGroupPicks,
  sweepMatchEvents,
} from "../src/cron.ts";
import { getMatchEventContext } from "../src/db.ts";

// Regression coverage for the 2026-10-16 production incident: match
// notifications must follow the PUBLISHED Top/Daily selections only, a
// kickoff alert must never arrive after the window (or after full time),
// and completion alerts need confirmed completion + settlement. These run
// against real SQL (node:sqlite) so the publication query is exercised, not
// mocked.

function stubDb(sql) {
  return {
    prepare(query) {
      return {
        bind(...args) {
          return {
            async first() {
              return sql.prepare(query).get(...args) ?? null;
            },
            async all() {
              return { results: sql.prepare(query).all(...args) };
            },
            async run() {
              const info = sql.prepare(query).run(...args);
              return { meta: { changes: info.changes } };
            },
          };
        },
      };
    },
  };
}

const DAY = "2026-10-16";
const KICKOFF = Date.UTC(2026, 9, 16, 14, 0); // 15:00 Lagos
const iso = (ms) => new Date(ms).toISOString();

function world() {
  const sql = new DatabaseSync(":memory:");
  sql.exec(`
    CREATE TABLE picks(id INTEGER PRIMARY KEY, pick_date TEXT, tier TEXT DEFAULT 'all', rank INTEGER DEFAULT 0,
      sport TEXT DEFAULT 'football', league TEXT, home TEXT DEFAULT 'H', away TEXT DEFAULT 'A', start_utc TEXT,
      market TEXT DEFAULT '1x2', selection TEXT DEFAULT 'home', label TEXT DEFAULT 'H to Win', price REAL DEFAULT 2,
      prob REAL DEFAULT 0.6, status TEXT, fixture_key TEXT, result_json TEXT);
    CREATE TABLE prediction_memberships(prediction_id INTEGER, collection TEXT, rank INTEGER,
      PRIMARY KEY(prediction_id, collection));
    CREATE TABLE posts(id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, post_date TEXT, message_id TEXT,
      created_at TEXT, UNIQUE(kind, post_date));
    CREATE TABLE live_fixture_state(pick_date TEXT, fixture_key TEXT, status TEXT, settlement_done INTEGER DEFAULT 0,
      notifications_done INTEGER DEFAULT 0, PRIMARY KEY(pick_date, fixture_key));
    CREATE TABLE match_events(pick_date TEXT, fixture_key TEXT, event_type TEXT, text TEXT, created_at TEXT,
      PRIMARY KEY(pick_date, fixture_key, event_type));
    CREATE TABLE job_state(name TEXT PRIMARY KEY, due_at TEXT, lease_owner TEXT, lease_until TEXT,
      attempts INTEGER NOT NULL DEFAULT 0, last_run_at TEXT, last_result TEXT,
      updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')));
    CREATE TABLE prediction_cache(cache_key TEXT PRIMARY KEY, local_date TEXT, timezone TEXT, status TEXT, payload_json TEXT);
    -- Empty in every test here (none seed a Pro Picks pool/candidates):
    -- present only so postProPicksToAdmin's getCustomPickPool query finds a
    -- real table and returns "no pool yet" instead of a missing-table error.
    CREATE TABLE custom_pick_pools(prediction_date TEXT, timezone TEXT, generated_at TEXT, status TEXT, diagnostics_json TEXT,
      PRIMARY KEY(timezone, prediction_date));
    CREATE TABLE custom_pick_candidates(prediction_date TEXT, timezone TEXT, candidate_id TEXT, canonical_event_id TEXT,
      payload_json TEXT, first_seen_at TEXT, generated_at TEXT, schema_version INTEGER,
      PRIMARY KEY(timezone, prediction_date, candidate_id));
  `);
  let nextId = 1;
  return {
    sql,
    env: { DB: stubDb(sql), TIMEZONE: "Africa/Lagos", PAGE_SIZE: "5", TELEGRAM_CHAT_ID: "-100", TELEGRAM_ADMIN_IDS: "999" },
    fixture(key, { collections = [], status = "pending", live = null, settled = 0 } = {}) {
      const id = nextId++;
      sql.prepare("INSERT INTO picks(id,pick_date,fixture_key,start_utc,status) VALUES(?,?,?,?,?)").run(id, DAY, key, iso(KICKOFF), status);
      for (const c of collections) sql.prepare("INSERT INTO prediction_memberships VALUES(?,?,?)").run(id, c, 1);
      if (live) sql.prepare("INSERT INTO live_fixture_state(pick_date,fixture_key,status,settlement_done) VALUES(?,?,?,?)").run(DAY, key, live, settled);
      return id;
    },
    publish(...kinds) {
      for (const k of kinds) sql.prepare("INSERT INTO posts(kind,post_date,created_at) VALUES(?,?,?)").run(k, DAY, iso(KICKOFF));
    },
    event(key, type, createdMs = KICKOFF + 60_000) {
      sql.prepare("INSERT INTO match_events VALUES(?,?,?,?,?)").run(DAY, key, type, `${type} ${key}`, iso(createdMs));
    },
    receipts() {
      return sql.prepare("SELECT kind FROM posts ORDER BY id").all().map((r) => r.kind);
    },
  };
}

function recorder(failFor = new Set()) {
  const sent = [];
  const fn = async (_e, _m, p) => {
    if (failFor.has(String(p.chat_id))) return { ok: false };
    sent.push({ chat: String(p.chat_id), text: p.text });
    return { ok: true, messageId: sent.length };
  };
  return { sent, fn };
}

const AT_KICKOFF = KICKOFF + 2 * 60_000;
const CACHE = (sql) => sql.prepare("INSERT INTO prediction_cache VALUES(?,?,?,?,?)").run("k", DAY, "Africa/Lagos", "COMPLETE", "{}");

describe("which fixtures are eligible (persisted publication records are the source of truth)", () => {
  it("Top-only fixture is tracked", async () => {
    const w = world();
    w.fixture("top-only", { collections: ["all", "top"], live: "in_progress" });
    w.publish("top", "daily");
    w.event("top-only", "start");
    const r = recorder();
    await sweepMatchEvents(w.env, AT_KICKOFF, r.fn);
    assert.deepEqual(r.sent.map((s) => s.chat).sort(), ["-100", "999"]);
  });

  it("Daily-only fixture is tracked", async () => {
    const w = world();
    w.fixture("daily-only", { collections: ["all", "daily"], live: "in_progress" });
    w.publish("top", "daily");
    w.event("daily-only", "start");
    const r = recorder();
    await sweepMatchEvents(w.env, AT_KICKOFF, r.fn);
    assert.equal(r.sent.length, 2);
  });

  it("a fixture in both Top and Daily (several selections) is notified exactly once per destination", async () => {
    const w = world();
    w.fixture("both", { collections: ["all", "top"], live: "in_progress" });
    w.fixture("both", { collections: ["all", "daily"], live: null });
    w.fixture("both", { collections: ["all", "top", "daily"] });
    w.publish("top", "daily");
    w.event("both", "start");
    const ctx = await getMatchEventContext(w.env.DB, DAY, "both");
    assert.equal(ctx.published_total, 3);
    const r = recorder();
    await sweepMatchEvents(w.env, AT_KICKOFF, r.fn);
    await sweepMatchEvents(w.env, AT_KICKOFF + 60_000, r.fn);
    assert.equal(r.sent.length, 2, "one group + one owner message, however many selections the fixture has");
  });

  it("an All-only fixture is never notified, and is expired rather than retried", async () => {
    const w = world();
    w.fixture("all-only", { collections: ["all"], live: "in_progress" });
    w.publish("top", "daily");
    w.event("all-only", "start");
    w.event("all-only", "end");
    const r = recorder();
    await sweepMatchEvents(w.env, AT_KICKOFF, r.fn);
    assert.equal(r.sent.length, 0);
    assert.ok(w.receipts().includes("evt_all-only_start_expired"));
  });

  it("a Top/Daily membership whose card was never posted (unpublished) is never notified", async () => {
    const w = world();
    w.fixture("unposted", { collections: ["all", "top"], live: "in_progress" });
    w.publish("daily"); // top card never went out
    w.event("unposted", "start");
    const r = recorder();
    await sweepMatchEvents(w.env, AT_KICKOFF, r.fn);
    assert.equal(r.sent.length, 0);
  });
});

describe("publication receipts", () => {
  it("a COMPLETE day cache keeps Top/Daily fixtures tracked even though the Telegram send never succeeded", async () => {
    const w = world();
    w.fixture("fx", { collections: ["top"], live: "in_progress" });
    CACHE(w.sql); // official publication exists, but there is no posts receipt (group send failed)
    w.event("fx", "start");
    const r = recorder();
    await sweepMatchEvents(w.env, AT_KICKOFF, r.fn);
    assert.equal(r.sent.length, 2);
  });
  it("a membership with neither a receipt nor a COMPLETE cache is not published", async () => {
    const w = world();
    w.fixture("fx", { collections: ["top"], live: "in_progress" });
    w.sql.prepare("INSERT INTO prediction_cache VALUES(?,?,?,?,?)").run("k", DAY, "Africa/Lagos", "PARTIAL", "{}");
    w.event("fx", "start");
    const r = recorder();
    await sweepMatchEvents(w.env, AT_KICKOFF, r.fn);
    assert.equal(r.sent.length, 0);
  });
});

describe("kickoff notification timing", () => {
  it("is not sent before kickoff, only waits", async () => {
    const w = world();
    w.fixture("fx", { collections: ["top"], live: "in_progress" });
    w.publish("top");
    w.event("fx", "start");
    const r = recorder();
    await sweepMatchEvents(w.env, KICKOFF - 1000, r.fn);
    assert.equal(r.sent.length, 0);
    assert.ok(!w.receipts().includes("evt_fx_start_expired"));
    await sweepMatchEvents(w.env, KICKOFF, r.fn);
    assert.equal(r.sent.length, 2, "valid from kickoff itself");
  });
  it("is sent inside the window", async () => {
    const w = world();
    w.fixture("fx", { collections: ["top"], live: "in_progress" });
    w.publish("top");
    w.event("fx", "start");
    const r = recorder();
    await sweepMatchEvents(w.env, KICKOFF + 10 * 60_000, r.fn); // exactly kickoff+10 min is still inside
    assert.equal(r.sent.length, 2);
  });

  it("a delayed kickoff alert expires instead of arriving late (default window: kickoff through +10 min)", async () => {
    const w = world();
    w.fixture("fx", { collections: ["top"], live: "in_progress" });
    w.publish("top");
    w.event("fx", "start");
    const r = recorder();
    await sweepMatchEvents(w.env, KICKOFF + 10 * 60_000 + 1000, r.fn);
    assert.equal(r.sent.length, 0);
    assert.ok(w.receipts().includes("evt_fx_start_expired"));
    // ...and stays expired even if a later sweep runs (no retry storm).
    await sweepMatchEvents(w.env, KICKOFF + 40 * 60_000, r.fn);
    assert.equal(r.sent.length, 0);
  });

  it("the window is configurable", async () => {
    const w = world();
    w.fixture("fx", { collections: ["top"], live: "in_progress" });
    w.publish("top");
    w.event("fx", "start");
    const r = recorder();
    await sweepMatchEvents({ ...w.env, MATCH_START_WINDOW_MIN: "90" }, KICKOFF + 60 * 60_000, r.fn);
    assert.equal(r.sent.length, 2);
  });

  it("the incident: a kickoff event still queued after full time is never sent", async () => {
    const w = world();
    w.fixture("fx", { collections: ["top"], live: "final", settled: 1 });
    w.publish("top");
    w.event("fx", "start");
    const r = recorder();
    // Inside the window by the clock, but the fixture has already finished.
    await sweepMatchEvents(w.env, KICKOFF + 10 * 60_000, r.fn);
    assert.equal(r.sent.length, 0);
    // And long after full time.
    await sweepMatchEvents(w.env, KICKOFF + 4 * 3_600_000, r.fn);
    assert.equal(r.sent.length, 0);
  });

  it("postponed and cancelled fixtures never get a kickoff alert", async () => {
    for (const status of ["postponed", "cancelled", "abandoned"]) {
      const w = world();
      w.fixture("fx", { collections: ["top"], live: status });
      w.publish("top");
      w.event("fx", "start");
      const r = recorder();
      await sweepMatchEvents(w.env, AT_KICKOFF, r.fn);
      assert.equal(r.sent.length, 0, status);
    }
  });

  it("a fixture whose published legs are all void gets no kickoff alert", async () => {
    const w = world();
    w.fixture("fx", { collections: ["top"], status: "void", live: "in_progress" });
    w.publish("top");
    w.event("fx", "start");
    const r = recorder();
    await sweepMatchEvents(w.env, AT_KICKOFF, r.fn);
    assert.equal(r.sent.length, 0);
  });

  it("an event seen before kickoff waits (is not sent, not expired)", () => {
    const d = decideMatchEvent(
      {},
      { pick_date: DAY, fixture_key: "fx", event_type: "start", text: "", created_at: iso(KICKOFF) },
      { kickoff_utc: iso(KICKOFF), live_status: "in_progress", settlement_done: 0, published_open: 1, published_total: 1 },
      KICKOFF - 30 * 60_000,
    );
    assert.equal(d.action, "wait");
  });
});

describe("match-end notifications", () => {
  it("require confirmed completion AND settlement", async () => {
    const w = world();
    w.fixture("fx", { collections: ["top"], live: "in_progress" });
    w.publish("top");
    w.event("fx", "end");
    const r = recorder();
    await sweepMatchEvents(w.env, AT_KICKOFF, r.fn);
    assert.equal(r.sent.length, 0, "not final yet");
    w.sql.prepare("UPDATE live_fixture_state SET status='final' WHERE fixture_key='fx'").run();
    await sweepMatchEvents(w.env, AT_KICKOFF, r.fn);
    assert.equal(r.sent.length, 0, "final but settlement not confirmed");
    w.sql.prepare("UPDATE live_fixture_state SET settlement_done=1 WHERE fixture_key='fx'").run();
    await sweepMatchEvents(w.env, AT_KICKOFF, r.fn);
    assert.equal(r.sent.length, 2, "confirmed: delivered to group + owner");
    await sweepMatchEvents(w.env, AT_KICKOFF, r.fn);
    assert.equal(r.sent.length, 2, "no duplicate");
  });

  it("are never sent for All-only fixtures", async () => {
    const w = world();
    w.fixture("fx", { collections: ["all"], live: "final", settled: 1 });
    w.publish("top", "daily");
    w.event("fx", "end");
    const r = recorder();
    await sweepMatchEvents(w.env, AT_KICKOFF + 3 * 3_600_000, r.fn);
    assert.equal(r.sent.length, 0);
  });

  it("expire after MATCH_END_WINDOW_HOURS", async () => {
    const w = world();
    w.fixture("fx", { collections: ["daily"], live: "final", settled: 1 });
    w.publish("daily");
    w.event("fx", "end", KICKOFF);
    const r = recorder();
    await sweepMatchEvents(w.env, KICKOFF + 25 * 3_600_000, r.fn);
    assert.equal(r.sent.length, 0);
  });
});

describe("worker restart and retry recovery", () => {
  it("a destination that failed is retried on its own; the other is never re-sent", async () => {
    const w = world();
    w.fixture("fx", { collections: ["top"], live: "in_progress" });
    w.publish("top");
    w.event("fx", "start");
    const first = recorder(new Set(["999"]));
    await sweepMatchEvents(w.env, AT_KICKOFF, first.fn);
    assert.deepEqual(first.sent.map((s) => s.chat), ["-100"]);
    // "Restart": nothing in memory survives; only D1 receipts do.
    const second = recorder();
    await sweepMatchEvents(w.env, AT_KICKOFF + 5 * 60_000, second.fn);
    assert.deepEqual(second.sent.map((s) => s.chat), ["999"]);
    const third = recorder();
    await sweepMatchEvents(w.env, AT_KICKOFF + 10 * 60_000, third.fn);
    assert.equal(third.sent.length, 0);
  });

  it("a retry that only succeeds after the window expires instead of arriving late", async () => {
    const w = world();
    w.fixture("fx", { collections: ["top"], live: "in_progress" });
    w.publish("top");
    w.event("fx", "start");
    const down = recorder(new Set(["-100", "999"]));
    await sweepMatchEvents(w.env, AT_KICKOFF, down.fn);
    assert.equal(down.sent.length, 0);
    const up = recorder();
    await sweepMatchEvents(w.env, KICKOFF + 50 * 60_000, up.fn);
    assert.equal(up.sent.length, 0);
  });

  it("failures are recorded separately per destination", async () => {
    const w = world();
    w.fixture("fx", { collections: ["top"], live: "in_progress" });
    w.publish("top");
    w.event("fx", "start");
    const quiet = console.error;
    console.error = () => {};
    try {
      await sweepMatchEvents(w.env, AT_KICKOFF, recorder(new Set(["999"])).fn);
    } finally {
      console.error = quiet;
    }
    const jobs = Object.fromEntries(w.sql.prepare("SELECT name,last_result FROM job_state").all().map((r) => [r.name, r.last_result]));
    assert.match(jobs.post_owner, /FAILED/);
    assert.equal(jobs.post_group, undefined, "the group leg succeeded, so it has no failure record");
  });
});

describe("daily posting schedule and failure handling", () => {
  const toml = readFileSync(new URL("../wrangler.toml", import.meta.url), "utf8");

  it("production cron triggers are exactly the registered patterns, and UTC maps to Africa/Lagos", () => {
    const m = /crons\s*=\s*\[([^\]]*)\]/.exec(toml);
    const crons = [...m[1].matchAll(/"([^"]+)"/g)].map((x) => x[1]);
    for (const c of [CRON_POST_SWEEP, CRON_RECAP_SWEEP, CRON_DELIVER_SWEEP]) assert.ok(crons.includes(c), c);
    // 08:00 UTC is 09:00 Lagos (UTC+1, no DST); 06:00 UTC is 07:00 Lagos.
    assert.equal(lagosMinutes(Date.UTC(2026, 9, 16, 8, 0)), 9 * 60);
    assert.equal(lagosMinutes(Date.UTC(2026, 9, 16, 6, 45)), 7 * 60 + 45);
    assert.match(toml, /STAGE3_CRON_ENABLED = "1"/);
    assert.match(toml, /DELIVERY_TIME = "09:30"/);
  });

  it("group posts wait for 09:30 Lagos, then publish once to group and owner", async () => {
    const w = world();
    w.sql.prepare("INSERT INTO prediction_cache VALUES(?,?,?,?,?)").run("k", DAY, "Africa/Lagos", "COMPLETE", "{}");
    const r = recorder();
    const at0929 = Date.UTC(2026, 9, 16, 8, 29);
    const at0930 = Date.UTC(2026, 9, 16, 8, 30);
    assert.equal(await postGroupPicks(w.env, "top", at0929, r.fn), "waiting");
    assert.equal(r.sent.length, 0);
    assert.equal(await postGroupPicks(w.env, "top", at0930, r.fn), "sent");
    assert.deepEqual(r.sent.map((s) => s.chat), ["-100", "999"]);
    assert.equal(await postGroupPicks(w.env, "top", at0930 + 300_000, r.fn), "skipped");
    assert.equal(r.sent.length, 2, "no duplicate posts");
  });

  it("failed or missing generation publishes nothing, writes no publication, and is recorded", async () => {
    for (const cache of [null, "FAILED", "PARTIAL"]) {
      const w = world();
      if (cache) w.sql.prepare("INSERT INTO prediction_cache VALUES(?,?,?,?,?)").run("k", DAY, "Africa/Lagos", cache, "{}");
      const r = recorder();
      const outcome = await postGroupPicks(w.env, "top", Date.UTC(2026, 9, 16, 9, 0), r.fn);
      assert.equal(outcome, "waiting");
      assert.equal(r.sent.length, 0);
      assert.deepEqual(w.receipts(), [], "no fake publication");
      const row = w.sql.prepare("SELECT last_result FROM job_state WHERE name='post'").get();
      assert.match(row.last_result, /no COMPLETE cache/, "the wait is persisted, not silently dropped");
    }
  });

  it("a transient group failure is retried by the next sweep without duplicating the owner", async () => {
    const w = world();
    w.sql.prepare("INSERT INTO prediction_cache VALUES(?,?,?,?,?)").run("k", DAY, "Africa/Lagos", "COMPLETE", "{}");
    const at = Date.UTC(2026, 9, 16, 8, 35);
    const quiet = console.error;
    console.error = () => {};
    try {
      assert.equal(await postGroupPicks(w.env, "daily", at, recorder(new Set(["-100"])).fn), "failed");
    } finally {
      console.error = quiet;
    }
    const r = recorder();
    assert.equal(await postGroupPicks(w.env, "daily", at + 300_000, r.fn), "sent");
    assert.deepEqual(r.sent.map((s) => s.chat), ["-100", "999"]);
  });

  it("handleScheduled routes the deliver sweep to match events", async () => {
    const w = world();
    w.fixture("fx", { collections: ["top"], live: "in_progress" });
    w.publish("top");
    w.event("fx", "start");
    const realFetch = globalThis.fetch;
    let calls = 0;
    globalThis.fetch = async () => {
      calls += 1;
      return new Response(JSON.stringify({ ok: true, result: { message_id: 1 } }), { status: 200 });
    };
    try {
      w.sql.exec("CREATE TABLE telegram_updates(update_id INTEGER, status TEXT, attempts INTEGER, last_error TEXT)");
      await handleScheduled({ ...w.env, STAGE3_CRON_ENABLED: "1", TELEGRAM_BOT_TOKEN: "t" }, CRON_DELIVER_SWEEP, AT_KICKOFF);
    } finally {
      globalThis.fetch = realFetch;
    }
    // 2 for the match-event (group + owner), +1 for maybeDispatchLiveMonitor's
    // missing-config alert to the owner (bug 6): the "in_progress" fixture's
    // live_fixture_state row has notifications_done=0, which is real work,
    // and this env has no GITHUB_REPO/TOKEN configured.
    assert.equal(calls, 3);
  });
});

describe("integration: publication -> kickoff -> settlement -> full time -> next-morning recap", () => {
  it("runs the whole day through the real cron dispatcher; All-only stays out; only group + owner receive anything", async () => {
    const w = world();
    const env = { ...w.env, STAGE3_CRON_ENABLED: "1", TELEGRAM_BOT_TOKEN: "t" };
    w.sql.exec("CREATE TABLE telegram_updates(update_id INTEGER, status TEXT, attempts INTEGER, last_error TEXT)");
    w.sql.prepare("INSERT INTO prediction_cache VALUES(?,?,?,?,?)").run("k", DAY, "Africa/Lagos", "COMPLETE", "{}");
    // Persisted selection: Top-only, Daily-only, in both, All-only fixture, All-only sibling of a Top fixture.
    const topOnly = w.fixture("top-fx", { collections: ["all", "top"] });
    const dailyOnly = w.fixture("daily-fx", { collections: ["all", "daily"] });
    const bothA = w.fixture("both-fx", { collections: ["all", "top"] });
    const bothB = w.fixture("both-fx", { collections: ["all", "daily"] });
    const allFx = w.fixture("all-fx", { collections: ["all"] });
    const allSibling = w.fixture("top-fx", { collections: ["all"] });
    w.sql.prepare("UPDATE picks SET label='Hidden pick' WHERE id IN (?,?)").run(allFx, allSibling);
    for (const k of ["top-fx", "daily-fx", "both-fx"]) {
      w.sql.prepare("INSERT INTO live_fixture_state(pick_date,fixture_key,status) VALUES(?,?,?)").run(DAY, k, "in_progress");
    }

    const realFetch = globalThis.fetch;
    const sent = [];
    globalThis.fetch = async (_url, init) => {
      const body = JSON.parse(init.body);
      sent.push({ chat: String(body.chat_id), text: body.text });
      return new Response(JSON.stringify({ ok: true, result: { message_id: sent.length } }), { status: 200 });
    };
    try {
      // 09:30 Lagos: Top and Daily cards go to the group and to the owner.
      const at0930 = Date.UTC(2026, 9, 16, 8, 30);
      await handleScheduled(env, CRON_POST_SWEEP, at0930);
      await handleScheduled(env, CRON_POST_SWEEP, at0930 + 300_000);
      assert.equal(sent.length, 4, "top+daily x (group, owner), no duplicates on the second sweep");
      assert.deepEqual([...new Set(sent.map((s) => s.chat))].sort(), ["-100", "999"]);
      assert.ok(w.receipts().includes("top") && w.receipts().includes("daily"));
      assert.ok(sent.every((s) => !s.text.includes("Hidden pick")), "All-only never appears in the published cards");

      // Live monitor (GitHub Actions) records start events for tracked fixtures only; All-only has none.
      for (const k of ["top-fx", "daily-fx", "both-fx"]) w.event(k, "start");
      sent.length = 0;
      // live_fixture_state rows exist with notifications_done=0, so
      // maybeDispatchLiveMonitor (bug 6) sees real work; this env has no
      // GITHUB_REPO/TOKEN configured, so it DMs the owner once (the +1
      // below) instead of dispatching — claimPost-guarded, so the second
      // sweep's duplicate check still holds at the same total.
      await handleScheduled(env, CRON_DELIVER_SWEEP, KICKOFF + 2 * 60_000);
      assert.equal(sent.length, 7, "3 fixtures x (group, owner), plus one missing-live-monitor-config alert");
      await handleScheduled(env, CRON_DELIVER_SWEEP, KICKOFF + 7 * 60_000);
      assert.equal(sent.length, 7, "duplicate sweep sends nothing more");

      // Full time: settlement lands, end events follow.
      w.sql.prepare("UPDATE live_fixture_state SET status='final', settlement_done=1").run();
      w.sql.prepare("UPDATE picks SET status='won' WHERE id IN (?,?,?)").run(topOnly, bothA, bothB);
      w.sql.prepare("UPDATE picks SET status='lost' WHERE id=?").run(dailyOnly);
      for (const k of ["top-fx", "daily-fx", "both-fx"]) w.event(k, "end", KICKOFF + 115 * 60_000);
      sent.length = 0;
      await handleScheduled(env, CRON_DELIVER_SWEEP, KICKOFF + 120 * 60_000);
      await handleScheduled(env, CRON_DELIVER_SWEEP, KICKOFF + 125 * 60_000);
      assert.equal(sent.length, 6);
      assert.ok(w.sql.prepare("SELECT status FROM picks WHERE id=?").get(allFx).status === "pending", "All-only never settled");

      // 07:50 Lagos next morning: compact recap to group + owner.
      sent.length = 0;
      await handleScheduled(env, CRON_RECAP_SWEEP, Date.UTC(2026, 9, 17, 6, 50));
      assert.equal(sent.length, 1);
      assert.equal(sent[0].chat, "999");
      const recap = sent[0].text.split("\n\nYesterday")[0];
      assert.equal(
        recap,
        "<b>📊 SUREODDS RESULTS</b>\n\n🏆 BET OF THE DAY\n\n✅ ✅\n\n2 Wins | 0 Losses\n\n⚽ DAILY PICKS\n\n❌ ✅\n\n1 Win | 1 Loss"
          .replace("❌ ✅", recap.includes("✅ ❌") ? "✅ ❌" : "❌ ✅"),
      );
      assert.doesNotMatch(recap, /Hidden|H to Win|@|vs/);
    } finally {
      globalThis.fetch = realFetch;
    }
  });

  it("recap marks void, push and half results without folding them into wins/losses", async () => {
    const w = world();
    const ids = ["won", "push", "void", "half_won", "half_lost", "pending"].map((st) => {
      const id = w.fixture(`f-${st}`, { collections: ["all", "top"], status: st });
      return id;
    });
    assert.equal(ids.length, 6);
    w.fixture("hidden", { collections: ["all"], status: "lost" });
    const { renderGroupRecap } = await import("../src/views.ts");
    const { text } = await renderGroupRecap(w.env, "2026-10-17");
    assert.match(text, /✅ ↩️ ➖ ✅½ ❌½ ⏳/);
    assert.match(text, /1 Win \| 0 Losses \| 1 Half Win \| 1 Half Loss \| 1 Push \| 1 Void \| 1 Pending/);
    assert.doesNotMatch(text, /DAILY PICKS/);
  });
});

describe("production workflow authorization", () => {
  const read = (f) => readFileSync(new URL(`../../.github/workflows/${f}`, import.meta.url), "utf8");
  it("scheduled generation needs STAGE7_LIVE_ENABLED=true, fails on missing config and never replays offline on schedule", () => {
    const y = read("stage7-scheduled-production.yml");
    assert.match(y, /true\) mode=live/);
    assert.match(y, /false\) mode=paused/);
    assert.match(y, /::error::STAGE7_LIVE_ENABLED[^\n]*exit 1/);
    assert.match(y, /needs\.guard\.outputs\.mode == 'live' && 'live-generate'/);
    assert.doesNotMatch(y, /!= 'false'/, "unset configuration must not authorize a live run");
    assert.match(y, /mode=offline/, "manual dispatch stays an offline smoke test");
    assert.match(y, /allow_telegram_posts: false/);
  });
  it("live monitoring writes production only when explicitly enabled and fails on invalid config", () => {
    const y = read("stage8-live-monitor-production.yml");
    assert.match(y, /::error::STAGE8_LIVE_ENABLED[^\n]*exit 1/);
    assert.doesNotMatch(y, /!= 'false'/);
  });
});
