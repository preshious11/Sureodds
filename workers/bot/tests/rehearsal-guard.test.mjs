// Tests for the rehearsal safety guard: refuses to start when
// REHEARSAL_MODE=true and the configured bot token or any configured
// target chat id matches production, and is a complete no-op when
// REHEARSAL_MODE is unset/false (a normal production run must be
// unaffected by this guard existing).
import { readFileSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { describe, it } from "node:test";
import assert from "node:assert/strict";

import { checkRehearsalSafety, hashToken } from "../rehearsal-guard.ts";

const PRODUCTION_TOKEN = "123456789:AAFakeProductionTokenForTestingOnly";
const PRODUCTION_TOKEN_HASH = hashToken(PRODUCTION_TOKEN);
const PRODUCTION_GROUP_ID = "-1009999999999";
const PRODUCTION_ADMIN_ID = "8606800000";
const PRODUCTION_CHAT_IDS = `${PRODUCTION_GROUP_ID},${PRODUCTION_ADMIN_ID}`;

const TEST_TOKEN = "987654321:BBFakeTestTokenForRehearsalOnly";
const TEST_GROUP_ID = "-1001111111111";
const TEST_ADMIN_ID = "1112223333";

describe("checkRehearsalSafety: disabled (normal production run)", () => {
  it("is always safe when rehearsalMode is false, even with production values configured", () => {
    const result = checkRehearsalSafety({
      rehearsalMode: false,
      botToken: PRODUCTION_TOKEN,
      telegramChatId: PRODUCTION_GROUP_ID,
      telegramAdminIds: PRODUCTION_ADMIN_ID,
      productionBotTokenHash: PRODUCTION_TOKEN_HASH,
      productionChatIds: PRODUCTION_CHAT_IDS,
    });
    assert.equal(result.safe, true);
    assert.deepEqual(result.violations, []);
  });

  it("does nothing at all when no production values are configured to compare against", () => {
    const result = checkRehearsalSafety({ rehearsalMode: true, botToken: TEST_TOKEN });
    assert.equal(result.safe, true);
  });
});

describe("checkRehearsalSafety: rehearsal mode, token check", () => {
  it("blocks when the configured token matches production's hash", () => {
    const result = checkRehearsalSafety({
      rehearsalMode: true,
      botToken: PRODUCTION_TOKEN,
      productionBotTokenHash: PRODUCTION_TOKEN_HASH,
    });
    assert.equal(result.safe, false);
    assert.ok(result.violations.some((v) => v.includes("TELEGRAM_BOT_TOKEN")));
  });

  it("passes when the configured token does not match", () => {
    const result = checkRehearsalSafety({
      rehearsalMode: true,
      botToken: TEST_TOKEN,
      productionBotTokenHash: PRODUCTION_TOKEN_HASH,
    });
    assert.equal(result.safe, true);
  });
});

describe("checkRehearsalSafety: rehearsal mode, chat id check", () => {
  it("blocks when TELEGRAM_CHAT_ID matches a production chat id", () => {
    const result = checkRehearsalSafety({
      rehearsalMode: true,
      botToken: TEST_TOKEN,
      telegramChatId: PRODUCTION_GROUP_ID,
      productionChatIds: PRODUCTION_CHAT_IDS,
    });
    assert.equal(result.safe, false);
    assert.ok(result.violations.some((v) => v.includes(PRODUCTION_GROUP_ID)));
  });

  it("blocks when any entry of TELEGRAM_ADMIN_IDS matches a production chat id, not just the first", () => {
    const result = checkRehearsalSafety({
      rehearsalMode: true,
      botToken: TEST_TOKEN,
      telegramAdminIds: `${TEST_ADMIN_ID}, ${PRODUCTION_ADMIN_ID}`,
      productionChatIds: PRODUCTION_CHAT_IDS,
    });
    assert.equal(result.safe, false);
    assert.ok(result.violations.some((v) => v.includes(PRODUCTION_ADMIN_ID)));
  });

  it("passes when every configured chat id is distinct from every production chat id", () => {
    const result = checkRehearsalSafety({
      rehearsalMode: true,
      botToken: TEST_TOKEN,
      telegramChatId: TEST_GROUP_ID,
      telegramAdminIds: TEST_ADMIN_ID,
      productionChatIds: PRODUCTION_CHAT_IDS,
    });
    assert.equal(result.safe, true);
    assert.deepEqual(result.violations, []);
  });

  it("reports every violation found, not just the first", () => {
    const result = checkRehearsalSafety({
      rehearsalMode: true,
      botToken: PRODUCTION_TOKEN,
      telegramChatId: PRODUCTION_GROUP_ID,
      telegramAdminIds: PRODUCTION_ADMIN_ID,
      productionBotTokenHash: PRODUCTION_TOKEN_HASH,
      productionChatIds: PRODUCTION_CHAT_IDS,
    });
    assert.equal(result.safe, false);
    assert.equal(result.violations.length, 3, "token + group id + admin id should each be reported");
  });
});

describe("checkRehearsalSafety: realistic full-safe rehearsal config", () => {
  it("passes a complete, correctly-isolated rehearsal .env", () => {
    const result = checkRehearsalSafety({
      rehearsalMode: true,
      botToken: TEST_TOKEN,
      telegramChatId: TEST_GROUP_ID,
      telegramAdminIds: TEST_ADMIN_ID,
      productionBotTokenHash: PRODUCTION_TOKEN_HASH,
      productionChatIds: PRODUCTION_CHAT_IDS,
    });
    assert.equal(result.safe, true);
  });
});

describe("checkRehearsalSafety: REHEARSAL_ALLOWED_ADMIN_ID (single-account fallback)", () => {
  it("passes when the allowed admin id is the only admin id and it matches production", () => {
    const result = checkRehearsalSafety({
      rehearsalMode: true,
      botToken: TEST_TOKEN,
      telegramAdminIds: PRODUCTION_ADMIN_ID,
      productionChatIds: PRODUCTION_CHAT_IDS,
      rehearsalAllowedAdminId: PRODUCTION_ADMIN_ID,
    });
    assert.equal(result.safe, true);
    assert.deepEqual(result.violations, []);
  });

  it("still blocks a different production id even when an allowance is configured", () => {
    const OTHER_PRODUCTION_ADMIN_ID = "7778889999";
    const result = checkRehearsalSafety({
      rehearsalMode: true,
      botToken: TEST_TOKEN,
      telegramAdminIds: OTHER_PRODUCTION_ADMIN_ID,
      productionChatIds: `${PRODUCTION_CHAT_IDS},${OTHER_PRODUCTION_ADMIN_ID}`,
      rehearsalAllowedAdminId: PRODUCTION_ADMIN_ID,
    });
    assert.equal(result.safe, false);
    assert.ok(result.violations.some((v) => v.includes(OTHER_PRODUCTION_ADMIN_ID)));
  });

  it("never allows the allowance to cover TELEGRAM_CHAT_ID, even if set to the same value", () => {
    const result = checkRehearsalSafety({
      rehearsalMode: true,
      botToken: TEST_TOKEN,
      telegramChatId: PRODUCTION_ADMIN_ID,
      productionChatIds: PRODUCTION_CHAT_IDS,
      rehearsalAllowedAdminId: PRODUCTION_ADMIN_ID,
    });
    assert.equal(result.safe, false);
    assert.ok(result.violations.some((v) => v.includes(PRODUCTION_ADMIN_ID)));
  });

  it("never allows a negative (group/channel-shaped) id through the allowance", () => {
    const result = checkRehearsalSafety({
      rehearsalMode: true,
      botToken: TEST_TOKEN,
      telegramAdminIds: PRODUCTION_GROUP_ID,
      productionChatIds: PRODUCTION_CHAT_IDS,
      rehearsalAllowedAdminId: PRODUCTION_GROUP_ID,
    });
    assert.equal(result.safe, false);
    assert.ok(result.violations.some((v) => v.includes(PRODUCTION_GROUP_ID)));
  });

  it("has no effect at all when unset - existing two-account behaviour is unchanged", () => {
    const result = checkRehearsalSafety({
      rehearsalMode: true,
      botToken: TEST_TOKEN,
      telegramAdminIds: PRODUCTION_ADMIN_ID,
      productionChatIds: PRODUCTION_CHAT_IDS,
    });
    assert.equal(result.safe, false);
    assert.ok(result.violations.some((v) => v.includes(PRODUCTION_ADMIN_ID)));
  });
});

describe("enforceRehearsalSafety: actually exits the process on a violation", () => {
  it("a child process invoking enforceRehearsalSafety with a matching token exits non-zero and logs the violation", () => {
    const scriptUrl = new URL("../rehearsal-guard.ts", import.meta.url).href;
    let threw = false;
    let stderr = "";
    try {
      execFileSync(
        process.execPath,
        ["-e", `import('${scriptUrl}').then(m => m.enforceRehearsalSafety({ rehearsalMode: true, botToken: 't', productionBotTokenHash: m.hashToken('t') }))`],
        { encoding: "utf8" },
      );
    } catch (err) {
      threw = true;
      stderr = err.stderr;
      assert.equal(err.status, 1);
    }
    assert.equal(threw, true, "the process must exit non-zero on a violation");
    assert.match(stderr, /REHEARSAL GUARD/);
  });
});

describe("subscriber delivery is structurally disabled - sweepDeliveries is never reachable from handleScheduled", () => {
  it("handleScheduled's own source never calls sweepDeliveries for any of the 4 cron dispatches", () => {
    // Textual check, not a behavioural one: workers/src/cron.ts is
    // unmodified (reused as-is, per the no-copying/no-rewriting rule), so
    // this proves the CURRENT source has no subscriber-broadcast call in
    // the scheduled-dispatch path - every proactive send targets only
    // TELEGRAM_CHAT_ID/TELEGRAM_ADMIN_IDS (ownerChatId), never a
    // subscribers-table row. If this ever changes, this test catches it.
    const cronSource = readFileSync(new URL("../../src/cron.ts", import.meta.url), "utf8");
    const handleScheduledStart = cronSource.indexOf("export async function handleScheduled");
    assert.ok(handleScheduledStart >= 0, "handleScheduled not found in cron.ts - has it moved or been renamed?");
    const handleScheduledBody = cronSource.slice(handleScheduledStart);
    assert.doesNotMatch(handleScheduledBody, /sweepDeliveries\(/, "sweepDeliveries must stay unreachable from the scheduled-dispatch path");
  });
});
