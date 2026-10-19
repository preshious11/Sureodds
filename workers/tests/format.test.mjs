import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, it } from "node:test";
import assert from "node:assert/strict";
import {
  cardHeader,
  combinedOdds,
  dayLabel,
  nPicks,
  picksCard,
  pickLines,
  computeStatsBlock,
  yesterdayLines,
  TOP_TITLE,
  DAILY_TITLE,
} from "../src/format.ts";

const FIXTURE = JSON.parse(
  readFileSync(fileURLToPath(new URL("./fixtures/golden_pick_line.json", import.meta.url)), "utf-8"),
);

describe("shared golden fixture (predictions-bot/tests/test_bot_format_golden.py renders the same input)", () => {
  it("pickLines matches the Python production formatter byte-for-byte", () => {
    const i = FIXTURE.input;
    const lines = pickLines(
      { sport: i.sport_key, league: i.league, home: i.home, away: i.away, label: i.outcome, price: i.price, prob: i.confidence, startUtc: i.start_utc },
      new Date(i.now_utc).getTime(),
    );
    assert.deepEqual(lines, FIXTURE.expected_lines);
  });
});

describe("format.ts unit coverage (predbot/bot/format.py port)", () => {
  it("combinedOdds multiplies, never adds, and rejects invalid prices", () => {
    assert.equal(combinedOdds([1.5, 2.0]), 3.0);
    assert.throws(() => combinedOdds([1.5, 1.0]));
    assert.throws(() => combinedOdds([1.5, Number.NaN]));
  });

  it("nPicks pluralizes correctly", () => {
    assert.equal(nPicks(1), "1 pick");
    assert.equal(nPicks(2), "2 picks");
  });

  it("dayLabel formats like production (Sat 19 Sep)", () => {
    assert.equal(dayLabel("2026-09-19"), "Sat 19 Sep");
  });

  it("cardHeader adds the combined-odds line only when there are prices", () => {
    assert.deepEqual(cardHeader(TOP_TITLE, "2026-09-19", []), [`${TOP_TITLE} · Sat 19 Sep`]);
    assert.deepEqual(cardHeader(TOP_TITLE, "2026-09-19", [1.5, 2.0], "WAT"), [
      `${TOP_TITLE} · Sat 19 Sep`,
      "🎟 Combined odds 3.00 · 2 picks · 🕘 times in WAT",
    ]);
  });

  it("picksCard renders production's exact empty-state text per title", () => {
    assert.match(picksCard([], "2026-09-19", TOP_TITLE), /No Bet of the Day for today\.\n\nSee other picks:/);
    assert.match(picksCard([], "2026-09-19", DAILY_TITLE), /No selections qualified today\./);
  });

  it("picksCard sorts by kickoff and appends the Page X of Y footer", () => {
    const later = { sport: "football", league: "L", home: "B", away: "C", label: "x", price: 1.5, prob: 0.5, startUtc: "2026-09-19T20:00:00Z" };
    const earlier = { sport: "football", league: "L", home: "A", away: "D", label: "y", price: 1.5, prob: 0.5, startUtc: "2026-09-19T10:00:00Z" };
    const text = picksCard([later, earlier], "2026-09-19", DAILY_TITLE, { page: 1, pages: 3 });
    const aIdx = text.indexOf("A vs D");
    const bIdx = text.indexOf("B vs C");
    assert.ok(aIdx > -1 && bIdx > -1 && aIdx < bIdx, "earlier kickoff must render first");
    assert.match(text, /Page 1 of 3\n\nBet only what you can afford to lose\.$/);
  });

  it("picksCard's combined odds use totalPrices across the whole ticket, not just the page shown", () => {
    const shown = { sport: "football", league: "L", home: "A", away: "B", label: "x", price: 1.5, prob: 0.5, startUtc: "2026-09-19T10:00:00Z" };
    const text = picksCard([shown], "2026-09-19", TOP_TITLE, { totalPrices: [1.5, 2.0, 1.8] });
    assert.match(text, /Combined odds 5\.40 · 3 picks/);
  });

  it("computeStatsBlock mirrors predbot/jobs/stats.py _block (flat 1-unit stakes)", () => {
    const rows = [
      { status: "won", price: 2.0, label: "x", home: "A", away: "B", result_json: null },
      { status: "lost", price: 1.5, label: "y", home: "C", away: "D", result_json: null },
      { status: "push", price: 1.9, label: "z", home: "E", away: "F", result_json: null },
      { status: "void", price: 1.4, label: "w", home: "G", away: "H", result_json: null },
      { status: "void", price: 1.4, label: "v", home: "I", away: "J", result_json: JSON.stringify({ reason: "settlement deadline expired" }) },
      { status: "pending", price: 2.5, label: "u", home: "K", away: "L", result_json: null },
    ];
    const b = computeStatsBlock(rows);
    assert.equal(b.won, 1);
    assert.equal(b.lost, 1);
    assert.equal(b.push, 1);
    assert.equal(b.void, 1);
    assert.equal(b.unresolved, 1);
    assert.equal(b.n, 3); // won + lost + push only
    assert.equal(b.profit, 0); // (2.0-1) + (-1) + 0 = 0
    assert.equal(b.roi, 0);
    assert.deepEqual(b.bestWin, { label: "x", price: 2.0, home: "A", away: "B" });
  });

  it("yesterdayLines renders the no-picks and settled-with-results cases", () => {
    const empty = computeStatsBlock([]);
    assert.deepEqual(yesterdayLines("2026-09-18", empty, 0), ["🗓 Yesterday · Fri 18 Sep — no picks were issued."]);
    const rows = [
      { status: "won", price: 2.0, label: "x", home: "A", away: "B", result_json: null },
      { status: "lost", price: 1.5, label: "y", home: "C", away: "D", result_json: null },
    ];
    const lines = yesterdayLines("2026-09-18", computeStatsBlock(rows), rows.length);
    assert.equal(lines[0], "🗓 Yesterday · Fri 18 Sep · 2 picks");
    assert.equal(lines[1], "✅ 1  ❌ 1  ➖ 0");
    assert.match(lines[2], /^🎯 50%/);
  });
});
