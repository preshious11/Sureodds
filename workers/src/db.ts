// D1 access layer. Stage 1 foundation only: update dedupe, subscriber
// upsert, day-cache reads, idempotent post/delivery receipts, job state.
// All queries are indexed point-lookups or date-scoped range reads so the
// free tier (5M rows read / 100K written per day) is never stressed.

export interface Env {
  DB: D1Database;
  TELEGRAM_BOT_TOKEN: string;
  TELEGRAM_WEBHOOK_SECRET: string;
  TELEGRAM_CHAT_ID?: string;
  TIMEZONE?: string;
  PAGE_SIZE?: string;
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

/// Backwards-compatible single-phase claim (kept for existing callers/tests):
/// true only when this delivery should be processed.
export async function claimUpdate(db: D1Database, updateId: number): Promise<boolean> {
  const outcome = await tryClaimUpdate(db, updateId);
  return outcome !== "duplicate";
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

export async function setJobResult(
  db: D1Database,
  name: string,
  result: string,
): Promise<void> {
  await db
    .prepare(
      "UPDATE job_state SET last_result = ?2, last_run_at = strftime('%Y-%m-%dT%H:%M:%SZ','now'), updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE name = ?1",
    )
    .bind(name, result.slice(0, 500))
    .run();
}
export async function getJobState(
  db: D1Database,
  name: string,
): Promise<{ name: string; due_at: string | null; lease_owner: string | null; attempts: number } | null> {
  return await db.prepare("SELECT name, due_at, lease_owner, attempts FROM job_state WHERE name = ?1").bind(name).first();
}

export async function setJobDue(db: D1Database, name: string, dueAt: string | null): Promise<void> {
  await db
    .prepare(
      `INSERT INTO job_state (name, due_at, attempts, updated_at)
       VALUES (?1, ?2, 0, strftime('%Y-%m-%dT%H:%M:%SZ','now'))
       ON CONFLICT(name) DO UPDATE SET due_at = ?2, updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')`,
    )
    .bind(name, dueAt)
    .run();
}
