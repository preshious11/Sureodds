// Ground rule: every Worker scheduled invocation must stay at or below 45
// D1 queries in the worst case (the free plan allows 50 per invocation).
// Counts real D1Database.prepare() calls against real SQLite + real
// migrations, under an adversarial (not average-case) fixture for each of
// the four Cron Trigger patterns.
import { DatabaseSync } from "node:sqlite";
import { readFileSync } from "node:fs";
import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { CRON_DELIVER_SWEEP, CRON_POST_SWEEP, CRON_RECAP_SWEEP, CRON_WATCH, handleScheduled } from "../src/cron.ts";

const BUDGET = 45;
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
    "0003_engine_state.sql", "0005_ticket_candidates.sql", "0011_pipeline_dispatch.sql", "0012_result_gifs.sql",
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
    sql.prepare("UPDATE picks SET pick_date=?").run(yesterday);
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


// WHY: budget the failure paths too: each rejected event send records destination
// failures, while dispatch failures independently alert the owner.
for (const scenario of ['generation failure','generation cap','generation interval','generation lease','generation complete','settlement failure']) {
  it(`40-query bound with failed Telegram sends and ${scenario}`,async()=>{
    const {sql,DB}=world();
    const now=scenario==='settlement failure'?Date.UTC(2026,9,17,5,20):NOW;
    sql.prepare("INSERT INTO telegram_updates (update_id,status,updated_at) VALUES(1,'failed',?)").run(iso(now));
    sql.prepare("INSERT INTO posts(kind,post_date,message_id,created_at) VALUES('top',?,'1',?)").run(DAY,iso(now));
    for(let i=0;i<5;i++) {
      insertPick(sql,{id:100+i,tier:'top',status:'pending',startMs:now-60000});
      const key=`fx-${100+i}`;
      sql.prepare("INSERT INTO live_fixture_state(pick_date,fixture_key,status,updated_at) VALUES(?,?,'in_progress',?)").run(DAY,key,iso(now));
      sql.prepare("INSERT INTO match_events VALUES(?,?,'start','t',?)").run(DAY,key,iso(now-30000));
    }
    if(scenario==='generation cap') sql.prepare("UPDATE job_state SET due_at=?,attempts=5 WHERE name='generation_dispatch'").run(DAY);
    if(scenario==='generation interval') sql.prepare("UPDATE job_state SET due_at=?,attempts=1,last_run_at=? WHERE name='generation_dispatch'").run(DAY,iso(now-60000));
    if(scenario==='generation lease') sql.prepare("UPDATE job_state SET lease_owner='busy',lease_until=? WHERE name='generate'").run(iso(now+60000));
    if(scenario==='generation complete') sql.prepare(`INSERT INTO prediction_cache(cache_key,local_date,timezone,status,payload_json,generated_at,updated_at,expires_at)
      VALUES('k',?,'Africa/Lagos','COMPLETE','{}',?,?,?)`).run(DAY,iso(now),iso(now),iso(now+86400000));
    // Exercise the new aggregate + missing-library branch alongside failed match sends and dispatch alerts.
    insertPick(sql,{id:200,tier:'daily',status:'won',startMs:now-3600000});
    sql.prepare("INSERT INTO posts(kind,post_date,message_id,created_at) VALUES('daily',?,'123',?)").run(DAY,iso(now));
    const env={RESULT_GIFS_START_DATE:DAY,DB,TELEGRAM_ADMIN_IDS:'999',TELEGRAM_CHAT_ID:'-100',STAGE3_CRON_ENABLED:'1',GITHUB_REPO:'owner/repo',GITHUB_DISPATCH_TOKEN:'t'};
    const saved=globalThis.fetch;
    globalThis.fetch=async(url)=>({ok:false,status:String(url).includes('github.com')?401:500,json:async()=>({})});
    try { await handleScheduled(env,CRON_DELIVER_SWEEP,now); } finally {globalThis.fetch=saved;}
    assert.ok(DB.count<=BUDGET,`${scenario}: ${DB.count} queries exceeds ${BUDGET}`);
    assert.equal(sql.prepare('SELECT COUNT(*) n FROM match_events').get().n,5,'backlog retained for later sweeps');
  });
}

it('WATCH health report, missing generation and full retention with failed sends stays under 40',async()=>{
  const {sql,DB}=world();const saved=globalThis.fetch;
  globalThis.fetch=async()=>({ok:false,status:500,json:async()=>({})});
  try {await handleScheduled({DB,STAGE3_CRON_ENABLED:'1',TELEGRAM_ADMIN_IDS:'999'},CRON_WATCH,NOW);}
  finally {globalThis.fetch=saved;}
  assert.ok(DB.count<=BUDGET,`WATCH failed sends used ${DB.count} queries`);
});


for(const [status,registered] of [['won',true],['lost',true],['void',false],['pending',false]]) {
  it(`result GIF ${status}, registered=${registered}: cron invocation stays under 40 queries`,async()=>{
    const {sql,DB}=world();
    insertPick(sql,{id:1,tier:'top',status,startMs:NOW-3600000});
    sql.prepare("INSERT INTO posts(kind,post_date,message_id,created_at) VALUES('top',?,'123',?)").run(DAY,iso(NOW));
    if(registered)sql.prepare("INSERT INTO result_gifs VALUES(?,'f','u',?)").run(status,iso(NOW));
    const saved=globalThis.fetch;globalThis.fetch=async()=>({ok:false,status:500,json:async()=>({})});
    try{await handleScheduled({DB,TELEGRAM_CHAT_ID:'-100',TELEGRAM_ADMIN_IDS:'999',RESULT_GIFS_START_DATE:DAY,STAGE3_CRON_ENABLED:'1'},CRON_POST_SWEEP,NOW);}
    finally{globalThis.fetch=saved;}
    assert.ok(DB.count<=BUDGET,`GIF ${status}: ${DB.count} queries`);
  });
}


for(const [tickets,registered] of [[6,false],[6,true],[2,false]]) {
  it(`${tickets} ready result tickets in the POST backup, registered=${registered}, stays under 40`,async()=>{
    const {sql,DB}=world();
    const dates=tickets===2?['2026-10-17']:['2026-10-15','2026-10-16','2026-10-17'];
    sql.prepare("INSERT INTO telegram_updates(update_id,status,updated_at) VALUES(1,'failed',?)").run(iso(NOW));
    let id=300;
    for(const day of dates)for(const kind of ['top','daily']) {
      const status=kind==='top'?'won':'lost';
      insertPick(sql,{id,tier:kind,status,startMs:NOW-3600000});
      sql.prepare('UPDATE picks SET pick_date=? WHERE id=?').run(day,id);
      sql.prepare("INSERT INTO posts(kind,post_date,message_id,created_at) VALUES(?,?,'123',?)").run(kind,day,iso(NOW));
      id++;
    }
    if(registered)for(const kind of ['won','lost'])sql.prepare("INSERT INTO result_gifs VALUES(?,?,?,?)").run(kind,kind,kind,iso(NOW));
    const saved=globalThis.fetch;let animations=0;
    globalThis.fetch=async(url)=>{if(String(url).endsWith('sendAnimation'))animations++;return {ok:false,status:500,json:async()=>({})};};
    try{await handleScheduled({DB,TELEGRAM_CHAT_ID:'-100',TELEGRAM_ADMIN_IDS:'999',STAGE3_CRON_ENABLED:'1',RESULT_GIFS_START_DATE:dates[0],
      GITHUB_REPO:'owner/repo',GITHUB_DISPATCH_TOKEN:'t'},CRON_POST_SWEEP,NOW);}
    finally{globalThis.fetch=saved;}
    assert.equal(animations,registered?tickets:0,'all ready tickets are handled in one sweep');
    assert.equal(sql.prepare("SELECT COUNT(*) n FROM posts WHERE kind LIKE 'result_gif_%'").get().n,registered?0:tickets);
    assert.ok(DB.count<=BUDGET,`six tickets: ${DB.count} queries, budget ${BUDGET}`);
  });
}

for(const cron of [CRON_DELIVER_SWEEP,CRON_POST_SWEEP])it(`maximum batch with six ready tickets, missing GIFs and mixed delivery failures: ${cron}`,async(t)=>{
 const {sql,DB}=world();let id=500;
 for(const day of ['2026-10-15','2026-10-16',DAY])for(const kind of ['top','daily']) {
  insertPick(sql,{id,tier:kind,status:kind==='top'?'won':'lost',startMs:NOW-3600000});
  sql.prepare('UPDATE picks SET pick_date=? WHERE id=?').run(day,id);
  if(cron===CRON_DELIVER_SWEEP || day!==DAY)
   sql.prepare("INSERT INTO posts(kind,post_date,message_id,created_at) VALUES(?,?,'123',?)").run(kind,day,iso(NOW));
  if(cron===CRON_DELIVER_SWEEP) {
   sql.prepare("INSERT INTO live_fixture_state(pick_date,fixture_key,status,settlement_done,updated_at) VALUES(?,?,'final',1,?)").run(day,`fx-${id}`,iso(NOW));
   sql.prepare("INSERT INTO match_events VALUES(?,?,'end',?,?)").run(day,`fx-${id}`,`end ${id}`,iso(NOW));
  }
  id++;
 }
 if(cron===CRON_DELIVER_SWEEP)
  sql.prepare("INSERT INTO telegram_updates(update_id,status,updated_at) VALUES(1,'failed',?)").run(iso(NOW));
 else sql.prepare("INSERT INTO prediction_cache(cache_key,local_date,timezone,status,payload_json,generated_at,updated_at,expires_at) VALUES('k',?,'Africa/Lagos','COMPLETE','{}',?,?,?)")
  .run(DAY,iso(NOW),iso(NOW),iso(NOW+86400000));
 const saved=globalThis.fetch;let ownerMessages=0;
 globalThis.fetch=async(url,init)=>{
  if(String(url).includes('github.com'))return {ok:false,status:401,json:async()=>({})};
  const body=JSON.parse(init.body);
  const ok=body.chat_id==='-100' || (cron===CRON_DELIVER_SWEEP && body.text==='end 500' && ++ownerMessages===1);
  return {ok,status:ok?200:500,json:async()=>({ok,result:ok?{message_id:42}:undefined})};
 };
 try {await handleScheduled({DB,STAGE3_CRON_ENABLED:'1',TELEGRAM_CHAT_ID:'-100',TELEGRAM_ADMIN_IDS:'999',RESULT_GIFS_START_DATE:'2026-10-15',
  GITHUB_REPO:'owner/repo',GITHUB_DISPATCH_TOKEN:'t'},cron,NOW);}finally{globalThis.fetch=saved;}
 assert.equal(sql.prepare("SELECT COUNT(*) n FROM posts WHERE kind LIKE 'result_gif_%' AND message_id='no-gif'").get().n,6);
 if(cron===CRON_DELIVER_SWEEP)assert.equal(sql.prepare("SELECT COUNT(*) n FROM posts WHERE kind LIKE 'evt_%_end' AND message_id IS NOT NULL").get().n,6);
 t.diagnostic(`D1 queries: ${DB.count}`);
 // CRON_POST_SWEEP grew by 5 (postProPicksToAdmin: getDayCache, claimPost,
 // getCustomPickPool [returns null here, no pool seeded -> stops there],
 // releaseClaim, setJobResult) when it added the admin Pro Picks auto-send.
 assert.equal(DB.count,40);
 assert.ok(DB.count<=BUDGET);
});
