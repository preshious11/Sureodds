// Scheduled operations — LIVE in production since 2026-10-15
// (STAGE3_CRON_ENABLED = "1" and the four triggers below are attached in
// wrangler.toml; see its own header comment). Master-switched off only in
// an environment that omits STAGE3_CRON_ENABLED (e.g. a fresh/staging D1
// with no triggers configured yet).
//
// Division of labour (Lagos times preserved: POST 09:00, DELIVERY 09:30,
// RECAP 07:45):
//   Worker Cron (lightweight, no providers): delivery sweeps, update-retry
//     observability, missed-run detection. Never generates, never refetches.
//   GitHub Actions (heavy): generation + provider settlement (manual
//     dispatch until Stage 4; see .github/workflows/stage3-engine.yml).
//
// Idempotency: every send is fenced by the deliveries/posts receipts (INSERT
// OR IGNORE); a Cron that fires twice, late, or overlapping can only no-op.
// Telegram caps (~30 msgs/sec broadcast, 20/min/channel, 1/sec/chat) are
// enforced by the in-invocation token bucket plus Retry-After honouring.

import {
  claimPost,
  confirmPosted,
  deliveriesDone,
  getDayCache,
  getFailedUpdates,
  getJobLastRunAt,
  type MatchEventContext,
  type MatchEventRow,
  getRecapStatuses,
  getUndeliveredMatchEvents,
  hasPosted,
  liveMonitorWorkExists,
  listActiveSubscribers,
  markDelivery,
  markPosted,
  recordJobRun,
  releaseClaim,
  setJobResult,
  type Env,
} from "./db.ts";
import { lagosDateString, renderGroupCard, renderGroupRecap, renderRecapStats, renderView } from "./views.ts";
import { shiftDate, splitMessage } from "./format.ts";
import { allButtonModeFor, topEmptyKeyboard, menuKeyboard, isAdmin } from "./telegram.ts";
import { ticketPhoto, type TicketPhoto } from './ticket_image.ts';

export const BROADCAST_PER_SECOND = 25;

export function lagosMinutes(nowMs = Date.now()): number {
  const lagos = new Date(nowMs + 60 * 60_000); // UTC+1, no DST
  return lagos.getUTCHours() * 60 + lagos.getUTCMinutes();
}

export function parseHhmm(s: string): number {
  const m = /^(\d{1,2}):(\d{2})$/.exec((s ?? "").trim());
  if (!m) return 0;
  return Number(m[1]) * 60 + Number(m[2]);
}

export class TokenBucket {
  private tokens: number;
  private lastMs: number;
  private ratePerSec: number;
  private burst: number;
  constructor(ratePerSec: number, burst: number) {
    this.ratePerSec = ratePerSec;
    this.burst = burst;
    this.tokens = burst;
    this.lastMs = Date.now();
  }
  take(nowMs = Date.now()): boolean {
    const elapsed = Math.max(0, (nowMs - this.lastMs) / 1000);
    this.lastMs = nowMs;
    this.tokens = Math.min(this.burst, this.tokens + elapsed * this.ratePerSec);
    if (this.tokens < 1) return false;
    this.tokens -= 1;
    return true;
  }
}

async function sleep(ms: number): Promise<void> {
  await new Promise((r) => setTimeout(r, ms));
}

export interface SendResult {
  ok: boolean;
  messageId?: number;
}

export async function sendTelegram(
  env: Env,
  method: string,
  payload: Record<string, unknown>,
): Promise<SendResult> {
  // One Retry-After honour + one retry; a second 429 is recorded as failure
  // (receipt NOT written) so the next sweep retries — never a silent drop.
  for (let attempt = 0; attempt < 2; attempt++) {
    const form = method === 'sendPhoto' && typeof payload.photo_base64 === 'string' ? new FormData() : undefined;
    if (form) {
      const bytes = Uint8Array.from(atob(payload.photo_base64 as string), c=>c.charCodeAt(0));
      form.append('photo',new Blob([bytes],{type:'image/jpeg'}),'kovqi-bet-of-the-day.jpg');
      for (const [key,value] of Object.entries(payload)) if (key !== 'photo_base64') form.append(key,typeof value === 'object' ? JSON.stringify(value) : String(value));
    }
    const res = await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/${method}`, {
      method: "POST",
      headers: form ? undefined : { "content-type": "application/json" },
      body: form ?? JSON.stringify(payload),
    });
    if (res.status === 429) {
      let waitS = 2;
      try {
        const body = (await res.json()) as { parameters?: { retry_after?: number } };
        if (body.parameters && Number.isFinite(body.parameters.retry_after)) {
          waitS = Math.min(Number(body.parameters.retry_after), 60);
        }
      } catch {
        // keep default
      }
      await sleep(waitS * 1000);
      continue;
    }
    if (!res.ok) return { ok: false };
    try {
      const body = (await res.json()) as { ok?: boolean; result?: { message_id?: number } };
      const messageId = body.result?.message_id;
      if (body.ok === false) return {ok:false};
      if (method === 'sendPhoto' && (body.ok !== true || typeof messageId !== 'number')) throw Error('photo receipt unavailable');
      return { ok: true, messageId: typeof messageId === "number" ? messageId : undefined };
    } catch (error) {
      if (method === 'sendPhoto') throw error;
      return { ok: true };
    }
  }
  return { ok: false };
}

/**
 * Sends `text` as one or more Telegram messages (splitMessage, 4000-char
 * chunks) so a card that grows past the 4096-char API limit — a long
 * ticket, a busy results recap — fails fast with a visible error instead of
 * retrying forever the way one unsplit sendMessage call would. `replyMarkup`
 * is attached to the LAST chunk only (it refers to the whole card, which the
 * reader only has in full once every chunk has arrived); the FIRST chunk's
 * message_id is what gets stored in the posts receipt (confirmPosted), same
 * as a single-message send always did.
 *
 * All-or-nothing: if any chunk fails, this reports failure even though
 * earlier chunks already reached the chat — Telegram has no multi-message
 * atomicity, and a retry will resend every chunk including those earlier
 * ones. This only matters for a card that is both over the limit AND hits a
 * mid-send failure, which splitMessage's very existence should make rare.
 */
export async function sendSplit(
  env: Env,
  chatId: string,
  text: string,
  replyMarkup: Record<string, unknown> | undefined,
  sender: typeof sendTelegram,
): Promise<SendResult> {
  const chunks = splitMessage(text);
  let firstMessageId: number | undefined;
  for (let i = 0; i < chunks.length; i++) {
    const { ok, messageId } = await sender(env, "sendMessage", {
      chat_id: chatId,
      text: chunks[i],
      parse_mode: "HTML",
      ...(i === chunks.length - 1 && replyMarkup ? { reply_markup: replyMarkup } : {}),
    });
    if (!ok) return { ok: false };
    if (i === 0) firstMessageId = messageId;
  }
  return { ok: true, messageId: firstMessageId };
}

export interface SweepResult {
  sent: number;
  skipped: number;
  failed: number;
}

export async function sweepDeliveries(
  env: Env,
  kind: "picks" | "recap",
  nowMs = Date.now(),
  sender: typeof sendTelegram = sendTelegram,
): Promise<SweepResult> {
  const out: SweepResult = { sent: 0, skipped: 0, failed: 0 };
  // Gate on Lagos clock: no delivery before its time (DELIVERY 09:30 for
  // picks; recap follows RECAP 07:45). A late Cron still delivers — that IS
  // the missed-run recovery (receipts prevent duplicates).
  const gate = kind === "picks" ? parseHhmm(env.DELIVERY_TIME ?? "09:30") : parseHhmm(env.RECAP_TIME ?? "07:45");
  if (lagosMinutes(nowMs) < gate) {
    await setJobResult(env.DB, "deliver", `gated: Lagos time before ${kind} window`);
    return out;
  }
  const localDate = lagosDateString(nowMs);
  const timezone = env.TIMEZONE ?? "Africa/Lagos";
  const cache = await getDayCache(env.DB, localDate, timezone);
  if (!cache || cache.status !== "COMPLETE") {
    await setJobResult(env.DB, "deliver", `waiting: no COMPLETE cache for ${localDate}`);
    return out; // Generation (Actions) hasn't landed — never fabricate.
  }
  const done = await deliveriesDone(env.DB, kind, localDate);
  const bucket = new TokenBucket(BROADCAST_PER_SECOND, BROADCAST_PER_SECOND);
  const view = kind === "picks" ? "daily" : "stats";
  let offset = 0;
  for (;;) {
    const subs = await listActiveSubscribers(env.DB, 100, offset);
    if (subs.length === 0) break;
    offset += subs.length;
    for (const sub of subs) {
      if (sub.chat_id === env.TELEGRAM_CHAT_ID) continue; // WHY: legacy subscriber sweeps cannot publish group views.
      if (done.has(sub.chat_id)) {
        out.skipped += 1;
        continue;
      }
      if (!bucket.take()) {
        await sleep(1100);
      }
      const { text, replyMarkup } = await renderView(env, view, 0, localDate);
      const { ok } = await sendSplit(env, sub.chat_id, text, replyMarkup, sender);
      if (ok) {
        await markDelivery(env.DB, kind, localDate, sub.chat_id);
        out.sent += 1;
      } else {
        out.failed += 1;
      }
    }
    // Free-tier guard: max ~1k D1 rows read + bounded sends per invocation.
    if (offset >= 1000) break;
  }
  await setJobResult(env.DB, "deliver", `${kind} ${localDate}: sent=${out.sent} skipped=${out.skipped} failed=${out.failed}`);
  return out;
}

export type GroupPostOutcome = "sent" | "skipped" | "waiting" | "no-chat" | "failed";

/** The bot owner's private chat id — the first entry of TELEGRAM_ADMIN_IDS, same id /pinmenu's admin check uses. */
function ownerChatId(env: Env): string | null {
  const first = (env.TELEGRAM_ADMIN_IDS ?? "").split(",").map((s) => s.trim()).filter(Boolean)[0];
  return first || null;
}

/**
 * Records a failed send for ONE destination, separately from every other
 * destination: a structured log line (visible in `wrangler tail`) plus a
 * durable job_state row per destination, so "group failed, owner fine" is
 * distinguishable after the fact instead of one overwritten generic message.
 */
export async function recordDeliveryFailure(
  env: Env,
  destination: "group" | "owner",
  what: string,
): Promise<void> {
  console.error(JSON.stringify({ event: "delivery_failed", destination, what }));
  try {
    await setJobResult(env.DB, `post_${destination}`, `FAILED ${what}`);
  } catch {
    // observability must never break delivery
  }
  await setJobResult(env.DB, "post", `${what}: ${destination} send failed, will retry next sweep`);
}

/**
 * Sends the already-rendered group card to the owner's private chat too
 * (production repair, 2026-10-15: subscriber-DM broadcasts are
 * discontinued — every automated community message goes to exactly the
 * group AND the owner, never other subscribers). Uses its own
 * `${kind}_owner` receipt, completely independent of the group's: a
 * group-already-sent day still retries the owner leg if it previously
 * failed, and a failed owner send here never re-triggers or blocks the
 * group message, which has already been marked posted by the caller.
 */
async function postToOwner(
  env: Env,
  kind: string,
  localDate: string,
  text: string,
  replyMarkup: Record<string, unknown> | undefined,
  sender: typeof sendTelegram,
  photo?: TicketPhoto,
): Promise<void> {
  const owner = ownerChatId(env);
  if (!owner) return; // no admin configured — never a dead destination, just skipped
  const ownerKind = `${kind}_owner`;
  if (!(await claimPost(env.DB, ownerKind, localDate))) return;
  const { ok, messageId } = photo
    ? await sender(env,'sendPhoto',{chat_id:owner,photo_base64:photo.jpeg,caption:photo.caption,parse_mode:'HTML'})
    : await sendSplit(env, owner, text, replyMarkup ? menuKeyboard(isAdmin(env, owner) ? "admin" : "callback") : undefined, sender);
  if (!ok) {
    await recordDeliveryFailure(env, "owner", `${kind} ${localDate}`);
    await releaseClaim(env.DB, ownerKind, localDate);
    return;
  }
  await confirmPosted(env.DB, ownerKind, localDate, messageId != null ? String(messageId) : null);
}

/**
 * Posts Top/Daily Picks to the single configured Telegram group
 * (TELEGRAM_CHAT_ID) — the production model (predbot/jobs/daily_picks.py:
 * one group send, gated by a post_done receipt under a publication lock).
 * Individual-subscriber broadcasting is intentionally NOT used here: that
 * is sweepDeliveries(), which production keeps disabled via
 * SUBSCRIBER_DELIVERY_ENABLED=false and this Worker never calls from the
 * scheduler for the same reason (see handleScheduled).
 */
export async function postGroupPicks(
  env: Env,
  kind: "top" | "daily",
  nowMs = Date.now(),
  sender: typeof sendTelegram = sendTelegram,
): Promise<GroupPostOutcome> {
  // The owner leg is independent of the group leg (postToOwner below) — only bail
  // out here when there is truly nowhere to send, not just because the group
  // chat is unset (that used to also skip the owner, contradicting the design).
  if (!env.TELEGRAM_CHAT_ID && !ownerChatId(env)) return "no-chat";
  // Generation runs from POST_TIME (09:00); the published cards go out from
  // DELIVERY_TIME (09:30). A late sweep still delivers (receipts dedupe).
  const gate = parseHhmm(env.DELIVERY_TIME ?? "09:30");
  if (lagosMinutes(nowMs) < gate) return "waiting";
  const localDate = lagosDateString(nowMs);
  const timezone = env.TIMEZONE ?? "Africa/Lagos";
  const cache = await getDayCache(env.DB, localDate, timezone);
  if (!cache || cache.status !== "COMPLETE") {
    await setJobResult(env.DB, "post", `waiting: no COMPLETE cache for ${localDate}`);
    return "waiting"; // Generation (Actions) hasn't landed — never fabricate.
  }
  // Claim BEFORE sending: two overlapping Cron fires calling claimPost for
  // the same (kind, localDate) can never both win, since only one atomic
  // INSERT/DO UPDATE can report a changed row for the unique key — this
  // closes the read-then-send-then-write race a plain hasPosted() check
  // (read) followed by markPosted() (write) left open. The loser treats the
  // group as already being handled by the winner and only retries the
  // (independently-gated) owner leg. Nothing to claim when there is no group chat.
  const claimedGroup = env.TELEGRAM_CHAT_ID ? await claimPost(env.DB, kind, localDate) : false;
  // The WHOLE ticket in one message, never paginated (predbot/bot/
  // broadcast.py picks_messages/summary_card) — renderView()'s paginated
  // /top /daily output is for interactive replies, not this automatic post.
  const { text, isEmpty } = await renderGroupCard(env, kind, localDate);
  const photo = kind === 'top' && !isEmpty ? await ticketPhoto(env,localDate,cache.payload_json) : undefined;
  // predbot/jobs/daily_picks.py: markup is None for every automatic group
  // card EXCEPT the genuinely-empty Top card, which swaps in the Custom
  // Picks deep-link keyboard (top_empty_markup). Never shown without a
  // configured bot username (never a dead link). This card is always
  // posted to the group, so its All button is the private deep link too
  // (allButtonModeFor("group", ...)), never the in-group callback.
  const replyMarkup =
    kind === "top" && isEmpty && env.TELEGRAM_BOT_USERNAME
      ? topEmptyKeyboard(env.TELEGRAM_BOT_USERNAME, allButtonModeFor("group", env.TELEGRAM_BOT_USERNAME))
      : undefined;
  if (!env.TELEGRAM_CHAT_ID) {
    // No group configured: the owner leg still gets this card (independent destination).
    await postToOwner(env, kind, localDate, text, replyMarkup, sender, photo);
    return "no-chat";
  }
  if (!claimedGroup) {
    await postToOwner(env, kind, localDate, text, replyMarkup, sender, photo);
    return "skipped";
  }
  const { ok, messageId } = photo
    ? await sender(env,'sendPhoto',{chat_id:env.TELEGRAM_CHAT_ID,photo_base64:photo.jpeg,caption:photo.caption,parse_mode:'HTML'})
    : await sendSplit(env, env.TELEGRAM_CHAT_ID, text, replyMarkup, sender);
  if (!ok) {
    await recordDeliveryFailure(env, "group", `${kind} ${localDate}`);
    // Release the claim so the very next sweep retries immediately rather
    // than waiting out CLAIM_POST_TIMEOUT_SECONDS; a genuine crash (rather
    // than this clean failure return) is still covered by that timeout.
    await releaseClaim(env.DB, kind, localDate);
    return "failed";
  }
  await confirmPosted(env.DB, kind, localDate, messageId != null ? String(messageId) : null);
  await setJobResult(env.DB, "post", `${kind} ${localDate}: posted message=${messageId ?? "?"}`);
  await postToOwner(env, kind, localDate, text, replyMarkup, sender, photo);
  return "sent";
}

// Must stay inside CRON_RECAP_SWEEP's own window (07:00-08:55 Lagos) — a
// deadline outside that window would never actually get a sweep fire to act on it.
export const DEFAULT_RECAP_DEADLINE = "08:45";

/**
 * Sends yesterday's settled recap privately to the owner — production equivalent is
 * predbot/jobs/recap.py (single group send, post_done("recap", date) gate).
 */
export async function postGroupRecap(
  env: Env,
  nowMs = Date.now(),
  sender: typeof sendTelegram = sendTelegram,
): Promise<GroupPostOutcome> {
  if (!ownerChatId(env)) return "no-chat";
  const gate = parseHhmm(env.RECAP_TIME ?? "07:45");
  if (lagosMinutes(nowMs) < gate) return "waiting";
  const localDate = lagosDateString(nowMs);
  if (!(await hasPosted(env.DB, "recap", localDate))) {
    const yesterday = shiftDate(localDate, -1);
    const [topStatuses, dailyStatuses] = await Promise.all([
      getRecapStatuses(env.DB, yesterday, "top"),
      getRecapStatuses(env.DB, yesterday, "daily"),
    ]);
    if (topStatuses.length === 0 && dailyStatuses.length === 0) {
      // predbot/jobs/recap.py: no rows for yesterday -> skip entirely (never the
      // "No results yet" placeholder, which reads as a bug, not an empty slate).
      // Claim+confirm the receipt (a non-null sentinel, never NULL, so it can
      // never look like an abandoned in-flight claim) so every later sweep in
      // the window hits the hasPosted() short-circuit above instead of re-running
      // these two getRecapStatuses queries on every 5-minute tick until the deadline.
      await claimPost(env.DB, "recap", localDate);
      await confirmPosted(env.DB, "recap", localDate, "skipped-empty");
      await setJobResult(env.DB, "post", `recap ${localDate}: nothing to recap for ${yesterday}, marked handled`);
      return "skipped";
    }
    // Before the deadline, hold off if yesterday's Top/Daily legs are still
    // "pending" (settlement from the live monitor hasn't landed yet): the posts
    // receipt below would otherwise lock in an unfinished recap for the rest of
    // the day. At/after the deadline, fall through and post regardless, so the
    // recap is never skipped entirely.
    const deadline = parseHhmm(env.RECAP_DEADLINE ?? DEFAULT_RECAP_DEADLINE);
    if (lagosMinutes(nowMs) < deadline && [...topStatuses, ...dailyStatuses].some((s) => s === "pending")) {
      await setJobResult(env.DB, "post", `waiting for settlement: recap ${localDate}`);
      return "waiting";
    }
  }
  // WHY: results belong only in the owner's private chat; never publish a group recap.
  const owner = ownerChatId(env);
  if (!owner) return "no-chat";
  if (!(await claimPost(env.DB, "recap", localDate))) return "skipped";
  const recap = await renderGroupRecap(env, localDate);
  const stats = await renderRecapStats(env, localDate);
  const {ok,messageId} = await sendSplit(env, owner, `${recap.text}\n\n${stats}`, undefined, sender);
  if (!ok) {
    await releaseClaim(env.DB, "recap", localDate);
    await recordDeliveryFailure(env,"owner",`recap ${localDate}`);
    return "failed";
  }
  await confirmPosted(env.DB,"recap",localDate,messageId != null ? String(messageId) : "sent");
  await setJobResult(env.DB,"post",`recap ${localDate}: sent privately`);
  return "sent";
}

export async function reportFailedUpdates(env: Env): Promise<number> {
  const failed = await getFailedUpdates(env.DB, 50);
  if (failed.length > 0) {
    await setJobResult(
      env.DB,
      "deliver",
      `webhook retries pending: ${failed.length} update(s), oldest=${failed[0]?.update_id} attempts=${failed[0]?.attempts}`,
    );
  }
  // Redelivery arrives via Telegram webhook retries (tryClaimUpdate reaps
  // `failed` rows); the sweeper never fabricates or drops payloads.
  return failed.length;
}

// How far back to look for events each sweep. This bounds the query's read
// cost (match_events + its correlated posts lookups), not correctness:
// decideMatchEvent still decides whether a row inside the window is worth
// sending. 48h, not 30 days (see getUndeliveredMatchEvents in db.ts) - the
// tradeoff for keeping this one query cheap on a backlog.
const MATCH_EVENT_LOOKBACK_MS = 48 * 3_600_000;

// Six events clear 15 simultaneous kickoffs in three five-minute sweeps.
// Batch context/claims/cleanup and one ticket aggregate keep DELIVER <=40
// D1 queries even with six ready tickets and failed recovery dispatches.
const MATCH_EVENT_SWEEP_LIMIT = 6;

/** A kickoff alert is valid from kickoff through this many minutes afterward (inclusive), never earlier or later. */
export const DEFAULT_MATCH_START_WINDOW_MIN = 10;
export const DEFAULT_MATCH_END_WINDOW_HOURS = 24;
const NOT_PLAYED_STATUSES = new Set(["postponed", "cancelled", "canceled", "abandoned", "suspended"]);

function positiveNumber(raw: string | undefined, fallback: number): number {
  const n = Number(raw);
  return Number.isFinite(n) && n > 0 ? n : fallback;
}

export type MatchEventDecision =
  | { action: "send" }
  | { action: "wait"; reason: string } // not decidable yet; re-evaluated next sweep, never sent now
  | { action: "expire"; reason: string }; // permanently not worth sending; recorded so it is never retried

/**
 * Re-validates ONE persisted match event at dispatch time (a pure function of
 * persisted state, so it is re-run on every sweep and after any Worker
 * restart). The event row itself is never trusted to still be current.
 *
 *   start: only for a published Top/Daily fixture that has not finished, was
 *          not postponed/cancelled, and is inside the configurable
 *          window [kickoff, kickoff + window] (MATCH_START_WINDOW_MIN, default 10: kickoff through kickoff+10 min).
 *   end:   only for a published Top/Daily fixture whose completion AND
 *          settlement are confirmed (live_fixture_state final + settled),
 *          and still inside MATCH_END_WINDOW_HOURS of being recorded.
 */
export function decideMatchEvent(env: Env, evt: MatchEventRow, ctx: MatchEventContext, nowMs: number): MatchEventDecision {
  if (ctx.published_total <= 0) {
    // Not in a POSTED Top/Daily card (All-only, unpublished, or removed).
    return { action: "expire", reason: "fixture is not in a published Top/Daily selection" };
  }
  const status = (ctx.live_status ?? "").toLowerCase();
  if (evt.event_type === "start") {
    if (status === "final" || NOT_PLAYED_STATUSES.has(status)) {
      return { action: "expire", reason: `fixture is ${status}` };
    }
    if (ctx.published_open <= 0) return { action: "expire", reason: "no open published selection (all void)" };
    const kickoff = ctx.kickoff_utc ? Date.parse(ctx.kickoff_utc) : NaN;
    if (!Number.isFinite(kickoff)) return { action: "wait", reason: "kickoff time unknown" };
    if (nowMs < kickoff) return { action: "wait", reason: "before kickoff" };
    const windowMs = positiveNumber(env.MATCH_START_WINDOW_MIN, DEFAULT_MATCH_START_WINDOW_MIN) * 60_000;
    if (nowMs > kickoff + windowMs) return { action: "expire", reason: "kickoff window elapsed" };
    return { action: "send" };
  }
  // end
  if (NOT_PLAYED_STATUSES.has(status)) return { action: "expire", reason: `fixture is ${status}, not completed` };
  if (status !== "final") return { action: "wait", reason: "completion not confirmed" };
  if (!ctx.settlement_done) return { action: "wait", reason: "settlement not confirmed" };
  const created = Date.parse(evt.created_at ?? "");
  const windowMs = positiveNumber(env.MATCH_END_WINDOW_HOURS, DEFAULT_MATCH_END_WINDOW_HOURS) * 3_600_000;
  if (Number.isFinite(created) && nowMs > created + windowMs) return { action: "expire", reason: "result window elapsed" };
  return { action: "send" };
}

/**
 * Delivers persisted match-start/match-end events (written by the
 * GitHub Actions live-monitoring workflow into match_events,
 * 0009_match_events.sql) to the group AND the owner, independently.
 *
 * This Worker sweep is the ONLY thing that ever sends these to Telegram.
 * Before EVERY dispatch the event is re-validated against persisted
 * publication/fixture state (decideMatchEvent): a stale, ineligible or
 * out-of-window event is expired (receipt `evt_<fixture>_<type>_expired`),
 * never sent late. Duplicate protection is the posts receipts:
 * `evt_<fixture>_<type>` (group) and `..._owner` are independent
 * (kind, post_date) keys, so a fire that sends one leg but not the other
 * retries only the missing leg, and a repeat/restart is a safe no-op.
 */
export async function sweepMatchEvents(env: Env, nowMs = Date.now(), sender: typeof sendTelegram = sendTelegram, limit = MATCH_EVENT_SWEEP_LIMIT): Promise<number> {
  const since = new Date(nowMs - MATCH_EVENT_LOOKBACK_MS).toISOString();
  const owner = ownerChatId(env);
  const groupConfigured = !!env.TELEGRAM_CHAT_ID;
  const ownerConfigured = !!owner;
  // Receipt filtering and persisted fixture/publication context share one
  // batch query; no per-event context or receipt reads are needed.
  const events = await getUndeliveredMatchEvents(env.DB, since, groupConfigured, ownerConfigured, limit);
  // One atomic statement claims every destination in the batch. It has the
  // same stale-claim timeout and conflict predicate as claimPost.
  const claims: {kind:string;date:string;message:string|null}[]=[];
  const jobs=new Map<string,string>();
  const actionable=events.filter(evt=>{
    const decision=decideMatchEvent(env,evt,evt,nowMs);
    const base=`evt_${evt.fixture_key}_${evt.event_type}`;
    if(decision.action==='expire') {
      claims.push({kind:`${base}_expired`,date:evt.pick_date,message:'expired'});
      jobs.set('match_events',`expired ${evt.fixture_key}/${evt.event_type}: ${decision.reason}`);
      return false;
    }
    if(decision.action!=='send') return false;
    if(groupConfigured) claims.push({kind:base,date:evt.pick_date,message:null});
    if(ownerConfigured) claims.push({kind:`${base}_owner`,date:evt.pick_date,message:null});
    return true;
  });
  if(!claims.length) return 0;
  const {results:claimed}=await env.DB.prepare(`INSERT INTO posts(kind,post_date,message_id,created_at)
    SELECT json_extract(value,'$.kind'),json_extract(value,'$.date'),json_extract(value,'$.message'),
      strftime('%Y-%m-%dT%H:%M:%SZ','now') FROM json_each(?1) WHERE true
    ON CONFLICT(kind,post_date) DO UPDATE SET created_at=excluded.created_at,message_id=excluded.message_id
      WHERE posts.message_id IS NULL AND posts.created_at<strftime('%Y-%m-%dT%H:%M:%SZ','now','-300 seconds')
    RETURNING kind,post_date`).bind(JSON.stringify(claims)).all<{kind:string;post_date:string}>();
  const owned=new Set(claimed.map(r=>`${r.kind}|${r.post_date}`));
  const confirmed:{kind:string;date:string;message:string}[]=[];
  const failed:{kind:string;date:string}[]=[];
  const endAlerts=new Set<string>();
  const alerted=new Set<string>();
  let tickets:ResultTicket[]|undefined;
  let delivered=0;
  for(const evt of actionable) {
    const base=`evt_${evt.fixture_key}_${evt.event_type}`;
    for(const [destination,chat,kind] of [['group',env.TELEGRAM_CHAT_ID,base],['owner',owner,`${base}_owner`]] as const) {
      if(!chat || !owned.has(`${kind}|${evt.pick_date}`)) continue;
      const {ok,messageId}=await sendSplit(env,chat,evt.text,undefined,sender);
      if(!ok) {
        failed.push({kind,date:evt.pick_date});
        const error=`FAILED match event ${evt.fixture_key}/${evt.event_type}`;
        console.error(JSON.stringify({event:'delivery_failed',destination,error}));
        jobs.set(`post_${destination}`,error);jobs.set('post',error);
        continue;
      }
      delivered++;
      if(destination==='group' && evt.event_type==='end') {
        // Confirm the END before even checking its tickets. Other destination
        // confirmations can be batched without affecting this ordering.
        await confirmPosted(env.DB,kind,evt.pick_date,String(messageId??'sent'));
        endAlerts.add(`${evt.pick_date}|${evt.fixture_key}`);
        // One aggregate for all recent tickets, shared by every END in this
        // batch. Each event only considers tickets containing its fixture.
        // pending_ends also fences the POST backup until queued END alerts
        // are confirmed, including alerts outside this six-event batch.
        tickets??=await getResultTickets(env,nowMs,actionable.filter(e=>e.event_type==='end').map(e=>({date:e.pick_date,fixture:e.fixture_key})));
        await sendReadyResultGifs(env,nowMs,sender,tickets.filter(t=>t.post_date===evt.pick_date &&
          JSON.parse(t.fixtures).includes(evt.fixture_key)),endAlerts,alerted);
      } else confirmed.push({kind,date:evt.pick_date,message:String(messageId??'sent')});
    }
  }
  if(confirmed.length) await env.DB.prepare(`UPDATE posts SET message_id=(SELECT json_extract(value,'$.message')
    FROM json_each(?1) WHERE json_extract(value,'$.kind')=posts.kind AND json_extract(value,'$.date')=posts.post_date)
    WHERE EXISTS(SELECT 1 FROM json_each(?1) WHERE json_extract(value,'$.kind')=posts.kind
      AND json_extract(value,'$.date')=posts.post_date)`).bind(JSON.stringify(confirmed)).run();
  if(failed.length) await env.DB.prepare(`DELETE FROM posts WHERE message_id IS NULL AND EXISTS(
    SELECT 1 FROM json_each(?1) WHERE json_extract(value,'$.kind')=posts.kind AND json_extract(value,'$.date')=posts.post_date)`)
    .bind(JSON.stringify(failed)).run();
  if(jobs.size) await env.DB.prepare(`INSERT INTO job_state(name,last_result,last_run_at,updated_at)
    SELECT key,value,strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now') FROM json_each(?1) WHERE true
    ON CONFLICT(name) DO UPDATE SET last_result=excluded.last_result,last_run_at=excluded.last_run_at,updated_at=excluded.updated_at`)
    .bind(JSON.stringify(Object.fromEntries(jobs))).run();
  return delivered;
}

interface ResultTicket {
  kind:string;post_date:string;message_id:string;legs:number;pending:number;losses:number;wins:number;
  file_id:string|null;fixtures:string;pending_ends:string;
}
async function getResultTickets(env:Env,nowMs:number,fixtures?:{date:string;fixture:string}[]):Promise<ResultTicket[]> {
  if ((!env.TELEGRAM_CHAT_ID && !ownerChatId(env)) || !/^\d{4}-\d{2}-\d{2}$/.test(env.RESULT_GIFS_START_DATE??'')) return [];
  const day=lagosDateString(nowMs);
  // WHY: one aggregate covers all recent posted tickets, not one read per ticket or leg.
  const {results}=await env.DB.prepare(`WITH tickets AS (SELECT po.kind,po.post_date,po.message_id,COUNT(p.id) AS legs,
    json_group_array(DISTINCT p.fixture_key) AS fixtures,
    (SELECT json_group_array(me.pick_date||'|'||me.fixture_key) FROM match_events me
      WHERE me.pick_date=po.post_date AND me.event_type='end'
      AND EXISTS(SELECT 1 FROM picks ep JOIN prediction_memberships em ON em.prediction_id=ep.id
        WHERE ep.pick_date=po.post_date AND ep.fixture_key=me.fixture_key AND em.collection=replace(po.kind,'_owner',''))
      AND NOT EXISTS(SELECT 1 FROM posts er WHERE er.post_date=me.pick_date
        AND er.kind='evt_'||me.fixture_key||'_end' AND er.message_id IS NOT NULL)
      AND NOT EXISTS(SELECT 1 FROM posts expired WHERE expired.post_date=me.pick_date
        AND expired.kind='evt_'||me.fixture_key||'_end_expired')) AS pending_ends,
    SUM(CASE WHEN p.status NOT IN ('won','half_won','lost','half_lost','void','push','cancelled','canceled','abandoned') THEN 1 ELSE 0 END) AS pending,
    SUM(CASE WHEN p.status IN ('lost','half_lost') THEN 1 ELSE 0 END) AS losses,
    SUM(CASE WHEN p.status IN ('won','half_won') THEN 1 ELSE 0 END) AS wins,
    SUM(CASE WHEN p.status='won' THEN 1 ELSE 0 END) AS won_legs,
    SUM(CASE WHEN p.status='half_won' THEN 1 ELSE 0 END) AS half_won_legs,
    SUM(CASE WHEN p.status='lost' THEN 1 ELSE 0 END) AS lost_legs,
    SUM(CASE WHEN p.status='half_lost' THEN 1 ELSE 0 END) AS half_lost_legs,
    SUM(CASE WHEN p.status='push' THEN 1 ELSE 0 END) AS push_legs,
    SUM(CASE WHEN p.status='void' THEN 1 ELSE 0 END) AS void_legs,
    SUM(CASE WHEN p.status='pending' THEN 1 ELSE 0 END) AS pending_legs
    FROM posts po LEFT JOIN picks p ON p.pick_date=po.post_date AND EXISTS(
      SELECT 1 FROM prediction_memberships pm WHERE pm.prediction_id=p.id AND pm.collection=replace(po.kind,'_owner',''))
    WHERE po.kind IN ('top','daily','top_owner','daily_owner') AND po.message_id IS NOT NULL AND po.post_date>=?1 AND po.post_date>=?2
      AND po.post_date<=?3
      AND (?5 IS NULL OR EXISTS(SELECT 1 FROM picks fp JOIN prediction_memberships fm ON fm.prediction_id=fp.id
        JOIN json_each(?5) ef ON json_extract(ef.value,'$.date')=fp.pick_date AND json_extract(ef.value,'$.fixture')=fp.fixture_key
        WHERE fp.pick_date=po.post_date AND fm.collection=replace(po.kind,'_owner','')))
      AND NOT EXISTS(SELECT 1 FROM posts done WHERE done.kind='result_gif_'||po.kind
        AND done.post_date=po.post_date AND (done.message_id IS NOT NULL OR julianday(done.created_at)>julianday(?4,'-5 minutes')))
    GROUP BY po.kind,po.post_date,po.message_id)
    SELECT tickets.*, (SELECT file_id FROM result_gifs WHERE kind=CASE WHEN tickets.losses>0 THEN 'lost' ELSE 'won' END
      ORDER BY random() LIMIT 1) AS file_id FROM tickets ORDER BY post_date,kind`)
    .bind(shiftDate(day,-2),env.RESULT_GIFS_START_DATE,day,nowStr(nowMs),fixtures?JSON.stringify(fixtures):null)
    .all<ResultTicket>();
  return results;
}

async function sendReadyResultGifs(env:Env,nowMs:number,sender:typeof sendTelegram,tickets:ResultTicket[],
  endAlerts=new Set<string>(),alerted=new Set<string>()):Promise<void> {
  const day=lagosDateString(nowMs);
  const ready=tickets.filter(ticket=>!ticket.pending && /^\d+$/.test(ticket.message_id) &&
    JSON.parse(ticket.pending_ends).every((key:string)=>endAlerts.has(key)));
  for(const ticket of ready) {
    if (ticket.pending || !/^\d+$/.test(ticket.message_id)) continue;
    ticket.pending=1; // This batch attempts a ticket at most once, including clean send failures.
    const receipt=`result_gif_${ticket.kind}`;
    if (!(await claimPost(env.DB,receipt,ticket.post_date))) continue;
    const kind=ticket.losses?'lost':ticket.wins?'won':null;
    if (!kind) {await confirmPosted(env.DB,receipt,ticket.post_date,'all-void');continue;}
    if (!ticket.file_id) {
      console.warn(`No ${kind} GIFs registered`);
      await confirmPosted(env.DB,receipt,ticket.post_date,'no-gif');
      if (!alerted.has(kind)) {
        alerted.add(kind);
        await ownerNoticeOnce(env,`alert_no_${kind}_gifs`,day,`No ${kind} GIFs registered \u2014 use /addgif`,sender);
      }
      continue;
    }
    try {
      const {ok,messageId}=await sender(env,'sendAnimation',{chat_id:ticket.kind.endsWith('_owner')?ownerChatId(env):env.TELEGRAM_CHAT_ID,animation:ticket.file_id,
        caption:kind==='won'?'Won':'Lost',
        reply_parameters:{message_id:Number(ticket.message_id),allow_sending_without_reply:false}});
      if (!ok) await releaseClaim(env.DB,receipt,ticket.post_date);
      else await confirmPosted(env.DB,receipt,ticket.post_date,messageId!=null?String(messageId):'sent');
    } catch {await releaseClaim(env.DB,receipt,ticket.post_date);}
  }
}

export async function sweepResultGifs(env:Env,nowMs=Date.now(),sender:typeof sendTelegram=sendTelegram):Promise<void> {
  await sendReadyResultGifs(env,nowMs,sender,await getResultTickets(env,nowMs));
}

export async function detectMissedGeneration(
  env: Env,
  nowMs = Date.now(),
  sender: typeof sendTelegram = sendTelegram,
): Promise<boolean> {
  // Generation is Actions-owned (09:00 Lagos). If we are past delivery time
  // with no COMPLETE cache, record the miss for the operator — the Worker
  // must NOT generate (no providers, no CPU budget for it).
  const localDate = lagosDateString(nowMs);
  if (lagosMinutes(nowMs) < parseHhmm(env.DELIVERY_TIME ?? "09:30")) return false;
  const cache = await getDayCache(env.DB, localDate, env.TIMEZONE ?? "Africa/Lagos");
  const missed = !cache || cache.status !== "COMPLETE";
  if (missed) {
    await setJobResult(env.DB, "generate", `MISSED generation for ${localDate}: no COMPLETE cache by delivery time`);
    // A silent job_state row is easy to miss until someone checks the dashboard —
    // DM the owner once per day too, guarded by the same claim/confirm/release
    // receipts used everywhere else (CRON_WATCH fires once daily, but the claim
    // still protects against an overlapping/duplicate invocation).
    const owner = ownerChatId(env);
    if (owner && (await claimPost(env.DB, "alert_missed_generation", localDate))) {
      const { ok, messageId } = await sendSplit(
        env,
        owner,
        `⚠️ No predictions generated for ${localDate} by delivery time (${env.DELIVERY_TIME ?? "09:30"} Lagos).`,
        undefined,
        sender,
      );
      if (ok) {
        await confirmPosted(env.DB, "alert_missed_generation", localDate, messageId != null ? String(messageId) : null);
      } else {
        await releaseClaim(env.DB, "alert_missed_generation", localDate);
      }
    }
  }
  return missed;
}

/**
 * Daily housekeeping (CRON_WATCH, once a day): deletes rows that have
 * genuinely finished their job and only cost read budget by sticking
 * around — never anything a later sweep could still need. One DELETE per
 * table (8 total), well inside the per-invocation query budget.
 *   - telegram_updates: 'done' rows older than 7 days (dedupe only needs
 *     recent history; MAX_UPDATE_ATTEMPTS-exhausted/failed rows are left
 *     alone — those are still operator-visible signal, not dedupe noise).
 *   - custom_suggestions: past their own expires_at (the TTL a button tap
 *     can redeem is already over).
 *   - custom_sessions: untouched (updated_at) for 2+ days — an abandoned
 *     Custom Picks session or conversation refinement, not a live one.
 *   - match_events: older than 30 days (matches the sweep's own lookback
 *     intent; decideMatchEvent's own windows mean nothing this old was
 *     ever going to be delivered anyway).
 */
export async function cleanupOldRows(env: Env, nowMs = Date.now()): Promise<void> {
  const cutoff7d = new Date(nowMs - 7 * 24 * 3_600_000).toISOString();
  const cutoff2d = new Date(nowMs - 2 * 24 * 3_600_000).toISOString();
  const cutoff3Dates = shiftDate(lagosDateString(nowMs), -3);
  const cutoff7Dates = shiftDate(lagosDateString(nowMs), -7);
  const cutoff30d = new Date(nowMs - 30 * 24 * 3_600_000).toISOString();
  await env.DB.prepare("DELETE FROM telegram_updates WHERE status = 'done' AND updated_at < ?1").bind(cutoff7d).run();
  await env.DB.prepare("DELETE FROM custom_suggestions WHERE expires_at < ?1").bind(nowStr(nowMs)).run();
  await env.DB.prepare("DELETE FROM custom_sessions WHERE updated_at < ?1").bind(cutoff2d).run();
  await env.DB.prepare("DELETE FROM match_events WHERE created_at < ?1").bind(cutoff30d).run();
  // WHY: expired candidate archives dominate storage; permanent picks/learning data are untouched.
  await env.DB.prepare("DELETE FROM ticket_candidates WHERE pick_date < ?1").bind(cutoff3Dates).run();
  await env.DB.prepare("DELETE FROM custom_pick_candidates WHERE prediction_date < ?1").bind(cutoff3Dates).run();
  await env.DB.prepare("DELETE FROM custom_pick_pools WHERE prediction_date < ?1").bind(cutoff3Dates).run();
  // Provider cache is date-scoped and expires at midnight; never needed after seven days.
  await env.DB.prepare("DELETE FROM provider_cache WHERE local_date < ?1 AND expires_at < ?2").bind(cutoff7Dates, nowStr(nowMs)).run();
}

function nowStr(nowMs: number): string {
  return new Date(nowMs).toISOString();
}

export const DEFAULT_LIVE_MONITOR_INTERVAL_MIN = 10;

/**
 * Dispatches stage8-live-monitor-production.yml on demand instead of it
 * running on its own 5-minute schedule 288x/day even at night with nothing to
 * track (bug 6/A3) — this sweep decides whether there is any live-tracking
 * work right now (liveMonitorWorkExists: a 'pending' published Top/Daily
 * pick due to kick off soon or already underway, or any live_fixture_state
 * row not yet fully processed), and if so, dispatches the workflow via the
 * GitHub REST API, throttled to at most once per LIVE_MONITOR_INTERVAL_MIN.
 *
 * If GITHUB_REPO/GITHUB_DISPATCH_TOKEN aren't configured while real work
 * exists, the owner is DMed once per Lagos day (claimPost-guarded, same
 * pattern as every other once-a-day alert here) rather than this silently
 * never tracking anything.
 */
export async function maybeDispatchLiveMonitor(
  env: Env,
  nowMs = Date.now(),
  fetchImpl: typeof fetch = fetch,
  sender: typeof sendTelegram = sendTelegram,
): Promise<void> {
  const since = nowStr(nowMs - 4 * 3_600_000);
  const until = nowStr(nowMs + 10 * 60_000);
  if (!(await liveMonitorWorkExists(env.DB, since, until))) return;

  if (!env.GITHUB_REPO || !env.GITHUB_DISPATCH_TOKEN) {
    const owner = ownerChatId(env);
    if (!owner) return;
    const localDate = lagosDateString(nowMs);
    const kind = "alert_livemon_not_configured";
    if (!(await claimPost(env.DB, kind, localDate))) return;
    const { ok, messageId } = await sendSplit(
      env,
      owner,
      "⚠️ Live match monitoring has work to do but GITHUB_REPO/GITHUB_DISPATCH_TOKEN are not configured — match-start/end tracking will not update until this is set.",
      undefined,
      sender,
    );
    if (ok) {
      await confirmPosted(env.DB, kind, localDate, messageId != null ? String(messageId) : null);
    } else {
      await releaseClaim(env.DB, kind, localDate);
    }
    return;
  }

  const intervalMs = positiveNumber(env.LIVE_MONITOR_INTERVAL_MIN, DEFAULT_LIVE_MONITOR_INTERVAL_MIN) * 60_000;
  const lastRunAt = await getJobLastRunAt(env.DB, "livemon_dispatch");
  const lastMs = lastRunAt ? Date.parse(lastRunAt) : NaN;
  if (Number.isFinite(lastMs) && nowMs - lastMs < intervalMs) return;

  await dispatchWorkflow(env, "stage8-live-monitor-production.yml", "livemon_dispatch",
    { dry_run: "false" }, nowMs, fetchImpl, sender);
}

// WHY: GitHub rejection/transport failures must be visible without leaking tokens or response bodies.
async function ownerNoticeOnce(env: Env, kind: string, day: string, text: string, sender: typeof sendTelegram): Promise<void> {
  const owner = ownerChatId(env);
  if (!owner || !(await claimPost(env.DB, kind, day))) return;
  try {
    await sender(env, "sendMessage", { chat_id: owner, text }); // plain text
  } catch {
    console.error("Owner notice could not be sent", kind);
  } finally {
    // At most one attempt/day, even if Telegram fails or returns no message id.
    await confirmPosted(env.DB, kind, day, "attempted");
  }
}

export async function dispatchWorkflow(
  env: Env, workflow: string, job: string, inputs: Record<string, string>, nowMs: number,
  fetchImpl: typeof fetch = fetch, sender: typeof sendTelegram = sendTelegram,
): Promise<void> {
  let status = "network error";
  let ok = false;
  try {
    if (!env.GITHUB_REPO || !env.GITHUB_DISPATCH_TOKEN) {
      status = "not configured";
    } else {
      const res = await fetchImpl(`https://api.github.com/repos/${env.GITHUB_REPO}/actions/workflows/${workflow}/dispatches`, {
        method: "POST",
        headers: { Authorization: `Bearer ${env.GITHUB_DISPATCH_TOKEN}`, Accept: "application/vnd.github+json",
          "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "sureodds-worker", "content-type": "application/json" },
        body: JSON.stringify({ ref: env.GITHUB_REF ?? "main", inputs }),
      });
      status = String(res.status);
      ok = res.ok;
    }
  } catch {
    // Exceptions may contain request headers/URLs; record only a safe classification.
  }
  await recordJobRun(env.DB, job, nowMs, ok ? "OK dispatched" : `FAILED dispatch returned ${status}`);
  if (!ok) {
    const hint = status === "401" || status === "403"
      ? " The GitHub token (GITHUB_DISPATCH_TOKEN) may have expired \u2014 create a new one and update the Cloudflare secret." : "";
    await ownerNoticeOnce(env, `alert_dispatch_${workflow}`, lagosDateString(nowMs),
      `\u26a0\ufe0f ${workflow} dispatch failed: HTTP ${status}.${hint}`, sender);
  }
}

export async function maybeDispatchGeneration(
  env: Env, nowMs = Date.now(), fetchImpl: typeof fetch = fetch, sender: typeof sendTelegram = sendTelegram,
): Promise<void> {
  const minute = lagosMinutes(nowMs);
  if (minute < 9 * 60 + 20 || minute >= 12 * 60) return;
  const day = lagosDateString(nowMs);
  const cache = await getDayCache(env.DB, day, env.TIMEZONE ?? "Africa/Lagos");
  if (cache?.status === "COMPLETE") return;
  const max = Math.floor(positiveNumber(env.GENERATION_MAX_DISPATCHES_PER_DAY, 5));
  // WHY: reserve the count AND timestamp atomically before HTTP, including failed dispatches.
  // Neither overlapping cron invocations nor the GitHub generate lease may bypass these guards.
  const reserved = await env.DB.prepare(`UPDATE job_state SET
      attempts = CASE WHEN due_at = ?1 THEN attempts + 1 ELSE 1 END,
      due_at = ?1, last_run_at = ?2, updated_at = ?2
    WHERE name = 'generation_dispatch'
      AND (due_at IS NULL OR due_at <> ?1 OR attempts < ?3)
      AND (last_run_at IS NULL OR julianday(last_run_at) <= julianday(?2, '-30 minutes'))
      AND NOT EXISTS (SELECT 1 FROM job_state WHERE name = 'generate'
        AND lease_owner IS NOT NULL AND julianday(lease_until) > julianday(?2))`)
    .bind(day, nowStr(nowMs), max).run();
  if ((reserved.meta?.changes ?? 0) > 0) {
    await dispatchWorkflow(env, "stage5-acceptance-production.yml", "generation_dispatch", {
      operation: "live-generate", dry_run: "false", live_confirm: "RUN-LIVE",
      allow_telegram_posts: "false", write_budget: "40000", destructive_budget: "2000",
    }, nowMs, fetchImpl, sender);
    return;
  }
  const state = await env.DB.prepare("SELECT due_at, attempts FROM job_state WHERE name = 'generation_dispatch'")
    .first<{ due_at: string; attempts: number }>();
  if (state?.due_at === day && state.attempts >= max) {
    await ownerNoticeOnce(env, "alert_generation_cap", day,
      `\u26a0\ufe0f Generation retry limit reached (${state.attempts}/${max}) for ${day}; no COMPLETE picks are available.`, sender);
  }
}

export async function maybeDispatchSettlement(
  env: Env, nowMs = Date.now(), fetchImpl: typeof fetch = fetch, sender: typeof sendTelegram = sendTelegram,
): Promise<void> {
  const minute = lagosMinutes(nowMs);
  if (minute < 6 * 60 + 15 || minute >= 7 * 60 + 30) return;
  const day = lagosDateString(nowMs);
  // WHY: one durable dispatch receipt per Lagos day, even when GitHub fails or cron overlaps.
  const claim = await env.DB.prepare(`INSERT OR IGNORE INTO posts (kind, post_date, message_id, created_at)
    SELECT 'dispatch_settlement', ?1, 'attempted', ?2
    WHERE NOT EXISTS (SELECT 1 FROM job_state WHERE name = 'settle'
      AND last_result LIKE 'OK %mode=live%'
      AND date(last_run_at, '+1 hour') = ?1)`)
    .bind(day, nowStr(nowMs)).run();
  if ((claim.meta?.changes ?? 0) === 0) return;
  await dispatchWorkflow(env, "stage9-settle-production.yml", "settlement_dispatch", { dry_run: "false" },
    nowMs, fetchImpl, sender);
}

export async function dailyHealthReport(env: Env, nowMs = Date.now(), sender: typeof sendTelegram = sendTelegram): Promise<void> {
  if (!ownerChatId(env)) return;
  const day = lagosDateString(nowMs);
  // WHY: one bounded read per data source, instead of a query per receipt/table row.
  const { results: receipts } = await env.DB.prepare(
    "SELECT kind, message_id, created_at FROM posts WHERE post_date = ?1 AND kind IN ('top','daily','recap')")
    .bind(day).all<{ kind: string; message_id: string | null; created_at: string }>();
  const { results: jobs } = await env.DB.prepare(
    "SELECT name, last_result, last_run_at, due_at, attempts FROM job_state WHERE name IN ('generate','settle','livemon_dispatch','generation_dispatch')")
    .all<{ name: string; last_result: string | null; last_run_at: string | null; due_at: string | null; attempts: number }>();
  const cache = await getDayCache(env.DB, day, env.TIMEZONE ?? "Africa/Lagos");
  const pending = await env.DB.prepare(`SELECT COUNT(*) AS n FROM picks p WHERE pick_date = ?1 AND status = 'pending'
    AND EXISTS (SELECT 1 FROM prediction_memberships pm WHERE pm.prediction_id = p.id AND pm.collection IN ('top','daily'))`)
    .bind(shiftDate(day, -1)).first<{ n: number }>();
  const counts = await env.DB.prepare(`SELECT (SELECT COUNT(*) FROM picks) AS picks,
    (SELECT COUNT(*) FROM ticket_candidates) AS ticket_candidates,
    (SELECT COUNT(*) FROM custom_pick_candidates) AS custom_pick_candidates,
    (SELECT COUNT(*) FROM telegram_updates) AS telegram_updates`).first<Record<string, number>>();
  const posted = (kind: string) => receipts.find((r) => r.kind === kind && r.message_id != null);
  const job = (name: string) => jobs.find((r) => r.name === name);
  const summary = (name: string) => `${job(name)?.last_result ?? 'never run'} @ ${job(name)?.last_run_at ?? '?'}`;
  const problems: string[] = [];
  if (!posted('top') || !posted('daily')) problems.push('picks not fully posted');
  if (!posted('recap')) problems.push('recap not posted');
  if (pending?.n) problems.push(`${pending.n} pending legs from yesterday`);
  for (const name of ['settle', 'livemon_dispatch']) {
    if (job(name)?.last_result?.startsWith('FAILED')) problems.push(`${name} failed`);
  }
  const settle = job('settle');
  if (!settle?.last_result?.includes('mode=live') || !settle.last_result.startsWith('OK ')
      || !settle.last_run_at || lagosDateString(Date.parse(settle.last_run_at)) !== day) {
    problems.push('no successful live settlement today');
  }
  if (cache?.status !== 'COMPLETE') problems.push('no COMPLETE cache today');
  let fallback = false;
  let generation: Record<string, any> = {};
  try {
    const payload = JSON.parse(cache?.payload_json ?? '{}');
    generation = payload.generation ?? payload.diagnostics ?? {};
    fallback = !!generation.provider_coverage?.fallback_used;
  } catch { problems.push('invalid cache payload'); }
  if (fallback) problems.push('backup odds providers used');
  // WHY: distinguish daily/cycle usage from provider-reported monthly quotas.
  const providerLines = ['sharpapi', 'oddsapi', 'sportsgameodds'].map((name) => {
    const report = generation.providers?.[name] ?? {};
    const usage = generation.api_usage?.[name] ?? {};
    const quota = report.quota ?? {};
    const status = report.status ?? 'not reported';
    if (status !== 'complete') problems.push(`${name} ${status}`);
    const readings: string[] = [];
    const addUsage = (used: unknown, limit: unknown, period: string) => {
      if (typeof used !== 'number' || !Number.isFinite(used)) return;
      const bounded = typeof limit === 'number' && Number.isFinite(limit) && limit > 0;
      readings.push(`${period} ${used}/${bounded ? limit : '?'}`);
      if (bounded && used >= limit * 0.8) problems.push(`${name} ${period} usage >=80%`);
    };
    addUsage(usage.today, usage.limit, 'today');
    addUsage(report.requests?.spent, report.requests?.budget, 'cycle');
    addUsage(quota.monthly_entities_used, quota.monthly_entities_max, 'monthly objects');
    if (typeof quota.requests_used === 'number' && typeof quota.requests_remaining === 'number') {
      addUsage(quota.requests_used, quota.requests_used + quota.requests_remaining, 'monthly credits');
    }
    return `${name}: ${status}; ${readings.join(', ') || 'usage not recorded'}.`;
  });
  const receiptTime = (kind: string) => posted(kind)?.created_at ?? 'not posted';
  const text = [problems.length ? `\u26a0\ufe0f Needs attention: ${problems.join('; ')}` : '\u2705 All good',
    `${day} (Lagos)`,
    `Picks: Top ${receiptTime('top')}; Daily ${receiptTime('daily')}; fallback=${fallback}.`,
    ...(!posted('top') || !posted('daily') ? [`Generation: ${summary('generate')}`] : []),
    `Recap: ${receiptTime('recap')}; yesterday pending=${pending?.n ?? 0}.`,
    `Settlement: ${summary('settle')}`, `Live monitor: ${summary('livemon_dispatch')}`,
    `Generation dispatches: ${job('generation_dispatch')?.due_at === day ? job('generation_dispatch')?.attempts : 0}/${env.GENERATION_MAX_DISPATCHES_PER_DAY ?? '5'}.`,
    ...providerLines,
    `Rows: ${Object.entries(counts ?? {}).map(([k,v]) => `${k}=${v}`).join(', ')}.`].join('\n');
  await ownerNoticeOnce(env, "daily_health", day, text, sender);
}

// Cloudflare passes the exact matched Cron Trigger pattern as `event.cron` —
// plain cron syntax (e.g. "*/5 8-22 * * *"), never a descriptive word. A
// previous version of this dispatcher matched on `cron.includes("post")` /
// `"recap"` / `"deliver"`, which can never match a real trigger string once
// wrangler.staging.toml's `[[triggers.crons]]` is a list of several plain
// patterns — every scheduled invocation would have silently no-op'd. Match
// on the exact pattern instead, via the constants wrangler.staging.toml's
// comment must use verbatim.
//
// POST/RECAP are READINESS SWEEPS, not one-shot fires: generation (GitHub
// Actions, stage6-scheduled-staging.yml) and this Worker run on separate
// infrastructure with no shared lock, so a single cron fire at exactly
// 09:00 Lagos cannot be trusted to find a COMPLETE cache — a slow or
// retried generation run would otherwise mean NOTHING ever posts that day
// (one-shot, no second chance). Instead the Cron fires every 5 minutes
// across a multi-hour window; postGroupPicks/postGroupRecap re-check
// readiness (COMPLETE cache) and their own post_date receipt on every fire,
// so repeated fires before readiness are safe no-ops ("waiting") and
// repeated fires after the first successful send are safe no-ops
// ("skipped") — across the whole window, each kind posts at most once.
//
// The window is wide (09:00-23:55 Lagos, not just 09:00-12:55): a manual
// or retried GitHub Actions generation run has landed as late as 13:55
// Lagos in production (2026-10-16), well past the old 12:55 cutoff, and
// that cutoff meant NOTHING posted automatically that day even though
// generation had actually succeeded - the only recovery was a manual
// trigger. A same-day COMPLETE cache, however late, now always gets
// picked up by the next 5-minute fire instead of requiring one.
export const CRON_POST_SWEEP = "*/5 8-22 * * *"; // 08:00-22:55 UTC = 09:00-23:55 Lagos
export const CRON_RECAP_SWEEP = "*/5 6-7 * * *"; // 06:00-07:55 UTC = 07:00-08:55 Lagos
export const CRON_DELIVER_SWEEP = "*/5 * * * *"; // webhook-retry observability + match-event delivery, any time
export const CRON_WATCH = "0 10 * * *"; // 11:00 Lagos: log a miss if generation never landed

export async function handleScheduled(env: Env, cron: string, nowMs = Date.now()): Promise<void> {
  // Ships OFF: enable by setting STAGE3_CRON_ENABLED=1 in staging only.
  // No triggers are attached in wrangler.toml yet (Stage 4).
  if ((env as Record<string, unknown>).STAGE3_CRON_ENABLED !== "1") return;
  if (cron === CRON_POST_SWEEP) {
    // Group-only (production model). Never calls sweepDeliveries here —
    // individual-subscriber broadcasting stays disabled, matching
    // production's SUBSCRIBER_DELIVERY_ENABLED=false.
    await postGroupPicks(env, "top", nowMs);
    await postGroupPicks(env, "daily", nowMs);
    await sweepResultGifs(env, nowMs);
  } else if (cron === CRON_DELIVER_SWEEP) {
    await reportFailedUpdates(env);
    // Match-start/match-end events: written by GitHub Actions, delivered
    // only here (group + owner, independent receipts — see sweepMatchEvents).
    await sweepMatchEvents(env, nowMs);
    // Dispatches the live-monitoring workflow on demand instead of it
    // running unconditionally every 5 minutes (bug 6/A3).
    await maybeDispatchGeneration(env, nowMs);
    await maybeDispatchSettlement(env, nowMs);
    await maybeDispatchLiveMonitor(env, nowMs);
  } else if (cron === CRON_RECAP_SWEEP) {
    await postGroupRecap(env, nowMs);
  } else if (cron === CRON_WATCH) {
    await detectMissedGeneration(env, nowMs);
    await cleanupOldRows(env, nowMs);
    await dailyHealthReport(env, nowMs);
  }
}
