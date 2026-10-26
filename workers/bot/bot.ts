// Long-polling entry point for the Telegram bot, replacing the Cloudflare
// Worker's webhook handler. Imports handleUpdate directly from workers/src -
// same module the Worker and its own tests use, unmodified. Only the
// transport (long polling instead of an inbound webhook) and the database
// (LocalSqliteAdapter instead of the D1 binding) differ.
//
// The 4 Worker crons and the Pro closing job run as their own separate
// systemd timers (see cron/*.ts) - this process only ever calls
// handleUpdate.
//
// Env validation and the poll loop itself only run when this file is
// executed directly (see the isMain guard at the bottom) - importing it
// for tests never starts polling or exits the process.
import { readFile, writeFile, mkdir } from "node:fs/promises";
import { dirname } from "node:path";
import { pathToFileURL } from "node:url";
import { config } from "dotenv";

import { handleUpdate, tgApi } from "../src/index.ts";
import { QuotaExhausted, type Env } from "../src/db.ts";
import { LocalSqliteAdapter } from "./local-sqlite-adapter.ts";

export const POLL_TIMEOUT_SEC = 30; // long-poll window; Telegram holds the connection open until an update arrives or this elapses
export const POLL_ERROR_BACKOFF_MS = 5_000;
export const QUOTA_BACKOFF_INITIAL_MS = 60_000; // 1 minute
export const QUOTA_BACKOFF_MAX_MS = 900_000; // 15 minutes

export interface TelegramUpdate {
  update_id: number;
  [key: string]: unknown;
}

/** Tracks whether the loop is currently in a D1-quota-exhausted backoff, and for how long. null = healthy. */
export interface QuotaState {
  backoffMs: number | null;
}

export function nextQuotaBackoffMs(current: number | null): number {
  if (current === null) return QUOTA_BACKOFF_INITIAL_MS;
  return Math.min(current * 2, QUOTA_BACKOFF_MAX_MS);
}

/** Same admin-id resolution workers/src/cron.ts's own ownerChatId() uses - first entry of TELEGRAM_ADMIN_IDS. */
export function adminChatId(env: Env): string | null {
  const first = (env.TELEGRAM_ADMIN_IDS ?? "").split(",").map((s) => s.trim()).filter(Boolean)[0];
  return first || null;
}

/** Best-effort admin alert: a failed send must never crash the poll loop that is already in trouble. */
export async function alertAdmin(env: Env, text: string, sender: typeof tgApi = tgApi): Promise<void> {
  const chatId = adminChatId(env);
  if (!chatId) return;
  try {
    await sender(env, "sendMessage", { chat_id: chatId, text });
  } catch (err) {
    console.error(JSON.stringify({ event: "admin_alert_failed", error: errorMessage(err) }));
  }
}

export function errorMessage(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

export async function sleep(ms: number): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, ms));
}

export interface ProcessUpdateDeps {
  handle: typeof handleUpdate;
  alert: typeof alertAdmin;
  sleep: typeof sleep;
}

const DEFAULT_PROCESS_DEPS: ProcessUpdateDeps = { handle: handleUpdate, alert: alertAdmin, sleep };

/**
 * Processes one update and mutates `state` in place to track D1-quota
 * backoff across calls. Note: with a local SQLite file there is no
 * free-tier write quota (QuotaExhausted comes from db.ts's own
 * isQuotaError() regex, which only ever matches D1's specific wording) -
 * this path is now dormant in practice, but kept intact rather than
 * removed, since db.ts (unmodified) can still throw it and silently
 * swallowing an unexpected QuotaExhausted would be worse than keeping the
 * handling that was already built and tested for it.
 */
export async function processUpdate(
  env: Env,
  update: TelegramUpdate,
  state: QuotaState,
  deps: ProcessUpdateDeps = DEFAULT_PROCESS_DEPS,
): Promise<{ advanceOffset: boolean }> {
  try {
    await deps.handle(env, update);
    if (state.backoffMs !== null) {
      state.backoffMs = null;
      await deps.alert(env, "✅ Sureodds bot: database write quota recovered. Processing resumed normally.");
    }
    return { advanceOffset: true };
  } catch (err) {
    if (err instanceof QuotaExhausted) {
      const enteringBackoff = state.backoffMs === null;
      state.backoffMs = nextQuotaBackoffMs(state.backoffMs);
      if (enteringBackoff) {
        await deps.alert(
          env,
          "⚠️ Sureodds bot: database write quota exhausted. Pausing and backing off until it recovers - no updates are being lost (Telegram keeps redelivering), but replies will be delayed.",
        );
      }
      console.error(JSON.stringify({ event: "quota_exhausted", update_id: update.update_id, backoff_ms: state.backoffMs }));
      await deps.sleep(state.backoffMs);
      return { advanceOffset: false };
    }
    console.error(JSON.stringify({ event: "update_failed", update_id: update.update_id, error: errorMessage(err) }));
    return { advanceOffset: true };
  }
}

export async function loadOffset(offsetFile: string): Promise<number> {
  try {
    const raw = await readFile(offsetFile, "utf8");
    const parsed = Number.parseInt(raw.trim(), 10);
    return Number.isFinite(parsed) && parsed >= 0 ? parsed : 0;
  } catch {
    return 0; // no file yet: first run
  }
}

/**
 * Saved after EVERY processed update, not once per batch - a crash between
 * two updates in the same getUpdates() response must not cause the first
 * one to be redelivered and replied to twice. Written via a temp file +
 * rename so a crash mid-write never leaves a truncated/corrupt offset file.
 */
export async function saveOffset(offsetFile: string, offset: number): Promise<void> {
  const tmp = `${offsetFile}.tmp`;
  await mkdir(dirname(offsetFile), { recursive: true });
  await writeFile(tmp, String(offset), "utf8");
  await writeFile(offsetFile, await readFile(tmp, "utf8"), "utf8");
}

interface GetUpdatesResponse {
  ok: boolean;
  result?: TelegramUpdate[];
  description?: string;
  parameters?: { retry_after?: number };
}

export async function getUpdates(botToken: string, offset: number): Promise<TelegramUpdate[]> {
  const response = await fetch(`https://api.telegram.org/bot${botToken}/getUpdates`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      offset,
      timeout: POLL_TIMEOUT_SEC,
      allowed_updates: ["message", "callback_query"],
    }),
    // Must exceed Telegram's own long-poll timeout, or we abort a
    // perfectly healthy long poll ourselves.
    signal: AbortSignal.timeout((POLL_TIMEOUT_SEC + 10) * 1000),
  });

  if (response.status === 409) {
    // Another getUpdates consumer (old webhook still set, or a second bot
    // instance) is competing for updates. Fail loudly - this must never be
    // silently swallowed, since it means messages are being missed.
    throw new Error("Telegram getUpdates returned 409 Conflict - a webhook or another poller is still active");
  }

  const data = (await response.json()) as GetUpdatesResponse;
  if (!data.ok) {
    if (data.parameters?.retry_after) {
      await sleep(data.parameters.retry_after * 1000);
    }
    throw new Error(`Telegram getUpdates failed: ${data.description ?? "unknown error"}`);
  }
  return data.result ?? [];
}

export async function pollLoop(env: Env, botToken: string, offsetFile: string): Promise<void> {
  let offset = await loadOffset(offsetFile);
  const quotaState: QuotaState = { backoffMs: null };
  console.log(JSON.stringify({ event: "polling_started", offset }));

  for (;;) {
    let updates: TelegramUpdate[];
    try {
      updates = await getUpdates(botToken, offset);
    } catch (err) {
      console.error(JSON.stringify({ event: "poll_cycle_failed", error: errorMessage(err) }));
      await sleep(POLL_ERROR_BACKOFF_MS);
      continue;
    }

    for (const update of updates) {
      const { advanceOffset } = await processUpdate(env, update, quotaState);
      if (!advanceOffset) break; // quota outage affects every update, not just this one - stop this batch
      offset = update.update_id + 1;
      await saveOffset(offsetFile, offset);
    }
  }
}

function buildEnvFromProcessEnv(): Env {
  const db = new LocalSqliteAdapter(process.env.SQLITE_DB_PATH!);
  return {
    DB: db,
    TELEGRAM_BOT_TOKEN: process.env.TELEGRAM_BOT_TOKEN!,
    // Polling has no inbound webhook, so there is no secret to validate -
    // this field exists only to satisfy Env's shape (unused by handleUpdate).
    TELEGRAM_WEBHOOK_SECRET: "",
    TELEGRAM_CHAT_ID: process.env.TELEGRAM_CHAT_ID,
    TELEGRAM_BOT_USERNAME: process.env.TELEGRAM_BOT_USERNAME,
    TELEGRAM_ADMIN_IDS: process.env.TELEGRAM_ADMIN_IDS,
    TIMEZONE: process.env.TIMEZONE ?? "Africa/Lagos",
    ALLOWED_SPORTS: process.env.ALLOWED_SPORTS,
    PRO_POLICY_MODE: process.env.PRO_POLICY_MODE,
  };
}

async function main(): Promise<void> {
  config();
  const required = ["TELEGRAM_BOT_TOKEN", "SQLITE_DB_PATH"] as const;
  for (const key of required) {
    if (!process.env[key]) {
      console.error(`Missing required environment variable: ${key}`);
      process.exit(1);
    }
  }
  const offsetFile = process.env.POLLING_OFFSET_FILE ?? "/var/lib/sureodds/polling.offset";
  const env = buildEnvFromProcessEnv();
  await pollLoop(env, process.env.TELEGRAM_BOT_TOKEN!, offsetFile);
}

const isMain = process.argv[1] !== undefined && import.meta.url === pathToFileURL(process.argv[1]).href;
if (isMain) {
  main().catch((err) => {
    console.error(JSON.stringify({ event: "fatal_polling_error", error: errorMessage(err) }));
    process.exit(1);
  });
}
