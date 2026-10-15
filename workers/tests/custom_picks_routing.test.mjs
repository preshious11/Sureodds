import { DatabaseSync } from "node:sqlite";
import { readFileSync } from "node:fs";
import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { handleUpdate } from "../src/index.ts";

// Every seed in this file archives day 2026-10-14, which the handler looks
// up by CURRENT Lagos date — so a wall-clock rollover past midnight Lagos
// would flip every ticket test to UNAVAILABLE. Freeze the clock file-wide
// (all assertions here are about routing/content, never about time passing;
// per-test withFrozenClock helpers below re-assert the same instant).
Date.now = () => Date.UTC(2026, 9, 14, 8, 0); // 09:00 Africa/Lagos

// Real SQLite, real migration files — the actual schema, not a hand-rolled
// subset, so a column rename in a migration breaks this test instead of a
// live chat. Supports .first()/.all()/.run(), unlike the lighter per-SQL
// stubs elsewhere in this suite.
function realDb() {
  const sql = new DatabaseSync(":memory:");
  // 0002 only ALTERs columns that 0001 already defines inline for a fresh
  // DB (see 0001's own comment) — applying both to a brand-new :memory: DB
  // would duplicate those columns, so 0002 is skipped here deliberately.
  for (const file of ["0001_foundation.sql", "0003_engine_state.sql", "0006_custom_sessions.sql", "0007_custom_picks_conversation.sql", "0008_pinned_menu.sql"]) {
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
    // calls[0] is the "⏳ Building your picks..." loading indicator; calls[1] is the real result.
    assert.equal(requestFetch.calls.length, 2);
    assert.match(requestFetch.calls[0].body.text, /Building your picks/);
    assert.match(requestFetch.calls[1].body.text, /Try these instead/);
    const keyboard = requestFetch.calls[1].body.reply_markup;
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
    const token = requestFetch.calls[1].body.reply_markup.inline_keyboard[0][0].callback_data;
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

describe("/pinmenu admin command — private-chat only, posts + pins the group menu", () => {
  function stubFetchForPin({ pinOk = true } = {}) {
    const calls = [];
    let nextId = 500;
    const fn = async (url, init) => {
      const body = init?.body ? JSON.parse(init.body) : null;
      calls.push({ url, body });
      if (url.includes("pinChatMessage")) {
        return pinOk ? { ok: true, status: 200, json: async () => ({ ok: true }) } : { ok: false, status: 400, json: async () => ({}) };
      }
      if (url.includes("sendMessage")) {
        return { ok: true, status: 200, json: async () => ({ ok: true, result: { message_id: nextId++ } }) };
      }
      return { ok: true, status: 200, json: async () => ({ ok: true }) };
    };
    fn.calls = calls;
    return fn;
  }
  function pinEnv(db, overrides = {}) {
    return { ...env(db), TELEGRAM_CHAT_ID: "group-1", TELEGRAM_BOT_USERNAME: "SureOddsBot", TELEGRAM_ADMIN_IDS: "7", ...overrides };
  }
  it("rejects a non-admin in private chat: 'Not allowed.', nothing posted or pinned", async () => {
    const db = realDb();
    const fetchImpl = stubFetchForPin();
    await withFetch(fetchImpl, () => handleUpdate(pinEnv(db), textMessage(1, "private-9", "9", "/pinmenu")));
    assert.equal(fetchImpl.calls.length, 1);
    assert.equal(fetchImpl.calls[0].body.chat_id, "private-9");
    assert.match(fetchImpl.calls[0].body.text, /Not allowed/);
  });
  it("is silently ignored when sent from a group, even by an admin: private-chat only", async () => {
    const db = realDb();
    const fetchImpl = stubFetchForPin();
    await withFetch(fetchImpl, () => handleUpdate(pinEnv(db), textMessage(1, "group-1", "7", "/pinmenu", "supergroup")));
    assert.equal(fetchImpl.calls.length, 0, "no response at all from a group — not just a refusal");
  });
  it("refuses to post without a configured group destination or bot username, same as /menu", async () => {
    const db = realDb();
    const noChat = { ...env(db), TELEGRAM_BOT_USERNAME: "SureOddsBot", TELEGRAM_ADMIN_IDS: "7" };
    const f1 = stubFetchForPin();
    await withFetch(f1, () => handleUpdate(noChat, textMessage(1, "private-7", "7", "/pinmenu")));
    assert.equal(f1.calls.length, 1);
    assert.match(f1.calls[0].body.text, /not configured/);
    const noUsername = { ...env(db), TELEGRAM_CHAT_ID: "group-1", TELEGRAM_ADMIN_IDS: "7" };
    const f2 = stubFetchForPin();
    await withFetch(f2, () => handleUpdate(noUsername, textMessage(2, "private-7", "7", "/pinmenu")));
    assert.equal(f2.calls.length, 1);
    assert.match(f2.calls[0].body.text, /username/);
  });
  it("an admin in private chat posts the full existing group menu and pins it, then confirms privately", async () => {
    const db = realDb();
    const fetchImpl = stubFetchForPin();
    await withFetch(fetchImpl, () => handleUpdate(pinEnv(db), textMessage(1, "private-7", "7", "/pinmenu")));
    const posted = fetchImpl.calls.find((c) => c.url.includes("sendMessage") && c.body.chat_id === "group-1");
    assert.ok(posted, "the menu must be posted to the group, not the admin's own chat");
    assert.equal(posted.body.text, "⚽ Predictions\n\nToday's picks, results and more.");
    const kb = JSON.stringify(posted.body.reply_markup);
    // Reuses the exact same keyboard /menu already posts: Top/Daily, All
    // (private deep link), Stats/Yesterday, Custom Picks (private deep link).
    assert.match(kb, /Top Picks/);
    assert.match(kb, /Daily Picks/);
    assert.match(kb, /https:\/\/t\.me\/SureOddsBot\?start=all/);
    assert.match(kb, /Stats/);
    assert.match(kb, /Yesterday/);
    assert.match(kb, /✨ Custom Picks/);
    assert.match(kb, /https:\/\/t\.me\/SureOddsBot\?start=custom_picks/);
    const pin = fetchImpl.calls.find((c) => c.url.includes("pinChatMessage"));
    assert.ok(pin, "the posted message must be pinned");
    assert.equal(pin.body.chat_id, "group-1");
    assert.equal(pin.body.message_id, posted.body.message_id ?? 500);
    const ack = fetchImpl.calls.find((c) => c.body.chat_id === "private-7" && /posted and pinned/.test(c.body.text));
    assert.ok(ack, "the admin must get a simple private confirmation");
    const row = db._sql.prepare("SELECT message_id, pinned_by FROM pinned_menu WHERE chat_id='group-1'").get();
    assert.equal(row.message_id, 500);
    assert.equal(row.pinned_by, "private-7");
  });
  it("gives a simple permission notice when pinning fails, without crashing or losing the post", async () => {
    const db = realDb();
    const fetchImpl = stubFetchForPin({ pinOk: false });
    await withFetch(fetchImpl, () => handleUpdate(pinEnv(db), textMessage(1, "private-7", "7", "/pinmenu")));
    const posted = fetchImpl.calls.find((c) => c.url.includes("sendMessage") && c.body.chat_id === "group-1");
    assert.ok(posted, "the menu is still posted even though pinning will fail");
    const ack = fetchImpl.calls.find((c) => c.body.chat_id === "private-7");
    assert.match(ack.body.text, /Pin Messages/);
    assert.match(ack.body.text, /permission/);
    const row = db._sql.prepare("SELECT 1 FROM pinned_menu WHERE chat_id='group-1'").get();
    assert.equal(row, undefined, "nothing was actually pinned, so nothing should be recorded as pinned");
  });
  it("gives a simple failure notice when the group post itself fails (e.g. bot not in that chat), and logs the real reason instead of showing it to the admin", async () => {
    const calls = [];
    const errors = [];
    const realConsoleError = console.error;
    console.error = (...args) => errors.push(args.join(" "));
    const flaky = async (url, init) => {
      const body = init?.body ? JSON.parse(init.body) : null;
      calls.push({ url, body });
      if (url.includes("sendMessage") && body?.chat_id === "group-1") {
        return { ok: false, status: 400, json: async () => ({ ok: false, description: "Bad Request: chat not found" }) };
      }
      return { ok: true, status: 200, json: async () => ({ ok: true }) };
    };
    const db = realDb();
    try {
      await withFetch(flaky, () => handleUpdate(pinEnv(db), textMessage(1, "private-7", "7", "/pinmenu")));
    } finally {
      console.error = realConsoleError;
    }
    const ack = calls.find((c) => c.body.chat_id === "private-7");
    assert.ok(ack, "the admin must still get a private explanation");
    assert.equal(ack.body.text, "❌ Failed to post the menu.", "the admin-facing message stays short and simple");
    assert.ok(errors.some((e) => e.includes("chat not found")), "the real Telegram error must still be logged for operators");
    const pinned = db._sql.prepare("SELECT 1 FROM pinned_menu WHERE chat_id='group-1'").get();
    assert.equal(pinned, undefined);
  });
  it("repeating /pinmenu after a success edits the SAME message in place — no duplicate post, no re-pin needed", async () => {
    const db = realDb();
    const first = stubFetchForPin();
    await withFetch(first, () => handleUpdate(pinEnv(db), textMessage(1, "private-7", "7", "/pinmenu")));
    const second = stubFetchForPin();
    await withFetch(second, () => handleUpdate(pinEnv(db), textMessage(2, "private-7", "7", "/pinmenu")));
    const edit = second.calls.find((c) => c.url.includes("editMessageText"));
    assert.ok(edit, "the second run must edit the already-pinned message");
    assert.equal(edit.body.chat_id, "group-1");
    assert.equal(edit.body.message_id, 500);
    assert.equal(second.calls.some((c) => c.url.includes("sendMessage") && c.body.chat_id === "group-1"), false, "no duplicate posted to the group");
    assert.equal(second.calls.some((c) => c.url.includes("pinChatMessage")), false, "editing in place never needs a re-pin");
    const ack = second.calls.find((c) => c.body.chat_id === "private-7");
    assert.equal(ack.body.text, "✅ Menu refreshed.");
  });
  it("if the previously pinned message is gone, falls back to posting and pinning a fresh one", async () => {
    const db = realDb();
    const first = stubFetchForPin();
    await withFetch(first, () => handleUpdate(pinEnv(db), textMessage(1, "private-7", "7", "/pinmenu")));
    let editAttempted = false;
    const second = (() => {
      const calls = [];
      let nextId = 900;
      const fn = async (url, init) => {
        const body = init?.body ? JSON.parse(init.body) : null;
        calls.push({ url, body });
        if (url.includes("editMessageText")) {
          editAttempted = true;
          return { ok: false, status: 400, json: async () => ({}) }; // message deleted/too old
        }
        if (url.includes("sendMessage")) return { ok: true, status: 200, json: async () => ({ ok: true, result: { message_id: nextId++ } }) };
        return { ok: true, status: 200, json: async () => ({ ok: true }) };
      };
      fn.calls = calls;
      return fn;
    })();
    await withFetch(second, () => handleUpdate(pinEnv(db), textMessage(2, "private-7", "7", "/pinmenu")));
    assert.equal(editAttempted, true);
    const posted = second.calls.find((c) => c.url.includes("sendMessage") && c.body.chat_id === "group-1");
    assert.ok(posted, "a fresh menu must still be posted when the old one can't be edited");
    const pin = second.calls.find((c) => c.url.includes("pinChatMessage"));
    assert.ok(pin, "the fresh post must be pinned");
    const row = db._sql.prepare("SELECT message_id FROM pinned_menu WHERE chat_id='group-1'").get();
    assert.equal(row.message_id, 900);
  });
  it("the existing public /menu command is completely unaffected: still works from any chat, still never pins", async () => {
    const db = realDb();
    const fetchImpl = stubFetchForPin();
    await withFetch(fetchImpl, () => handleUpdate(pinEnv(db), textMessage(1, "group-1", "7", "/menu", "supergroup")));
    assert.equal(fetchImpl.calls.some((c) => c.url.includes("pinChatMessage")), false, "/menu must never pin");
    const posted = fetchImpl.calls.find((c) => c.body.chat_id === "group-1" && c.url.includes("sendMessage"));
    assert.ok(posted, "/menu still posts to the group from a non-private chat, unlike /pinmenu");
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
  it("/start custom_picks in a group redirects to a private chat: no session created, one redirect sent", async () => {
    const db = realDb();
    const fetchImpl = stubFetch();
    await withFetch(fetchImpl, () =>
      handleUpdate(env(db), textMessage(1, "g1", "u1", "/start custom_picks", "supergroup")),
    );
    assert.equal(fetchImpl.calls.length, 1);
    assert.match(fetchImpl.calls[0].body.text, /private chat/);
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
    assert.equal(fetchImpl.calls.length, 2);
    assert.match(fetchImpl.calls[0].body.text, /Building your picks/);
    assert.match(fetchImpl.calls[1].body.text, /🔥/);
    assert.match(fetchImpl.calls[1].body.text, /Arsenal vs Chelsea/);
  });
  it("UNAVAILABLE (not a fabricated ticket) when no pool has been captured for today", async () => {
    const db = realDb(); // no seedPool
    await withFetch(stubFetch(), () => handleUpdate(env(db), textMessage(1, "c1", "u1", "/start custom_picks")));
    const fetchImpl = stubFetch();
    await withFetch(fetchImpl, () => handleUpdate(env(db), textMessage(2, "c1", "u1", "3 picks")));
    assert.equal(fetchImpl.calls.length, 2);
    assert.match(fetchImpl.calls[0].body.text, /Building your picks/);
    assert.match(fetchImpl.calls[1].body.text, /Still cooking/);
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
    // request 1: loading indicator + ticket; request 2 (on cooldown): BUSY only, no loading step.
    assert.equal(fetchImpl.calls.length, 3);
    assert.match(fetchImpl.calls[0].body.text, /Building your picks/);
    assert.match(fetchImpl.calls[1].body.text, /🔥/);
    assert.match(fetchImpl.calls[2].body.text, /Still working on your last request/);
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
    seedProductionRow(db._sql, day, generatedAt, { canonical_event_id: "fx-2", home: "Barca", away: "Real", home_id: "bar", away_id: "rea", price: 1.6, p_model: 0.65 });
    seedProductionRow(db._sql, day, generatedAt, { canonical_event_id: "fx-3", home: "Bayern", away: "Dortmund", home_id: "bay", away_id: "dor", price: 1.9, p_model: 0.68 });
    await withFrozenClock(async () => {
      await withFetch(stubFetch(), () => handleUpdate(env(db), textMessage(1, "c1", "u1", "/start custom_picks")));
      const fetchImpl = stubFetch();
      await withFetch(fetchImpl, () => handleUpdate(env(db), textMessage(2, "c1", "u1", "give me 5 odds")));
      assert.equal(fetchImpl.calls.length, 2);
      assert.match(fetchImpl.calls[1].body.text, /🔥/);
      assert.match(fetchImpl.calls[1].body.text, /Arsenal vs Chelsea/);
    });
  });
  it("a production-shaped pool answers 30 odds honestly (unreachable) instead of an empty-pool message", async () => {
    const db = realDb();
    const day = "2026-10-14";
    const generatedAt = "2026-10-14T06:00:00Z";
    db._sql.exec(`INSERT INTO custom_pick_pools VALUES('${day}','Africa/Lagos','${generatedAt}','COMPLETE','{}')`);
    seedProductionRow(db._sql, day, generatedAt);
    seedProductionRow(db._sql, day, generatedAt, { canonical_event_id: "fx-2", home: "Barca", away: "Real", home_id: "bar", away_id: "rea", price: 1.6, p_model: 0.65 });
    await withFrozenClock(async () => {
      await withFetch(stubFetch(), () => handleUpdate(env(db), textMessage(1, "c1", "u1", "/start custom_picks")));
      const fetchImpl = stubFetch();
      await withFetch(fetchImpl, () => handleUpdate(env(db), textMessage(2, "c1", "u1", "Give me 30 odds")));
      assert.equal(fetchImpl.calls.length, 2);
      assert.doesNotMatch(fetchImpl.calls[1].body.text, /Nothing made the cut/);
    });
  });
});

describe("All Predictions: group-to-private flow", () => {
  function seedAllPick(sql, day) {
    sql.exec(
      `INSERT INTO picks (pick_date,tier,rank,provider,provider_event_id,sport,league,home,away,start_utc,
        market,period,stat,entity,selection,label,price,prob,ev,status,created_at)
       VALUES ('${day}','all',1,'sgo','ev-1','football','Premier League','Arsenal','Chelsea','${day}T18:00:00Z',
        'result2','full','points','all','home','Arsenal to Win',1.8,0.6,0.05,'pending','${day}T06:00:00Z')`,
    );
    sql.exec(
      `INSERT INTO prediction_cache (cache_key,local_date,timezone,status,payload_json,generated_at,updated_at,expires_at)
       VALUES ('daily_predictions:Africa/Lagos:${day}','${day}','Africa/Lagos','COMPLETE','{}','${day}T06:00:00Z','${day}T06:00:00Z','${day}T23:59:59Z')`,
    );
  }
  it("/all typed directly in a group redirects to the private bot instead of rendering the list", async () => {
    const db = realDb();
    const theEnv = { ...env(db), TELEGRAM_BOT_USERNAME: "SureOddsBot" };
    const fetchImpl = stubFetch();
    await withFetch(fetchImpl, () => handleUpdate(theEnv, textMessage(1, "g1", "u1", "/all", "supergroup")));
    assert.equal(fetchImpl.calls.length, 1, "only the redirect, never the prediction list");
    assert.match(fetchImpl.calls[0].url, /sendMessage/);
    assert.doesNotMatch(fetchImpl.calls[0].body.text, /ALL PREDICTIONS/);
    const kb = JSON.stringify(fetchImpl.calls[0].body.reply_markup);
    assert.match(kb, /https:\/\/t\.me\/SureOddsBot\?start=all/);
  });
  it("/all in a group with no configured bot username: a plain redirect, never a dead link", async () => {
    const db = realDb();
    const fetchImpl = stubFetch();
    await withFetch(fetchImpl, () => handleUpdate(env(db), textMessage(1, "g1", "u1", "/all", "supergroup")));
    assert.equal(fetchImpl.calls.length, 1);
    assert.equal(fetchImpl.calls[0].body.reply_markup, undefined, "no dead-link button without a configured username");
    assert.match(fetchImpl.calls[0].body.text, /private chat/);
  });
  it("/all in a private chat is unaffected: still renders the sport menu, not a redirect", async () => {
    const db = realDb();
    const day = "2026-10-14";
    seedAllPick(db._sql, day);
    const fetchImpl = stubFetch();
    const FROZEN_NOW = Date.UTC(2026, 9, 14, 8, 0);
    const realNow = Date.now;
    Date.now = () => FROZEN_NOW;
    try {
      await withFetch(fetchImpl, () => handleUpdate(env(db), textMessage(1, "c1", "u1", "/all")));
    } finally {
      Date.now = realNow;
    }
    assert.equal(fetchImpl.calls.length, 1);
    assert.match(fetchImpl.calls[0].body.text, /ALL PREDICTIONS/);
    assert.match(fetchImpl.calls[0].body.text, /Pick a sport/);
  });
  // Regression (commit 4135f81): tapping an old group message's pg|all|0
  // button used to editMessageText that message into a bare redirect,
  // which also wiped out every OTHER button sharing that same keyboard
  // (Top/Daily/Stats/Yesterday) — "the group menu disappeared". The fix
  // answers the callback with a deep-link url (Telegram Bot API's own
  // documented mechanism for answerCallbackQuery) and touches NOTHING
  // about the original message: no edit, no delete, no replacement send.
  it("a legacy group message's old pg|all|0 button opens the private deep link via the callback answer — the message itself is never touched", async () => {
    const db = realDb();
    const theEnv = { ...env(db), TELEGRAM_BOT_USERNAME: "SureOddsBot" };
    const fetchImpl = stubFetch();
    await withFetch(fetchImpl, () =>
      handleUpdate(theEnv, {
        update_id: 1,
        callback_query: {
          id: "cbq-1",
          data: "pg|all|0",
          from: { id: "u1" },
          message: { message_id: 55, chat: { id: "g1", type: "supergroup" } },
        },
      }),
    );
    assert.equal(fetchImpl.calls.length, 1, "only the callback answer — no edit, no new message");
    const [call] = fetchImpl.calls;
    assert.match(call.url, /answerCallbackQuery/);
    assert.equal(call.body.callback_query_id, "cbq-1");
    assert.equal(call.body.url, "https://t.me/SureOddsBot?start=all");
    assert.equal(fetchImpl.calls.some((c) => c.url.includes("editMessageText")), false, "the original message must never be edited");
    assert.equal(fetchImpl.calls.some((c) => c.url.includes("sendMessage")), false, "no replacement/extra message either");
  });
  it("a legacy group pg|all_<sport>|... sub-page callback is redirected the same way — no edit, no new message", async () => {
    const db = realDb();
    const theEnv = { ...env(db), TELEGRAM_BOT_USERNAME: "SureOddsBot" };
    const fetchImpl = stubFetch();
    await withFetch(fetchImpl, () =>
      handleUpdate(theEnv, {
        update_id: 1,
        callback_query: {
          id: "cbq-1",
          data: "pg|all_football|0|20261014",
          from: { id: "u1" },
          message: { message_id: 56, chat: { id: "g1", type: "supergroup" } },
        },
      }),
    );
    assert.equal(fetchImpl.calls.length, 1);
    assert.equal(fetchImpl.calls[0].body.url, "https://t.me/SureOddsBot?start=all");
    assert.equal(fetchImpl.calls.some((c) => c.url.includes("editMessageText") || c.url.includes("sendMessage")), false);
  });
  it("a legacy group All tap with no configured bot username falls back to an alert toast, still never touching the message", async () => {
    const db = realDb();
    const fetchImpl = stubFetch();
    await withFetch(fetchImpl, () =>
      handleUpdate(env(db), {
        update_id: 1,
        callback_query: {
          id: "cbq-1",
          data: "pg|all|0",
          from: { id: "u1" },
          message: { message_id: 55, chat: { id: "g1", type: "supergroup" } },
        },
      }),
    );
    assert.equal(fetchImpl.calls.length, 1);
    assert.match(fetchImpl.calls[0].url, /answerCallbackQuery/);
    assert.equal(fetchImpl.calls[0].body.url, undefined);
    assert.match(fetchImpl.calls[0].body.text, /private chat/);
  });
  it("tapping All on a group message does not disturb the Top/Daily/Stats/Yesterday buttons sharing that same keyboard", async () => {
    // The exact scenario from the regression: a /top response in a group
    // carries the full switch row (Top/Daily/Stats/Yesterday + All) in one
    // message. Tapping All must leave that whole keyboard exactly as the
    // group members already saw it — proven here by never issuing any
    // edit/send against the message at all.
    const db = realDb();
    const theEnv = { ...env(db), TELEGRAM_BOT_USERNAME: "SureOddsBot" };
    const originalKeyboard = {
      inline_keyboard: [
        [{ text: "📚 All Predictions", callback_data: "pg|all|0" }],
        [{ text: "🔥 Top Picks", callback_data: "pg|top|0" }, { text: "📋 Daily Picks", callback_data: "pg|daily|0" }],
        [{ text: "📊 Stats", callback_data: "pg|stats|0" }, { text: "🗓 Yesterday", callback_data: "pg|yesterday|0" }],
      ],
    };
    const fetchImpl = stubFetch();
    await withFetch(fetchImpl, () =>
      handleUpdate(theEnv, {
        update_id: 1,
        callback_query: {
          id: "cbq-1",
          data: "pg|all|0",
          from: { id: "u1" },
          message: { message_id: 55, chat: { id: "g1", type: "supergroup" }, reply_markup: originalKeyboard },
        },
      }),
    );
    // No editMessageText/editMessageReplyMarkup/deleteMessage/sendMessage of
    // any kind was issued, so the message — and every button on it,
    // including Top/Daily/Stats/Yesterday — is provably untouched.
    const mutating = fetchImpl.calls.filter((c) => /editMessage|deleteMessage|sendMessage/.test(c.url));
    assert.equal(mutating.length, 0, "the group menu message must not be edited, replaced, or deleted");
  });
  it("a private-chat pg|all|0 tap is unaffected: still edits in the real sport menu", async () => {
    const db = realDb();
    const day = "2026-10-14";
    seedAllPick(db._sql, day);
    const fetchImpl = stubFetch();
    const FROZEN_NOW = Date.UTC(2026, 9, 14, 8, 0);
    const realNow = Date.now;
    Date.now = () => FROZEN_NOW;
    try {
      await withFetch(fetchImpl, () => handleUpdate(env(db), callbackQuery(1, "cbq-1", "c1", "u1", "pg|all|0")));
    } finally {
      Date.now = realNow;
    }
    const edit = fetchImpl.calls.find((c) => c.url.includes("editMessageText") || c.url.includes("sendMessage"));
    assert.match(edit.body.text, /ALL PREDICTIONS/);
  });
  it("/start all (the group button's deep link) immediately shows today's All Predictions in the private chat", async () => {
    const db = realDb();
    const day = "2026-10-14";
    seedAllPick(db._sql, day);
    const fetchImpl = stubFetch();
    const FROZEN_NOW = Date.UTC(2026, 9, 14, 8, 0);
    const realNow = Date.now;
    Date.now = () => FROZEN_NOW;
    try {
      await withFetch(fetchImpl, () => handleUpdate(env(db), textMessage(1, "c1", "u1", "/start all")));
    } finally {
      Date.now = realNow;
    }
    assert.equal(fetchImpl.calls.length, 1);
    assert.match(fetchImpl.calls[0].body.text, /ALL PREDICTIONS/);
    assert.match(fetchImpl.calls[0].body.text, /Pick a sport/);
  });
  it("/start all in a group redirects instead of rendering: deep links only ever open a private chat", async () => {
    const db = realDb();
    const fetchImpl = stubFetch();
    await withFetch(fetchImpl, () => handleUpdate(env(db), textMessage(1, "g1", "u1", "/start all", "supergroup")));
    assert.equal(fetchImpl.calls.length, 1);
    assert.doesNotMatch(fetchImpl.calls[0].body.text, /Pick a sport/);
  });
});

describe("Custom Picks loading-state lifecycle", () => {
  function stubFetchWithMessageIds() {
    const calls = [];
    let nextId = 1000;
    const fn = async (url, init) => {
      const body = init?.body ? JSON.parse(init.body) : null;
      calls.push({ url, body });
      if (url.includes("sendMessage")) {
        return { ok: true, status: 200, json: async () => ({ ok: true, result: { message_id: nextId++ } }) };
      }
      return { ok: true, status: 200, json: async () => ({ ok: true }) };
    };
    fn.calls = calls;
    return fn;
  }
  it("sends a loading indicator, deletes it once a real message_id comes back, then delivers the ticket", async () => {
    const db = realDb();
    seedPool(db._sql);
    await withFetch(stubFetch(), () => handleUpdate(env(db), textMessage(1, "c1", "u1", "/start custom_picks")));
    const fetchImpl = stubFetchWithMessageIds();
    await withFetch(fetchImpl, () => handleUpdate(env(db), textMessage(2, "c1", "u1", "1 picks")));
    assert.equal(fetchImpl.calls.length, 3, "loading sendMessage, deleteMessage, final sendMessage");
    assert.match(fetchImpl.calls[0].url, /sendMessage/);
    assert.match(fetchImpl.calls[0].body.text, /Building your picks/);
    assert.match(fetchImpl.calls[1].url, /deleteMessage/);
    assert.equal(fetchImpl.calls[1].body.message_id, 1000);
    assert.match(fetchImpl.calls[2].url, /sendMessage/);
    assert.match(fetchImpl.calls[2].body.text, /🔥/);
  });
  it("a failed loading-indicator send never blocks the real ticket (Telegram failure handled safely)", async () => {
    const db = realDb();
    seedPool(db._sql);
    await withFetch(stubFetch(), () => handleUpdate(env(db), textMessage(1, "c1", "u1", "/start custom_picks")));
    let first = true;
    const flaky = async (url, init) => {
      if (first && url.includes("sendMessage")) {
        first = false;
        return { ok: false, status: 500, json: async () => ({}) };
      }
      return { ok: true, status: 200, json: async () => ({ ok: true }) };
    };
    const calls = [];
    const wrapped = async (url, init) => {
      calls.push({ url, body: init?.body ? JSON.parse(init.body) : null });
      return flaky(url, init);
    };
    await withFetch(wrapped, () => handleUpdate(env(db), textMessage(2, "c1", "u1", "1 picks")));
    const delivered = calls.find((c) => c.url.includes("sendMessage") && /🔥/.test(c.body?.text ?? ""));
    assert.ok(delivered, "the ticket must still be delivered even though the loading indicator failed to send");
  });
  it("a failed delete of the loading indicator never blocks the real ticket", async () => {
    const db = realDb();
    seedPool(db._sql);
    await withFetch(stubFetch(), () => handleUpdate(env(db), textMessage(1, "c1", "u1", "/start custom_picks")));
    const calls = [];
    const flakyDelete = async (url, init) => {
      const body = init?.body ? JSON.parse(init.body) : null;
      calls.push({ url, body });
      if (url.includes("deleteMessage")) return { ok: false, status: 400, json: async () => ({}) };
      if (url.includes("sendMessage")) return { ok: true, status: 200, json: async () => ({ ok: true, result: { message_id: 42 } }) };
      return { ok: true, status: 200, json: async () => ({ ok: true }) };
    };
    await withFetch(flakyDelete, () => handleUpdate(env(db), textMessage(2, "c1", "u1", "1 picks")));
    const delivered = calls.find((c) => c.url.includes("sendMessage") && /🔥/.test(c.body?.text ?? ""));
    assert.ok(delivered, "the ticket must still be delivered even though deleting the loading indicator failed");
  });
  it("the loading indicator is also cleaned up when delivering the final result fails, never left stuck", async () => {
    const db = realDb();
    seedPool(db._sql);
    await withFetch(stubFetch(), () => handleUpdate(env(db), textMessage(1, "c1", "u1", "/start custom_picks")));
    const calls = [];
    // Everything succeeds except the final ticket send, which 5xxs — proving
    // the loading indicator is still torn down before the failure is
    // re-thrown (and the update left retryable), never stuck on screen.
    const failOnFinalSend = async (url, init) => {
      const body = init?.body ? JSON.parse(init.body) : null;
      calls.push({ url, body });
      if (url.includes("sendMessage") && body?.text === "⏳ Building your picks...") {
        return { ok: true, status: 200, json: async () => ({ ok: true, result: { message_id: 77 } }) };
      }
      if (url.includes("sendMessage")) return { ok: false, status: 500, json: async () => ({}) };
      return { ok: true, status: 200, json: async () => ({ ok: true }) };
    };
    await assert.rejects(() => withFetch(failOnFinalSend, () => handleUpdate(env(db), textMessage(2, "c1", "u1", "1 picks"))));
    const deleteCall = calls.find((c) => c.url.includes("deleteMessage"));
    assert.ok(deleteCall, "the loading indicator must be cleaned up even when the final send fails");
    assert.equal(deleteCall.body.message_id, 77);
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
  // Regression (production incident, 2026-10-15, first real Cron-triggered
  // traffic after activation): a user re-tapped a button whose re-render
  // produced content identical to what was already shown. Telegram
  // correctly rejects that edit with 400 "message is not modified" — this
  // must complete the update, not retry it forever (retrying can never
  // succeed: the content staying the same is exactly why it's rejected).
  it("Telegram's 'message is not modified' on an edit is a harmless no-op, not a retryable failure", async () => {
    const db = realDb();
    const flaky = async (url, init) => {
      if (url.includes("editMessageText")) {
        return { ok: false, status: 400, json: async () => ({ ok: false, description: "Bad Request: message is not modified: specified new message content and reply markup are exactly the same as a current content and reply markup of the message" }) };
      }
      return { ok: true, status: 200, json: async () => ({ ok: true }) };
    };
    await withFetch(flaky, () => handleUpdate(env(db), callbackQuery(1, "cbq-1", "c1", "u1", "pg|top|0")));
    const row = db._sql.prepare("SELECT status, attempts, last_error FROM telegram_updates WHERE update_id=1").get();
    assert.equal(row.status, "done", "must complete, not sit in a failed/retrying loop that can never resolve");
    assert.equal(row.attempts, 0);
    assert.equal(row.last_error, null);
  });
  it("any OTHER editMessageText failure (not 'not modified') still fails and retries normally", async () => {
    const db = realDb();
    const failing = async (url) => {
      if (url.includes("editMessageText")) return { ok: false, status: 400, json: async () => ({ ok: false, description: "Bad Request: message to edit not found" }) };
      return { ok: true, status: 200, json: async () => ({ ok: true }) };
    };
    await assert.rejects(() => withFetch(failing, () => handleUpdate(env(db), callbackQuery(1, "cbq-1", "c1", "u1", "pg|top|0"))));
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

describe("Phase A/B reliability: /stop, group /start redirects, @BotName, cooldown retry", () => {
  const FROZEN_NOW = Date.UTC(2026, 9, 14, 8, 0); // 09:00 Africa/Lagos
  async function withFrozenClock(fn) {
    const realNow = Date.now;
    Date.now = () => FROZEN_NOW;
    try {
      return await fn();
    } finally {
      Date.now = realNow;
    }
  }
  function adminEnv(db, extra = {}) {
    return {
      DB: db, TELEGRAM_BOT_TOKEN: "t", TELEGRAM_WEBHOOK_SECRET: "s", TIMEZONE: "Africa/Lagos",
      PAGE_SIZE: "5", TELEGRAM_BOT_USERNAME: "SureOddsBot", TELEGRAM_ADMIN_IDS: "7",
      TELEGRAM_CHAT_ID: "group-1", ...extra,
    };
  }
  it("/stop confirms, deactivates delivery, and leaves the Custom Picks session", async () => {
    const db = realDb();
    await withFrozenClock(async () => {
      await withFetch(stubFetch(), () => handleUpdate(env(db), textMessage(1, "c1", "u1", "/start")));
      await withFetch(stubFetch(), () => handleUpdate(env(db), textMessage(2, "c1", "u1", "/start custom_picks")));
      const fetchImpl = stubFetch();
      await withFetch(fetchImpl, () => handleUpdate(env(db), textMessage(3, "c1", "u1", "/stop")));
      assert.equal(fetchImpl.calls.length, 1);
      assert.match(fetchImpl.calls[0].body.text, /Unsubscribed/);
      const sub = db._sql.prepare("SELECT active FROM subscribers WHERE chat_id='c1'").get();
      assert.equal(sub.active, 0, "delivery subscription must actually deactivate");
      const session = db._sql.prepare("SELECT active FROM custom_sessions WHERE user_id='u1' AND chat_id='c1'").get();
      assert.equal(session.active, 0, "custom session must be left");
    });
  });
  it("/stop with no prior subscription still confirms instead of crashing", async () => {
    const db = realDb();
    const fetchImpl = stubFetch();
    await withFetch(fetchImpl, () => handleUpdate(env(db), textMessage(1, "c9", "u9", "/stop")));
    assert.equal(fetchImpl.calls.length, 1);
    assert.match(fetchImpl.calls[0].body.text, /Unsubscribed/);
  });
  it("group /start custom_picks redirects to a private chat with a deep-link button, no session", async () => {
    const db = realDb();
    const fetchImpl = stubFetch();
    await withFrozenClock(async () => {
      await withFetch(fetchImpl, () => handleUpdate(adminEnv(db), textMessage(1, "group-1", "u1", "/start custom_picks", "supergroup")));
    });
    assert.equal(fetchImpl.calls.length, 1);
    assert.match(fetchImpl.calls[0].body.text, /private chat/);
    assert.match(JSON.stringify(fetchImpl.calls[0].body.reply_markup), /start=custom_picks/);
    assert.equal(db._sql.prepare("SELECT 1 AS one FROM custom_sessions WHERE chat_id='group-1'").get(), undefined);
  });
  it("group /start custom_picks without a configured username sends plain text, never a dead link", async () => {
    const db = realDb();
    const fetchImpl = stubFetch();
    await withFetch(fetchImpl, () => handleUpdate(env(db), textMessage(1, "group-1", "u1", "/start custom_picks", "supergroup")));
    assert.equal(fetchImpl.calls.length, 1);
    assert.match(fetchImpl.calls[0].body.text, /private chat/);
    assert.equal(fetchImpl.calls[0].body.reply_markup, undefined);
  });
  it("group /start all redirects with the All deep link instead of rendering", async () => {
    const db = realDb();
    const fetchImpl = stubFetch();
    await withFetch(fetchImpl, () => handleUpdate(adminEnv(db), textMessage(1, "group-1", "u1", "/start all", "supergroup")));
    assert.equal(fetchImpl.calls.length, 1);
    assert.match(JSON.stringify(fetchImpl.calls[0].body.reply_markup ?? fetchImpl.calls[0].body.text), /start=all/);
  });
  it("@BotName suffix and case are normalized: /menu@Bot, /stop@Bot, /START all route correctly", async () => {
    const db = realDb();
    const menuFetch = stubFetch();
    await withFetch(menuFetch, () => handleUpdate(adminEnv(db), textMessage(1, "private-7", "7", "/menu@SureOddsBot")));
    assert.match(menuFetch.calls.find((c) => c.body.chat_id === "private-7").body.text, /posted/);
    const stopFetch = stubFetch();
    await withFetch(stopFetch, () => handleUpdate(adminEnv(db), textMessage(2, "c1", "u1", "/stop@SureOddsBot")));
    assert.match(stopFetch.calls[0].body.text, /Unsubscribed/);
    const startFetch = stubFetch();
    await withFetch(startFetch, () => handleUpdate(env(db), textMessage(3, "c1", "u1", "/START")));
    assert.match(startFetch.calls[0].body.text, /Use the menu below/);
    const sub = db._sql.prepare("SELECT active FROM subscribers WHERE chat_id='c1'").get();
    assert.equal(sub.active, 1, "/START must subscribe like /start");
  });
  it("a failed Custom Picks request does not poison the cooldown: the retry serves the ticket, not BUSY", async () => {
    const db = realDb();
    seedPool(db._sql);
    await withFrozenClock(async () => {
      await withFetch(stubFetch(), () => handleUpdate(env(db), textMessage(1, "c1", "u1", "/start custom_picks")));
      // Break the candidate read so respond() throws AFTER the cooldown
      // check but BEFORE any timestamp could be recorded.
      db._sql.exec("DROP TABLE custom_pick_candidates");
      await assert.rejects(
        withFetch(stubFetch(), () => handleUpdate(env(db), textMessage(2, "c1", "u1", "1 picks"))),
        "the broken read must fail the update (retryable), not answer BUSY",
      );
      // Restore the table exactly as the migrations define it, then reseed.
      db._sql.exec(readFileSync(new URL("../migrations/0003_engine_state.sql", import.meta.url), "utf8"));
      seedCandidate(db._sql, "2026-10-14", "2026-10-14T06:00:00Z", { canonical_event_id: "fx-1", home: "Arsenal", away: "Chelsea" });
      const fetchImpl = stubFetch();
      await withFetch(fetchImpl, () => handleUpdate(env(db), textMessage(3, "c1", "u1", "1 picks")));
      // The retry sends the loading indicator plus the real ticket — and
      // crucially no BUSY rejection.
      const bodies = fetchImpl.calls.map((c) => c.body.text);
      assert.ok(bodies.some((t) => /🔥/.test(t)), "retry must serve the ticket");
      assert.ok(bodies.every((t) => !/Still working on your last request/.test(t)), "retry must not be throttled");
    });
  });
  it("sequential rapid requests are still throttled: a served request stamps the cooldown", async () => {
    const db = realDb();
    seedPool(db._sql);
    await withFrozenClock(async () => {
      await withFetch(stubFetch(), () => handleUpdate(env(db), textMessage(1, "c1", "u1", "/start custom_picks")));
      const fetchImpl = stubFetch();
      await withFetch(fetchImpl, async () => {
        await handleUpdate(env(db), textMessage(2, "c1", "u1", "1 picks"));
        await handleUpdate(env(db), textMessage(3, "c1", "u1", "1 picks"));
      });
      // First request: loading indicator + ticket. Second: BUSY throttle.
      const bodies = fetchImpl.calls.map((c) => c.body.text);
      assert.equal(bodies.length, 3);
      assert.ok(/🔥/.test(bodies[1]), "first request serves the ticket");
      assert.match(bodies[2], /Still working on your last request/);
    });
  });
});
