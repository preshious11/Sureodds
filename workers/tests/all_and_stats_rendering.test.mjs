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
  it("renders yesterday's actual won/lost counts and hit rate", async () => {
    const sql = baseSchema();
    const today = "2026-10-14";
    const yesterday = "2026-10-13";
    setCache(sql, today);
    const insert = sql.prepare(
      "INSERT INTO picks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
    );
    const membership = sql.prepare("INSERT INTO prediction_memberships VALUES(?,?,?)");
    const settled = [
      [1, "won", 2.0], [2, "won", 1.6], [3, "lost", 1.5], [4, "push", 1.9],
    ];
    for (const [id, status, price] of settled) {
      insert.run(id, yesterday, "all", id, "football", "L", "A", "B", "2026-10-13T18:00:00Z", "x", "y", "lbl", price, 0.6, status, null);
      membership.run(id, "all", id);
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
});
