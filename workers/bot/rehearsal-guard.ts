// Rehearsal safety guard: when REHEARSAL_MODE=true, refuses to start the
// bot or any cron entry point if the configured bot token or any
// configured target chat id matches production - before anything else
// runs, before a single Telegram call is made.
//
// Why a hash, not the raw token: PRODUCTION_BOT_TOKEN_HASH lets .env
// declare "this is what production's token looks like, to compare
// against" without ever writing the real production token into the
// rehearsal server's .env at all - only its sha256 is needed for the
// comparison. Chat ids are not secret the way a token is, but
// PRODUCTION_CHAT_IDS is still read from .env, never hardcoded, so this
// file carries no real IDs either (see docs/STAGE1_REHEARSAL_PREREQUISITES.md).
//
// Every proactive send path in workers/src/cron.ts (post/recap/match-event
// sweeps, Pro-picks-to-admin) targets only TELEGRAM_CHAT_ID and the first
// entry of TELEGRAM_ADMIN_IDS - never a subscriber row from the database
// (confirmed by audit: sweepDeliveries, the one function that WOULD
// message individual subscribers, is never called from handleScheduled -
// see rehearsal-guard.test.mjs's own regression test for this). So
// guarding TELEGRAM_CHAT_ID and TELEGRAM_ADMIN_IDS here covers every
// chat the system can proactively message.
import { createHash } from "node:crypto";

export interface RehearsalGuardInput {
  rehearsalMode: boolean;
  botToken: string;
  telegramChatId?: string;
  telegramAdminIds?: string;
  productionBotTokenHash?: string;
  productionChatIds?: string;
  /**
   * One personal Telegram user id allowed to double as the rehearsal admin
   * even though it also appears in PRODUCTION_CHAT_IDS - for when a second
   * Telegram account isn't available. Only ever checked against
   * TELEGRAM_ADMIN_IDS entries, never against TELEGRAM_CHAT_ID (the
   * group/channel), and only when the id is positive - Telegram group and
   * channel ids are negative, so a negative value here can never match and
   * can't be used to smuggle a group/channel past the guard.
   */
  rehearsalAllowedAdminId?: string;
}

export interface RehearsalGuardResult {
  safe: boolean;
  violations: string[];
}

export function hashToken(token: string): string {
  return createHash("sha256").update(token).digest("hex");
}

function splitIds(csv: string | undefined): string[] {
  return (csv ?? "").split(",").map((s) => s.trim()).filter(Boolean);
}

/**
 * Returns { safe: true } immediately when rehearsalMode is false - this
 * guard does nothing and changes nothing about a normal production run
 * (the bot/crons running against real D1... now the real server... with
 * REHEARSAL_MODE unset or "false" is unaffected by this file's existence).
 */
export function checkRehearsalSafety(input: RehearsalGuardInput): RehearsalGuardResult {
  if (!input.rehearsalMode) {
    return { safe: true, violations: [] };
  }

  const violations: string[] = [];

  if (input.productionBotTokenHash) {
    if (hashToken(input.botToken) === input.productionBotTokenHash) {
      violations.push("TELEGRAM_BOT_TOKEN matches the production bot token (hash comparison) - refusing to start in rehearsal mode");
    }
  }

  const productionChatIds = new Set(splitIds(input.productionChatIds));
  if (productionChatIds.size > 0) {
    const allowedAdminId = input.rehearsalAllowedAdminId?.trim();
    // TELEGRAM_CHAT_ID is always the group/channel - the allowance never
    // applies here, regardless of what it's set to.
    if (input.telegramChatId) {
      const id = input.telegramChatId.trim();
      if (productionChatIds.has(id)) {
        violations.push(`configured chat id '${id}' matches a production chat id - refusing to start in rehearsal mode`);
      }
    }
    for (const id of splitIds(input.telegramAdminIds)) {
      if (!productionChatIds.has(id)) continue;
      const isAllowedPersonalAdmin = allowedAdminId !== undefined && allowedAdminId !== "" && id === allowedAdminId && !id.startsWith("-");
      if (isAllowedPersonalAdmin) continue;
      violations.push(`configured chat id '${id}' matches a production chat id - refusing to start in rehearsal mode`);
    }
  }

  return { safe: violations.length === 0, violations };
}

/** Logs every violation and exits the process - the shared bootstrap action for bot.ts and every cron/*.ts entry point. */
export function enforceRehearsalSafety(input: RehearsalGuardInput): void {
  const result = checkRehearsalSafety(input);
  if (!result.safe) {
    for (const v of result.violations) {
      console.error(`REHEARSAL GUARD: ${v}`);
    }
    process.exit(1);
  }
}
