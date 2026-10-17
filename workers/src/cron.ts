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
  getMatchEventContext,
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
import { lagosDateString, renderGroupCard, renderGroupRecap, renderView } from "./views.ts";
import { shiftDate, splitMessage } from "./format.ts";
import { allButtonModeFor, topEmptyKeyboard } from "./telegram.ts";

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
    const res = await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/${method}`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(payload),
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
      const body = (await res.json()) as { result?: { message_id?: number } };
      const messageId = body.result?.message_id;
      return { ok: true, messageId: typeof messageId === "number" ? messageId : undefined };
    } catch {
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
): Promise<void> {
  const owner = ownerChatId(env);
  if (!owner) return; // no admin configured — never a dead destination, just skipped
  const ownerKind = `${kind}_owner`;
  if (!(await claimPost(env.DB, ownerKind, localDate))) return;
  const { ok, messageId } = await sendSplit(env, owner, text, replyMarkup, sender);
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
    await postToOwner(env, kind, localDate, text, replyMarkup, sender);
    return "no-chat";
  }
  if (!claimedGroup) {
    await postToOwner(env, kind, localDate, text, replyMarkup, sender);
    return "skipped";
  }
  const { ok, messageId } = await sendSplit(env, env.TELEGRAM_CHAT_ID, text, replyMarkup, sender);
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
  await postToOwner(env, kind, localDate, text, replyMarkup, sender);
  return "sent";
}

// Must stay inside CRON_RECAP_SWEEP's own window (07:00-08:55 Lagos) — a
// deadline outside that window would never actually get a sweep fire to act on it.
export const DEFAULT_RECAP_DEADLINE = "08:45";

/**
 * Posts yesterday's settled recap to the group — production equivalent is
 * predbot/jobs/recap.py (single group send, post_done("recap", date) gate).
 */
export async function postGroupRecap(
  env: Env,
  nowMs = Date.now(),
  sender: typeof sendTelegram = sendTelegram,
): Promise<GroupPostOutcome> {
  if (!env.TELEGRAM_CHAT_ID && !ownerChatId(env)) return "no-chat";
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
  const claimedGroup = env.TELEGRAM_CHAT_ID ? await claimPost(env.DB, "recap", localDate) : false;
  // predbot/jobs/recap.py: tg.send(text) — no keyboard on the automatic
  // group recap, same as the picks cards above. renderGroupRecap (not the
  // generic renderView) titles this "📊 RESULTS", matching recap_message()
  // exactly — distinct from the interactive /yesterday button's own title.
  const { text } = await renderGroupRecap(env, localDate);
  if (!env.TELEGRAM_CHAT_ID) {
    // No group configured: the owner leg still gets the recap (independent destination).
    await postToOwner(env, "recap", localDate, text, undefined, sender);
    return "no-chat";
  }
  if (!claimedGroup) {
    await postToOwner(env, "recap", localDate, text, undefined, sender);
    return "skipped";
  }
  const { ok, messageId } = await sendSplit(env, env.TELEGRAM_CHAT_ID, text, undefined, sender);
  if (!ok) {
    await recordDeliveryFailure(env, "group", `recap ${localDate}`);
    await releaseClaim(env.DB, "recap", localDate);
    return "failed";
  }
  await confirmPosted(env.DB, "recap", localDate, messageId != null ? String(messageId) : null);
  await setJobResult(env.DB, "post", `recap ${localDate}: posted message=${messageId ?? "?"}`);
  await postToOwner(env, "recap", localDate, text, undefined, sender);
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

// Caps the per-sweep cost regardless of how large the undelivered backlog
// is: getUndeliveredMatchEvents + per-event (context + up to 2 claim/
// confirm pairs) must stay well inside the 50-queries-per-invocation free-
// tier limit even in the worst case (see tests/query_budget.test.mjs).
const MATCH_EVENT_SWEEP_LIMIT = 5;

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
export async function sweepMatchEvents(env: Env, nowMs = Date.now(), sender: typeof sendTelegram = sendTelegram): Promise<number> {
  const since = new Date(nowMs - MATCH_EVENT_LOOKBACK_MS).toISOString();
  const owner = ownerChatId(env);
  const groupConfigured = !!env.TELEGRAM_CHAT_ID;
  const ownerConfigured = !!owner;
  // The receipt filtering (expired + per-leg "still outstanding") now happens
  // INSIDE this one query (see getUndeliveredMatchEvents) instead of a
  // hasPosted() read per event per leg out here — that per-event pattern is
  // exactly what exceeded the D1 query budget on a backlog.
  const events = await getUndeliveredMatchEvents(env.DB, since, groupConfigured, ownerConfigured, MATCH_EVENT_SWEEP_LIMIT);
  let delivered = 0;
  for (const evt of events) {
    const eventKey = `evt_${evt.fixture_key}_${evt.event_type}`;
    const groupKind = eventKey;
    const ownerKind = `${eventKey}_owner`;
    const expiredKind = `${eventKey}_expired`;

    const ctx = await getMatchEventContext(env.DB, evt.pick_date, evt.fixture_key);
    const decision = decideMatchEvent(env, evt, ctx, nowMs);
    if (decision.action === "wait") continue;
    if (decision.action === "expire") {
      await markPosted(env.DB, expiredKind, evt.pick_date, null);
      console.warn(JSON.stringify({ event: "match_event_expired", fixture: evt.fixture_key, type: evt.event_type, reason: decision.reason }));
      await setJobResult(env.DB, "match_events", `expired ${evt.fixture_key}/${evt.event_type}: ${decision.reason}`);
      continue;
    }

    // claimPost is what actually prevents two overlapping sweeps from both
    // sending the same leg; a leg the fetch query already knows is done for
    // this event (not configured, or already delivered) is skipped here too,
    // so an event returned only because its OTHER leg is outstanding never
    // re-attempts a leg that is already finished.
    if (groupConfigured && (await claimPost(env.DB, groupKind, evt.pick_date))) {
      const { ok, messageId } = await sendSplit(env, env.TELEGRAM_CHAT_ID as string, evt.text, undefined, sender);
      if (ok) {
        await confirmPosted(env.DB, groupKind, evt.pick_date, messageId != null ? String(messageId) : null);
        delivered += 1;
      } else {
        await recordDeliveryFailure(env, "group", `match event ${evt.fixture_key}/${evt.event_type}`);
        await releaseClaim(env.DB, groupKind, evt.pick_date);
      }
    }
    if (ownerConfigured && (await claimPost(env.DB, ownerKind, evt.pick_date))) {
      const { ok, messageId } = await sendSplit(env, owner as string, evt.text, undefined, sender);
      if (ok) {
        await confirmPosted(env.DB, ownerKind, evt.pick_date, messageId != null ? String(messageId) : null);
        delivered += 1;
      } else {
        await releaseClaim(env.DB, ownerKind, evt.pick_date);
        await recordDeliveryFailure(env, "owner", `match event ${evt.fixture_key}/${evt.event_type}`);
      }
    }
  }
  return delivered;
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
 * table (4 total), well inside the per-invocation query budget.
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
  const cutoff30d = new Date(nowMs - 30 * 24 * 3_600_000).toISOString();
  await env.DB.prepare("DELETE FROM telegram_updates WHERE status = 'done' AND updated_at < ?1").bind(cutoff7d).run();
  await env.DB.prepare("DELETE FROM custom_suggestions WHERE expires_at < ?1").bind(nowStr(nowMs)).run();
  await env.DB.prepare("DELETE FROM custom_sessions WHERE updated_at < ?1").bind(cutoff2d).run();
  await env.DB.prepare("DELETE FROM match_events WHERE created_at < ?1").bind(cutoff30d).run();
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

  try {
    const res = await fetchImpl(
      `https://api.github.com/repos/${env.GITHUB_REPO}/actions/workflows/stage8-live-monitor-production.yml/dispatches`,
      {
        method: "POST",
        headers: {
          Authorization: `Bearer ${env.GITHUB_DISPATCH_TOKEN}`,
          Accept: "application/vnd.github+json",
          "X-GitHub-Api-Version": "2022-11-28",
          "User-Agent": "sureodds-worker",
          "content-type": "application/json",
        },
        body: JSON.stringify({ ref: env.GITHUB_REF ?? "main", inputs: { dry_run: "false" } }),
      },
    );
    await recordJobRun(env.DB, "livemon_dispatch", nowMs, res.ok ? "OK dispatched" : `FAILED dispatch returned ${res.status}`);
  } catch (err) {
    await recordJobRun(env.DB, "livemon_dispatch", nowMs, `FAILED ${err instanceof Error ? err.message : String(err)}`);
  }
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
  } else if (cron === CRON_DELIVER_SWEEP) {
    await reportFailedUpdates(env);
    // Match-start/match-end events: written by GitHub Actions, delivered
    // only here (group + owner, independent receipts — see sweepMatchEvents).
    await sweepMatchEvents(env, nowMs);
    // Dispatches the live-monitoring workflow on demand instead of it
    // running unconditionally every 5 minutes (bug 6/A3).
    await maybeDispatchLiveMonitor(env, nowMs);
  } else if (cron === CRON_RECAP_SWEEP) {
    await postGroupRecap(env, nowMs);
  } else if (cron === CRON_WATCH) {
    await detectMissedGeneration(env, nowMs);
    await cleanupOldRows(env, nowMs);
  }
}
