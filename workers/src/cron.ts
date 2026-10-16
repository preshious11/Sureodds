// Stage 3 scheduled operations (ships INACTIVE: STAGE3_CRON_ENABLED must be
// "1", and no triggers are attached in wrangler.toml yet).
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
  deliveriesDone,
  getDayCache,
  getFailedUpdates,
  getMatchEventContext,
  type MatchEventContext,
  type MatchEventRow,
  getRecentMatchEvents,
  hasPosted,
  listActiveSubscribers,
  markDelivery,
  markPosted,
  setJobResult,
  type Env,
} from "./db.ts";
import { lagosDateString, renderGroupCard, renderGroupRecap, renderView } from "./views.ts";
import { allButtonModeFor, topEmptyKeyboard } from "./telegram.ts";

export const BROADCAST_PER_SECOND = 25;
export const CHAT_PER_SECOND = 1;

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
      const { ok } = await sender(env, "sendMessage", {
        chat_id: sub.chat_id,
        text,
        parse_mode: "HTML",
        reply_markup: replyMarkup,
      });
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
  if (await hasPosted(env.DB, ownerKind, localDate)) return;
  const { ok, messageId } = await sender(env, "sendMessage", {
    chat_id: owner,
    text,
    parse_mode: "HTML",
    ...(replyMarkup ? { reply_markup: replyMarkup } : {}),
  });
  if (!ok) {
    await recordDeliveryFailure(env, "owner", `${kind} ${localDate}`);
    return;
  }
  await markPosted(env.DB, ownerKind, localDate, messageId != null ? String(messageId) : null);
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
  if (!env.TELEGRAM_CHAT_ID) return "no-chat";
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
  // Receipt check IS the lock: posts.(kind,post_date) is a UNIQUE key, and
  // markPosted below uses INSERT ... ON CONFLICT DO NOTHING, so a Cron that
  // fires twice (or overlapping invocations) can send at most once — the
  // loser's markPosted is a no-op, mirroring production's post_done/mark_post.
  // The group leg's own receipt is checked independently further down, but
  // rendering happens unconditionally here: even on a day the GROUP already
  // received its card, the OWNER leg (postToOwner) may still need a retry.
  const groupAlreadySent = await hasPosted(env.DB, kind, localDate);
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
  if (groupAlreadySent) {
    await postToOwner(env, kind, localDate, text, replyMarkup, sender);
    return "skipped";
  }
  const { ok, messageId } = await sender(env, "sendMessage", {
    chat_id: env.TELEGRAM_CHAT_ID,
    text,
    parse_mode: "HTML",
    ...(replyMarkup ? { reply_markup: replyMarkup } : {}),
  });
  if (!ok) {
    await recordDeliveryFailure(env, "group", `${kind} ${localDate}`);
    // The group itself never went out this attempt — retry it next sweep
    // rather than racing ahead to the owner with a card the group hasn't
    // seen yet (the ordering the requirement implies: group, then owner).
    return "failed";
  }
  await markPosted(env.DB, kind, localDate, messageId != null ? String(messageId) : null);
  await setJobResult(env.DB, "post", `${kind} ${localDate}: posted message=${messageId ?? "?"}`);
  await postToOwner(env, kind, localDate, text, replyMarkup, sender);
  return "sent";
}

/**
 * Posts yesterday's settled recap to the group — production equivalent is
 * predbot/jobs/recap.py (single group send, post_done("recap", date) gate).
 */
export async function postGroupRecap(
  env: Env,
  nowMs = Date.now(),
  sender: typeof sendTelegram = sendTelegram,
): Promise<GroupPostOutcome> {
  if (!env.TELEGRAM_CHAT_ID) return "no-chat";
  const gate = parseHhmm(env.RECAP_TIME ?? "07:45");
  if (lagosMinutes(nowMs) < gate) return "waiting";
  const localDate = lagosDateString(nowMs);
  const groupAlreadySent = await hasPosted(env.DB, "recap", localDate);
  // predbot/jobs/recap.py: tg.send(text) — no keyboard on the automatic
  // group recap, same as the picks cards above. renderGroupRecap (not the
  // generic renderView) titles this "📊 RESULTS", matching recap_message()
  // exactly — distinct from the interactive /yesterday button's own title.
  const { text } = await renderGroupRecap(env, localDate);
  if (groupAlreadySent) {
    await postToOwner(env, "recap", localDate, text, undefined, sender);
    return "skipped";
  }
  const { ok, messageId } = await sender(env, "sendMessage", {
    chat_id: env.TELEGRAM_CHAT_ID,
    text,
    parse_mode: "HTML",
  });
  if (!ok) {
    await recordDeliveryFailure(env, "group", `recap ${localDate}`);
    return "failed";
  }
  await markPosted(env.DB, "recap", localDate, messageId != null ? String(messageId) : null);
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

// How far back to look for events each sweep. This bounds a table scan,
// not correctness. Eligibility/expiry (decideMatchEvent) decides whether a
// row inside that window is still worth sending.
const MATCH_EVENT_LOOKBACK_MS = 30 * 24 * 3_600_000;

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
  const events = await getRecentMatchEvents(env.DB, since);
  const owner = ownerChatId(env);
  let delivered = 0;
  for (const evt of events) {
    const eventKey = `evt_${evt.fixture_key}_${evt.event_type}`;
    const groupKind = eventKey;
    const ownerKind = `${eventKey}_owner`;
    const expiredKind = `${eventKey}_expired`;
    const groupDone = !env.TELEGRAM_CHAT_ID || (await hasPosted(env.DB, groupKind, evt.pick_date));
    const ownerDone = !owner || (await hasPosted(env.DB, ownerKind, evt.pick_date));
    if (groupDone && ownerDone) continue;
    if (await hasPosted(env.DB, expiredKind, evt.pick_date)) continue;

    const ctx = await getMatchEventContext(env.DB, evt.pick_date, evt.fixture_key);
    const decision = decideMatchEvent(env, evt, ctx, nowMs);
    if (decision.action === "wait") continue;
    if (decision.action === "expire") {
      await markPosted(env.DB, expiredKind, evt.pick_date, null);
      console.warn(JSON.stringify({ event: "match_event_expired", fixture: evt.fixture_key, type: evt.event_type, reason: decision.reason }));
      await setJobResult(env.DB, "match_events", `expired ${evt.fixture_key}/${evt.event_type}: ${decision.reason}`);
      continue;
    }

    if (!groupDone) {
      const { ok, messageId } = await sender(env, "sendMessage", { chat_id: env.TELEGRAM_CHAT_ID, text: evt.text, parse_mode: "HTML" });
      if (ok) {
        await markPosted(env.DB, groupKind, evt.pick_date, messageId != null ? String(messageId) : null);
        delivered += 1;
      } else {
        await recordDeliveryFailure(env, "group", `match event ${evt.fixture_key}/${evt.event_type}`);
      }
    }
    if (!ownerDone) {
      const { ok, messageId } = await sender(env, "sendMessage", { chat_id: owner, text: evt.text, parse_mode: "HTML" });
      if (ok) {
        await markPosted(env.DB, ownerKind, evt.pick_date, messageId != null ? String(messageId) : null);
        delivered += 1;
      } else {
        await recordDeliveryFailure(env, "owner", `match event ${evt.fixture_key}/${evt.event_type}`);
      }
    }
  }
  return delivered;
}

export async function detectMissedGeneration(env: Env, nowMs = Date.now()): Promise<boolean> {
  // Generation is Actions-owned (09:00 Lagos). If we are past delivery time
  // with no COMPLETE cache, record the miss for the operator — the Worker
  // must NOT generate (no providers, no CPU budget for it).
  const localDate = lagosDateString(nowMs);
  if (lagosMinutes(nowMs) < parseHhmm(env.DELIVERY_TIME ?? "09:30")) return false;
  const cache = await getDayCache(env.DB, localDate, env.TIMEZONE ?? "Africa/Lagos");
  const missed = !cache || cache.status !== "COMPLETE";
  if (missed) {
    await setJobResult(env.DB, "generate", `MISSED generation for ${localDate}: no COMPLETE cache by delivery time`);
  }
  return missed;
}

// Cloudflare passes the exact matched Cron Trigger pattern as `event.cron` —
// plain cron syntax (e.g. "*/5 8-11 * * *"), never a descriptive word. A
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
export const CRON_POST_SWEEP = "*/5 8-11 * * *"; // 08:00-11:55 UTC = 09:00-12:55 Lagos
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
  } else if (cron === CRON_RECAP_SWEEP) {
    await postGroupRecap(env, nowMs);
  } else if (cron === CRON_WATCH) {
    await detectMissedGeneration(env, nowMs);
  }
}
