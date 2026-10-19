import { getAllPicksForCollection, type Env } from './db.ts';
import { escHtml } from './format.ts';

export interface TicketPhoto { jpeg: string; caption: string }

/** Optional presentation from the SAME immutable cache row as generation. */
export async function ticketPhoto(env: Env, day: string, payload: string): Promise<TicketPhoto | undefined> {
  try {
    const data = JSON.parse(payload);
    const media = data.bet_image;
    if (!media) return;
    const picks = data.top_picks;
    if (data.date !== day || media.date !== day || !Array.isArray(picks) || picks.length < 1 || picks.length > 3) throw Error('date/count');
    const identity = [day, picks.map(p => [p.pick_id,p.fixture_id,p.market,p.selection,p.price,p.home,p.away,p.label,p.kickoff_utc])];
    if (JSON.stringify(JSON.parse(media.identity)) !== JSON.stringify(identity)) throw Error('identity');
    const rows = await getAllPicksForCollection(env.DB,day,'top');
    const {results: fixtures} = await env.DB.prepare(
      "SELECT id, COALESCE(NULLIF(canonical_event_id,''),fixture_key) AS fixture_id FROM picks WHERE id IN (?1,?2,?3) AND pick_date=?4"
    ).bind(picks[0]?.pick_id ?? null,picks[1]?.pick_id ?? null,picks[2]?.pick_id ?? null,day)
      .all<{id:number;fixture_id:string}>();
    if (rows.length !== picks.length || new Set(picks.map(p=>p.pick_id)).size !== picks.length ||
        picks.some(p => !p.fixture_id || !p.market || !p.selection || !Number.isFinite(p.price) || p.price <= 1 ||
          !fixtures.some(f=>f.id===p.pick_id && f.fixture_id===p.fixture_id) ||
          !rows.some(r=>r.id===p.pick_id && r.market===p.market && r.selection===p.selection && r.price===p.price &&
            r.home===p.home && r.away===p.away && r.label===p.label && r.start_utc===p.kickoff_utc))) throw Error('membership');
    if (typeof media.jpeg !== 'string' || media.jpeg.length > 160000 || !/^\/9j\/[A-Za-z0-9+/=]+$/.test(media.jpeg)) throw Error('jpeg');
    const link = typeof media.link === 'string' && /^https:\/\/t\.me\/(?:\+[A-Za-z0-9_-]{8,}|joinchat\/[A-Za-z0-9_-]{8,}|[A-Za-z][A-Za-z0-9_]{4,31})$/.test(media.link) ? media.link : '';
    return {jpeg:media.jpeg,caption:`<b>BET OF THE DAY · ${day}</b>\nFootball. Smarter.\n\nBet only what you can afford to lose.${link ? `\n\n<a href="${escHtml(link)}">Join the Kovqi community</a>` : ''}`};
  } catch {
    console.warn('Kovqi image validation failed; using existing text ticket');
  }
}
