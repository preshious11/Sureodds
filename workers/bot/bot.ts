// Long-polling entry point for the Telegram bot, replacing the Cloudflare
// Worker's webhook handler. Imports handleUpdate directly from workers/src -
// same module the Worker and its own tests use, unmodified. Only the
// transport (long polling instead of an inbound webhook) and the database
// (D1RestAdapter instead of the D1 binding) differ.
//
// Scheduled work (posting/delivery/recap/watch) stays on the Worker's own
// Cron in Stage 1 - this process only ever calls handleUpdate.
import { readFile, writeFile, mkdir } from "node:fs/promises";
import { dirname } from "node:path";
import { config } from "dotenv";

import { handleUpdate } from "../src/index.ts";
import { QuotaExhausted, type Env } from "../src/db.ts";
import { D1RestAdapter } from "./d1-rest-adapter.ts";

config();

const required = ["TELEGRAM_BOT_TOKEN", "CF_ACCOUNT_ID", "CF_DB_ID", "CF_API_TOKEN"] as const;
for (const key of required) {
  if (!process.env[key]) {
    console.error(`Missing required environment variable: ${key}`);
    process.exit(1);
  }
}

const TELEGRAM_BOT_TOKEN = process.env.TELEGRAM_BOT_TOKEN!;
const OFFSET_FILE = process.env.POLLING_OFFSET_FILE ?? "/var/lib/sureodds-bot/polling.offset";
const POLL_TIMEOUT_SEC = 30; // long-poll window; Telegram holds the connection open until an update arrives or this elapses
const POLL_ERROR_BACKOFF_MS = 5_000;

const db = new D1RestAdapter(process.env.CF_ACCOUNT_ID!, process.env.CF_DB_ID!, process.env.CF_API_TOKEN!);

const env: Env = {
  DB: db,
  TELEGRAM_BOT_TOKEN,
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

async function loadOffset(): Promise<number> {
  try {
    const raw = await readFile(OFFSET_FILE, "utf8");
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
async function saveOffset(offset: number): Promise<void> {
  const tmp = `${OFFSET_FILE}.tmp`;
  await mkdir(dirname(OFFSET_FILE), { recursive: true });
  await writeFile(tmp, String(offset), "utf8");
  await writeFile(OFFSET_FILE, await readFile(tmp, "utf8"), "utf8");
}

interface TelegramUpdate {
  update_id: number;
  [key: string]: unknown;
}

interface GetUpdatesResponse {
  ok: boolean;
  result?: TelegramUpdate[];
  description?: string;
  parameters?: { retry_after?: number };
}

async function getUpdates(offset: number): Promise<TelegramUpdate[]> {
  const response = await fetch(`https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getUpdates`, {
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

async function sleep(ms: number): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, ms));
}

async function pollLoop(): Promise<void> {
  let offset = await loadOffset();
  console.log(JSON.stringify({ event: "polling_started", offset }));

  for (;;) {
    let updates: TelegramUpdate[];
    try {
      updates = await getUpdates(offset);
    } catch (err) {
      console.error(JSON.stringify({ event: "poll_cycle_failed", error: errorMessage(err) }));
      await sleep(POLL_ERROR_BACKOFF_MS);
      continue;
    }

    for (const update of updates) {
      try {
        await handleUpdate(env, update);
      } catch (err) {
        if (err instanceof QuotaExhausted) {
          // handleUpdate deliberately wrote NOTHING to D1 for this update
          // (fail-closed - see db.ts QuotaExhausted's own doc comment). The
          // webhook model relies on Telegram redelivering because the Worker
          // returns a 500; polling's equivalent is simply not advancing the
          // offset, so the next getUpdates() call hands back this same
          // update. Stop this batch and back off - the quota outage affects
          // every update, not just this one.
          console.error(JSON.stringify({ event: "quota_exhausted", update_id: update.update_id }));
          await sleep(POLL_ERROR_BACKOFF_MS);
          break;
        }
        // Every other failure path (TelegramApiError, a transient D1 error
        // that isn't quota, etc.) already left a durable record in D1 via
        // failUpdate/completeUpdate - handleUpdate's own try/catch (index.ts)
        // writes a `failed` row with an attempt count for permanent errors,
        // or marks the update `done` for ones it judges unrecoverable. The
        // existing Stage-3 Cron sweeper (still running on the Worker) is
        // what retries a `failed` row, not Telegram redelivery - so the
        // offset advances regardless, same as it would for a success.
        // Re-fetching the same update forever on one bad payload would
        // otherwise wedge the whole poll loop.
        console.error(JSON.stringify({ event: "update_failed", update_id: update.update_id, error: errorMessage(err) }));
      }
      offset = update.update_id + 1;
      await saveOffset(offset);
    }
  }
}

function errorMessage(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

pollLoop().catch((err) => {
  console.error(JSON.stringify({ event: "fatal_polling_error", error: errorMessage(err) }));
  process.exit(1);
});
