import { DatabaseSync } from "node:sqlite";
import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { renderView } from "../src/views.ts";

// Regression coverage for the 2026-10-14 incident: generation and D1 sync
// wrote correct prediction_memberships rows (2 top, 5 daily, 192 all), but
// /top and /daily still rendered empty in the staging bot. This drives
// renderView() — the exact function the Telegram command handler calls —
// through a REAL SQLite-backed D1 stub shaped like production, at a
// realistic scale (not the 6-row unit fixture), to catch any regression in
// the full retrieval path: command -> renderView -> getDayCache/getPicksPage
// -> rendered text. If this ever goes back to empty, this test fails here
// instead of only being visible in a live Telegram chat.
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

function fixture() {
  const sql = new DatabaseSync(":memory:");
  sql.exec(`
    CREATE TABLE picks(id INTEGER PRIMARY KEY, pick_date TEXT, tier TEXT, rank INTEGER,
      sport TEXT, league TEXT, home TEXT, away TEXT, start_utc TEXT, market TEXT,
      selection TEXT, label TEXT, price REAL, prob REAL, status TEXT);
    CREATE TABLE prediction_memberships(prediction_id INTEGER REFERENCES picks(id),
      collection TEXT, rank INTEGER, PRIMARY KEY(prediction_id, collection));
    CREATE TABLE prediction_cache(cache_key TEXT PRIMARY KEY, local_date TEXT,
      timezone TEXT, status TEXT, payload_json TEXT);
  `);
  const day = "2026-10-14";
  const insertPick = sql.prepare(
    "INSERT INTO picks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
  );
  const insertMembership = sql.prepare(
    "INSERT INTO prediction_memberships VALUES(?,?,?)",
  );
  // 192 all-tier predictions, matching the real Oct 14 generation count.
  for (let i = 1; i <= 192; i += 1) {
    insertPick.run(
      i, day, "all", i, "football", "League", `Home${i}`, `Away${i}`,
      "2026-10-14T18:00:00Z", "total", "over", `Over 2.5 (#${i})`, 1.9, 0.6, "pending",
    );
    insertMembership.run(i, "all", i);
  }
  // 2 top picks and 5 daily picks drawn from that same pool, as the real
  // selector does — ranks restart at 1 within each collection.
  for (const [rank, pid] of [[1, 5], [2, 17]]) insertMembership.run(pid, "top", rank);
  for (const [rank, pid] of [[1, 2], [2, 9], [3, 40], [4, 88], [5, 150]]) {
    insertMembership.run(pid, "daily", rank);
  }
  sql.prepare(
    "INSERT INTO prediction_cache VALUES(?,?,?,?,?)",
  ).run(`daily_predictions:Africa/Lagos:${day}`, day, "Africa/Lagos", "COMPLETE", "{}");
  return { sql, db: stubDb(sql), day };
}

describe("Telegram /top and /daily rendering at production scale", () => {
  it("/top lists exactly the 2 top-collection picks, not empty", async () => {
    const { sql, db, day } = fixture();
    try {
      const env = { DB: db, TIMEZONE: "Africa/Lagos", PAGE_SIZE: "5" };
      const res = await renderView(env, "top", 0, day);
      assert.match(res.text, /Home5 vs Away5/);
      assert.match(res.text, /Home17 vs Away17/);
      assert.doesNotMatch(res.text, /No qualified|honest empty/);
    } finally {
      sql.close();
    }
  });
  it("/daily lists exactly the 5 daily-collection picks, not empty", async () => {
    const { sql, db, day } = fixture();
    try {
      const env = { DB: db, TIMEZONE: "Africa/Lagos", PAGE_SIZE: "25" };
      const res = await renderView(env, "daily", 0, day);
      for (const pid of [2, 9, 40, 88, 150]) {
        assert.match(res.text, new RegExp(`Home${pid} vs Away${pid}`));
      }
      assert.doesNotMatch(res.text, /No qualified|honest empty/);
    } finally {
      sql.close();
    }
  });
  it("/all still serves the full 192-prediction pool alongside top/daily", async () => {
    const { sql, db, day } = fixture();
    try {
      const env = { DB: db, TIMEZONE: "Africa/Lagos", PAGE_SIZE: "25" };
      const res = await renderView(env, "all", 0, day);
      assert.doesNotMatch(res.text, /No qualified|honest empty/);
    } finally {
      sql.close();
    }
  });
});
