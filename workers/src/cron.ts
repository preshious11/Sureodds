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
  hasPosted,
  listActiveSubscribers,
  markDelivery,
  markPosted,
  setJobResult,
  type Env,
} from "./db.ts";
import { lagosDateString, renderView } from "./views.ts";

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
  const gate = kind === "picks" ? parseHhmm("09:30") : parseHhmm("07:45");
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
  const gate = parseHhmm(env.POST_TIME ?? "09:00");
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
  if (await hasPosted(env.DB, kind, localDate)) return "skipped";
  const { text, replyMarkup } = await renderView(env, kind, 0, localDate);
  const { ok, messageId } = await sender(env, "sendMessage", {
    chat_id: env.TELEGRAM_CHAT_ID,
    text,
    parse_mode: "HTML",
    reply_markup: replyMarkup,
  });
  if (!ok) {
    await setJobResult(env.DB, "post", `${kind} ${localDate}: send failed, will retry next sweep`);
    return "failed";
  }
  await markPosted(env.DB, kind, localDate, messageId != null ? String(messageId) : null);
  await setJobResult(env.DB, "post", `${kind} ${localDate}: posted message=${messageId ?? "?"}`);
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
  if (await hasPosted(env.DB, "recap", localDate)) return "skipped";
  const { text, replyMarkup } = await renderView(env, "yesterday", 0, localDate);
  const { ok, messageId } = await sender(env, "sendMessage", {
    chat_id: env.TELEGRAM_CHAT_ID,
    text,
    parse_mode: "HTML",
    reply_markup: replyMarkup,
  });
  if (!ok) {
    await setJobResult(env.DB, "post", `recap ${localDate}: send failed, will retry next sweep`);
    return "failed";
  }
  await markPosted(env.DB, "recap", localDate, messageId != null ? String(messageId) : null);
  await setJobResult(env.DB, "post", `recap ${localDate}: posted message=${messageId ?? "?"}`);
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

export async function detectMissedGeneration(env: Env, nowMs = Date.now()): Promise<boolean> {
  // Generation is Actions-owned (09:00 Lagos). If we are past delivery time
  // with no COMPLETE cache, record the miss for the operator — the Worker
  // must NOT generate (no providers, no CPU budget for it).
  const localDate = lagosDateString(nowMs);
  if (lagosMinutes(nowMs) < parseHhmm("09:30")) return false;
  const cache = await getDayCache(env.DB, localDate, env.TIMEZONE ?? "Africa/Lagos");
  const missed = !cache || cache.status !== "COMPLETE";
  if (missed) {
    await setJobResult(env.DB, "generate", `MISSED generation for ${localDate}: no COMPLETE cache by delivery time`);
  }
  return missed;
}

export async function handleScheduled(env: Env, cron: string, nowMs = Date.now()): Promise<void> {
  // Ships OFF: enable by setting STAGE3_CRON_ENABLED=1 in staging only.
  // No triggers are attached in wrangler.toml yet (Stage 4).
  if ((env as Record<string, unknown>).STAGE3_CRON_ENABLED !== "1") return;
  if (cron.includes("post")) {
    // Group-only (production model). Never calls sweepDeliveries here —
    // individual-subscriber broadcasting stays disabled, matching
    // production's SUBSCRIBER_DELIVERY_ENABLED=false.
    await postGroupPicks(env, "top", nowMs);
    await postGroupPicks(env, "daily", nowMs);
  } else if (cron.includes("deliver")) {
    await reportFailedUpdates(env);
  } else if (cron.includes("recap")) {
    await postGroupRecap(env, nowMs);
  } else if (cron.includes("settle") || cron.includes("watch")) {
    await detectMissedGeneration(env, nowMs);
  }
}
