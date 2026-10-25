// Synthetic private-relay contract tests. MOCK01 is NOT a SportyBet response/code.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { bookingEnabled, bookingButton, eligible, ticket, book, verifiedReply, BOOKING_FAILURE } from '../src/sporty_booking.ts';
import { handleUpdate } from '../src/index.ts';

const now=Date.now();
const row=(id=1)=>({id,canonical_event_id:`test-${id}`,canonical_competition_id:'test-comp',sport:'football',
  home:`Home ${id}`,away:`Away ${id}`,start_utc:new Date(now+3600000+id*1000).toISOString(),
  market:'total',period:'full',stat:'points',entity:'all',selection:'over',line:2.5,price:1.8,status:'pending'});

test('verified optional booking reuses the complete stored Daily card and shared clipboard presentation',async()=>{
  const rows=[{...row(),label:'Over 2.5 Total Goals',prob:0.65,league:'Test League'}];
  const e=env(rows,sent=>({status:'verified',fingerprint:sent.fingerprint,selection_count:1,
    exact_membership_verified:true,code:'MOCK01',expires_at_ms:Date.now()+60000}));
  const callback=(await bookingButton(e,'2030-01-01','daily','123'))[0][0].callback_data;
  const reply=await book(e,callback,'123');
  assert.ok(reply.text.includes('DAILY PICKS')); assert.ok(reply.text.includes('Over 2.5 Total Goals'));
  assert.ok(reply.text.includes('Bet only what you can afford to lose.'));
  assert.ok(reply.text.includes('<code>MOCK01</code>'));
  assert.deepEqual(reply.reply_markup.inline_keyboard[0][0],{text:'Copy Booking Code',copy_text:{text:'MOCK01'}});
});

function env(rows=[row()],response={status:'unverified'}) {
  const claims=new Set();let calls=0;
  const db={prepare(sql){return {args:[],bind(...args){this.args=args;return this;},
    async all(){return {results:rows};},async first(){return rows.find(p=>p.id===this.args[0])??null;},
    async run(){if(sql.startsWith('INSERT')) {const key=this.args[0];const changes=claims.has(key)?0:1;claims.add(key);return {meta:{changes}};} return {meta:{changes:1}};}};}};
  return {DB:db,TELEGRAM_WEBHOOK_SECRET:'unit-webhook-secret',SPORTYBET_BOOKING_ENABLED:'true',SPORTYBET_BOOKING_SERVICE:{async fetch(_url,init){calls++;
    const sent=JSON.parse(init.body);const body=typeof response==='function'?response(sent):response;
    return Response.json(body);}},get calls(){return calls;}};
}

test('disabled/unbound means no DB, network or keyboard changes',async()=>{
  const e={DB:{prepare(){throw Error('must not read');}}};
  assert.equal(bookingEnabled(e),false);assert.deepEqual(await bookingButton(e,'2030-01-01','top','123'),[]);
  assert.equal((await book(e,'sb|top|20300101|bad')).text,BOOKING_FAILURE);
  assert.equal(bookingEnabled({...e,SPORTYBET_BOOKING_ENABLED:'true'}),false);
  assert.equal(bookingEnabled({...e,SPORTYBET_BOOKING_ENABLED:'true',SPORTYBET_BOOKING_SERVICE:{}}),false);
});

test('HTTP relay needs a fixed HTTPS endpoint and a private token',()=>{
  const base={SPORTYBET_BOOKING_ENABLED:'true',SPORTYBET_BOOKING_SERVICE_TOKEN:'x'.repeat(32)};
  assert.equal(bookingEnabled({...base,SPORTYBET_BOOKING_API_URL:'https://relay.example/v1/bookings'}),true);
  assert.equal(bookingEnabled({...base,SPORTYBET_BOOKING_API_URL:'https://relay.example/v1/bookings',SPORTYBET_BOOKING_SERVICE_TOKEN:' '.repeat(32)}),false);
  for(const url of ['http://relay.example/v1/bookings','https://a:b@relay.example/v1/bookings',
    'https://relay.example/v1/bookings?token=secret','https://relay.example/other'])
      assert.equal(bookingEnabled({...base,SPORTYBET_BOOKING_API_URL:url}),false);
});

test('wrong count or membership from relay never produces a copy button',async()=>{
  const e=env([row()],sent=>({status:'verified',fingerprint:sent.fingerprint,code:'MOCK01',
    selection_count:1,exact_membership_verified:false,expires_at_ms:Date.now()+60000}));
  const data=(await bookingButton(e,'2030-01-01','top','123'))[0][0].callback_data;
  const reply=await book(e,data,'123');
  assert.equal(reply.text,BOOKING_FAILURE);assert.equal(reply.reply_markup,undefined);
});

test('button binds whole exact ticket, with Telegram-safe callback length',async()=>{
  const e=env([row(),row(2)]);const buttons=await bookingButton(e,'2030-01-01','daily','123');
  const data=buttons[0][0].callback_data;
  assert.ok(Buffer.byteLength(data)<=64);assert.match(data,/^sb\|daily\|20300101\|[a-f0-9]{24}$/);
  assert.equal(e.calls,0);
});

test('unsupported, mixed, duplicate, expired or oversized tickets are never trimmed',()=>{
  for(const rows of [[row(),{...row(2),sport:'basketball'}],[row(),row()],
    [{...row(),start_utc:new Date(now-1000).toISOString()}],[{...row(),period:'1i'}],
    [{...row(),canonical_event_id:null}],[{...row(),status:'won'}],Array.from({length:6},(_,i)=>row(i+1))])
      assert.equal(eligible(rows,now),false);
});

test('changed ticket after rendering never submits a different slip',async()=>{
  const rows=[row()];const e=env(rows);const data=(await bookingButton(e,'2030-01-01','top','123'))[0][0].callback_data;
  rows[0].line=3.5;
  assert.equal((await book(e,data,'123')).text,BOOKING_FAILURE);assert.equal(e.calls,0);
});

test('callback token is bound to originating chat, collection and day',async()=>{
  const e=env([row()]);
  const data=(await bookingButton(e,'2030-01-01','top','123'))[0][0].callback_data;
  for(const [value,chat] of [[data,'456'],[data.replace('|top|','|daily|'),'123'],
    [data.replace('|20300101|','|20300102|'),'123']]) {
    assert.equal((await book(e,value,chat)).text,BOOKING_FAILURE);
  }
  assert.equal(e.calls,0);
});

test('button hashes the rendered rows without rereading a different collection',async()=>{
  const rendered=[row()];const e=env(rendered);
  const original=e.DB.prepare;
  e.DB.prepare=sql=>{
    const statement=original(sql);
    statement.all=async()=>{throw Error('must not reread collection after rendering');};
    return statement;
  };
  assert.equal((await bookingButton(e,'2030-01-01','top','123',rendered)).length,1);
  const stale=[{...rendered[0],line:3.5}];
  assert.deepEqual(await bookingButton(e,'2030-01-01','top','123',stale),[]);
});

test('failed or unverified response never displays an upstream code; duplicates submit once',async()=>{
  const e=env([row()],{status:'unverified',code:'MOCK01'});
  const data=(await bookingButton(e,'2030-01-01','top','123'))[0][0].callback_data;
  const replies=await Promise.all([book(e,data,'123'),book(e,data,'123')]);
  assert.equal(e.calls,1);assert.ok(replies.every(r=>!JSON.stringify(r).includes('MOCK01')));
});

test('verified private-relay response preserves manifest, count and copy-text contract',async()=>{
  let sentRows;
  const e=env([row()],sent=>{sentRows=JSON.parse(sent.manifest_json);return {status:'verified',fingerprint:sent.fingerprint,
    code:'MOCK01',selection_count:1,exact_membership_verified:true,expires_at_ms:Date.now()+60000};});
  const data=(await bookingButton(e,'2030-01-01','top','123'))[0][0].callback_data;
  const reply=await book(e,data,'123');
  assert.equal(reply.reply_markup.inline_keyboard[0][0].copy_text.text,'MOCK01');
  assert.equal(sentRows[0].line,2.5);assert.equal(sentRows[0].selection,'over');assert.equal(sentRows[0].price,1.8);
});

test('wrong membership/hash/count/expiry/code shape cannot produce success markup',()=>{
  const good={status:'verified',fingerprint:'hash',code:'MOCK01',selection_count:1,exact_membership_verified:true,expires_at_ms:now+60000};
  for(const change of [{fingerprint:'other'},{selection_count:2},{exact_membership_verified:false},
    {expires_at_ms:now-1},{code:'<secret>'},{code:['MOCK01']},{status:'created'}])
      assert.equal(verifiedReply({...good,...change},'hash',1,now),null);
});

test('backend exceptions cannot crash rendering or permit another POST',async()=>{
  const e=env();e.SPORTYBET_BOOKING_SERVICE.fetch=async()=>{throw Error('private upstream text');};
  const data=(await bookingButton(e,'2030-01-01','top','123'))[0][0].callback_data;
  assert.equal((await book(e,data,'123')).text,BOOKING_FAILURE);
  assert.match((await book(e,data,'123')).text,/already handled/);
});

test('missing request schema fails closed before reaching the relay',async()=>{
  const e=env();const original=e.DB.prepare;
  e.DB.prepare=sql=>{
    const statement=original(sql);
    if(sql.startsWith('INSERT'))statement.run=async()=>{throw Error('no such table: sporty_booking_requests');};
    return statement;
  };
  const data=(await bookingButton(e,'2030-01-01','top','123'))[0][0].callback_data;
  assert.equal((await book(e,data,'123')).text,BOOKING_FAILURE);
  assert.equal(e.calls,0);
});

test('real callback router sends verified copy markup only in the originating private chat',async()=>{
  const e=env([row()],sent=>({status:'verified',fingerprint:sent.fingerprint,code:'MOCK01',
    selection_count:1,exact_membership_verified:true,expires_at_ms:Date.now()+60000}));
  e.TELEGRAM_BOT_TOKEN='unit-token';
  const data=(await bookingButton(e,'2030-01-01','top','123'))[0][0].callback_data;
  const requests=[];const original=globalThis.fetch;
  globalThis.fetch=async(url,init)=>{requests.push({url:String(url),body:JSON.parse(init.body)});
    return Response.json({ok:true,result:{message_id:42}});};
  try {
    await handleUpdate(e,{update_id:100,callback_query:{id:'tap',data,from:{id:123},message:{message_id:1,chat:{id:123,type:'private'}}}});
    const sent=requests.find(r=>r.url.endsWith('/sendMessage'));
    assert.equal(sent.body.reply_markup.inline_keyboard[0][0].copy_text.text,'MOCK01');
    requests.length=0;
    await handleUpdate(e,{update_id:101,callback_query:{id:'group-tap',data,from:{id:123},message:{message_id:1,chat:{id:-123,type:'group'}}}});
    assert.ok(!requests.some(r=>r.url.endsWith('/sendMessage')));
    requests.length=0;
    await handleUpdate(e,{update_id:102,callback_query:{id:'forwarded-tap',data,from:{id:456},message:{message_id:1,chat:{id:456,type:'private'}}}});
    assert.equal(e.calls,1);
    assert.equal(requests.find(r=>r.url.endsWith('/sendMessage')).body.text,BOOKING_FAILURE);
    requests.length=0;e.SPORTYBET_BOOKING_ENABLED='false';
    await handleUpdate(e,{update_id:103,callback_query:{id:'disabled-tap',data,from:{id:123},message:{message_id:1,chat:{id:123,type:'private'}}}});
    assert.ok(!requests.some(r=>r.url.endsWith('/sendMessage')));
  } finally {globalThis.fetch=original;}
});

test('relay deadline fails without repeating an ambiguous request',async()=>{
  const e=env();let calls=0;
  e.SPORTYBET_BOOKING_SERVICE.fetch=async()=>{calls++;return new Promise(()=>{});};
  const data=(await bookingButton(e,'2030-01-01','top','123'))[0][0].callback_data;
  assert.equal((await book(e,data,'123')).text,BOOKING_FAILURE);
  await book(e,data,'123');
  assert.equal(calls,1);
});
