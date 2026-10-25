// Optional relay to the shared Python adapter, not a second SportyBet parser.
// No binding is deployed by this change. Unverified/unknown replies never expose a code.
import { getAllPicksForCollection, type Env, type CachedPick } from './db.ts';
import { marketContractReason } from './market_contract.ts';
import { timingSafeEqual } from './telegram.ts';
import { bookingCopyKeyboard, predictionBookingMessage } from './booking_message.ts';

export const BOOKING_FAILURE = 'A SportyBet booking code could not be verified. Your picks are unchanged.';
const FIELDS = ['canonical_event_id','canonical_competition_id','sport','home','away','start_utc',
  'market','period','stat','entity','selection','line','price'] as const;
const MARKETS = new Set(['result3','dc','dnb','btts','total','handicap']);
function relayUrl(env: Env): string | null {
  try {const u=new URL(env.SPORTYBET_BOOKING_API_URL ?? '');
    return u.protocol==='https:' && !u.username && !u.password && !u.search && !u.hash
      && u.pathname==='/v1/bookings' && /^[\x21-\x7e]{32,}$/.test(env.SPORTYBET_BOOKING_SERVICE_TOKEN ?? '') ? u.href : null;
  } catch {return null;}
}
export const bookingEnabled = (env: Env): boolean => env.SPORTYBET_BOOKING_ENABLED === 'true'
  && (typeof env.SPORTYBET_BOOKING_SERVICE?.fetch==='function' || !!relayUrl(env));

export async function digest(value: string): Promise<string> {
  return Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(value))))
    .map(x=>x.toString(16).padStart(2,'0')).join('');
}

async function callbackToken(env: Env, fingerprint: string, day: string, view: string, chatId: string): Promise<string> {
  if (!chatId || !env.TELEGRAM_WEBHOOK_SECRET) throw new Error('missing_callback_binding');
  const key=await crypto.subtle.importKey('raw',new TextEncoder().encode(env.TELEGRAM_WEBHOOK_SECRET),
    {name:'HMAC',hash:'SHA-256'},false,['sign']);
  const bytes=await crypto.subtle.sign('HMAC',key,new TextEncoder().encode(JSON.stringify([fingerprint,day,view,chatId])));
  return Array.from(new Uint8Array(bytes)).map(x=>x.toString(16).padStart(2,'0')).join('').slice(0,24);
}

export function eligible(rows: any[], now = Date.now()): boolean {
  return rows.length>0 && rows.length<=5 && new Set(rows.map(p=>p.canonical_event_id)).size===rows.length
    && rows.every(p=>p.canonical_event_id && p.canonical_competition_id && p.home && p.away
      && p.home!==p.away && p.sport==='football' && p.stat==='points' && p.entity==='all'
      && ['full','1h','2h'].includes(p.period) && MARKETS.has(p.market)
      && (p.period==='full' || ['result3','total'].includes(p.market))
      && !marketContractReason(p) && Number.isFinite(p.price) && p.price>1
      && Date.parse(p.start_utc)>now && p.status==='pending');
}

// Use exactly the collection the existing renderer serves, enriched with canonical
// IDs from those same immutable rows. Never search a candidate pool for substitutes.
export async function ticket(env: Env, day: string, view: 'top'|'daily', rendered?: CachedPick[]) {
  const displayed = rendered ?? await getAllPicksForCollection(env.DB,day,view);
  if (!displayed.length || displayed.length>5) return null;
  const rows = [];
  for (const p of displayed) {
    const full = await env.DB.prepare('SELECT * FROM picks WHERE id=?1 AND pick_date=?2').bind(p.id,day).first<any>();
    if (!full || FIELDS.some(k=>k in p && (p as any)[k]!==full[k])) return null;
    rows.push(full);
  }
  if (!eligible(rows)) return null;
  rows.sort((a,b)=>Date.parse(a.start_utc)-Date.parse(b.start_utc));
  const selections = rows.map(p=>({prediction_id:p.id,...Object.fromEntries(FIELDS.map(k=>[k,p[k]]))}));
  const manifest = JSON.stringify(selections);
  const fingerprint = await digest(manifest);
  return { selections, manifest, fingerprint, rows };
}

export async function bookingButton(env: Env, day: string, view: 'top'|'daily', chatId='', rendered?: CachedPick[]) {
  if (!bookingEnabled(env)) return [];
  try {
    const t = await ticket(env,day,view,rendered);
    if (!t) return [];
    const token=await callbackToken(env,t.fingerprint,day,view,chatId);
    return [[{text:'🎟️ Generate SportyBet Booking Code',callback_data:`sb|${view}|${day.replaceAll('-','')}|${token}`}]];
  } catch { return []; } // optional decoration must never break prediction rendering
}

export function verifiedReply(body: any, fingerprint: string, count: number, now=Date.now()) {
  if (!body || body.status!=='verified' || body.fingerprint!==fingerprint || body.selection_count!==count
      || body.exact_membership_verified!==true || typeof body.code!=='string' || !/^[A-Z0-9]{4,16}$/.test(body.code)
      || !Number.isFinite(body.expires_at_ms) || body.expires_at_ms<=now) return null;
  return {text:`<b>SportyBet booking code</b>\n<code>${body.code}</code>\n${count} selections · Slip verified.`,
    reply_markup:bookingCopyKeyboard(body.code)};
}

export async function book(env: Env, data: string, chatId=''): Promise<Record<string,unknown>> {
  const failure={text:BOOKING_FAILURE};
  if (!bookingEnabled(env)) return failure;
  const match=/^sb\|(top|daily)\|(\d{4})(\d{2})(\d{2})\|([a-f0-9]{24})$/.exec(data);
  if (!match) return failure;
  try {
    const day=`${match[2]}-${match[3]}-${match[4]}`;
    const t=await ticket(env,day,match[1] as 'top'|'daily');
    if (!t || !timingSafeEqual(await callbackToken(env,t.fingerprint,day,match[1],chatId),match[5])) return failure;
    // Durable at-most-once claim across isolates/repeated buttons. Crashed or
    // ambiguous attempts stay claimed: NEVER reclaim and blindly retry POST.
    const key=t.fingerprint;
    const claim=await env.DB.prepare("INSERT OR IGNORE INTO sporty_booking_requests (request_key,status) VALUES (?1,'processing')").bind(key).run();
    if (!(claim.meta.changes>0)) return {text:'This booking request was already handled or is still processing. It will not be retried automatically.'};
    const controller=new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    try {
      const timeout=new Promise<never>((_,reject)=>{timer=setTimeout(()=>{controller.abort();reject(new Error('timeout'));},12000);});
      const work=(async()=>{
        const binding=typeof env.SPORTYBET_BOOKING_SERVICE?.fetch==='function' ? env.SPORTYBET_BOOKING_SERVICE : undefined;
        const init={method:'POST',signal:controller.signal,redirect:'error' as RequestRedirect,
          headers:{'Content-Type':'application/json',...(!binding
            ? {'Authorization':`Bearer ${env.SPORTYBET_BOOKING_SERVICE_TOKEN}`} : {})},
          body:JSON.stringify({manifest_json:t.manifest,fingerprint:key})};
        const response=binding
          ? await binding.fetch('https://booking.internal/v1/bookings',init)
          : await fetch(relayUrl(env)!,init);
        if (response.status!==200 || !response.headers.get('Content-Type')?.includes('application/json')) return null;
        const reader=response.body?.getReader(); if (!reader) return null;
        let size=0; const chunks: Uint8Array[]=[];
        for (;;) { const {done,value}=await reader.read(); if (done) break; size+=value.length;
          if (size>4096) {await reader.cancel();return null;} chunks.push(value); }
        const bytes=new Uint8Array(size);let offset=0;for(const chunk of chunks){bytes.set(chunk,offset);offset+=chunk.length;}
        return JSON.parse(new TextDecoder().decode(bytes));
      })();
      const body=await Promise.race([work,timeout]);
      const reply=verifiedReply(body,key,t.selections.length);
      // No code is persisted or shown unless the backend's independent exact
      // membership check passed. The current adapter deliberately cannot pass.
      await env.DB.prepare('UPDATE sporty_booking_requests SET status=?1 WHERE request_key=?2')
        .bind(reply?'verified':'unverified',key).run();
      if (reply && t.rows.every(p=>typeof p.label==='string' && Number.isFinite(p.prob))) {
        return predictionBookingMessage(t.rows.map(p=>({sport:p.sport,league:p.league,
          home:p.home,away:p.away,label:p.label,price:p.price,prob:p.prob,startUtc:p.start_utc})),day,
          {code:body.code,selectionCount:t.selections.length,exactMembershipVerified:true},{enabled:true});
      }
      return reply ?? failure;
    } finally {if(timer)clearTimeout(timer);}
  } catch { console.warn('Optional SportyBet request withheld; no automatic retry.'); return failure; }
}
