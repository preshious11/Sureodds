import { DatabaseSync } from "node:sqlite";
import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { renderView } from "../src/views.ts";

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
          };
        },
      };
    },
  };
}

function baseSchema() {
  const sql = new DatabaseSync(":memory:");
  sql.exec(`
    CREATE TABLE picks(id INTEGER PRIMARY KEY, pick_date TEXT, tier TEXT, rank INTEGER,
      sport TEXT, league TEXT, home TEXT, away TEXT, start_utc TEXT, market TEXT,
      selection TEXT, label TEXT, price REAL, prob REAL, status TEXT, result_json TEXT);
    CREATE TABLE prediction_memberships(prediction_id INTEGER REFERENCES picks(id),
      collection TEXT, rank INTEGER, PRIMARY KEY(prediction_id, collection));
    CREATE TABLE prediction_cache(cache_key TEXT PRIMARY KEY, local_date TEXT,
      timezone TEXT, status TEXT, payload_json TEXT);
  `);
  return sql;
}

function setCache(sql, day) {
  sql.prepare("INSERT INTO prediction_cache VALUES(?,?,?,?,?)").run(
    `daily_predictions:Africa/Lagos:${day}`, day, "Africa/Lagos", "COMPLETE", "{}",
  );
}

describe("/all sport menu and per-sport fixture pages", () => {
  it("lists only sports with predictions, with correct counts and sport buttons", async () => {
    const sql = baseSchema();
    const day = "2026-10-14";
    setCache(sql, day);
    const insert = sql.prepare(
      "INSERT INTO picks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
    );
    const membership = sql.prepare("INSERT INTO prediction_memberships VALUES(?,?,?)");
    // Two football fixtures (one with 2 markets) + one basketball fixture.
    const rows = [
      [1, "football", "Premier League", "Arsenal", "Chelsea", "2026-10-14T18:00:00Z", "Arsenal to Win"],
      [2, "football", "Premier League", "Arsenal", "Chelsea", "2026-10-14T18:00:00Z", "Over 2.5 Goals"],
      [3, "football", "La Liga", "Barca", "Real", "2026-10-14T20:00:00Z", "Barca to Win"],
      [4, "basketball", "NBA", "Lakers", "Celtics", "2026-10-14T23:00:00Z", "Lakers -5.5"],
    ];
    for (const [id, sport, league, home, away, start, label] of rows) {
      insert.run(id, day, "all", id, sport, league, home, away, start, "x", "y", label, 1.8, 0.6, "pending", null);
      membership.run(id, "all", id);
    }
    try {
      const db = stubDb(sql);
      const menu = await renderView({ DB: db, TIMEZONE: "Africa/Lagos", PAGE_SIZE: "5" }, "all", 0, day);
      assert.match(menu.text, /📚 ALL PREDICTIONS/);
      assert.match(menu.text, /4 predictions · 3 matches/);
      assert.match(menu.text, /Pick a sport:/);
      const kb = JSON.stringify(menu.replyMarkup);
      assert.match(kb, /pg\|all_football\|0\|20261014/);
      assert.match(kb, /pg\|all_basketball\|0\|20261014/);
      assert.match(kb, /⚽ Football \(3 · 2 matches\)/);
      assert.match(kb, /🏀 Basketball \(1 · 1 match\)/);

      const footballPage = await renderView({ DB: db, TIMEZONE: "Africa/Lagos", PAGE_SIZE: "5" }, "all_football", 0, day);
      assert.match(footballPage.text, /⚽ Football · 3 predictions · 2 matches · 🕘 times in WAT/);
      assert.match(footballPage.text, /Arsenal vs Chelsea/);
      assert.match(footballPage.text, /Arsenal to Win/);
      assert.match(footballPage.text, /Over 2\.5 Goals/); // second market of the SAME fixture, no repeated header
      assert.match(footballPage.text, /Barca vs Real/);
      const backKb = JSON.stringify(footballPage.replyMarkup);
      assert.match(backKb, /◀️ Sports/);
    } finally {
      sql.close();
    }
  });
});

describe("/stats and /yesterday real settlement data", () => {
  // predbot/storage/db.py picks_where(..., published_only=True) is the
  // production equivalent: only predictions with a 'top' or 'daily'
  // membership are tracked for stats. All Predictions ('all'-only rows)
  // must never contribute. This is the exact bug the 2026-10-14 screenshot
  // caught ("149 picks" instead of the real ~7 Top+Daily picks).
  const insertPick = (sql, id, day, status, price) =>
    sql.prepare("INSERT INTO picks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)")
      .run(id, day, "all", id, "football", "L", "A", "B", `${day}T18:00:00Z`, "x", "y", "lbl", price, 0.6, status, null);
  const addMembership = (sql, id, collection) =>
    sql.prepare("INSERT INTO prediction_memberships VALUES(?,?,?)").run(id, collection, id);

  it("renders yesterday's actual won/lost counts and hit rate from Top+Daily only", async () => {
    const sql = baseSchema();
    const today = "2026-10-14";
    const yesterday = "2026-10-13";
    setCache(sql, today);
    const settled = [
      [1, "won", 2.0, "top"], [2, "won", 1.6, "daily"], [3, "lost", 1.5, "daily"], [4, "push", 1.9, "top"],
    ];
    for (const [id, status, price, collection] of settled) {
      insertPick(sql, id, yesterday, status, price);
      addMembership(sql, id, "all"); // every pick also carries 'all' — must not matter
      addMembership(sql, id, collection);
    }
    try {
      const db = stubDb(sql);
      const stats = await renderView({ DB: db, TIMEZONE: "Africa/Lagos", PAGE_SIZE: "5" }, "stats", 0, today);
      assert.match(stats.text, /📊 STATS/);
      assert.match(stats.text, /🗓 Yesterday · Tue 13 Oct · 4 picks/);
      assert.match(stats.text, /✅ 2  ❌ 1  ➖ 0  ↩️ 1/);
      assert.match(stats.text, /🎯 67%/); // won/(won+lost) = 2/3
      const yview = await renderView({ DB: db, TIMEZONE: "Africa/Lagos", PAGE_SIZE: "5" }, "yesterday", 0, today);
      assert.match(yview.text, /🗓 YESTERDAY/);
      assert.match(yview.text, /✅ 2  ❌ 1/);
    } finally {
      sql.close();
    }
  });

  it("renders NO_RESULTS_YET when nothing settled yesterday", async () => {
    const sql = baseSchema();
    const today = "2026-10-14";
    setCache(sql, today);
    try {
      const db = stubDb(sql);
      const stats = await renderView({ DB: db, TIMEZONE: "Africa/Lagos", PAGE_SIZE: "5" }, "stats", 0, today);
      assert.match(stats.text, /No results yet/);
    } finally {
      sql.close();
    }
  });

  it("adding 100 All-Predictions-only rows does not change tracked statistics", async () => {
    const sql = baseSchema();
    const today = "2026-10-14";
    const yesterday = "2026-10-13";
    setCache(sql, today);
    // The two tracked legs (what production would actually count).
    insertPick(sql, 1, yesterday, "won", 2.0); addMembership(sql, 1, "all"); addMembership(sql, 1, "top");
    insertPick(sql, 2, yesterday, "lost", 1.5); addMembership(sql, 2, "all"); addMembership(sql, 2, "daily");
    const before = await renderView({ DB: stubDb(sql), TIMEZONE: "Africa/Lagos", PAGE_SIZE: "5" }, "stats", 0, today);
    // 100 All-Predictions-only rows, mixed won/lost, same day, no top/daily membership at all.
    for (let id = 100; id < 200; id += 1) {
      insertPick(sql, id, yesterday, id % 2 === 0 ? "won" : "lost", 3.0);
      addMembership(sql, id, "all");
    }
    try {
      const after = await renderView({ DB: stubDb(sql), TIMEZONE: "Africa/Lagos", PAGE_SIZE: "5" }, "stats", 0, today);
      assert.equal(after.text, before.text, "All Predictions must never change tracked stats");
      assert.match(after.text, /2 picks/); // still just the 2 tracked legs, not 102
      assert.doesNotMatch(after.text, /100|101|102/);
    } finally {
      sql.close();
    }
  });

  it("a prediction in BOTH Top and Daily is counted exactly once", async () => {
    const sql = baseSchema();
    const today = "2026-10-14";
    const yesterday = "2026-10-13";
    setCache(sql, today);
    insertPick(sql, 1, yesterday, "won", 2.0);
    addMembership(sql, 1, "all");
    addMembership(sql, 1, "top");
    addMembership(sql, 1, "daily"); // same prediction promoted into both tickets
    try {
      const stats = await renderView({ DB: stubDb(sql), TIMEZONE: "Africa/Lagos", PAGE_SIZE: "5" }, "stats", 0, today);
      assert.match(stats.text, /1 pick\b/);
      assert.doesNotMatch(stats.text, /2 picks/);
    } finally {
      sql.close();
    }
  });

  it("a day with a pending Top/Daily leg is not-yet-final, matching production's day_block gate", async () => {
    const sql = baseSchema();
    const today = "2026-10-14";
    const yesterday = "2026-10-13";
    setCache(sql, today);
    insertPick(sql, 1, yesterday, "won", 2.0); addMembership(sql, 1, "all"); addMembership(sql, 1, "top");
    insertPick(sql, 2, yesterday, "pending", 1.8); addMembership(sql, 2, "all"); addMembership(sql, 2, "daily");
    try {
      const stats = await renderView({ DB: stubDb(sql), TIMEZONE: "Africa/Lagos", PAGE_SIZE: "5" }, "stats", 0, today);
      assert.match(stats.text, /1 of 2 settled · final card after the last result/);
      // Yesterday's OWN hit rate is withheld until its day is fully settled;
      // the separate 7-day trend section legitimately has its own 🎯 (it
      // covers whichever days in the window ARE fully settled), so assert
      // on the yesterday block specifically, not the whole message.
      const yesterdayBlock = stats.text.split("📈 Last")[0];
      assert.doesNotMatch(yesterdayBlock, /🎯/);
    } finally {
      sql.close();
    }
  });

  it("void legs are counted but excluded from the staked/ROI denominator", async () => {
    const sql = baseSchema();
    const today = "2026-10-14";
    const yesterday = "2026-10-13";
    setCache(sql, today);
    insertPick(sql, 1, yesterday, "won", 2.0); addMembership(sql, 1, "all"); addMembership(sql, 1, "top");
    insertPick(sql, 2, yesterday, "void", 1.8); addMembership(sql, 2, "all"); addMembership(sql, 2, "daily");
    try {
      const stats = await renderView({ DB: stubDb(sql), TIMEZONE: "Africa/Lagos", PAGE_SIZE: "5" }, "stats", 0, today);
      assert.match(stats.text, /✅ 1  ❌ 0  ➖ 1/);
      assert.match(stats.text, /🎯 100%/); // decided = won+lost = 1, hit rate on decided legs only
    } finally {
      sql.close();
    }
  });

  it("By-sport breakdown and 7-day trend are also scoped to Top+Daily only — 100 All-only rows don't move them", async () => {
    const sql = baseSchema();
    const today = "2026-10-14";
    const yesterday = "2026-10-13";
    setCache(sql, today);
    insertPick(sql, 1, yesterday, "won", 2.0); addMembership(sql, 1, "all"); addMembership(sql, 1, "top");
    insertPick(sql, 2, yesterday, "lost", 1.5); addMembership(sql, 2, "all"); addMembership(sql, 2, "daily");
    const before = await renderView({ DB: stubDb(sql), TIMEZONE: "Africa/Lagos", PAGE_SIZE: "5" }, "stats", 0, today);
    for (let id = 100; id < 200; id += 1) {
      insertPick(sql, id, yesterday, id % 2 === 0 ? "won" : "lost", 3.0);
      addMembership(sql, id, "all"); // no top/daily membership: must never count
    }
    try {
      const after = await renderView({ DB: stubDb(sql), TIMEZONE: "Africa/Lagos", PAGE_SIZE: "5" }, "stats", 0, today);
      assert.equal(after.text, before.text, "All Predictions must never change by-sport or 7-day figures either");
      assert.match(after.text, /⚽ Football 1\/2/); // 1 won of 2 settled (tracked legs only)
      assert.match(after.text, /📈 Last 1 day/);
      assert.match(after.text, /✅ 1  ❌ 1  ➖ 0  ·  🎯 50%/);
    } finally {
      sql.close();
    }
  });

  it("the 7-day window is capped to how long the engine has actually run, never claims a full week early", async () => {
    const sql = baseSchema();
    const today = "2026-10-14";
    const yesterday = "2026-10-13";
    setCache(sql, today);
    // The engine's very first pick_date is yesterday — a 2-day-old table,
    // so the trend section must say "Last 1 day", never "Last 7 days".
    insertPick(sql, 1, yesterday, "won", 2.0); addMembership(sql, 1, "all"); addMembership(sql, 1, "top");
    try {
      const stats = await renderView({ DB: stubDb(sql), TIMEZONE: "Africa/Lagos", PAGE_SIZE: "5" }, "stats", 0, today);
      assert.match(stats.text, /📈 Last 1 day\b/);
      assert.doesNotMatch(stats.text, /📈 Last 7 days/);
    } finally {
      sql.close();
    }
  });

  it("a leg promoted into BOTH Top and Daily is counted once in the by-sport breakdown too", async () => {
    const sql = baseSchema();
    const today = "2026-10-14";
    const yesterday = "2026-10-13";
    setCache(sql, today);
    insertPick(sql, 1, yesterday, "won", 2.0);
    addMembership(sql, 1, "all");
    addMembership(sql, 1, "top");
    addMembership(sql, 1, "daily");
    try {
      const stats = await renderView({ DB: stubDb(sql), TIMEZONE: "Africa/Lagos", PAGE_SIZE: "5" }, "stats", 0, today);
      assert.match(stats.text, /⚽ Football 1\/1/);
      assert.doesNotMatch(stats.text, /⚽ Football 2\/2/);
    } finally {
      sql.close();
    }
  });
});
