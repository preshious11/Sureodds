import { DatabaseSync } from 'node:sqlite';
import { readFileSync } from 'node:fs';
import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { maybeDispatchGeneration, maybeDispatchSettlement, dispatchWorkflow, dailyHealthReport } from '../src/cron.ts';

const NOW = Date.UTC(2026, 9, 18, 8, 20); // 09:20 Lagos
const DAY = '2026-10-18';
const iso = ms => new Date(ms).toISOString();
function world() {
  const sql = new DatabaseSync(':memory:');
  for (const file of ['0001_foundation.sql','0003_engine_state.sql','0005_ticket_candidates.sql','0011_pipeline_dispatch.sql']) {
    sql.exec(readFileSync(new URL(`../migrations/${file}`, import.meta.url), 'utf8'));
  }
  const DB = { prepare(query) { return {
    args: [], bind(...args) { this.args = args; return this; },
    async first() { return sql.prepare(query).get(...this.args) ?? null; },
    async all() { return { results: sql.prepare(query).all(...this.args) }; },
    async run() { return { meta: { changes: sql.prepare(query).run(...this.args).changes } }; },
  }; } };
  const env = { DB, TELEGRAM_ADMIN_IDS: '999,888', GITHUB_REPO:'owner/repo', GITHUB_DISPATCH_TOKEN: 'private-token' };
  const requests = [], sent = [];
  const fetcher = async (url, init) => { requests.push({url, body:JSON.parse(init.body)}); return {ok:true, status:204}; };
  const sender = async (_env, _method, body) => { sent.push(body); return {ok:true, messageId:123}; };
  return {sql, env, requests, sent, fetcher, sender};
}
function cache(sql, payload = {}, status = 'COMPLETE') {
  sql.prepare(`INSERT INTO prediction_cache (cache_key,local_date,timezone,status,payload_json,generated_at,updated_at,expires_at)
    VALUES ('k',?,'Africa/Lagos',?,?,?, ?,?)`).run(DAY,status,JSON.stringify(payload),iso(NOW),iso(NOW),iso(NOW+86400000));
}

describe('generation self-healing', () => {
  it('dispatches the live production inputs and reserves durable daily count', async () => {
    const w=world();
    await maybeDispatchGeneration(w.env,NOW,w.fetcher,w.sender);
    assert.equal(w.requests.length,1);
    assert.match(w.requests[0].url,/stage5-acceptance-production.yml/);
    assert.deepEqual(w.requests[0].body,{ref:'main',inputs:{operation:'live-generate',dry_run:'false',live_confirm:'RUN-LIVE',
      allow_telegram_posts:'false',write_budget:'40000',destructive_budget:'2000'}});
    const row=w.sql.prepare("SELECT * FROM job_state WHERE name='generation_dispatch'").get();
    assert.equal(row.attempts,1); assert.equal(row.due_at,DAY); assert.equal(row.last_result,'OK dispatched');
  });
  it('checks time boundaries, COMPLETE cache, active leases and the exact interval', async () => {
    const w=world();
    await maybeDispatchGeneration(w.env,NOW-1,w.fetcher,w.sender);
    await maybeDispatchGeneration(w.env,Date.UTC(2026,9,18,11),w.fetcher,w.sender);
    assert.equal(w.requests.length,0);
    w.sql.prepare("UPDATE job_state SET lease_owner='busy',lease_until=? WHERE name='generate'").run(iso(NOW+60000));
    await maybeDispatchGeneration(w.env,NOW,w.fetcher,w.sender); assert.equal(w.requests.length,0);
    await maybeDispatchGeneration(w.env,NOW+60001,w.fetcher,w.sender); assert.equal(w.requests.length,1);
    await maybeDispatchGeneration(w.env,NOW+30*60000,w.fetcher,w.sender); assert.equal(w.requests.length,1);
    await maybeDispatchGeneration(w.env,NOW+31*60000+1,w.fetcher,w.sender); assert.equal(w.requests.length,2);
    cache(w.sql);
    await maybeDispatchGeneration(w.env,NOW+70*60000,w.fetcher,w.sender); assert.equal(w.requests.length,2);
  });
  it('bounds concurrent attempts, respects the daily cap, and alerts once even if Telegram fails', async () => {
    const w=world(); w.env.GENERATION_MAX_DISPATCHES_PER_DAY='2';
    await Promise.all([maybeDispatchGeneration(w.env,NOW,w.fetcher,w.sender),maybeDispatchGeneration(w.env,NOW,w.fetcher,w.sender)]);
    assert.equal(w.requests.length,1);
    await maybeDispatchGeneration(w.env,NOW+30*60000,w.fetcher,w.sender);
    for (const offset of [35,40,45]) await maybeDispatchGeneration(w.env,NOW+offset*60000,w.fetcher,w.sender);
    assert.equal(w.requests.length,2); assert.equal(w.sent.length,1); assert.match(w.sent[0].text,/limit reached/);
    assert.equal(w.sent[0].parse_mode,undefined);
    await maybeDispatchGeneration(w.env,NOW+86400000,w.fetcher,w.sender);
    assert.equal(w.requests.length,3);
    assert.equal(w.sql.prepare("SELECT attempts FROM job_state WHERE name='generation_dispatch'").get().attempts,1);
  });
  it('counts failures against the cap and still publishes no fake cache', async () => {
    const w=world();
    const failed=async()=>{throw Error('secret private-token');};
    await maybeDispatchGeneration(w.env,NOW,failed,w.sender);
    await maybeDispatchGeneration(w.env,NOW+60000,failed,w.sender);
    assert.equal(w.sql.prepare("SELECT attempts FROM job_state WHERE name='generation_dispatch'").get().attempts,1);
    assert.equal(w.sql.prepare('SELECT COUNT(*) n FROM prediction_cache').get().n,0);
    assert.ok(!w.sent[0].text.includes('private-token'));
  });
});

describe('settlement recovery',()=>{
  const start=Date.UTC(2026,9,18,5,15);
  it('dispatches once between 06:15 and 07:30 Lagos, guarded even when overlapping',async()=>{
    const w=world();
    await maybeDispatchSettlement(w.env,start-1,w.fetcher,w.sender);
    await maybeDispatchSettlement(w.env,Date.UTC(2026,9,18,6,30),w.fetcher,w.sender);
    assert.equal(w.requests.length,0);
    await Promise.all([maybeDispatchSettlement(w.env,start,w.fetcher,w.sender),maybeDispatchSettlement(w.env,start,w.fetcher,w.sender)]);
    assert.equal(w.requests.length,1); assert.match(w.requests[0].url,/stage9-settle-production/);
    assert.deepEqual(w.requests[0].body.inputs,{dry_run:'false'});
  });
  for(const [result,when,skip] of [['OK run 1 mode=live',iso(start),true],['OK run 1 mode=dry',iso(start),false],
    ['FAILED run 1 mode=live',iso(start),false],['OK run 1 mode=live',iso(start-86400000),false]]) {
    it(`success guard: ${result}, ${when}`,async()=>{
      const w=world();w.sql.prepare("UPDATE job_state SET last_result=?,last_run_at=? WHERE name='settle'").run(result,when);
      await maybeDispatchSettlement(w.env,start,w.fetcher,w.sender);
      assert.equal(w.requests.length,skip?0:1);
    });
  }
  it('does not repeatedly dispatch after rejection or Telegram errors',async()=>{
    const w=world();let calls=0;
    const reject=async()=>{calls++;return {ok:false,status:403};};
    const brokenSender=async()=>{throw Error('offline');};
    await maybeDispatchSettlement(w.env,start,reject,brokenSender);
    await maybeDispatchSettlement(w.env,start+60000,reject,brokenSender);
    assert.equal(calls,1);
  });
});

describe('dispatch failure alerts',()=>{
  for(const workflow of ['stage8-live-monitor-production.yml','stage5-acceptance-production.yml','stage9-settle-production.yml']) {
    for(const status of [401,403,500,'network']) it(`${workflow}: ${status}, once per day/workflow`,async()=>{
      const w=world(); const bad=async()=>{if(status==='network')throw Error('private-token');return {ok:false,status};};
      for(let i=0;i<2;i++) await dispatchWorkflow(w.env,workflow,'dispatch_test',{},NOW+i*60000,bad,w.sender);
      assert.equal(w.sent.length,1);assert.match(w.sent[0].text,/HTTP/);
      if(status===401||status===403) assert.match(w.sent[0].text,/GITHUB_DISPATCH_TOKEN.*may have expired/);
      assert.ok(!w.sent[0].text.includes('private-token'));
      await dispatchWorkflow(w.env,workflow,'dispatch_test',{},NOW+86400000,bad,w.sender);
      assert.equal(w.sent.length,2);
    });
  }
});

function healthy(w, providers) {
  for (const kind of ['top','daily','recap']) w.sql.prepare("INSERT INTO posts (kind,post_date,message_id,created_at) VALUES (?,?,'1',?)").run(kind,DAY,iso(NOW));
  w.sql.prepare("UPDATE job_state SET last_result='OK run 1 mode=live',last_run_at=? WHERE name='settle'").run(iso(NOW));
  cache(w.sql,{generation:{providers:providers??Object.fromEntries(['sharpapi','oddsapi','sportsgameodds'].map(n=>[n,{status:'complete'}])),
    api_usage:{sharpapi:{today:79,limit:100}}}});
}
describe('daily owner health report and provider coverage',()=>{
  it('sends one plain-text all-good report to the first owner with receipts, usage and row counts',async()=>{
    const w=world();healthy(w);
    await dailyHealthReport(w.env,NOW,w.sender); await dailyHealthReport(w.env,NOW,w.sender);
    assert.equal(w.sent.length,1);const msg=w.sent[0];
    assert.equal(msg.chat_id,'999');assert.equal(msg.parse_mode,undefined);assert.match(msg.text,/^\u2705 All good/);
    assert.match(msg.text,/sharpapi: complete; today 79\/100/);
    assert.match(msg.text,/sportsgameodds: complete/);assert.match(msg.text,/Rows:.*picks=0/);
    assert.match(msg.text,/Generation dispatches: 0\/5/); assert.match(msg.text,/Top .*Daily/);
  });
  it('flags provider degradation, >=80% limits, pending legs and failed settlement',async()=>{
    const w=world();healthy(w);
    const payload={generation:{providers:{sharpapi:{status:'partial',requests:{spent:48,budget:60}},
      oddsapi:{status:'complete',quota:{requests_used:400,requests_remaining:100}},
      sportsgameodds:{status:'complete',quota:{monthly_entities_used:80,monthly_entities_max:100}}},
      provider_coverage:{fallback_used:true},api_usage:{oddsapi:{today:80,limit:100}}}};
    w.sql.prepare('UPDATE prediction_cache SET payload_json=?').run(JSON.stringify(payload));
    w.sql.prepare("UPDATE job_state SET last_result='FAILED' WHERE name='settle'").run();
    w.sql.prepare(`INSERT INTO picks(pick_date,tier,rank,provider,provider_event_id,sport,home,away,start_utc,market,period,stat,entity,selection,label,price,prob,ev,status,created_at)
      VALUES ('2026-10-17','top',1,'p','e','football','H','A',?,'result3','full','points','all','home','H',1.5,.7,.1,'pending',?)`).run(iso(NOW),iso(NOW));
    w.sql.prepare("INSERT INTO prediction_memberships VALUES(1,'top',1)").run();
    w.sql.prepare("INSERT INTO prediction_memberships VALUES(1,'daily',1)").run();
    await dailyHealthReport(w.env,NOW,w.sender);const text=w.sent[0].text;
    assert.match(text,/^\u26a0\ufe0f Needs attention:/);
    for(const name of ['sharpapi','oddsapi','sportsgameodds']) assert.match(text,new RegExp(`${name} .*usage >=80%`));
    assert.match(text,/sharpapi partial/);assert.match(text,/yesterday pending=1/);assert.match(text,/fallback=true/);
  });
  it('reports missing cache/providers honestly, including generation failure and no recorded quotas',async()=>{
    const w=world();w.sql.prepare("UPDATE job_state SET last_result='FAILED engine' WHERE name='generate'").run();
    await dailyHealthReport(w.env,NOW,w.sender);
    assert.match(w.sent[0].text,/FAILED engine/);assert.match(w.sent[0].text,/sharpapi: not reported/);
    assert.match(w.sent[0].text,/usage not recorded/);
  });
});
