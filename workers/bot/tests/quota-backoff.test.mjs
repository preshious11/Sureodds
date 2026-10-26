// Tests the QuotaExhausted backoff/alert state machine in processUpdate()
// in isolation, via injected fakes (no real network, no real D1, no live
// polling). Importing bot.ts does not start polling or read process.env
// here - the bootstrap is gated behind an isMain check (see bot.ts).
import { describe, it } from "node:test";
import assert from "node:assert/strict";

import { processUpdate, nextQuotaBackoffMs, adminChatId, QUOTA_BACKOFF_INITIAL_MS, QUOTA_BACKOFF_MAX_MS } from "../bot.ts";
import { QuotaExhausted } from "../../src/db.ts";

const FAKE_ENV = { DB: {}, TELEGRAM_BOT_TOKEN: "test", TELEGRAM_WEBHOOK_SECRET: "", TELEGRAM_ADMIN_IDS: "999" };

function fakeDeps({ handleResults, sleeps = [], alerts = [] } = {}) {
  let call = 0;
  return {
    deps: {
      async handle(_env, _update) {
        const outcome = handleResults[call++];
        if (outcome instanceof Error) throw outcome;
        return outcome;
      },
      async alert(_env, text) {
        alerts.push(text);
      },
      async sleep(ms) {
        sleeps.push(ms);
      },
    },
    sleeps,
    alerts,
  };
}

describe("nextQuotaBackoffMs", () => {
  it("starts at the initial backoff from a healthy state", () => {
    assert.equal(nextQuotaBackoffMs(null), QUOTA_BACKOFF_INITIAL_MS);
  });

  it("doubles on each consecutive hit", () => {
    assert.equal(nextQuotaBackoffMs(QUOTA_BACKOFF_INITIAL_MS), QUOTA_BACKOFF_INITIAL_MS * 2);
    assert.equal(nextQuotaBackoffMs(QUOTA_BACKOFF_INITIAL_MS * 2), QUOTA_BACKOFF_INITIAL_MS * 4);
  });

  it("caps at QUOTA_BACKOFF_MAX_MS and never exceeds it", () => {
    assert.equal(nextQuotaBackoffMs(QUOTA_BACKOFF_MAX_MS), QUOTA_BACKOFF_MAX_MS);
    assert.equal(nextQuotaBackoffMs(QUOTA_BACKOFF_MAX_MS / 2 + 1), QUOTA_BACKOFF_MAX_MS);
  });
});

describe("adminChatId", () => {
  it("returns the first entry of TELEGRAM_ADMIN_IDS", () => {
    assert.equal(adminChatId({ TELEGRAM_ADMIN_IDS: "111, 222,333" }), "111");
  });

  it("returns null when unset or empty", () => {
    assert.equal(adminChatId({}), null);
    assert.equal(adminChatId({ TELEGRAM_ADMIN_IDS: "" }), null);
  });
});

describe("processUpdate: quota backoff state machine", () => {
  it("a single QuotaExhausted hit starts the backoff at the initial delay, sends exactly one alert, and does not advance the offset", async () => {
    const { deps, sleeps, alerts } = fakeDeps({ handleResults: [new QuotaExhausted()] });
    const state = { backoffMs: null };
    const result = await processUpdate(FAKE_ENV, { update_id: 1 }, state, deps);

    assert.equal(result.advanceOffset, false);
    assert.equal(state.backoffMs, QUOTA_BACKOFF_INITIAL_MS);
    assert.deepEqual(sleeps, [QUOTA_BACKOFF_INITIAL_MS]);
    assert.equal(alerts.length, 1);
    assert.match(alerts[0], /quota exhausted/i);
  });

  it("consecutive hits double the backoff and send NO further alerts (only the first entry alerts)", async () => {
    const { deps, sleeps, alerts } = fakeDeps({
      handleResults: [new QuotaExhausted(), new QuotaExhausted(), new QuotaExhausted()],
    });
    const state = { backoffMs: null };
    await processUpdate(FAKE_ENV, { update_id: 1 }, state, deps);
    await processUpdate(FAKE_ENV, { update_id: 1 }, state, deps);
    await processUpdate(FAKE_ENV, { update_id: 1 }, state, deps);

    assert.deepEqual(sleeps, [QUOTA_BACKOFF_INITIAL_MS, QUOTA_BACKOFF_INITIAL_MS * 2, QUOTA_BACKOFF_INITIAL_MS * 4]);
    assert.equal(alerts.length, 1, "only the transition into backoff should alert, not every retry");
  });

  it("backoff never exceeds the 15-minute cap even after many consecutive hits", async () => {
    const results = Array.from({ length: 10 }, () => new QuotaExhausted());
    const { deps, sleeps } = fakeDeps({ handleResults: results });
    const state = { backoffMs: null };
    for (let i = 0; i < results.length; i++) {
      await processUpdate(FAKE_ENV, { update_id: 1 }, state, deps);
    }
    assert.equal(state.backoffMs, QUOTA_BACKOFF_MAX_MS);
    assert.ok(sleeps.every((ms) => ms <= QUOTA_BACKOFF_MAX_MS));
    assert.equal(sleeps[sleeps.length - 1], QUOTA_BACKOFF_MAX_MS);
  });

  it("a success after backoff sends exactly one recovery alert and resets state to healthy", async () => {
    const { deps, alerts } = fakeDeps({ handleResults: [new QuotaExhausted(), undefined] });
    const state = { backoffMs: null };
    await processUpdate(FAKE_ENV, { update_id: 1 }, state, deps); // enters backoff
    assert.equal(alerts.length, 1);

    const result = await processUpdate(FAKE_ENV, { update_id: 2 }, state, deps); // recovers
    assert.equal(result.advanceOffset, true);
    assert.equal(state.backoffMs, null);
    assert.equal(alerts.length, 2);
    assert.match(alerts[1], /recovered/i);
  });

  it("a healthy run (no quota issue) never alerts and always advances the offset", async () => {
    const { deps, alerts, sleeps } = fakeDeps({ handleResults: [undefined] });
    const state = { backoffMs: null };
    const result = await processUpdate(FAKE_ENV, { update_id: 1 }, state, deps);
    assert.equal(result.advanceOffset, true);
    assert.equal(alerts.length, 0);
    assert.equal(sleeps.length, 0);
  });

  it("a non-quota error still advances the offset (db.ts's own failed-row bookkeeping is the retry path, not Telegram redelivery)", async () => {
    const { deps } = fakeDeps({ handleResults: [new Error("telegram sendMessage failed: 500")] });
    const state = { backoffMs: null };
    const result = await processUpdate(FAKE_ENV, { update_id: 1 }, state, deps);
    assert.equal(result.advanceOffset, true);
    assert.equal(state.backoffMs, null, "a non-quota error must not be mistaken for a quota backoff");
  });

  it("entering backoff twice in separate outage episodes alerts once per episode, not once total", async () => {
    const { deps, alerts } = fakeDeps({
      handleResults: [new QuotaExhausted(), undefined, new QuotaExhausted(), undefined],
    });
    const state = { backoffMs: null };
    await processUpdate(FAKE_ENV, { update_id: 1 }, state, deps); // exhausted #1
    await processUpdate(FAKE_ENV, { update_id: 2 }, state, deps); // recovered #1
    await processUpdate(FAKE_ENV, { update_id: 3 }, state, deps); // exhausted #2 (fresh episode)
    await processUpdate(FAKE_ENV, { update_id: 4 }, state, deps); // recovered #2

    const exhaustedAlerts = alerts.filter((a) => /exhausted/i.test(a));
    const recoveredAlerts = alerts.filter((a) => /recovered/i.test(a));
    assert.equal(exhaustedAlerts.length, 2);
    assert.equal(recoveredAlerts.length, 2);
  });
});
