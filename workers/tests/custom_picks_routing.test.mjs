import { DatabaseSync } from "node:sqlite";
import { readFileSync } from "node:fs";
import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { handleUpdate } from "../src/index.ts";

// Real SQLite, real migration files — the actual schema, not a hand-rolled
// subset, so a column rename in a migration breaks this test instead of a
// live chat. Supports .first()/.all()/.run(), unlike the lighter per-SQL
// stubs elsewhere in this suite.
function realDb() {
  const sql = new DatabaseSync(":memory:");
  // 0002 only ALTERs columns that 0001 already defines inline for a fresh
  // DB (see 0001's own comment) — applying both to a brand-new :memory: DB
  // would duplicate those columns, so 0002 is skipped here deliberately.
  for (const file of ["0001_foundation.sql", "0003_engine_state.sql", "0006_custom_sessions.sql", "0007_custom_picks_conversation.sql"]) {
    sql.exec(readFileSync(new URL(`../migrations/${file}`, import.meta.url), "utf8"));
  }
  return {
    _sql: sql,
    prepare(query) {
      return {
        bind(...args) {
          return {
            async first() {
              return sql.prepare(query).get(...args) ?? null;
            },
            async all() {
              return { results: sql.prepare(query).all(...args) };
            },
            async run() {
              const info = sql.prepare(query).run(...args);
              return { meta: { changes: info.changes } };
            },
          };
        },
      };
    },
  };
}

function textMessage(updateId, chatId, userId, text, chatType = "private") {
  return {
    update_id: updateId,
    message: {
      message_id: updateId,
      chat: { id: chatId, type: chatType },
      from: { id: userId, first_name: "Ada" },
      text,
    },
  };
}

function stubFetch(responder) {
  const calls = [];
  const fn = async (url, init) => {
    calls.push({ url, body: init?.body ? JSON.parse(init.body) : null });
    return responder ? responder(calls[calls.length - 1]) : { ok: true, status: 200, json: async () => ({ ok: true }) };
  };
  fn.calls = calls;
  return fn;
}

async function withFetch(fetchImpl, fn) {
  const real = globalThis.fetch;
  globalThis.fetch = fetchImpl;
  try {
    return await fn();
  } finally {
    globalThis.fetch = real;
  }
}

function env(db) {
  return { DB: db, TELEGRAM_BOT_TOKEN: "t", TELEGRAM_WEBHOOK_SECRET: "s", TIMEZONE: "Africa/Lagos", PAGE_SIZE: "5" };
}

function seedPool(sql, { day = "2026-10-14", generatedAt = "2026-10-14T06:00:00Z" } = {}) {
  sql.exec(
    `INSERT INTO custom_pick_pools VALUES('${day}','Africa/Lagos','${generatedAt}','COMPLETE','{}')`,
  );
  const payload = JSON.stringify({
    prediction_date: day,
    timezone: "Africa/Lagos",
    canonical_event_id: "fx-1",
    sport: "football",
    market: "result2",
    selection: "home",
    price: 1.8,
    p_model: 0.7,
    start_utc: `${day}T20:00:00Z`,
    generated_at: generatedAt,
    home: "Arsenal",
    away: "Chelsea",
    league: "Premier League",
  }).replace(/'/g, "''");
  sql.exec(
    `INSERT INTO custom_pick_candidates VALUES('${day}','Africa/Lagos','cand-1','fx-1','${payload}','${generatedAt}','${generatedAt}',1)`,
  );
}

function seedCandidate(sql, day, generatedAt, overrides = {}) {
  const base = {
    prediction_date: day, timezone: "Africa/Lagos", canonical_event_id: "fx-1",
    sport: "football", market: "result2", selection: "home", price: 1.8, p_model: 0.7,
    start_utc: `${day}T20:00:00Z`, generated_at: generatedAt, home: "Arsenal", away: "Chelsea",
    league: "Premier League", ...overrides,
  };
  const id = base.canonical_event_id;
  const payload = JSON.stringify(base).replace(/'/g, "''");
  sql.exec(
    `INSERT INTO custom_pick_candidates VALUES('${day}','Africa/Lagos','cand-${id}','${id}','${payload}','${generatedAt}','${generatedAt}',1)`,
  );
}

function callbackQuery(updateId, callbackId, chatId, userId, data) {
  return {
    update_id: updateId,
    callback_query: {
      id: callbackId,
      data,
      from: { id: userId },
      message: { message_id: updateId, chat: { id: chatId, type: "private" } },
    },
  };
}

describe("Custom Picks: group button -> private deep link -> request -> failure -> alternative -> tap", () => {
  it("a request with too high a pick count gets an honest failure plus a tappable alternative that delivers a real ticket", async () => {
    const db = realDb();
    const day = "2026-10-14";
    const generatedAt = "2026-10-14T06:00:00Z";
    db._sql.exec(`INSERT INTO custom_pick_pools VALUES('${day}','Africa/Lagos','${generatedAt}','COMPLETE','{}')`);
    seedCandidate(db._sql, day, generatedAt, { canonical_event_id: "fx-1", home: "Arsenal", away: "Chelsea" });
    seedCandidate(db._sql, day, generatedAt, { canonical_event_id: "fx-2", home: "Barca", away: "Real", price: 1.6, p_model: 0.65 });
    const theEnv = { DB: db, TELEGRAM_BOT_TOKEN: "t", TELEGRAM_WEBHOOK_SECRET: "s", TIMEZONE: "Africa/Lagos", PAGE_SIZE: "5" };

    // 1. The group button's deep link (?start=custom_picks) lands here as a
    //    private /start custom_picks — the only way Telegram ever invokes it.
    await withFetch(stubFetch(), () => handleUpdate(theEnv, textMessage(1, "c1", "u1", "/start custom_picks")));

    // 2. Ask for more picks than the (2-fixture) archive can satisfy ->
    //    insufficient_count, with a proven, tappable alternative attached.
    const requestFetch = stubFetch();
    await withFetch(requestFetch, () => handleUpdate(theEnv, textMessage(2, "c1", "u1", "5 picks")));
    assert.equal(requestFetch.calls.length, 1);
    assert.match(requestFetch.calls[0].body.text, /Try these instead/);
    const keyboard = requestFetch.calls[0].body.reply_markup;
    assert.ok(keyboard?.inline_keyboard?.length, "a real alternative must be offered, not a dead end");
    const token = keyboard.inline_keyboard[0][0].callback_data;
    assert.match(token, /^cp\|/);

    // 3. Tapping that button must deliver an actual ticket, not a promise.
    const tapFetch = stubFetch();
    await withFetch(tapFetch, () =>
      handleUpdate(theEnv, callbackQuery(3, "cbq-1", "c1", "u1", token)),
    );
    const sent = tapFetch.calls.find((c) => c.url.includes("sendMessage"));
    assert.ok(sent, "the tap must deliver a message");
    assert.match(sent.body.text, /🔥/);
  });
  it("a tap from a different chat/user than the suggestion was prepared for is refused", async () => {
    const db = realDb();
    const day = "2026-10-14";
    const generatedAt = "2026-10-14T06:00:00Z";
    db._sql.exec(`INSERT INTO custom_pick_pools VALUES('${day}','Africa/Lagos','${generatedAt}','COMPLETE','{}')`);
    seedCandidate(db._sql, day, generatedAt, { canonical_event_id: "fx-1" });
    seedCandidate(db._sql, day, generatedAt, { canonical_event_id: "fx-2", price: 1.6, p_model: 0.65 });
    const theEnv = { DB: db, TELEGRAM_BOT_TOKEN: "t", TELEGRAM_WEBHOOK_SECRET: "s", TIMEZONE: "Africa/Lagos", PAGE_SIZE: "5" };
    await withFetch(stubFetch(), () => handleUpdate(theEnv, textMessage(1, "c1", "u1", "/start custom_picks")));
    const requestFetch = stubFetch();
    await withFetch(requestFetch, () => handleUpdate(theEnv, textMessage(2, "c1", "u1", "5 picks")));
    const token = requestFetch.calls[0].body.reply_markup.inline_keyboard[0][0].callback_data;
    const tapFetch = stubFetch();
    await withFetch(tapFetch, () => handleUpdate(theEnv, callbackQuery(3, "cbq-1", "c1", "u2", token)));
    const toast = tapFetch.calls.find((c) => c.url.includes("answerCallbackQuery"));
    assert.match(toast.body.text, /Open your own Custom Picks/);
    const sendMessageCalls = tapFetch.calls.filter((c) => c.url.includes("sendMessage"));
    assert.equal(sendMessageCalls.length, 0, "no ticket must ever be delivered to the wrong user");
  });
});

describe("/menu admin command — the group's Custom Picks entry point", () => {
  it("a non-admin typing /menu in the group gets 'Not allowed.', nothing posted to the group", async () => {
    const db = realDb();
    const fetchImpl = stubFetch();
    const envWithAdmin = { ...env(db), TELEGRAM_CHAT_ID: "group-1", TELEGRAM_BOT_USERNAME: "SureOddsBot", TELEGRAM_ADMIN_IDS: "999" };
    await withFetch(fetchImpl, () => handleUpdate(envWithAdmin, textMessage(1, "group-1", "u1", "/menu", "supergroup")));
    assert.equal(fetchImpl.calls.length, 1);
    assert.equal(fetchImpl.calls[0].body.chat_id, "group-1");
    assert.match(fetchImpl.calls[0].body.text, /Not allowed/);
  });
  it("an admin typing /menu (from anywhere) posts the community menu with the Custom Picks button to the configured group", async () => {
    const db = realDb();
    const fetchImpl = stubFetch();
    const envWithAdmin = { ...env(db), TELEGRAM_CHAT_ID: "group-1", TELEGRAM_BOT_USERNAME: "SureOddsBot", TELEGRAM_ADMIN_IDS: "7" };
    await withFetch(fetchImpl, () => handleUpdate(envWithAdmin, textMessage(1, "private-7", "7", "/menu")));
    assert.equal(fetchImpl.calls.length, 2);
    const posted = fetchImpl.calls.find((c) => c.body.chat_id === "group-1");
    assert.ok(posted, "menu must be posted to the group, not the admin's own chat");
    const kb = JSON.stringify(posted.body.reply_markup);
    assert.match(kb, /✨ Custom Picks/);
    assert.match(kb, /https:\/\/t\.me\/SureOddsBot\?start=custom_picks/);
    const ack = fetchImpl.calls.find((c) => c.body.chat_id === "private-7");
    assert.match(ack.body.text, /posted/);
  });
  it("refuses to post without a configured group destination or bot username", async () => {
    const db = realDb();
    const noChat = { ...env(db), TELEGRAM_BOT_USERNAME: "SureOddsBot", TELEGRAM_ADMIN_IDS: "7" };
    const f1 = stubFetch();
    await withFetch(f1, () => handleUpdate(noChat, textMessage(1, "private-7", "7", "/menu")));
    assert.equal(f1.calls.length, 1);
    assert.match(f1.calls[0].body.text, /not configured/);
    const noUsername = { ...env(db), TELEGRAM_CHAT_ID: "group-1", TELEGRAM_ADMIN_IDS: "7" };
    const f2 = stubFetch();
    await withFetch(f2, () => handleUpdate(noUsername, textMessage(2, "private-7", "7", "/menu")));
    assert.equal(f2.calls.length, 1);
    assert.match(f2.calls[0].body.text, /username/);
  });
});

describe("Custom Picks private-chat access rules (index.ts routing)", () => {
  it("/start custom_picks sends the exact production INTRO text, private chat only", async () => {
    const db = realDb();
    const fetchImpl = stubFetch();
    await withFetch(fetchImpl, () => handleUpdate(env(db), textMessage(1, "c1", "u1", "/start custom_picks")));
    assert.equal(fetchImpl.calls.length, 1);
    assert.match(fetchImpl.calls[0].body.text, /✨ Custom Picks/);
    const row = db._sql.prepare("SELECT active FROM custom_sessions WHERE user_id='u1' AND chat_id='c1'").get();
    assert.equal(row.active, 1);
  });
  it("/start custom_picks in a group is refused: no session created, nothing sent", async () => {
    const db = realDb();
    const fetchImpl = stubFetch();
    await withFetch(fetchImpl, () =>
      handleUpdate(env(db), textMessage(1, "g1", "u1", "/start custom_picks", "supergroup")),
    );
    assert.equal(fetchImpl.calls.length, 0);
    const row = db._sql.prepare("SELECT 1 FROM custom_sessions WHERE chat_id='g1'").get();
    assert.equal(row, undefined);
  });
  it("plain /start leaves an active Custom Picks session", async () => {
    const db = realDb();
    await withFetch(stubFetch(), () => handleUpdate(env(db), textMessage(1, "c1", "u1", "/start custom_picks")));
    await withFetch(stubFetch(), () => handleUpdate(env(db), textMessage(2, "c1", "u1", "/start")));
    const row = db._sql.prepare("SELECT active FROM custom_sessions WHERE user_id='u1' AND chat_id='c1'").get();
    assert.equal(row.active, 0);
  });
  it("/stop leaves an active Custom Picks session", async () => {
    const db = realDb();
    await withFetch(stubFetch(), () => handleUpdate(env(db), textMessage(1, "c1", "u1", "/start custom_picks")));
    await withFetch(stubFetch(), () => handleUpdate(env(db), textMessage(2, "c1", "u1", "/stop")));
    const row = db._sql.prepare("SELECT active FROM custom_sessions WHERE user_id='u1' AND chat_id='c1'").get();
    assert.equal(row.active, 0);
  });
  it("free text outside an active session does NOT route into Custom Picks", async () => {
    const db = realDb();
    const fetchImpl = stubFetch();
    await withFetch(fetchImpl, () => handleUpdate(env(db), textMessage(1, "c1", "u1", "3 basketball picks")));
    // Falls through to the generic welcome-back reply, not a ticket/HELP reply.
    assert.equal(fetchImpl.calls.length, 1);
    assert.match(fetchImpl.calls[0].body.text, /Use the menu below/);
  });
  it("a request inside an active session builds a real ticket from archived candidates", async () => {
    const db = realDb();
    seedPool(db._sql);
    await withFetch(stubFetch(), () => handleUpdate(env(db), textMessage(1, "c1", "u1", "/start custom_picks")));
    const fetchImpl = stubFetch();
    await withFetch(fetchImpl, () => handleUpdate(env(db), textMessage(2, "c1", "u1", "1 picks"), ));
    assert.equal(fetchImpl.calls.length, 1);
    assert.match(fetchImpl.calls[0].body.text, /🔥/);
    assert.match(fetchImpl.calls[0].body.text, /Arsenal vs Chelsea/);
  });
  it("UNAVAILABLE (not a fabricated ticket) when no pool has been captured for today", async () => {
    const db = realDb(); // no seedPool
    await withFetch(stubFetch(), () => handleUpdate(env(db), textMessage(1, "c1", "u1", "/start custom_picks")));
    const fetchImpl = stubFetch();
    await withFetch(fetchImpl, () => handleUpdate(env(db), textMessage(2, "c1", "u1", "3 picks")));
    assert.match(fetchImpl.calls[0].body.text, /Still cooking/);
  });
  it("rapid repeat requests are throttled by the cooldown, not silently dropped or double-charged", async () => {
    const db = realDb();
    seedPool(db._sql);
    await withFetch(stubFetch(), () => handleUpdate(env(db), textMessage(1, "c1", "u1", "/start custom_picks")));
    const fetchImpl = stubFetch();
    await withFetch(fetchImpl, async () => {
      await handleUpdate(env(db), textMessage(2, "c1", "u1", "1 picks"));
      await handleUpdate(env(db), textMessage(3, "c1", "u1", "1 picks"));
    });
    assert.equal(fetchImpl.calls.length, 2);
    assert.match(fetchImpl.calls[0].body.text, /🔥/);
    assert.match(fetchImpl.calls[1].body.text, /Still working on your last request/);
  });
});

describe("Custom Picks production archive shape end-to-end (2026-10-14 regression)", () => {
  // The production D1 payload_json never carries prediction_date/timezone/
  // generated_at (capture_candidate serializes evaluated fields only); the
  // row columns do. Seed exactly that shape: before the row-merge fix this
  // test replied "Nothing made the cut" with a COMPLETE pool and usable
  // fixtures. The clock is frozen so the Lagos day/timezone derivation in
  // index.ts (lagosDateString + TIMEZONE) is asserted deterministically.
  const FROZEN_NOW = Date.UTC(2026, 9, 14, 8, 0); // 09:00 Africa/Lagos
  function seedProductionRow(sql, day, generatedAt, overrides = {}) {
    const payload = JSON.stringify({
      provider: "sgo",
      provider_event_id: "ev-1",
      sport: "football",
      league: "Premier League",
      home: "Arsenal",
      away: "Chelsea",
      start_utc: `${day}T20:00:00Z`,
      league_id: "epl",
      extra: {},
      event_sources: ["sgo"],
      canonical_event_id: "fx-1",
      canonical_competition_id: "epl",
      home_id: "ars",
      away_id: "che",
      market: "result2",
      selection: "home",
      price: 1.8,
      period: "full",
      stat: "points",
      entity: "all",
      line: null,
      market_label: "Home",
      p_market: 0.55,
      p_model: 0.7,
      sources: { sgo: 0.7 },
      agreement: 0.8,
      ev: 0.05,
      edge: 0.15,
      label: "Arsenal to Win",
      liquidity: 0.9,
      edge_ev: 0.03,
      bookmaker_count: 5,
      provider_count: 1,
      rank_score: null,
      quality: null,
      se: null,
      p_lcb: null,
      ...overrides,
    }).replace(/'/g, "''");
    const id = overrides.canonical_event_id ?? "fx-1";
    sql.exec(
      `INSERT INTO custom_pick_candidates VALUES('${day}','Africa/Lagos','cand-${id}','${id}','${payload}','${generatedAt}','${generatedAt}',1)`,
    );
  }
  async function withFrozenClock(fn) {
    const realNow = Date.now;
    Date.now = () => FROZEN_NOW;
    try {
      return await fn();
    } finally {
      Date.now = realNow;
    }
  }
  it("a production-shaped pool serves a real ticket, proving the handler derives the Lagos day/zone correctly", async () => {
    const db = realDb();
    const day = "2026-10-14";
    const generatedAt = "2026-10-14T06:00:00Z";
    db._sql.exec(`INSERT INTO custom_pick_pools VALUES('${day}','Africa/Lagos','${generatedAt}','COMPLETE','{}')`);
    // 1.8 × 1.6 × 1.9 = 5.47, inside the ±10% band around 5.
    seedProductionRow(db._sql, day, generatedAt);
    seedProductionRow(db._sql, day, generatedAt, { canonical_event_id: "fx-2", home: "Barca", away: "Real", price: 1.6, p_model: 0.65 });
    seedProductionRow(db._sql, day, generatedAt, { canonical_event_id: "fx-3", home: "Bayern", away: "Dortmund", price: 1.9, p_model: 0.68 });
    await withFrozenClock(async () => {
      await withFetch(stubFetch(), () => handleUpdate(env(db), textMessage(1, "c1", "u1", "/start custom_picks")));
      const fetchImpl = stubFetch();
      await withFetch(fetchImpl, () => handleUpdate(env(db), textMessage(2, "c1", "u1", "give me 5 odds")));
      assert.equal(fetchImpl.calls.length, 1);
      assert.match(fetchImpl.calls[0].body.text, /🔥/);
      assert.match(fetchImpl.calls[0].body.text, /Arsenal vs Chelsea/);
    });
  });
  it("a production-shaped pool answers 30 odds honestly (unreachable) instead of an empty-pool message", async () => {
    const db = realDb();
    const day = "2026-10-14";
    const generatedAt = "2026-10-14T06:00:00Z";
    db._sql.exec(`INSERT INTO custom_pick_pools VALUES('${day}','Africa/Lagos','${generatedAt}','COMPLETE','{}')`);
    seedProductionRow(db._sql, day, generatedAt);
    seedProductionRow(db._sql, day, generatedAt, { canonical_event_id: "fx-2", home: "Barca", away: "Real", price: 1.6, p_model: 0.65 });
    await withFrozenClock(async () => {
      await withFetch(stubFetch(), () => handleUpdate(env(db), textMessage(1, "c1", "u1", "/start custom_picks")));
      const fetchImpl = stubFetch();
      await withFetch(fetchImpl, () => handleUpdate(env(db), textMessage(2, "c1", "u1", "Give me 30 odds")));
      assert.equal(fetchImpl.calls.length, 1);
      assert.doesNotMatch(fetchImpl.calls[0].body.text, /Nothing made the cut/);
    });
  });
});

describe("failed Telegram sends during webhook processing", () => {
  it("a 5xx from the Telegram API fails the update and leaves it retryable, never silently dropped", async () => {
    const db = realDb();
    const failing = async () => ({ ok: false, status: 500, json: async () => ({}) });
    await assert.rejects(() => withFetch(failing, () => handleUpdate(env(db), textMessage(1, "c1", "u1", "/start"))));
    const row = db._sql.prepare("SELECT status, attempts FROM telegram_updates WHERE update_id=1").get();
    assert.equal(row.status, "failed");
    assert.equal(row.attempts, 1);
  });
  it("a duplicate delivery of an already-completed update is never reprocessed", async () => {
    const db = realDb();
    const fetchImpl = stubFetch();
    await withFetch(fetchImpl, () => handleUpdate(env(db), textMessage(1, "c1", "u1", "/start")));
    assert.equal(fetchImpl.calls.length, 1);
    await withFetch(fetchImpl, () => handleUpdate(env(db), textMessage(1, "c1", "u1", "/start")));
    assert.equal(fetchImpl.calls.length, 1, "duplicate update_id must not send a second message");
  });
  it("recovery after an interrupted deployment: a failed update is retried successfully next delivery", async () => {
    const db = realDb();
    let fail = true;
    const flaky = async (url, init) => {
      if (fail) return { ok: false, status: 500, json: async () => ({}) };
      return { ok: true, status: 200, json: async () => ({ ok: true }) };
    };
    await assert.rejects(() => withFetch(flaky, () => handleUpdate(env(db), textMessage(1, "c1", "u1", "/start"))));
    fail = false;
    const fetchImpl = stubFetch();
    await withFetch(fetchImpl, () => handleUpdate(env(db), textMessage(1, "c1", "u1", "/start")));
    assert.equal(fetchImpl.calls.length, 1);
    const row = db._sql.prepare("SELECT status FROM telegram_updates WHERE update_id=1").get();
    assert.equal(row.status, "done");
  });
});
