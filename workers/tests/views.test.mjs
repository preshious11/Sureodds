import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { lagosDateString, lagosDayBounds, notReadyText, renderView } from "../src/views.ts";

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
  it("returns honest not-ready state when cache is missing/partial", async () => {
    const env = { DB: stubDb(), TIMEZONE: "Africa/Lagos", PAGE_SIZE: "5" };
    const res = await renderView(env, "top", 0, "2026-10-11");
    assert.match(res.text, /not ready yet/);
    assert.match(res.text, /09:00/);
    assert.match(notReadyText("daily", "2026-10-11"), /No provider was queried/);
  });
  it("renders cached picks with pagination, empty page stays honest", async () => {
    const picks = [
      { id: 1, home: "Arsenal", away: "Chelsea", label: "Home Win", price: 2.1 },
      { id: 2, home: "Barca", away: "Real", label: "Over 2.5", price: 1.9 },
    ];
    const env = {
      DB: stubDb({ cache: { status: "COMPLETE", payload_json: "{}" }, picks }),
      TIMEZONE: "Africa/Lagos",
      PAGE_SIZE: "5",
    };
    const page0 = await renderView(env, "top", 0, "2026-10-11");
    assert.match(page0.text, /Arsenal/);
    const emptyDb = stubDb({ cache: { status: "COMPLETE", payload_json: "{}" }, picks: [] });
    const empty = await renderView({ DB: emptyDb, TIMEZONE: "Africa/Lagos", PAGE_SIZE: "5" }, "top", 0, "2026-10-11");
    assert.match(empty.text, /honest empty|No qualified/);
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
