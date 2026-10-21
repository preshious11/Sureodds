import { DatabaseSync } from 'node:sqlite';
import {readFileSync} from 'node:fs';
import {it,describe} from 'node:test';
import assert from 'node:assert/strict';
import {handleUpdate} from '../src/index.ts';
import {renderView} from '../src/views.ts';
import {menuKeyboard,communityMenuKeyboard,topEmptyKeyboard,buildKeyboard,allButtonModeFor,isAdmin} from '../src/telegram.ts';
import {sweepResultGifs,sweepMatchEvents,handleScheduled,CRON_POST_SWEEP,postGroupRecap,postGroupPicks,sendTelegram} from '../src/cron.ts';
const NOW=Date.UTC(2026,9,18,10),DAY='2026-10-18';
function world(){
 const sql=new DatabaseSync(':memory:');
 for(const file of ['0001_foundation.sql','0003_engine_state.sql','0004_sync_state.sql','0005_ticket_candidates.sql',
 '0006_custom_sessions.sql','0007_custom_picks_conversation.sql','0008_pinned_menu.sql','0009_match_events.sql',
 '0010_sync_lease.sql','0011_pipeline_dispatch.sql','0012_result_gifs.sql','0013_pro_picks.sql'])sql.exec(readFileSync(new URL(`../migrations/${file}`,import.meta.url),'utf8'));
 let count=0;
 const DB={prepare(query){count++;return {args:[],bind(...a){this.args=a;return this;},async first(){return sql.prepare(query).get(...this.args)??null;},
 async all(){return {results:sql.prepare(query).all(...this.args)};},async run(){return {meta:{changes:sql.prepare(query).run(...this.args).changes}};}};}};
 const env={DB,TELEGRAM_BOT_TOKEN:'test',TELEGRAM_ADMIN_IDS:'999,888',TELEGRAM_CHAT_ID:'-100',TELEGRAM_BOT_USERNAME:'SureOddsBot',RESULT_GIFS_START_DATE:'2026-10-16'};
 const sent=[];let updateId=0;
 const sender=async(_e,method,body)=>{sent.push({method,...body});return {ok:true,messageId:42};};
 const message=(text,user=1,type='private',extra={})=>({update_id:++updateId,message:{chat:{id:type==='private'?user:-100,type},from:{id:user,first_name:'Test'},text,...extra}});
 const callback=(view,user=1,type='private')=>({update_id:++updateId,callback_query:{id:'cb',from:{id:user},data:`pg|${view}|0`,message:{message_id:10,chat:{id:type==='private'?user:-100,type}}}});
 const route=async update=>{const old=globalThis.fetch;globalThis.fetch=async(url,init)=>{const body=JSON.parse(init.body);sent.push({method:String(url).split('/').at(-1),...body});return new Response(JSON.stringify({ok:true,result:{message_id:42}}),{status:200});};try{await handleUpdate(env,update);}finally{globalThis.fetch=old;}};
 return {sql,env,sent,sender,message,callback,route,get count(){return count;}};
}
function ticket(w,statuses,kind='top',day=DAY){
 const id=kind==='top'?123:456;
 w.sql.prepare('INSERT INTO posts(kind,post_date,message_id,created_at) VALUES(?,?,?,?)').run(kind,day,String(id),new Date(NOW).toISOString());
 for(const [i,status] of statuses.entries()){
 const r=w.sql.prepare(`INSERT INTO picks(pick_date,tier,rank,provider,provider_event_id,sport,home,away,start_utc,market,period,stat,entity,selection,label,price,prob,ev,status,created_at)
 VALUES(?,?,?,'test',?,'football','H','A',?,'result3','full','points','all','home','H',1.5,.7,.1,?,?)`).run(day,kind,i+1,`${kind}-${day}-${i}`,new Date(NOW-3600000).toISOString(),status,new Date(NOW).toISOString());
 w.sql.prepare('INSERT INTO prediction_memberships VALUES(?,?,?)').run(r.lastInsertRowid,kind,i+1);
 }
}
function gif(w,kind='won'){w.sql.prepare('INSERT INTO result_gifs VALUES(?,?,?,?)').run(kind,`${kind}-file`,`${kind}-unique`,new Date(NOW).toISOString());}

function photoCache(w, broken=false) {
 ticket(w,['pending']);w.sql.prepare("DELETE FROM posts").run();
 w.sql.prepare("UPDATE picks SET fixture_key='test:fixture'").run();
 const row=w.sql.prepare('SELECT * FROM picks').get();
 const p={...row,pick_id:row.id,fixture_id:'test:fixture',kickoff_utc:row.start_utc};
 const identity=JSON.stringify([DAY,[[p.pick_id,p.fixture_id,p.market,p.selection,p.price,p.home,p.away,p.label,p.kickoff_utc]]]);
 const data={date:DAY,top_picks:[p],bet_image:{date:DAY,identity,jpeg:broken?'invalid':'/9j/AAAA',link:'https://t.me/SampleCommunity'}};
 w.sql.prepare(`INSERT INTO prediction_cache(cache_key,local_date,timezone,status,payload_json,generated_at,updated_at,expires_at)
 VALUES('test',?,'Africa/Lagos','COMPLETE',?,?,?,?)`).run(DAY,JSON.stringify(data),'now','now','later');
}

describe('Bet of the Day photo publications',()=>{
 it('persists separate photo ids; retries owner only, Daily remains text',async()=>{
  const w=world();photoCache(w);let failOwner=true;
  const sender=async(e,m,p)=>{w.sent.push({method:m,...p});return p.chat_id==='999'&&failOwner?{ok:false}:{ok:true,messageId:p.chat_id==='999'?202:101};};
  await postGroupPicks(w.env,'top',NOW,sender);failOwner=false;await postGroupPicks(w.env,'top',NOW,sender);
  assert.equal(w.sent.filter(p=>p.chat_id==='-100').length,1);
  assert.ok(w.sent.every(p=>p.method==='sendPhoto'&&p.caption.includes('Join the Kovqi community')));
  assert.equal(w.sql.prepare("SELECT message_id FROM posts WHERE kind='top'").get().message_id,'101');
  assert.equal(w.sql.prepare("SELECT message_id FROM posts WHERE kind='top_owner'").get().message_id,'202');
  await postGroupPicks(w.env,'daily',NOW,w.sender);assert.equal(w.sent.at(-1).method,'sendMessage');
 });
 it('invalid media falls back once and upload failures stay retryable',async()=>{
  const w=world();photoCache(w,true);await postGroupPicks(w.env,'top',NOW,w.sender);await postGroupPicks(w.env,'top',NOW,w.sender);
  assert.equal(w.sent.length,2);assert.ok(w.sent.every(p=>p.method==='sendMessage'&&p.text.includes('Bet only what you can afford to lose.')));
  const v=world();photoCache(v);await postGroupPicks(v.env,'top',NOW,async()=>({ok:false}));
  assert.equal(v.sql.prepare("SELECT COUNT(*) n FROM posts WHERE message_id IS NOT NULL").get().n,0);
  await postGroupPicks(v.env,'top',NOW,v.sender);assert.equal(v.sent[0].method,'sendPhoto');
 });
 it('stale image selections cannot override structured storage',async()=>{
  const w=world();photoCache(w);
  w.sql.prepare('UPDATE picks SET price=1.7').run();
  await postGroupPicks(w.env,'top',NOW,w.sender);
  assert.ok(w.sent.every(p=>p.method==='sendMessage'&&p.text.includes('1.70')));
 });
 it('receipt write failure never triggers a second text publication',async()=>{
  const w=world();photoCache(w);const prepare=w.env.DB.prepare;
  w.env.DB.prepare=query=>{
   if(query.startsWith('UPDATE posts SET message_id')) return {bind(){return this;},async run(){throw Error('D1 unavailable');}};
   return prepare(query);
  };
  await assert.rejects(()=>postGroupPicks(w.env,'top',NOW,w.sender));
  assert.equal(w.sent.length,1);assert.equal(w.sent[0].method,'sendPhoto');
 });
 it('GIF replies use each destination photo id and dedupe independently',async()=>{
  const w=world();ticket(w,['won']);gif(w);
  w.sql.prepare("INSERT INTO posts(kind,post_date,message_id,created_at) VALUES('top_owner',?,'789',?)").run(DAY,new Date(NOW).toISOString());
  await sweepResultGifs(w.env,NOW,w.sender);await sweepResultGifs(w.env,NOW,w.sender);
  assert.deepEqual(w.sent.map(p=>[p.chat_id,p.reply_parameters.message_id,p.caption]),[['-100',123,'Won'],['999',789,'Won']]);
 });
 it('uploads multipart JPEG and requires a Telegram photo receipt',async()=>{
  const old=globalThis.fetch;
  try {globalThis.fetch=async(url,init)=>{
   assert.ok(init.body instanceof FormData);assert.equal(init.body.get('photo').type,'image/jpeg');
   assert.equal(init.body.get('chat_id'),'-100');return new Response(JSON.stringify({ok:true,result:{message_id:77}}));
  };
  assert.deepEqual(await sendTelegram(world().env,'sendPhoto',{chat_id:'-100',photo_base64:'/9j/AAAA'}),{ok:true,messageId:77});
  globalThis.fetch=async()=>new Response(JSON.stringify({ok:true}));
  await assert.rejects(()=>sendTelegram(world().env,'sendPhoto',{chat_id:'-100',photo_base64:'/9j/AAAA'}));
  } finally {globalThis.fetch=old;}
 });
});

describe('access model',()=>{
 it('group keyboards have exactly four URL buttons; regular private keyboards hide admin views',()=>{
 const mode=allButtonModeFor('supergroup','SureOddsBot');
 for(const kb of [communityMenuKeyboard('SureOddsBot'),menuKeyboard(mode),topEmptyKeyboard('SureOddsBot',mode),buildKeyboard('all',1,3,DAY,[],mode)]){
 const buttons=kb.inline_keyboard.flat();assert.equal(buttons.length,4);
 assert.deepEqual(new Set(buttons.map(b=>b.url.split('start=')[1])),new Set(['all','top','daily','custom_picks']));
 assert.ok(buttons.every(b=>b.url&&!b.callback_data));assert.doesNotMatch(JSON.stringify(kb),/Stats|Yesterday/);
 }
 assert.equal(menuKeyboard().inline_keyboard.flat().length,3);
 assert.match(JSON.stringify(menuKeyboard('admin')),/Stats/);
 });
 for(const view of ['stats','yesterday','stats_top','stats_daily'])it(`central render guard blocks ${view} without reading D1`,async()=>{
 const w=world();const result=await renderView(w.env,view,0,null);
 assert.match(result.text,/Use the menu below/);assert.doesNotMatch(JSON.stringify(result),/Stats|Yesterday/);assert.equal(w.count,0);
 });
 for(const command of ['/stats','/yesterday','/stats_top','/stats_daily','/start stats','/start yesterday'])it(`regular user ${command} gets private welcome; group is silent`,async()=>{
 const w=world();await w.route(w.message(command));assert.match(w.sent.at(-1).text,/Use the menu below/);
 w.sent.length=0;await w.route(w.message(command,1,'supergroup'));assert.equal(w.sent.length,0);
 });
 for(const view of ['stats','yesterday','stats_top','stats_daily'])it(`old ${view} callback is denied without exposing the view`,async()=>{
 const w=world();await w.route(w.callback(view));const messages=w.sent.filter(s=>s.method!=='answerCallbackQuery');
 assert.equal(messages.length,1);assert.match(messages[0].text,/Use the menu below/);assert.doesNotMatch(JSON.stringify(messages[0].reply_markup),/Stats|Yesterday/);
 });
 for(const command of ['/top','/daily','/today','/all','/start'])it(`${command} in the group is only a four-button redirect`,async()=>{
 const w=world();await w.route(w.message(command,1,'supergroup'));assert.equal(w.sent.length,1);assert.equal(w.sent[0].reply_markup.inline_keyboard.flat().length,4);
 assert.doesNotMatch(w.sent[0].text,/BET OF THE DAY|DAILY PICKS/);
 });
 it('admins browse private stats and group /stats sends only to their DM; anonymous admins are ignored',async()=>{
 const w=world();await w.route(w.message('/start',999));assert.match(JSON.stringify(w.sent.at(-1).reply_markup),/Stats/);
 w.sent.length=0;await w.route(w.message('/stats',999,'supergroup'));assert.equal(w.sent.length,1);assert.equal(w.sent[0].chat_id,'999');assert.match(w.sent[0].text,/STATS/);
 w.sent.length=0;await w.route(w.message('/stats',999,'supergroup',{sender_chat:{id:-100}}));assert.equal(w.sent.length,0);
 await w.route(w.message('/stats',999,'supergroup',{from:{id:999,username:'GroupAnonymousBot'}}));assert.equal(w.sent.length,0);
 assert.equal(isAdmin(w.env,999),true);assert.equal(isAdmin(w.env,1),false);
 });
 it('/setcommands installs the default and each admin-chat scope, privately and only for admins',async()=>{
 const w=world();await w.route(w.message('/setcommands'));assert.ok(!w.sent.some(s=>s.method==='setMyCommands'));
 w.sent.length=0;await w.route(w.message('/setcommands',999,'supergroup'));assert.equal(w.sent.length,0);
 await w.route(w.message('/setcommands',999));const calls=w.sent.filter(s=>s.method==='setMyCommands');assert.equal(calls.length,3);
 assert.deepEqual(calls[0].commands.map(c=>c.command),['start','top','daily','all']);
 assert.ok(calls.slice(1).every(c=>c.commands.some(x=>x.command==='stats')));assert.equal(w.sent.filter(s=>s.method==='sendMessage').length,3);
 });
});

describe('GIF registration',()=>{
 it('accepts all Telegram GIF/media forms, including via_bot, deduplicates, and supports commands/caption registration',async()=>{
 const w=world();await w.route(w.message('/addgif won',999));
 for(const media of [{animation:{file_id:'a',file_unique_id:'a'}},{document:{mime_type:'image/gif',file_id:'b',file_unique_id:'b'}},
 {document:{mime_type:'video/mp4',file_id:'c',file_unique_id:'c'}},{video:{file_id:'d',file_unique_id:'d'}},
 {animation:{file_id:'e',file_unique_id:'e'},via_bot:{id:9}},{animation:{file_id:'new-a',file_unique_id:'a'}}])await w.route(w.message('',999,'private',media));
 assert.equal(w.sql.prepare('SELECT COUNT(*) n FROM result_gifs').get().n,5);assert.equal(w.sent.at(-1).text,'Added (5 won GIFs)');
 await w.route(w.message('/done',999));await w.route(w.message('',999,'private',{animation:{file_id:'f',file_unique_id:'f'}}));assert.equal(w.sql.prepare('SELECT COUNT(*) n FROM result_gifs').get().n,5);
 await w.route(w.message('',999,'private',{caption:'lost',animation:{file_id:'lost',file_unique_id:'lost'}}));
 await w.route(w.message('/gifs',999));assert.match(w.sent.at(-1).text,/won: 5\nlost: 1/);
 await w.route(w.message('/cleargifs won',999));assert.equal(w.sql.prepare("SELECT COUNT(*) n FROM result_gifs WHERE kind='won'").get().n,0);
 });
 it('collect mode expires, any command ends it, and non-admin/group media never registers',async()=>{
 const w=world();const media={animation:{file_id:'a',file_unique_id:'a'}};
 await w.route(w.message('/addgif won',999));w.sql.prepare("UPDATE kv SET v=? WHERE k='gif_collect:999'").run(JSON.stringify({kind:'won',expires:0}));
 await w.route(w.message('',999,'private',media));assert.equal(w.sql.prepare('SELECT COUNT(*) n FROM result_gifs').get().n,0);
 await w.route(w.message('/addgif won',999));await w.route(w.message('/start',999));await w.route(w.message('',999,'private',media));
 for(const user of [1,999]){await w.route(w.message('/addgif won',user,'supergroup'));await w.route(w.message('',user,'supergroup',media));}
 await w.route(w.message('',1,'private',{...media,caption:'won'}));assert.match(w.sent.at(-1).text,/Use the menu below/);
 assert.equal(w.sql.prepare('SELECT COUNT(*) n FROM result_gifs').get().n,0);
 });
});

describe('settled ticket GIF replies',()=>{
 for(const [statuses,kind] of [[['won'],'won'],[['lost'],'lost'],[['void','won'],'won'],[['half_lost'],'lost'],[['half_won','push'],'won'],[['won','cancelled'],'won']]){
 it(`${statuses.join('+')} -> ${kind}, one group reply and no owner animation`,async()=>{
 const w=world();ticket(w,statuses);gif(w,kind);await sweepResultGifs(w.env,NOW,w.sender);await sweepResultGifs(w.env,NOW,w.sender);
 assert.equal(w.sent.length,1);assert.equal(w.sent[0].method,'sendAnimation');assert.equal(w.sent[0].animation,`${kind}-file`);
 assert.equal(w.sent[0].chat_id,'-100');assert.deepEqual(w.sent[0].reply_parameters,{message_id:123,allow_sending_without_reply:false});assert.equal(w.sent[0].caption,kind==='won'?'Won':'Lost');
 });}
 it('all void/push records a receipt without sending; pending/unknown/postponed do nothing',async()=>{
 for(const statuses of [['void','push'],['pending'],['unresolved'],['postponed'],['mystery']]){
 const w=world();ticket(w,statuses);gif(w);await sweepResultGifs(w.env,NOW,w.sender);assert.equal(w.sent.length,0);
 const receipts=w.sql.prepare("SELECT * FROM posts WHERE kind='result_gif_top'").all();assert.equal(receipts.length,statuses[0]==='void'?1:0);
 }
 const w=world();ticket(w,['void']);w.sql.prepare('UPDATE picks SET result_json=?').run(JSON.stringify({reason:'settlement deadline expired'}));await sweepResultGifs(w.env,NOW,w.sender);
 assert.equal(w.sql.prepare("SELECT COUNT(*) n FROM posts WHERE kind='result_gif_top'").get().n,1);
 });
 it('releases failed/throwing sends for the next sweep to retry',async()=>{
 const w=world();ticket(w,['won']);gif(w);await sweepResultGifs(w.env,NOW,async()=>({ok:false}));
 assert.equal(w.sql.prepare("SELECT COUNT(*) n FROM posts WHERE kind='result_gif_top'").get().n,0);
 await sweepResultGifs(w.env,NOW,async()=>{throw Error('offline');});await sweepResultGifs(w.env,NOW,w.sender);assert.equal(w.sent.length,1);
 });
 it('missing GIFs are receipted and cause only one owner notice per type/day',async()=>{
 const w=world();ticket(w,['won']);ticket(w,['won'],'daily');await sweepResultGifs(w.env,NOW,w.sender);await sweepResultGifs(w.env,NOW,w.sender);await sweepResultGifs(w.env,NOW,w.sender);
 assert.equal(w.sent.length,1);assert.equal(w.sent[0].chat_id,'999');assert.match(w.sent[0].text,/No won GIFs registered/);
 assert.equal(w.sql.prepare("SELECT COUNT(*) n FROM posts WHERE kind LIKE 'result_gif_%'").get().n,2);
 });
 it('requires opt-in start date and ignores tickets outside the three Lagos dates',async()=>{
 for(const [date,start,send] of [['2026-10-15','2026-10-01',false],['2026-10-16','2026-10-17',false],['2026-10-16','2026-10-16',true],['2026-10-18','',false]]){
 const w=world();ticket(w,['won'],'top',date);gif(w);w.env.RESULT_GIFS_START_DATE=start;await sweepResultGifs(w.env,NOW,w.sender);assert.equal(w.sent.length,Number(send));}
 });
 it('recap is private and includes yesterday and last-seven-day stats',async()=>{
 const w=world();ticket(w,['won'],'top','2026-10-17');const outcome=await postGroupRecap(w.env,NOW,w.sender);
 assert.equal(outcome,'sent');assert.equal(w.sent.length,1);assert.equal(w.sent[0].chat_id,'999');assert.match(w.sent[0].text,/Yesterday/);assert.match(w.sent[0].text,/Last 7 days/);
 });
});


for(const cmd of ['/addgif won','/done','/gifs','/cleargifs won']) {
 it(`${cmd} is admin-private only`,async()=>{
  const w=world();await w.route(w.message(cmd,1));assert.match(w.sent.at(-1).text,/Use the menu below/);
  assert.equal(w.sql.prepare("SELECT COUNT(*) n FROM kv WHERE k LIKE 'gif_collect:%'").get().n,0);
  w.sent.length=0;await w.route(w.message(cmd,999,'supergroup'));assert.equal(w.sent.length,0);
 });
}
it('empty posted tickets are receipted without sending and concurrent sweeps send one animation',async()=>{
 const empty=world();ticket(empty,[]);await sweepResultGifs(empty.env,NOW,empty.sender);
 assert.equal(empty.sent.length,0);assert.equal(empty.sql.prepare("SELECT COUNT(*) n FROM posts WHERE kind='result_gif_top'").get().n,1);
 const w=world();ticket(w,['won']);gif(w);
 await Promise.all([sweepResultGifs(w.env,NOW,w.sender),sweepResultGifs(w.env,NOW,w.sender)]);assert.equal(w.sent.length,1);
});

it('the deadline recap still includes both stats windows when every leg remains pending',async()=>{
 const w=world();ticket(w,['pending'],'top','2026-10-17');
 await postGroupRecap(w.env,NOW,w.sender);
 assert.equal(w.sent.length,1);assert.equal(w.sent[0].chat_id,'999');
 assert.match(w.sent[0].text,/Yesterday\nNo settled results yet/);
 assert.match(w.sent[0].text,/Last 7 days\nNo settled results yet/);
});

function fixture(w,id,key,type='end',at=NOW){
 w.sql.prepare('UPDATE picks SET fixture_key=? WHERE id=?').run(key,id);
 w.sql.prepare(`INSERT OR REPLACE INTO live_fixture_state(pick_date,fixture_key,status,settlement_done,updated_at)
 VALUES(?,?,?,?,?)`).run(DAY,key,type==='end'?'final':'in_progress',type==='end'?1:0,new Date(at).toISOString());
 w.sql.prepare('INSERT INTO match_events VALUES(?,?,?,?,?)').run(DAY,key,type,`${type} ${key}`,new Date(at).toISOString());
}
async function scheduled(w,cron,at=NOW){
 const saved=globalThis.fetch;
 globalThis.fetch=async(url,init)=>{const body=JSON.parse(init.body);w.sent.push({method:String(url).split('/').at(-1),...body});
 return new Response(JSON.stringify({ok:true,result:{message_id:42}}),{status:200});};
 try{await handleScheduled({...w.env,STAGE3_CRON_ENABLED:'1'},cron,at);}finally{globalThis.fetch=saved;}
}
it('a two-leg ticket replies in the same sweep immediately after its second END, three hours later',async()=>{
 const w=world();ticket(w,['won','pending']);gif(w);
 const ids=w.sql.prepare('SELECT id FROM picks ORDER BY id').all().map(r=>r.id);
 fixture(w,ids[0],'early');w.sql.prepare('UPDATE picks SET fixture_key=? WHERE id=?').run('late',ids[1]);
 await sweepMatchEvents(w.env,NOW,w.sender);assert.equal(w.sent.filter(s=>s.method==='sendAnimation').length,0);
 w.sql.prepare("UPDATE picks SET status='won' WHERE id=?").run(ids[1]);fixture(w,ids[1],'late','end',NOW+3*3600000);
 await sweepMatchEvents(w.env,NOW+3*3600000,w.sender);
 const i=w.sent.findIndex(s=>s.method==='sendAnimation');assert.ok(i>0);
 assert.equal(w.sent[i-1].text,'end late');assert.equal(w.sent[i-1].chat_id,'-100');
 assert.equal(w.sent[i].reply_parameters.message_id,123);
 assert.ok(w.sql.prepare("SELECT message_id FROM posts WHERE kind='evt_late_end'").get().message_id);
});
it('one fixture with different Top/Daily markets finishes both, replying to each ticket after END',async()=>{
 const w=world();ticket(w,['won']);ticket(w,['half_lost'],'daily');gif(w);gif(w,'lost');
 const ids=w.sql.prepare('SELECT id FROM picks ORDER BY id').all().map(r=>r.id);
 fixture(w,ids[0],'shared');w.sql.prepare("UPDATE picks SET fixture_key='shared',market='totals',selection='over' WHERE id=?").run(ids[1]);
 await sweepMatchEvents(w.env,NOW,w.sender);
 const group=w.sent.filter(s=>s.chat_id==='-100');assert.equal(group[0].text,'end shared');
 assert.deepEqual(group.slice(1).map(s=>s.reply_parameters.message_id).sort(),[123,456]);
 assert.equal(group.filter(s=>s.method==='sendAnimation').length,2);
});
it('even if all legs are settled before delivery, GIF waits for the final queued END alert',async()=>{
 const w=world();ticket(w,['won','won']);gif(w);
 const ids=w.sql.prepare('SELECT id FROM picks ORDER BY id').all().map(r=>r.id);
 fixture(w,ids[0],'first');fixture(w,ids[1],'last');
 await sweepMatchEvents(w.env,NOW,w.sender);
 const group=w.sent.filter(s=>s.chat_id==='-100');assert.deepEqual(group.map(s=>s.method),['sendMessage','sendMessage','sendAnimation']);
 assert.equal(group[1].text,'end last');
});
it('stage9 settlement with no END event is delivered by the scheduled POST backup',async()=>{
 const w=world();ticket(w,['won']);gif(w);await scheduled(w,CRON_POST_SWEEP);
 assert.equal(w.sent.filter(s=>s.method==='sendAnimation').length,1);
 await scheduled(w,CRON_POST_SWEEP);assert.equal(w.sent.filter(s=>s.method==='sendAnimation').length,1);
});
it('POST backup racing immediate END delivery sends exactly one GIF, after END confirmation',async()=>{
 const w=world();ticket(w,['won']);gif(w);fixture(w,w.sql.prepare('SELECT id FROM picks').get().id,'race');
 const sender=async(e,method,body)=>{
  w.sent.push({method,...body});
  if(method==='sendMessage' && body.chat_id==='-100') await sweepResultGifs(w.env,NOW,sender);
  if(method==='sendAnimation') {
   assert.ok(w.sql.prepare("SELECT message_id FROM posts WHERE kind='evt_race_end'").get().message_id);
   await sweepResultGifs(w.env,NOW,sender);
  }
  return {ok:true,messageId:42};
 };
 await Promise.all([sweepMatchEvents(w.env,NOW,sender),sweepResultGifs(w.env,NOW,sender)]);
 assert.equal(w.sent.filter(s=>s.method==='sendAnimation').length,1);
 assert.equal(w.sql.prepare("SELECT COUNT(*) n FROM posts WHERE kind='result_gif_top' AND message_id IS NOT NULL").get().n,1);
 // Reverse the winner: pause the immediate path after its aggregate snapshot,
 // let the backup claim/send, then resume the stale immediate result.
 const backup=world();ticket(backup,['won']);gif(backup);
 fixture(backup,backup.sql.prepare('SELECT id FROM picks').get().id,'backup-wins');
 const prepare=backup.env.DB.prepare;let intercepted=false;
 backup.env.DB.prepare=function(query){
  const statement=prepare.call(this,query),all=statement.all;
  statement.all=async function(){const result=await all.call(this);
   if(query.startsWith('WITH tickets')&&!intercepted){intercepted=true;await sweepResultGifs(backup.env,NOW,backup.sender);}
   return result;};return statement;
 };
 await sweepMatchEvents(backup.env,NOW,backup.sender);
 assert.equal(backup.sent.filter(s=>s.method==='sendAnimation').length,1);
 assert.equal(backup.sent[0].text,'end backup-wins');
});
it('15 simultaneous starts all reach group and owner within 45 minutes despite 13-minute monitor latency',async()=>{
 const w=world();ticket(w,Array(15).fill('pending'));w.env.MATCH_START_WINDOW_MIN='45';
 for(const r of w.sql.prepare('SELECT id FROM picks').all())fixture(w,r.id,`kickoff-${r.id}`,'start');
 w.sql.prepare('UPDATE picks SET start_utc=?').run(new Date(NOW).toISOString());
 for(let minutes=18;minutes<=43;minutes+=5)await sweepMatchEvents(w.env,NOW+minutes*60000,w.sender);
 for(const chat of ['-100','999'])assert.equal(w.sent.filter(s=>s.chat_id===chat).length,15);
 assert.equal(w.sql.prepare("SELECT COUNT(*) n FROM posts WHERE kind LIKE '%_expired'").get().n,0);
});
it('deadline-expired voids are final neutral: wins plus void wins; all neutral is receipted without a GIF',async()=>{
 for(const statuses of [['won','void'],['void','push']]) {
  const w=world();ticket(w,statuses);gif(w);
  w.sql.prepare("UPDATE picks SET result_json=? WHERE status='void'").run(JSON.stringify({reason:'settlement deadline expired'}));
  await sweepResultGifs(w.env,NOW,w.sender);
  assert.equal(w.sent.length,statuses[0]==='won'?1:0);
  assert.equal(w.sql.prepare("SELECT COUNT(*) n FROM posts WHERE kind='result_gif_top' AND message_id IS NOT NULL").get().n,1);
  assert.equal(w.sql.prepare("SELECT COUNT(*) n FROM picks WHERE status='void'").get().n,1);
 }
});
