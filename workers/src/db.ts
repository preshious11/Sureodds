// D1 access layer. Stage 1 foundation only: update dedupe, subscriber
// upsert, day-cache reads, idempotent post/delivery receipts, job state.
// All queries are indexed point-lookups or date-scoped range reads so the
// free tier (5M rows read / 100K written per day) is never stressed.

export interface Env {
  DB: D1Database;
  TELEGRAM_BOT_TOKEN: string;
  TELEGRAM_WEBHOOK_SECRET: string;
  TELEGRAM_CHAT_ID?: string;
  TELEGRAM_BOT_USERNAME?: string;
  TELEGRAM_ADMIN_IDS?: string;
  TIMEZONE?: string;
  PAGE_SIZE?: string;
  POST_TIME?: string;
  DELIVERY_TIME?: string;
  RECAP_TIME?: string;
  /** Latest the recap may wait for yesterday's settlement before posting anyway (never skipped entirely). */
  RECAP_DEADLINE?: string;
  /** Minutes after kickoff during which a match-start alert may still be sent (default 10). */
  MATCH_START_WINDOW_MIN?: string;
  /** Hours after the result was recorded during which a match-end alert may still be sent (default 24). */
  MATCH_END_WINDOW_HOURS?: string;
  /** Minutes that must pass between the Worker's own on-demand dispatches of stage8-live-monitor-production.yml (default 10). */
  LIVE_MONITOR_INTERVAL_MIN?: string;
  /** WHY: bound self-healing provider use even when generation keeps failing. */
  GENERATION_MAX_DISPATCHES_PER_DAY?: string;
  /** "owner/repo" for the stage8-live-monitor-production.yml workflow_dispatch call. Unset = live-monitor dispatch is disabled (alerts the owner once/day instead, if work exists). */
  GITHUB_REPO?: string;
  /** The ref (branch) to dispatch stage8 against. Defaults to "main". */
  GITHUB_REF?: string;
  /** Fine-grained PAT with this repo's "Actions: Read and write" permission, for the workflow_dispatch call. Secret, never a [vars] entry. */
  GITHUB_DISPATCH_TOKEN?: string;
}

/// Two-phase update claim (Stage 2 reliability).
/// An update is never discarded because processing failed after its ID was
/// recorded: `tryClaimUpdate` inserts a `pending` row (or reaps a retryable
/// `failed` row), `completeUpdate` marks `done`, `failUpdate` returns the row
/// to `failed` with an attempt count so a later sweeper (Stage 3 Cron) or a
/// Telegram retry can reprocess it. A `done` row is never reprocessed.
export type ClaimOutcome = "new" | "retry" | "duplicate";

export const MAX_UPDATE_ATTEMPTS = 10;

/// A `pending` claim older than this is presumed crashed (never completed)
/// and may be stolen by a redelivery. Younger than this, a duplicate
/// delivery means another invocation is still processing: back off.
/// 300s comfortably exceeds any healthy webhook handling time (D1 reads +
/// at most one Telegram send) while staying far inside Telegram's ~24h
/// redelivery window, so a crashed claim is always eventually reaped.
export const CLAIM_TIMEOUT_SECONDS = 300;

/// Quota-exhaustion signal from D1 (free tier: 100K rows written/day).
/// Operations that hit it must fail CLOSED: no state change, no false ack.
export class QuotaExhausted extends Error {
  constructor(message = "D1 free-tier daily row write limit exceeded") {
    super(message);
    this.name = "QuotaExhausted";
  }
}

export function isQuotaError(err: unknown): boolean {
  const msg = String((err as Error)?.message ?? err ?? "");
  return /row write limit|D1_ERROR|code:\s*7500|exceeded.*write/i.test(msg);
}

function throwIfQuota(err: unknown): void {
  if (isQuotaError(err)) throw new QuotaExhausted(String((err as Error)?.message ?? err));
  throw err;
}

export async function tryClaimUpdate(db: D1Database, updateId: number): Promise<ClaimOutcome> {
  if (!Number.isFinite(updateId)) return "duplicate";
  let inserted;
  try {
    inserted = await db
      .prepare("INSERT OR IGNORE INTO telegram_updates (update_id, status) VALUES (?1, 'pending')")
      .bind(updateId)
      .run();
  } catch (err) {
    throwIfQuota(err);
    throw err;
  }
  if ((inserted.meta.changes ?? 0) > 0) return "new";
  const row = await db
    .prepare("SELECT status, attempts, updated_at FROM telegram_updates WHERE update_id = ?1")
    .bind(updateId)
    .first<{ status: string; attempts: number; updated_at: string }>();
  if (!row) return "duplicate";
  if (row.status === "done") return "duplicate";
  if ((row.attempts ?? 0) >= MAX_UPDATE_ATTEMPTS) return "duplicate";
  if (row.status === "failed") {
    // Reap ONLY failed rows, and only if still failed (conditional write),
    // so two concurrent deliveries cannot steal each other's claim. Crash-
    // leftover `pending` rows are handled by the stale-lease rule below.
    let reaped;
    try {
      reaped = await db
        .prepare("UPDATE telegram_updates SET status = 'pending', updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE update_id = ?1 AND status = 'failed'")
        .bind(updateId)
        .run();
    } catch (err) {
      throwIfQuota(err);
      throw err;
    }
    return (reaped.meta.changes ?? 0) > 0 ? "retry" : "duplicate";
  }
  // Pending row, owned by whoever inserted it: a concurrent duplicate
  // delivery must NOT execute side effects twice. Only a STALE claim (older
  // than CLAIM_TIMEOUT_SECONDS: the owner crashed without completing) may be
  // stolen, again conditionally so two racers cannot both win. ISO-8601 UTC
  // timestamps compare lexicographically.
  const cutoff = new Date(Date.now() - CLAIM_TIMEOUT_SECONDS * 1000)
    .toISOString()
    .replace(/\.\d+Z$/, "Z");
  if (!row.updated_at || row.updated_at >= cutoff) return "duplicate";
  let stolen;
  try {
    stolen = await db
      .prepare("UPDATE telegram_updates SET status = 'pending', updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE update_id = ?1 AND status = 'pending' AND updated_at < ?2")
      .bind(updateId, cutoff)
      .run();
  } catch (err) {
    throwIfQuota(err);
    throw err;
  }
  return (stolen.meta.changes ?? 0) > 0 ? "retry" : "duplicate";
}

export async function completeUpdate(db: D1Database, updateId: number): Promise<void> {
  try {
    await db
      .prepare("UPDATE telegram_updates SET status = 'done', updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE update_id = ?1 AND status != 'done'")
      .bind(updateId)
      .run();
  } catch (err) {
    throwIfQuota(err);
    throw err;
  }
}

export async function failUpdate(db: D1Database, updateId: number, error: string): Promise<void> {
  // attempts++ is the retry budget itself: this write is load-bearing, but a
  // quota failure here must surface as QuotaExhausted (unprocessed, redeliver)
  // rather than being swallowed as a recorded failure.
  try {
    await db
      .prepare(
        "UPDATE telegram_updates SET status = 'failed', attempts = attempts + 1, last_error = ?2, updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE update_id = ?1",
      )
      .bind(updateId, error.slice(0, 500))
      .run();
  } catch (err) {
    throwIfQuota(err);
    throw err;
  }
}

export async function upsertSubscriber(
  db: D1Database,
  chatId: string,
  fields: { username?: string | null; firstName?: string | null; timezone?: string | null },
): Promise<void> {
  // Read-then-write: a no-op visit must not cost a row WRITE (writes are
  // 50x scarcer than reads on the free tier). Only an actual change writes.
  const username = fields.username ?? null;
  const firstName = fields.firstName ?? null;
  // NOTE: this statement never touches timezone/subscribed_at (insert-only
  // columns here); the no-op check below compares exactly the columns this
  // write can change, so an unchanged visit never costs a row write.
  const current = await db
    .prepare("SELECT username, first_name, active FROM subscribers WHERE chat_id = ?1")
    .bind(chatId)
    .first<{ username: string | null; first_name: string | null; active: number }>();
  if (
    current &&
    (current.username ?? null) === username &&
    (current.first_name ?? null) === firstName &&
    (current.active ?? 0) === 1
  ) {
    return;
  }
  try {
    await db
      .prepare(
        `INSERT INTO subscribers (chat_id, username, first_name, timezone, subscribed_at, active)
         VALUES (?1, ?2, ?3, ?4, strftime('%Y-%m-%dT%H:%M:%SZ','now'), 1)
         ON CONFLICT(chat_id) DO UPDATE SET
           username = COALESCE(?2, username),
           first_name = COALESCE(?3, first_name),
           active = 1`,
      )
      .bind(chatId, username, firstName, fields.timezone ?? "")
      .run();
  } catch (err) {
    throwIfQuota(err);
    throw err;
  }
}

/**
 * ~ predbot/storage/db.py subscriber_remove(): /stop deactivates daily-pick
 * delivery (cron's listActiveSubscribers only serves active = 1). UPDATE
 * only — never inserts a row for a chat that never subscribed, mirroring
 * production. The Custom Picks session is left separately by the caller.
 */
export async function deactivateSubscriber(db: D1Database, chatId: string): Promise<void> {
  try {
    await db
      .prepare("UPDATE subscribers SET active = 0 WHERE chat_id = ?1")
      .bind(chatId)
      .run();
  } catch (err) {
    throwIfQuota(err);
    throw err;
  }
}

export interface DayCache {
  cache_key: string;
  local_date: string;
  status: string;
  payload_json: string;
}

export async function getDayCache(
  db: D1Database,
  localDate: string,
  timezone: string,
): Promise<DayCache | null> {
  const row = await db
    .prepare(
      "SELECT cache_key, local_date, status, payload_json FROM prediction_cache WHERE local_date = ?1 AND timezone = ?2 LIMIT 1",
    )
    .bind(localDate, timezone)
    .first<DayCache>();
  return row ?? null;
}

/**
 * Whether ANY pick exists for `pickDate` — used as the readiness check for a
 * PAST day (bug 12), instead of getDayCache's COMPLETE prediction_cache row:
 * that row is retention-pruned after a few days, but the picks/
 * prediction_memberships rows themselves never are, so an old message's
 * page-turn button was going dark not because the data was gone, but
 * because the readiness receipt confirming a day that already finished had
 * been cleaned up. A plain existence check, not getAllPicksForCollection's
 * full row fetch, since only presence/absence is needed here.
 */
export async function hasPicksForDate(db: D1Database, pickDate: string): Promise<boolean> {
  const row = await db.prepare("SELECT 1 AS one FROM picks WHERE pick_date = ?1 LIMIT 1").bind(pickDate).first<{ one: number }>();
  return !!row;
}

export interface CachedPick {
  id: number;
  tier: string;
  rank: number;
  sport: string;
  league: string | null;
  home: string;
  away: string;
  start_utc: string;
  market: string;
  selection: string;
  label: string;
  price: number;
  prob: number;
  status: string;
}

const PICKS_PAGE_SQL = `SELECT p.id, m.collection AS tier, m.rank AS rank, p.sport, p.league, p.home, p.away,
              p.start_utc, p.market, p.selection, p.label, p.price, p.prob, p.status
       FROM picks p JOIN prediction_memberships m ON m.prediction_id = p.id
       WHERE p.pick_date = ?1 AND m.collection = ?2
       UNION ALL
       SELECT p.id, ?2 AS tier, p.rank AS rank, p.sport, p.league, p.home, p.away,
              p.start_utc, p.market, p.selection, p.label, p.price, p.prob, p.status
       FROM picks p
       WHERE p.pick_date = ?1 AND (?2 = 'all' OR p.tier = ?2)
         AND NOT EXISTS (SELECT 1 FROM prediction_memberships m WHERE m.prediction_id = p.id)
       ORDER BY rank ASC, id ASC`;

export async function getPicksPage(
  db: D1Database,
  pickDate: string,
  collection: "top" | "daily" | "all",
  limit: number,
  offset: number,
): Promise<CachedPick[]> {
  const safeLimit = Math.min(Math.max(limit, 1), 25);
  const safeOffset = Math.max(offset, 0);
  const { results } = await db
    .prepare(`${PICKS_PAGE_SQL} LIMIT ?3 OFFSET ?4`)
    .bind(pickDate, collection, safeLimit, safeOffset)
    .all<CachedPick>();
  return results ?? [];
}

/**
 * Every pick in a collection for a day, unpaged. Ticket headers (combined
 * odds, pick count) and the /all sport menu need the WHOLE collection, not
 * one page of it — mirrors predbot/bot/pagination.py pulling the full item
 * list before slicing a page.
 */
export async function getAllPicksForCollection(
  db: D1Database,
  pickDate: string,
  collection: "top" | "daily" | "all",
): Promise<CachedPick[]> {
  const { results } = await db.prepare(PICKS_PAGE_SQL).bind(pickDate, collection).all<CachedPick>();
  return results ?? [];
}

export interface StatsRow {
  status: string;
  price: number;
  label: string;
  home: string;
  away: string;
  result_json: string | null;
}

/**
 * Picks TRACKED for settlement stats: only predictions published as Top or
 * Daily (predbot/storage/db.py picks_where(..., published_only=True) is the
 * production equivalent — it keeps a row only when its derived tier, with
 * 'top' taking priority over 'daily', is not 'all'). All Predictions is a
 * browsing list and must never contribute to hit rate, ROI, or ticket
 * settlement. A prediction with EXISTS on either collection is returned
 * exactly once (picks.id is the primary key; this is a WHERE EXISTS, not a
 * JOIN, so a leg present in both Top and Daily is never double-counted).
 */
export async function getStatsRowsForDate(db: D1Database, pickDate: string): Promise<StatsRow[]> {
  const { results } = await db
    .prepare(
      `SELECT status, price, label, home, away, result_json FROM picks
       WHERE pick_date = ?1 AND EXISTS (
         SELECT 1 FROM prediction_memberships m WHERE m.prediction_id = picks.id AND m.collection IN ('top', 'daily')
       )`,
    )
    .bind(pickDate)
    .all<StatsRow>();
  return results ?? [];
}

export interface DayStatsRow extends StatsRow {
  pick_date: string;
  sport: string;
  start_utc: string;
}

export interface ItemizedStatsRow extends StatsRow {
  tier: "top" | "daily";
  start_utc: string;
}

/**
 * Per-leg rows for ONE day, with the resolved tier (top takes priority
 * over daily, same as production's picks_where published_only derivation)
 * — backs the itemized /yesterday listing production shows (✅/❌ per leg,
 * grouped by tier). A COALESCE pair of EXISTS checks, not a JOIN, so a leg
 * in both collections still returns exactly one row.
 */
export async function getItemizedStatsRowsForDate(db: D1Database, pickDate: string): Promise<ItemizedStatsRow[]> {
  const { results } = await db
    .prepare(
      `SELECT status, price, label, home, away, result_json, start_utc,
         CASE WHEN EXISTS (SELECT 1 FROM prediction_memberships m WHERE m.prediction_id = picks.id AND m.collection = 'top')
              THEN 'top' ELSE 'daily' END AS tier
       FROM picks
       WHERE pick_date = ?1 AND EXISTS (
         SELECT 1 FROM prediction_memberships m WHERE m.prediction_id = picks.id AND m.collection IN ('top', 'daily')
       )
       ORDER BY tier ASC, start_utc ASC`,
    )
    .bind(pickDate)
    .all<ItemizedStatsRow>();
  return results ?? [];
}

/**
 * Settlement statuses of ONE published collection for a day, in rank order —
 * backs the compact results recap. Membership-scoped, so an All-only
 * prediction can never appear.
 */
export async function getRecapStatuses(
  db: D1Database,
  pickDate: string,
  collection: "top" | "daily",
): Promise<string[]> {
  const { results } = await db
    .prepare(
      `SELECT p.status AS status FROM picks p
       JOIN prediction_memberships m ON m.prediction_id = p.id
       WHERE p.pick_date = ?1 AND m.collection = ?2
       ORDER BY m.rank ASC, p.start_utc ASC, p.id ASC`,
    )
    .bind(pickDate, collection)
    .all<{ status: string }>();
  return (results ?? []).map((r) => r.status);
}

/** Same Top/Daily-only scoping, restricted to ONE collection — backs /stats_top, /stats_daily. */
export async function getStatsRowsForDateByCollection(
  db: D1Database,
  pickDate: string,
  collection: "top" | "daily",
): Promise<StatsRow[]> {
  const { results } = await db
    .prepare(
      `SELECT status, price, label, home, away, result_json FROM picks
       WHERE pick_date = ?1 AND EXISTS (
         SELECT 1 FROM prediction_memberships m WHERE m.prediction_id = picks.id AND m.collection = ?2
       )`,
    )
    .bind(pickDate, collection)
    .all<StatsRow>();
  return results ?? [];
}

/**
 * Same Top/Daily-only scoping as getStatsRowsForDate, over a date RANGE
 * (`[sinceDate, beforeDate)`, both YYYY-MM-DD) — backs the 7-day trend and
 * by-sport breakdown (predbot/jobs/stats.py compute()'s `rows7`/
 * `rows_y_all`).
 */
export async function getStatsRowsForRange(
  db: D1Database,
  sinceDate: string,
  beforeDate: string,
): Promise<DayStatsRow[]> {
  const { results } = await db
    .prepare(
      `SELECT pick_date, sport, status, price, label, home, away, start_utc, result_json FROM picks
       WHERE pick_date >= ?1 AND pick_date < ?2 AND EXISTS (
         SELECT 1 FROM prediction_memberships m WHERE m.prediction_id = picks.id AND m.collection IN ('top', 'daily')
       )`,
    )
    .bind(sinceDate, beforeDate)
    .all<DayStatsRow>();
  return results ?? [];
}

export interface StatsAggregateRow {
  won: number;
  lost: number;
  push: number;
  voided: number;
  unresolved: number;
  staked: number;
  profit: number;
  bestWin: { label: string; price: number; home: string; away: string } | null;
}

/**
 * Bug 13: /stats "All time" used to call getStatsRowsForRange(firstPickDate,
 * tomorrow) — EVERY Top/Daily row ever, read in full just to feed
 * computeStatsBlock's per-row reducer — an unbounded and ever-growing read
 * as the bot accumulates history. This computes the exact same won/lost/
 * push/void/unresolved/staked/profit via SQL aggregates (the unresolved-vs-
 * void split mirrors computeStatsBlock's own result_json.reason ===
 * 'settlement deadline expired' check) plus a second indexed query for the
 * single highest-price 'won' row (computeStatsBlock's bestWin only ever
 * updates on an exact 'won', never 'half_won' - same here). Two point
 * queries instead of reading every settled row.
 */
export async function getStatsAggregateForRange(
  db: D1Database,
  sinceDate: string,
  beforeDate: string,
): Promise<StatsAggregateRow> {
  const scope = `EXISTS (SELECT 1 FROM prediction_memberships m WHERE m.prediction_id = picks.id AND m.collection IN ('top', 'daily'))`;
  const UNRESOLVED = "settlement deadline expired";
  const totals = await db
    .prepare(
      `SELECT
         SUM(CASE WHEN status IN ('won','half_won') THEN 1 ELSE 0 END) AS won,
         SUM(CASE WHEN status IN ('lost','half_lost') THEN 1 ELSE 0 END) AS lost,
         SUM(CASE WHEN status = 'push' THEN 1 ELSE 0 END) AS push,
         SUM(CASE WHEN status = 'void' AND NOT (result_json IS NOT NULL AND json_valid(result_json) AND json_extract(result_json, '$.reason') = ?3) THEN 1 ELSE 0 END) AS voided,
         SUM(CASE WHEN status = 'void' AND result_json IS NOT NULL AND json_valid(result_json) AND json_extract(result_json, '$.reason') = ?3 THEN 1 ELSE 0 END) AS unresolved,
         SUM(CASE WHEN status IN ('won','half_won','lost','half_lost','push') THEN 1 ELSE 0 END) AS staked,
         SUM(CASE WHEN status = 'won' THEN price - 1.0
                  WHEN status = 'half_won' THEN (price - 1.0) / 2.0
                  WHEN status = 'lost' THEN -1.0
                  WHEN status = 'half_lost' THEN -0.5
                  ELSE 0 END) AS profit
       FROM picks WHERE pick_date >= ?1 AND pick_date < ?2 AND ${scope}`,
    )
    .bind(sinceDate, beforeDate, UNRESOLVED)
    .first<{ won: number | null; lost: number | null; push: number | null; voided: number | null; unresolved: number | null; staked: number | null; profit: number | null }>();
  const best = await db
    .prepare(
      `SELECT label, price, home, away FROM picks
       WHERE pick_date >= ?1 AND pick_date < ?2 AND status = 'won' AND ${scope}
       ORDER BY price DESC LIMIT 1`,
    )
    .bind(sinceDate, beforeDate)
    .first<{ label: string; price: number; home: string; away: string }>();
  return {
    won: totals?.won ?? 0,
    lost: totals?.lost ?? 0,
    push: totals?.push ?? 0,
    voided: totals?.voided ?? 0,
    unresolved: totals?.unresolved ?? 0,
    staked: totals?.staked ?? 0,
    profit: totals?.profit ?? 0,
    // A plain object literal, not whatever shape the driver's row object
    // happens to be (e.g. node:sqlite returns a null-prototype object) —
    // computeStatsBlock's own bestWin is always a plain literal, and
    // callers comparing the two (tests/stats_aggregate.test.mjs) must see
    // the same shape.
    bestWin: best ? { label: best.label, price: best.price, home: best.home, away: best.away } : null,
  };
}

/** predbot/jobs/stats.py compute(): window_days = min(7, days since the first ever pick_date). */
export async function getFirstPickDate(db: D1Database): Promise<string | null> {
  const row = await db.prepare("SELECT MIN(pick_date) AS d FROM picks").bind().first<{ d: string | null }>();
  return row?.d ?? null;
}

export async function hasPosted(db: D1Database, kind: string, postDate: string): Promise<boolean> {
  const row = await db
    .prepare("SELECT 1 AS one FROM posts WHERE kind = ?1 AND post_date = ?2 LIMIT 1")
    .bind(kind, postDate)
    .first<{ one: number }>();
  return !!row;
}

export async function markPosted(
  db: D1Database,
  kind: string,
  postDate: string,
  messageId: string | null,
): Promise<void> {
  await db
    .prepare(
      `INSERT INTO posts (kind, post_date, message_id, created_at)
       VALUES (?1, ?2, ?3, strftime('%Y-%m-%dT%H:%M:%SZ','now'))
       ON CONFLICT(kind, post_date) DO NOTHING`,
    )
    .bind(kind, postDate, messageId)
    .run();
}

/// Seconds after which an unconfirmed claim (message_id still NULL — the
/// Worker was evicted/crashed between claiming and sending) is considered
/// abandoned and may be stolen by a later sweep. Comfortably longer than
/// any single sendTelegram() call (bounded retries, each capped at 60s).
export const CLAIM_POST_TIMEOUT_SECONDS = 300;

/**
 * Atomically claims the right to send `(kind, post_date)` — the fix for the
 * overlapping-Cron-fire race: two sweeps calling this concurrently can never
 * both win, because only one `INSERT`/`DO UPDATE` can report a changed row
 * for the same unique key. Returns true iff THIS call won the claim (a
 * fresh row, or reclaiming a stale unconfirmed one past
 * CLAIM_POST_TIMEOUT_SECONDS) — only the winner may call sendTelegram.
 * Confirm a successful send with `confirmPosted`; a failed send simply
 * leaves the claim in place for the next sweep to retry or reclaim.
 */
export async function claimPost(db: D1Database, kind: string, postDate: string): Promise<boolean> {
  const result = await db
    .prepare(
      `INSERT INTO posts (kind, post_date, message_id, created_at)
       VALUES (?1, ?2, NULL, strftime('%Y-%m-%dT%H:%M:%SZ','now'))
       ON CONFLICT(kind, post_date) DO UPDATE SET created_at = excluded.created_at
       WHERE posts.message_id IS NULL
         AND posts.created_at < strftime('%Y-%m-%dT%H:%M:%SZ','now', ?3)`,
    )
    .bind(kind, postDate, `-${CLAIM_POST_TIMEOUT_SECONDS} seconds`)
    .run();
  return (result.meta?.changes ?? 0) > 0;
}

/** Confirms a claim made by `claimPost` once the Telegram send actually succeeded. */
export async function confirmPosted(
  db: D1Database,
  kind: string,
  postDate: string,
  messageId: string | null,
): Promise<void> {
  await db
    .prepare("UPDATE posts SET message_id = ?3 WHERE kind = ?1 AND post_date = ?2")
    .bind(kind, postDate, messageId)
    .run();
}

/**
 * Releases a claim after a send that cleanly FAILED (sendTelegram returned,
 * it just wasn't ok) so the very next sweep can retry immediately instead
 * of waiting out CLAIM_POST_TIMEOUT_SECONDS. Only deletes the still-
 * unconfirmed (message_id IS NULL) row this invocation itself claimed — a
 * send that actually succeeded is always confirmed first and never reaches
 * this call, and a crash before this line simply leaves the stale-claim
 * path (claimPost's own timeout) to recover it instead.
 */
export async function releaseClaim(db: D1Database, kind: string, postDate: string): Promise<void> {
  await db
    .prepare("DELETE FROM posts WHERE kind = ?1 AND post_date = ?2 AND message_id IS NULL")
    .bind(kind, postDate)
    .run();
}

export async function getFailedUpdates(
  db: D1Database,
  limit = 50,
): Promise<{ update_id: number; attempts: number; last_error: string | null }[]> {
  const { results } = await db
    .prepare(
      "SELECT update_id, attempts, last_error FROM telegram_updates WHERE status = 'failed' ORDER BY update_id ASC LIMIT ?1",
    )
    .bind(Math.min(Math.max(limit, 1), 100))
    .all();
  return results ?? [];
}

export interface SubscriberRow {
  chat_id: string;
  timezone: string;
}

export async function listActiveSubscribers(
  db: D1Database,
  limit: number,
  offset: number,
): Promise<SubscriberRow[]> {
  const { results } = await db
    .prepare("SELECT chat_id, timezone FROM subscribers WHERE active = 1 ORDER BY subscribed_at ASC LIMIT ?1 OFFSET ?2")
    .bind(Math.min(Math.max(limit, 1), 500), Math.max(offset, 0))
    .all<SubscriberRow>();
  return results ?? [];
}

export async function deliveriesDone(
  db: D1Database,
  kind: string,
  postDate: string,
): Promise<Set<string>> {
  const { results } = await db
    .prepare("SELECT chat_id FROM deliveries WHERE kind = ?1 AND post_date = ?2")
    .bind(kind, postDate)
    .all<{ chat_id: string }>();
  return new Set((results ?? []).map((r) => r.chat_id));
}

export async function markDelivery(
  db: D1Database,
  kind: string,
  postDate: string,
  chatId: string,
): Promise<void> {
  await db
    .prepare(
      "INSERT OR IGNORE INTO deliveries (kind, post_date, chat_id, created_at) VALUES (?1, ?2, ?3, strftime('%Y-%m-%dT%H:%M:%SZ','now'))",
    )
    .bind(kind, postDate, chatId)
    .run();
}

export interface CustomPickPoolRow {
  prediction_date: string;
  timezone: string;
  generated_at: string;
  status: "COMPLETE" | "PARTIAL";
}

export async function getCustomPickPool(
  db: D1Database,
  predictionDate: string,
  timezone: string,
): Promise<CustomPickPoolRow | null> {
  const row = await db
    .prepare(
      "SELECT prediction_date, timezone, generated_at, status FROM custom_pick_pools WHERE prediction_date = ?1 AND timezone = ?2",
    )
    .bind(predictionDate, timezone)
    .first<CustomPickPoolRow>();
  return row ?? null;
}

export interface CustomPickCandidateRow {
  candidate_id: string;
  canonical_event_id: string;
  payload_json: string;
  generated_at: string;
  prediction_date: string;
  timezone: string;
}

export async function getCustomPickCandidates(
  db: D1Database,
  predictionDate: string,
  timezone: string,
): Promise<CustomPickCandidateRow[]> {
  // prediction_date/timezone are selected (not just filtered) because the
  // archived payload_json never carries them — loadPool() in custom_picks.ts
  // merges these row columns over the parsed JSON, mirroring production's
  // get_custom_pick_candidates. Without them every row fails normalization.
  const { results } = await db
    .prepare(
      `SELECT candidate_id, canonical_event_id, payload_json, generated_at, prediction_date, timezone FROM custom_pick_candidates
       WHERE prediction_date = ?1 AND timezone = ?2 ORDER BY canonical_event_id, candidate_id`,
    )
    .bind(predictionDate, timezone)
    .all<CustomPickCandidateRow>();
  return results ?? [];
}

/**
 * Custom Picks session state. Production tracks "(user_id, chat_id) is in
 * Custom Picks mode" and a request cooldown in in-memory dicts on one
 * long-lived process (predbot/custom_recovery.py); a Worker has no memory
 * shared across invocations, so this is the durable substitute (see
 * migrations/0006_custom_sessions.sql). Tickets themselves are still never
 * persisted, matching production.
 */
export async function isCustomSessionActive(db: D1Database, userId: string, chatId: string): Promise<boolean> {
  const row = await db
    .prepare("SELECT active FROM custom_sessions WHERE user_id = ?1 AND chat_id = ?2")
    .bind(userId, chatId)
    .first<{ active: number }>();
  return !!row && row.active === 1;
}

export async function enterCustomSession(db: D1Database, userId: string, chatId: string): Promise<void> {
  await db
    .prepare(
      `INSERT INTO custom_sessions (user_id, chat_id, active, last_request_at, updated_at)
       VALUES (?1, ?2, 1, NULL, strftime('%Y-%m-%dT%H:%M:%SZ','now'))
       ON CONFLICT(user_id, chat_id) DO UPDATE SET active = 1, updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')`,
    )
    .bind(userId, chatId)
    .run();
}

export async function leaveCustomSession(db: D1Database, userId: string, chatId: string): Promise<void> {
  await db
    .prepare(
      `INSERT INTO custom_sessions (user_id, chat_id, active, last_request_at, updated_at)
       VALUES (?1, ?2, 0, NULL, strftime('%Y-%m-%dT%H:%M:%SZ','now'))
       ON CONFLICT(user_id, chat_id) DO UPDATE SET active = 0, updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')`,
    )
    .bind(userId, chatId)
    .run();
}

/**
 * Simplified stand-in for production's token-bucket work budget + 30/60s
 * request counter + 16-slot queue (commands.py CUSTOM_WORK_LIMIT/
 * CUSTOM_LIMIT/CUSTOM_QUEUE_LIMIT) — a Worker invocation can't share an
 * in-process limiter across requests either. This is a flat per-chat
 * cooldown: returns true (and does NOT update the timestamp) if the last
 * request was within `cooldownMs`, so the caller can reject without
 * spending a search. Call recordCustomRequest() only after a request is
 * actually allowed to proceed.
 */
export async function isCustomOnCooldown(
  db: D1Database,
  userId: string,
  chatId: string,
  nowMs: number,
  cooldownMs: number,
): Promise<boolean> {
  const row = await db
    .prepare("SELECT last_request_at FROM custom_sessions WHERE user_id = ?1 AND chat_id = ?2")
    .bind(userId, chatId)
    .first<{ last_request_at: string | null }>();
  if (!row?.last_request_at) return false;
  const lastMs = Date.parse(row.last_request_at);
  return Number.isFinite(lastMs) && nowMs - lastMs < cooldownMs;
}

export async function recordCustomRequest(db: D1Database, userId: string, chatId: string, nowMs: number): Promise<void> {
  await db
    .prepare(
      `UPDATE custom_sessions SET last_request_at = ?3, updated_at = ?3 WHERE user_id = ?1 AND chat_id = ?2`,
    )
    .bind(userId, chatId, new Date(nowMs).toISOString().replace(/\.\d+Z$/, "Z"))
    .run();
}

/**
 * Conversation refinement state (~ custom_recovery.py Recovery.conversations):
 * the last parsed request, so a follow-up message classified as a
 * "refinement" merges into it instead of starting over.
 */
export async function getConversationRequest(
  db: D1Database,
  userId: string,
  chatId: string,
  nowMs: number,
): Promise<string | null> {
  const row = await db
    .prepare("SELECT request_json, request_expires_at FROM custom_sessions WHERE user_id = ?1 AND chat_id = ?2")
    .bind(userId, chatId)
    .first<{ request_json: string | null; request_expires_at: string | null }>();
  if (!row?.request_json || !row.request_expires_at) return null;
  return Date.parse(row.request_expires_at) > nowMs ? row.request_json : null;
}

export async function setConversationRequest(
  db: D1Database,
  userId: string,
  chatId: string,
  requestJson: string,
  expiresAtMs: number,
): Promise<void> {
  await db
    .prepare(
      `UPDATE custom_sessions SET request_json = ?3, request_expires_at = ?4, updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')
       WHERE user_id = ?1 AND chat_id = ?2`,
    )
    .bind(userId, chatId, requestJson, new Date(expiresAtMs).toISOString())
    .run();
}

/** ~ custom_recovery.py Recovery.ready: alternative-suggestion tokens a button tap can redeem. */
export async function storeSuggestion(
  db: D1Database,
  token: string,
  userId: string,
  chatId: string,
  label: string,
  requestJson: string,
  ticketJson: string,
  nowMs: number,
  expiresAtMs: number,
): Promise<void> {
  await db
    .prepare(
      `INSERT INTO custom_suggestions (token, user_id, chat_id, label, request_json, ticket_json, created_at, expires_at)
       VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)`,
    )
    .bind(token, userId, chatId, label, requestJson, ticketJson, new Date(nowMs).toISOString(), new Date(expiresAtMs).toISOString())
    .run();
}

export interface SuggestionRow {
  token: string;
  user_id: string;
  chat_id: string;
  label: string;
  request_json: string;
  ticket_json: string;
  expires_at: string;
}

export async function getSuggestion(db: D1Database, token: string): Promise<SuggestionRow | null> {
  const row = await db.prepare("SELECT * FROM custom_suggestions WHERE token = ?1").bind(token).first<SuggestionRow>();
  return row ?? null;
}

export async function listActiveSuggestions(
  db: D1Database,
  userId: string,
  chatId: string,
  nowMs: number,
): Promise<SuggestionRow[]> {
  const { results } = await db
    .prepare("SELECT * FROM custom_suggestions WHERE user_id = ?1 AND chat_id = ?2 AND expires_at > ?3 ORDER BY created_at DESC")
    .bind(userId, chatId, new Date(nowMs).toISOString())
    .all<SuggestionRow>();
  return results ?? [];
}

export async function setJobResult(
  db: D1Database,
  name: string,
  result: string,
): Promise<void> {
  // Upsert: an UPDATE-only write silently did nothing for any job name the
  // migration did not seed (the Worker's "post" job was never seeded), so
  // every posting failure/wait/success record was being dropped.
  await db
    .prepare(
      `INSERT INTO job_state (name, last_result, last_run_at, updated_at)
       VALUES (?1, ?2, strftime('%Y-%m-%dT%H:%M:%SZ','now'), strftime('%Y-%m-%dT%H:%M:%SZ','now'))
       ON CONFLICT(name) DO UPDATE SET last_result = excluded.last_result,
         last_run_at = excluded.last_run_at, updated_at = excluded.updated_at`,
    )
    .bind(name, result.slice(0, 500))
    .run();
}

/**
 * Same upsert as setJobResult, but `last_run_at` is the CALLER's `nowMs`
 * instead of D1's own `strftime('now')` — needed wherever the caller does
 * its own elapsed-time math against that timestamp (maybeDispatchLiveMonitor's
 * interval check): comparing a caller-supplied "now" against a D1-clock
 * timestamp is only ever correct by coincidence.
 */
export async function recordJobRun(db: D1Database, name: string, nowMs: number, result: string): Promise<void> {
  const now = new Date(nowMs).toISOString();
  await db
    .prepare(
      `INSERT INTO job_state (name, last_result, last_run_at, updated_at)
       VALUES (?1, ?2, ?3, ?3)
       ON CONFLICT(name) DO UPDATE SET last_result = excluded.last_result,
         last_run_at = excluded.last_run_at, updated_at = excluded.updated_at`,
    )
    .bind(name, result.slice(0, 500), now)
    .run();
}

/** `last_run_at` of a named job_state row, or null if it has never run. */
export async function getJobLastRunAt(db: D1Database, name: string): Promise<string | null> {
  const row = await db.prepare("SELECT last_run_at FROM job_state WHERE name = ?1").bind(name).first<{ last_run_at: string | null }>();
  return row?.last_run_at ?? null;
}

/**
 * Whether the live monitor has any actual work to do right now (bug 6: the
 * Worker decides this and dispatches GitHub Actions on demand, instead of
 * stage8 running on its own 5-minute schedule 288x/day even at night with
 * nothing to track). ONE query, two EXISTS clauses:
 *   - a published Top/Daily pick (same publication rule as
 *     getMatchEventContext: a posts receipt for its collection, or a
 *     COMPLETE day cache) that is still 'pending' and due to kick off soon
 *     or already underway (`sinceIso`..`untilIso`);
 *   - OR any live_fixture_state row not yet fully processed
 *     (notifications_done = 0 or settlement_done = 0) - covers a fixture
 *     already past kickoff that still needs tracking even if its pick row
 *     itself isn't 'pending' any more.
 */
export async function liveMonitorWorkExists(db: D1Database, sinceIso: string, untilIso: string): Promise<boolean> {
  const row = await db
    .prepare(
      `SELECT (
         EXISTS (
           SELECT 1 FROM picks p
           JOIN prediction_memberships m ON m.prediction_id = p.id
           WHERE p.status = 'pending' AND p.start_utc BETWEEN ?1 AND ?2
             AND m.collection IN ('top', 'daily')
             AND (
               EXISTS (SELECT 1 FROM posts po WHERE po.post_date = p.pick_date AND po.kind = m.collection)
               OR EXISTS (SELECT 1 FROM prediction_cache c WHERE c.local_date = p.pick_date AND c.status = 'COMPLETE')
             )
         )
         OR EXISTS (SELECT 1 FROM live_fixture_state WHERE notifications_done = 0 OR settlement_done = 0)
       ) AS work`,
    )
    .bind(sinceIso, untilIso)
    .first<{ work: number }>();
  return !!row?.work;
}

export interface PinnedMenuRow {
  chat_id: string;
  message_id: number;
  pinned_by: string;
  pinned_at: string;
}

/** ~/pinmenu's memory of which group message is the currently pinned community menu (0008_pinned_menu.sql). */
export async function getPinnedMenu(db: D1Database, chatId: string): Promise<PinnedMenuRow | null> {
  const row = await db.prepare("SELECT chat_id, message_id, pinned_by, pinned_at FROM pinned_menu WHERE chat_id = ?1").bind(chatId).first<PinnedMenuRow>();
  return row ?? null;
}

export async function setPinnedMenu(db: D1Database, chatId: string, messageId: number, pinnedBy: string, pinnedAt: string): Promise<void> {
  await db
    .prepare(
      `INSERT INTO pinned_menu (chat_id, message_id, pinned_by, pinned_at)
       VALUES (?1, ?2, ?3, ?4)
       ON CONFLICT(chat_id) DO UPDATE SET message_id = ?2, pinned_by = ?3, pinned_at = ?4`,
    )
    .bind(chatId, messageId, pinnedBy, pinnedAt)
    .run();
}

export interface MatchEventRow {
  pick_date: string;
  fixture_key: string;
  event_type: "start" | "end";
  text: string;
  created_at: string;
}

/**
 * Match-start/match-end events (0009_match_events.sql) that STILL need
 * delivery to at least one configured destination — the Worker's own Cron
 * sweep is the only thing that ever delivers these to Telegram.
 *
 * Previously this read the last 30 days oldest-first with LIMIT 200 and let
 * the caller check each row's group/owner/expired receipts itself: on the
 * free plan that is 2-3 extra D1 queries PER EVENT, including already-
 * delivered ones, which blew past the 50-query-per-invocation limit within
 * a day or two, and a backlog of 200+ already-delivered events meant a new
 * event could never even be reached (LIMIT 200 oldest-first). This single
 * query does the receipt filtering itself (NOT EXISTS against `posts`, the
 * same table the caller used to re-check one row at a time) so only events
 * genuinely still outstanding are ever returned, and LIMIT 5 bounds the
 * per-sweep cost regardless of backlog size.
 *
 * `groupConfigured`/`ownerConfigured` mirror the caller's own "no chat/no
 * owner means that leg is never outstanding" rule (same as the old
 * `groupDone = !env.TELEGRAM_CHAT_ID || hasPosted(...)` check) - a leg that
 * was never going to be delivered must not keep an otherwise-fully-handled
 * event matching forever.
 *
 * The window is now 48 hours, not 30 days: a 30-day scan is unbounded read
 * cost on a backlog, and decideMatchEvent's own kickoff/settlement windows
 * mean nothing genuinely actionable survives this long anyway — the
 * tradeoff is that an outage longer than 48h now permanently loses an
 * undelivered alert instead of the old 30-day recovery margin.
 */
export async function getUndeliveredMatchEvents(
  db: D1Database,
  sinceIso: string,
  groupConfigured: boolean,
  ownerConfigured: boolean,
  limit = 5,
): Promise<MatchEventRow[]> {
  const { results } = await db
    .prepare(
      `SELECT pick_date, fixture_key, event_type, text, created_at FROM match_events me
       WHERE created_at >= ?1
         AND NOT EXISTS (SELECT 1 FROM posts po WHERE po.post_date = me.pick_date
           AND po.kind = 'evt_' || me.fixture_key || '_' || me.event_type || '_expired')
         AND (
           (?2 = 1 AND NOT EXISTS (SELECT 1 FROM posts po WHERE po.post_date = me.pick_date
             AND po.kind = 'evt_' || me.fixture_key || '_' || me.event_type))
           OR (?3 = 1 AND NOT EXISTS (SELECT 1 FROM posts po WHERE po.post_date = me.pick_date
             AND po.kind = 'evt_' || me.fixture_key || '_' || me.event_type || '_owner'))
         )
       ORDER BY created_at ASC LIMIT ?4`,
    )
    .bind(sinceIso, groupConfigured ? 1 : 0, ownerConfigured ? 1 : 0, limit)
    .all<MatchEventRow>();
  return results ?? [];
}


/**
 * Everything the dispatcher must re-verify for ONE persisted match event at
 * the moment it is about to be sent. Reads only persisted state — never the
 * candidate pool:
 *   - kickoff_utc: the fixture's scheduled start (from its stored picks)
 *   - live_status / settlement_done: the live monitor's durable fixture state
 *   - published_open / published_total: predictions of this fixture that are
 *     themselves members of Top/Daily AND whose day was officially published
 *     (a posts receipt for the collection, or a COMPLETE prediction cache —
 *     a failed Telegram send must not un-publish a day), excluding / including VOID legs
 * A fixture only in All Predictions, or whose Top/Daily card was never
 * posted, yields published_total = 0.
 */
export interface MatchEventContext {
  kickoff_utc: string | null;
  live_status: string | null;
  settlement_done: number | null;
  published_open: number;
  published_total: number;
}

export async function getMatchEventContext(
  db: D1Database,
  pickDate: string,
  fixtureKey: string,
): Promise<MatchEventContext> {
  const published = (extra: string) =>
    `(SELECT COUNT(DISTINCT p.id) FROM picks p
        JOIN prediction_memberships m ON m.prediction_id = p.id
       WHERE p.pick_date = ?1 AND p.fixture_key = ?2 AND m.collection IN ('top', 'daily')
         AND (EXISTS (SELECT 1 FROM posts po WHERE po.post_date = ?1 AND po.kind = m.collection)
              OR EXISTS (SELECT 1 FROM prediction_cache c WHERE c.local_date = ?1 AND c.status = 'COMPLETE')) ${extra})`;
  const row = await db
    .prepare(
      `SELECT
         (SELECT MIN(start_utc) FROM picks WHERE pick_date = ?1 AND fixture_key = ?2) AS kickoff_utc,
         (SELECT status FROM live_fixture_state WHERE pick_date = ?1 AND fixture_key = ?2) AS live_status,
         (SELECT settlement_done FROM live_fixture_state WHERE pick_date = ?1 AND fixture_key = ?2) AS settlement_done,
         ${published("AND p.status <> 'void'")} AS published_open,
         ${published("")} AS published_total`,
    )
    .bind(pickDate, fixtureKey)
    .first<MatchEventContext>();
  return (
    row ?? { kickoff_utc: null, live_status: null, settlement_done: null, published_open: 0, published_total: 0 }
  );
}
