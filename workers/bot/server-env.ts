// Shared Env construction for every process that runs on the server: the
// bot (bot.ts) and each of the 4 cron entry scripts (cron/*.ts). One place
// to keep the env var list in sync with workers/src/db.ts's Env interface,
// rather than repeating it five times.
import { config } from "dotenv";
import type { Env } from "../src/db.ts";
import { LocalSqliteAdapter } from "./local-sqlite-adapter.ts";
import { enforceRehearsalSafety } from "./rehearsal-guard.ts";

export function requireEnvVars(keys: readonly string[]): void {
  config();
  for (const key of keys) {
    if (!process.env[key]) {
      console.error(`Missing required environment variable: ${key}`);
      process.exit(1);
    }
  }
  // Runs for every process that calls requireEnvVars - bot.ts and all 4
  // cron entry scripts - so no server-side entry point can proactively
  // message anyone before this check has passed. A no-op (returns
  // immediately) unless REHEARSAL_MODE is exactly "true" - a normal
  // production run is unaffected by this call existing.
  enforceRehearsalSafety({
    rehearsalMode: process.env.REHEARSAL_MODE === "true",
    botToken: process.env.TELEGRAM_BOT_TOKEN ?? "",
    telegramChatId: process.env.TELEGRAM_CHAT_ID,
    telegramAdminIds: process.env.TELEGRAM_ADMIN_IDS,
    productionBotTokenHash: process.env.PRODUCTION_BOT_TOKEN_HASH,
    productionChatIds: process.env.PRODUCTION_CHAT_IDS,
    rehearsalAllowedAdminId: process.env.REHEARSAL_ALLOWED_ADMIN_ID,
  });
}

export function buildServerEnv(): Env {
  const db = new LocalSqliteAdapter(process.env.SQLITE_DB_PATH!);
  return {
    DB: db,
    TELEGRAM_BOT_TOKEN: process.env.TELEGRAM_BOT_TOKEN ?? "",
    TELEGRAM_WEBHOOK_SECRET: "",
    TELEGRAM_CHAT_ID: process.env.TELEGRAM_CHAT_ID,
    TELEGRAM_BOT_USERNAME: process.env.TELEGRAM_BOT_USERNAME,
    TELEGRAM_ADMIN_IDS: process.env.TELEGRAM_ADMIN_IDS,
    TIMEZONE: process.env.TIMEZONE ?? "Africa/Lagos",
    PAGE_SIZE: process.env.PAGE_SIZE,
    POST_TIME: process.env.POST_TIME,
    DELIVERY_TIME: process.env.DELIVERY_TIME,
    RECAP_TIME: process.env.RECAP_TIME,
    RESULT_GIFS_START_DATE: process.env.RESULT_GIFS_START_DATE,
    RECAP_DEADLINE: process.env.RECAP_DEADLINE,
    MATCH_START_WINDOW_MIN: process.env.MATCH_START_WINDOW_MIN,
    MATCH_END_WINDOW_HOURS: process.env.MATCH_END_WINDOW_HOURS,
    LIVE_MONITOR_INTERVAL_MIN: process.env.LIVE_MONITOR_INTERVAL_MIN,
    GENERATION_MAX_DISPATCHES_PER_DAY: process.env.GENERATION_MAX_DISPATCHES_PER_DAY,
    GITHUB_REPO: process.env.GITHUB_REPO,
    GITHUB_REF: process.env.GITHUB_REF,
    GITHUB_DISPATCH_TOKEN: process.env.GITHUB_DISPATCH_TOKEN,
    ALLOWED_SPORTS: process.env.ALLOWED_SPORTS,
    PRO_POLICY_MODE: process.env.PRO_POLICY_MODE,
    // handleScheduled() is a no-op unless this is exactly "1" (see
    // workers/src/cron.ts) - the Cron Trigger gate that previously lived
    // in wrangler.toml's [vars] now lives in the server's .env instead.
    STAGE3_CRON_ENABLED: process.env.STAGE3_CRON_ENABLED,
  } as Env;
}
