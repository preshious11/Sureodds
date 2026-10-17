// Bug 13: /stats "All time" now computes via SQL aggregate
// (getStatsAggregateForRange) instead of reading every row
// (getStatsRowsForRange) into computeStatsBlock. Proves both paths produce
// byte-identical StatsBlock output on the same sample data, covering every
// status the reducer branches on (won/half_won/lost/half_lost/push/void/
// unresolved/pending) plus the bestWin tie-break and the All-only exclusion.
import { DatabaseSync } from "node:sqlite";
import { readFileSync } from "node:fs";
import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { computeStatsBlock, statsBlockFromAggregate } from "../src/format.ts";
import { getStatsAggregateForRange, getStatsRowsForRange } from "../src/db.ts";

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

function world() {
  const sql = new DatabaseSync(":memory:");
  sql.exec(readFileSync(new URL("../migrations/0001_foundation.sql", import.meta.url), "utf8"));
  return { sql, DB: stubDb(sql) };
}

let nextId = 1;
function insertPick(sql, { tier, status, price = 2.0, resultJson = null, label = "Pick", home = "H", away = "A" }) {
  const id = nextId++;
  sql.prepare(
    `INSERT INTO picks (id, pick_date, tier, rank, provider, provider_event_id, sport, home, away, start_utc,
       market, period, stat, entity, selection, label, price, prob, ev, status, result_json, created_at)
     VALUES (?,'2026-10-01',?,1,'p','e','football',?,?,'2026-10-01T18:00:00Z','1x2','full','points','all','home',?,?,0.6,0.1,?,?,'2026-10-01T00:00:00Z')`,
  ).run(id, tier, home, away, label, price, status, resultJson);
  if (tier !== "all") sql.prepare("INSERT INTO prediction_memberships VALUES (?,?,1)").run(id, tier);
  return id;
}

describe("stats aggregate (SQL) matches the row-by-row reducer exactly", () => {
  it("every status branch, the bestWin tie-break, and the All-only exclusion all agree", async () => {
    const { sql, DB } = world();
    insertPick(sql, { tier: "top", status: "won", price: 2.5, label: "A to Win", home: "Arsenal", away: "Chelsea" });
    insertPick(sql, { tier: "top", status: "won", price: 4.0, label: "B to Win", home: "Barca", away: "Real" }); // higher price -> bestWin
    insertPick(sql, { tier: "daily", status: "half_won", price: 1.9 });
    insertPick(sql, { tier: "daily", status: "lost", price: 1.8 });
    insertPick(sql, { tier: "top", status: "half_lost", price: 2.2 });
    insertPick(sql, { tier: "daily", status: "push", price: 2.0 });
    insertPick(sql, { tier: "top", status: "void", price: 1.5, resultJson: null }); // plain void
    insertPick(sql, { tier: "daily", status: "void", resultJson: JSON.stringify({ reason: "settlement deadline expired" }) }); // unresolved
    insertPick(sql, { tier: "top", status: "void", resultJson: "not valid json{{{" }); // malformed JSON -> plain void, never throws
    insertPick(sql, { tier: "daily", status: "pending" }); // excluded entirely
    insertPick(sql, { tier: "all", status: "won", price: 9.0, label: "Should never count" }); // All-only: excluded

    const since = "2026-09-01", before = "2026-11-01";
    const rows = await getStatsRowsForRange(DB, since, before);
    const fromRows = computeStatsBlock(rows);
    const agg = await getStatsAggregateForRange(DB, since, before);
    const fromAgg = statsBlockFromAggregate(agg);

    assert.deepEqual(fromAgg, fromRows);
    // Sanity-check the actual numbers too, not just that both paths agree with each other.
    assert.equal(fromRows.won, 3); // 2 won + 1 half_won
    assert.equal(fromRows.lost, 2); // 1 lost + 1 half_lost
    assert.equal(fromRows.push, 1);
    assert.equal(fromRows.void, 2); // plain void + malformed-json void
    assert.equal(fromRows.unresolved, 1);
    assert.equal(fromRows.n, 6); // won+half_won+lost+half_lost+push = 2+1+1+1+1
    assert.deepEqual(fromRows.bestWin, { label: "B to Win", price: 4.0, home: "Barca", away: "Real" });
  });

  it("agrees on a genuinely empty range too (n=0, bestWin=null, no division by zero)", async () => {
    const { sql, DB } = world();
    insertPick(sql, { tier: "top", status: "pending" }); // the only row, excluded from both paths
    const rows = await getStatsRowsForRange(DB, "2026-09-01", "2026-11-01");
    const agg = await getStatsAggregateForRange(DB, "2026-09-01", "2026-11-01");
    assert.deepEqual(statsBlockFromAggregate(agg), computeStatsBlock(rows));
    assert.equal(statsBlockFromAggregate(agg).n, 0);
    assert.equal(statsBlockFromAggregate(agg).bestWin, null);
  });

  it("agrees when every leg is a losing/void mix (profit stays negative, matching rounding)", async () => {
    const { sql, DB } = world();
    for (let i = 0; i < 7; i++) insertPick(sql, { tier: "top", status: "lost", price: 1.91 });
    const rows = await getStatsRowsForRange(DB, "2026-09-01", "2026-11-01");
    const agg = await getStatsAggregateForRange(DB, "2026-09-01", "2026-11-01");
    assert.deepEqual(statsBlockFromAggregate(agg), computeStatsBlock(rows));
  });
});
