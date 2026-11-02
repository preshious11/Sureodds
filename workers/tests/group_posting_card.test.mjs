import { DatabaseSync } from "node:sqlite";
import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { postGroupPicks } from "../src/cron.ts";

// Regression coverage for two production-parity bugs found and fixed in
// this pass: (1) the automatic group post reused the PAGINATED /top /daily
// view, so a ticket with more than PICKS_PER_PAGE (5) legs would have
// posted only its first page to the group with a stray "Page 1 of N"
// footer, which production's picks_messages/summary_card never does — the
// group always gets the whole ticket in one message; (2) the group post
// always carried the interactive pager/switch keyboard, where production
// sends NO keyboard at all except the genuinely-empty Top card, which gets
// the Custom Picks deep-link keyboard instead (predbot/jobs/daily_picks.py).
function stubDb(sql) {
  return {
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

function fixture() {
  const sql = new DatabaseSync(":memory:");
  sql.exec(`
    CREATE TABLE picks(id INTEGER PRIMARY KEY, pick_date TEXT, revision_id INTEGER NOT NULL DEFAULT 1, tier TEXT, rank INTEGER,
      sport TEXT, league TEXT, home TEXT, away TEXT, start_utc TEXT, market TEXT,
      period TEXT DEFAULT 'full', stat TEXT DEFAULT 'points', entity TEXT DEFAULT 'all', line REAL DEFAULT 2.5,
      selection TEXT, label TEXT, price REAL, prob REAL, status TEXT);
    CREATE TABLE prediction_memberships(prediction_id INTEGER REFERENCES picks(id),
      collection TEXT, rank INTEGER, PRIMARY KEY(prediction_id, collection));
    CREATE TABLE prediction_cache(cache_key TEXT PRIMARY KEY, local_date TEXT,
      timezone TEXT, status TEXT, payload_json TEXT);
    CREATE TABLE posts(id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, post_date TEXT,
      message_id TEXT, created_at TEXT, UNIQUE(kind, post_date));
    CREATE TABLE active_ticket_revisions(pick_date TEXT PRIMARY KEY, revision_id INTEGER, updated_at TEXT);
    CREATE TABLE ticket_publications(pick_date TEXT, collection TEXT, revision_id INTEGER, destination TEXT,
      recipient_id TEXT, state TEXT, message_id TEXT, attempts INTEGER, error TEXT, created_at TEXT, updated_at TEXT,
      PRIMARY KEY(pick_date,collection,revision_id,destination));
    CREATE TABLE job_state(name TEXT PRIMARY KEY, due_at TEXT, lease_owner TEXT,
      lease_until TEXT, attempts INTEGER NOT NULL DEFAULT 0, last_run_at TEXT,
      updated_at TEXT, last_result TEXT);
  `);
  return sql;
}

function seedDay(sql, day, { dailyCount = 8 } = {}) {
  const insertPick = sql.prepare(
    "INSERT INTO picks(id,pick_date,tier,rank,sport,league,home,away,start_utc,market,selection,label,price,prob,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
  );
  const insertMembership = sql.prepare("INSERT INTO prediction_memberships VALUES(?,?,?)");
  const price = dailyCount > 5 ? 1.35 : 2.2;
  for (let i = 1; i <= dailyCount; i += 1) {
    insertPick.run(
      i, day, "all", i, "football", "League", `Home${i}`, `Away${i}`,
      `${day}T${String(10 + i).padStart(2, "0")}:00:00Z`, "total", "over", `Over 2.5 (#${i})`, price, 0.6, "pending",
    );
    insertMembership.run(i, "all", i);
    insertMembership.run(i, "daily", i);
  }
  sql.prepare("INSERT INTO prediction_cache VALUES(?,?,?,?,?)").run(
    `daily_predictions:Africa/Lagos:${day}`, day, "Africa/Lagos", "COMPLETE", "{}",
  );
}

const AFTER_GATE = Date.UTC(2026, 9, 14, 8, 40); // 09:40 Lagos, past DELIVERY_TIME 09:30

describe("automatic group post renders the whole ticket, never paginated", () => {
  it("a Daily ticket with more legs than PICKS_PER_PAGE (5) is posted whole, no 'Page 1 of N' footer", async () => {
    const sql = fixture();
    const day = "2026-10-14";
    seedDay(sql, day, { dailyCount: 8 });
    const env = { DB: stubDb(sql), TIMEZONE: "Africa/Lagos", PAGE_SIZE: "5", TELEGRAM_CHAT_ID: "-100123" };
    let sentText = "";
    const sender = async (_e, _m, p) => {
      sentText = p.text;
      return { ok: true, messageId: 1 };
    };
    const outcome = await postGroupPicks(env, "daily", AFTER_GATE, sender);
    assert.equal(outcome, "sent");
    for (let i = 1; i <= 8; i += 1) assert.match(sentText, new RegExp(`Home${i} vs Away${i}`));
    assert.doesNotMatch(sentText, /Page \d+ of \d+/);
  });
  it("a non-empty post carries no keyboard at all (production sends tg.send(text) with no markup)", async () => {
    const sql = fixture();
    const day = "2026-10-14";
    seedDay(sql, day, { dailyCount: 3 });
    const env = { DB: stubDb(sql), TIMEZONE: "Africa/Lagos", PAGE_SIZE: "5", TELEGRAM_CHAT_ID: "-100123" };
    let sentPayload = null;
    const outcome = await postGroupPicks(env, "daily", AFTER_GATE, async (_e, _m, p) => {
      sentPayload = p;
      return { ok: true, messageId: 1 };
    });
    assert.equal(outcome, "sent");
    assert.equal("reply_markup" in sentPayload, false);
  });
  it("a genuinely-empty Top card is not sent", async () => {
    const sql = fixture();
    const day = "2026-10-14";
    // COMPLETE cache, zero top-collection rows -> genuinely empty, not "not ready".
    sql.prepare("INSERT INTO prediction_cache VALUES(?,?,?,?,?)").run(
      `daily_predictions:Africa/Lagos:${day}`, day, "Africa/Lagos", "COMPLETE", "{}",
    );
    const env = {
      DB: stubDb(sql),
      TIMEZONE: "Africa/Lagos",
      PAGE_SIZE: "5",
      TELEGRAM_CHAT_ID: "-100123",
      TELEGRAM_BOT_USERNAME: "SureOddsBot",
    };
    let sentPayload = null;
    const outcome = await postGroupPicks(env, "top", AFTER_GATE, async (_e, _m, p) => {
      sentPayload = p;
      return { ok: true, messageId: 1 };
    });
    assert.equal(outcome, "skipped");
    assert.equal(sentPayload, null);
    assert.equal(sql.prepare("SELECT COUNT(*) AS n FROM ticket_publications").get().n, 0);
  });
  it("without a configured bot username, an empty Top is still not sent", async () => {
    const sql = fixture();
    const day = "2026-10-14";
    sql.prepare("INSERT INTO prediction_cache VALUES(?,?,?,?,?)").run(
      `daily_predictions:Africa/Lagos:${day}`, day, "Africa/Lagos", "COMPLETE", "{}",
    );
    const env = { DB: stubDb(sql), TIMEZONE: "Africa/Lagos", PAGE_SIZE: "5", TELEGRAM_CHAT_ID: "-100123" };
    let sentPayload = null;
    const outcome = await postGroupPicks(env, "top", AFTER_GATE, async (_e, _m, p) => {
      sentPayload = p;
      return { ok: true, messageId: 1 };
    });
    assert.equal(outcome, "skipped");
    assert.equal(sentPayload, null);
  });
});
