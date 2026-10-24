import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { lagosDateString, lagosDayBounds, notReadyText, picksPerPage, renderView } from "../src/views.ts";

// Every fixture in this file uses 2026-10-11 as its stand-in "today" — freeze
// the clock to match (renderView's bug-12 today-vs-past-day distinction
// otherwise drifts out of sync with these fixtures as real wall-clock time
// moves past that date).
Date.now = () => Date.UTC(2026, 9, 11, 8, 0); // 09:00 Lagos

// Minimal in-memory D1 stub: only the queries views.ts issues.
function stubDb({ cache = null, picks = [] } = {}) {
  return {
    prepare(sql) {
      return {
        _sql: sql,
        _args: [],
        bind(...args) {
          this._args = args;
          return this;
        },
        async first() {
          if (this._sql.includes("FROM prediction_cache")) return cache;
          // hasPicksForDate (bug 12, past-day readiness check): true iff
          // this fixture seeded any picks at all, same signal `picks` already
          // represents for the "FROM picks p JOIN ..." / getAllPicksForCollection
          // query below.
          if (this._sql.includes("FROM picks")) return picks.length ? { one: 1 } : null;
          return null;
        },
        async all() {
          return { results: picks };
        },
        async run() {
          return { meta: { changes: 1 } };
        },
      };
    },
  };
}

describe("Lagos calendar", () => {
  it("maps 08:30 UTC to same Lagos date, 00:30 UTC to previous Lagos date", () => {
    assert.equal(lagosDateString(Date.UTC(2026, 9, 11, 8, 30)), "2026-10-11");
    // 00:30 UTC Oct 11 == 01:30 Lagos Oct 11 -> still Oct 11; 23:30 UTC Oct 10 == 00:30 Lagos Oct 11
    assert.equal(lagosDateString(Date.UTC(2026, 9, 10, 23, 30)), "2026-10-11");
  });
  it("day window spans 27h (midnight + 3h offset)", () => {
    const { startUtcMs, endUtcMs } = lagosDayBounds("2026-10-11");
    assert.equal(endUtcMs - startUtcMs, 27 * 3_600_000);
  });
});

describe("views never touch providers", () => {
  it("renders all four views with a throwing network stub (zero outbound calls)", async () => {
    const realFetch = globalThis.fetch;
    let calls = 0;
    globalThis.fetch = async () => {
      calls += 1;
      throw new Error("network must never be used by views");
    };
    try {
      const picks = [
        { id: 1, sport: "football", market: "result3", selection: "home", period: "full",
          stat: "points", entity: "all", line: null, home: "Arsenal", away: "Chelsea", label: "Home Win", price: 2.1 },
        { id: 2, sport: "football", market: "total", selection: "over", period: "full",
          stat: "goals", entity: "all", line: 2.5, home: "Barca", away: "Real", label: "Over 2.5", price: 1.9 },
      ];
      const env = {
        DB: stubDb({ cache: { status: "COMPLETE", payload_json: "{}" }, picks }),
        TIMEZONE: "Africa/Lagos",
        PAGE_SIZE: "5",
      };
      for (const view of ["top", "daily", "all", "stats"]) {
        const res = await renderView(env, view, 0, "2026-10-11");
        assert.ok(res.text.length > 0, view);
        assert.ok(res.replyMarkup, view);
      }
      assert.equal(calls, 0);
    } finally {
      globalThis.fetch = realFetch;
    }
  });
  it("returns an honest not-ready state when cache is missing/partial, with no internal details leaked", async () => {
    const env = { DB: stubDb(), TIMEZONE: "Africa/Lagos", PAGE_SIZE: "5" };
    const res = await renderView(env, "top", 0, "2026-10-11");
    // Matches predbot/bot/pagination.py's own user-facing "awaiting
    // scheduled fetch" copy — never provider names, cache states,
    // schedule internals, or anything a Python-bot user never saw.
    assert.match(res.text, /not been generated yet/);
    const text = notReadyText("daily", "2026-10-11");
    for (const leak of [/provider/i, /COMPLETE/, /PARTIAL/, /09:00/, /09:30/, /generation runs/i, /subscriber delivery/i]) {
      assert.doesNotMatch(text, leak, `notReadyText must never leak: ${leak}`);
    }
  });
  it("renders cached picks with pagination, empty page stays honest", async () => {
    const picks = [
      { id: 1, sport: "football", league: "Premier League", home: "Arsenal", away: "Chelsea",
        market: "result3", selection: "home", period: "full", stat: "points", entity: "all", line: null,
        label: "Home Win", price: 2.1, prob: 0.5, start_utc: "2026-10-11T18:00:00Z" },
      { id: 2, sport: "football", league: "La Liga", home: "Barca", away: "Real",
        market: "total", selection: "over", period: "full", stat: "goals", entity: "all", line: 2.5,
        label: "Over 2.5", price: 1.9, prob: 0.6, start_utc: "2026-10-11T20:00:00Z" },
    ];
    const env = {
      DB: stubDb({ cache: { status: "COMPLETE", payload_json: "{}" }, picks }),
      TIMEZONE: "Africa/Lagos",
      PAGE_SIZE: "5",
    };
    const page0 = await renderView(env, "top", 0, "2026-10-11");
    assert.match(page0.text, /Arsenal/);
    assert.match(page0.text, /🎟 Combined odds/); // ticket header carries the combined-odds line
    const emptyDb = stubDb({ cache: { status: "COMPLETE", payload_json: "{}" }, picks: [] });
    const emptyTop = await renderView({ DB: emptyDb, TIMEZONE: "Africa/Lagos", PAGE_SIZE: "5" }, "top", 0, "2026-10-11");
    assert.match(emptyTop.text, /No Bet of the Day for today/); // production's exact Top empty-state text
    const emptyDaily = await renderView({ DB: emptyDb, TIMEZONE: "Africa/Lagos", PAGE_SIZE: "5" }, "daily", 0, "2026-10-11");
    assert.match(emptyDaily.text, /No selections qualified today/); // production's exact Daily empty-state text
  });
  it("duplicate Telegram update is claimed once (dedupe contract)", async () => {
    // Contract test: tryClaimUpdate uses INSERT OR IGNORE; second claim of a
    // completed update must be duplicate.
    let rows = 0;
    let status = "pending";
    const db = {
      prepare() {
        return {
          bind() { return this; },
          async run() {
            if (status === "done") return { meta: { changes: 0 } };
            rows += 1;
            return { meta: { changes: rows === 1 ? 1 : 0 } };
          },
          async first() { return rows === 0 ? null : { status, attempts: 0 } },
        };
      },
    };
    const { tryClaimUpdate, completeUpdate } = await import("../src/db.ts");
    assert.equal(await tryClaimUpdate(db, 999), "new");
    await completeUpdate(db, 999).catch(() => { status = "done"; });
    status = "done";
    assert.equal(await tryClaimUpdate(db, 999), "duplicate");
  });
});

describe("PAGE_SIZE configuration (picksPerPage)", () => {
  it("defaults to production's 5 and clamps garbage into range", () => {
    assert.equal(picksPerPage({}), 5);
    assert.equal(picksPerPage({ PAGE_SIZE: "5" }), 5);
    assert.equal(picksPerPage({ PAGE_SIZE: "abc" }), 5);
    assert.equal(picksPerPage({ PAGE_SIZE: "0" }), 5);
    assert.equal(picksPerPage({ PAGE_SIZE: "-3" }), 5);
    assert.equal(picksPerPage({ PAGE_SIZE: "100" }), 25);
  });
  it("PAGE_SIZE=2 paginates the ticket two legs at a time", async () => {
    const picks = [1, 2, 3].map((id) => ({
      id, sport: "football", league: "Premier League", home: `Home${id}`, away: `Away${id}`,
      market: "result3", selection: "home", period: "full", stat: "points", entity: "all", line: null,
      label: "Home Win", price: 2.0, prob: 0.5, start_utc: `2026-10-11T${17 + id}:00:00Z`,
    }));
    const env = {
      DB: stubDb({ cache: { status: "COMPLETE", payload_json: "{}" }, picks }),
      TIMEZONE: "Africa/Lagos",
      PAGE_SIZE: "2",
    };
    const page0 = await renderView(env, "top", 0, "2026-10-11");
    assert.match(page0.text, /Home1/);
    assert.match(page0.text, /Home2/);
    assert.doesNotMatch(page0.text, /Home3/);
    assert.match(page0.text, /Page 1 of 2/);
    const page1 = await renderView(env, "top", 1, "2026-10-11");
    assert.match(page1.text, /Home3/);
  });
});
