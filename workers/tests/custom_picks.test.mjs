import { describe, it } from "node:test";
import assert from "node:assert/strict";
import {
  HELP,
  TODAY_ONLY,
  UNAVAILABLE,
  buildTicket,
  formatTicket,
  normalizeCandidate,
  parseSimpleRequest,
  respond,
} from "../src/custom_picks.ts";

const DAY = "2026-10-14";
const ZONE = "Africa/Lagos";
const NOW = Date.UTC(2026, 9, 14, 8, 0); // 09:00 Lagos

function candidatePayload(overrides = {}) {
  return {
    prediction_date: DAY,
    timezone: ZONE,
    canonical_event_id: "fx-1",
    sport: "football",
    market: "result2",
    selection: "home",
    price: 1.8,
    p_model: 0.7,
    start_utc: "2026-10-14T18:00:00Z",
    generated_at: "2026-10-14T06:00:00Z",
    home: "Arsenal",
    away: "Chelsea",
    league: "Premier League",
    ...overrides,
  };
}

describe("normalizeCandidate (~ custom_picks.py normalize)", () => {
  it("accepts a valid archived row and builds a human-readable outcome", () => {
    const c = normalizeCandidate(candidatePayload(), DAY, ZONE, NOW, Date.parse("2026-10-14T06:30:00Z"));
    assert.ok(c);
    assert.equal(c.outcome, "Arsenal to Win");
    assert.equal(c.sport, "football");
    assert.equal(c.competitionLabel, "Premier League");
  });
  it("rejects a different day/timezone (archive is date+zone scoped)", () => {
    assert.equal(normalizeCandidate(candidatePayload({ prediction_date: "2026-10-13" }), DAY, ZONE, NOW, NOW), null);
    assert.equal(normalizeCandidate(candidatePayload({ timezone: "UTC" }), DAY, ZONE, NOW, NOW), null);
  });
  it("rejects cancelled/live/finished fixtures", () => {
    assert.equal(normalizeCandidate(candidatePayload({ status: "cancelled" }), DAY, ZONE, NOW, NOW), null);
    assert.equal(normalizeCandidate(candidatePayload({ is_live: true }), DAY, ZONE, NOW, NOW), null);
  });
  it("rejects a fixture that has already started (today-only, not-started-only)", () => {
    const started = candidatePayload({ start_utc: "2026-10-14T07:00:00Z" }); // before NOW (08:00 UTC)
    assert.equal(normalizeCandidate(started, DAY, ZONE, NOW, NOW), null);
  });
  it("rejects an evaluation newer than the pool snapshot or the current time", () => {
    const futureEval = candidatePayload({ generated_at: "2026-10-14T09:00:00Z" });
    assert.equal(normalizeCandidate(futureEval, DAY, ZONE, NOW, Date.parse("2026-10-14T06:00:00Z")), null);
  });
  it("rejects out-of-range price/probability", () => {
    assert.equal(normalizeCandidate(candidatePayload({ price: 1.0 }), DAY, ZONE, NOW, NOW), null);
    assert.equal(normalizeCandidate(candidatePayload({ p_model: 1.0 }), DAY, ZONE, NOW, NOW), null);
  });
  it("rejects an unsupported sport or an invalid market/selection pair", () => {
    assert.equal(normalizeCandidate(candidatePayload({ sport: "cricket" }), DAY, ZONE, NOW, NOW), null);
    assert.equal(normalizeCandidate(candidatePayload({ market: "result2", selection: "draw" }), DAY, ZONE, NOW, NOW), null);
  });
  it("requires a line for total/handicap markets", () => {
    assert.equal(normalizeCandidate(candidatePayload({ market: "total", selection: "over" }), DAY, ZONE, NOW, NOW), null);
    const withLine = normalizeCandidate(candidatePayload({ market: "total", selection: "over", line: 2.5 }), DAY, ZONE, NOW, NOW);
    assert.ok(withLine);
    assert.equal(withLine.outcome, "Over 2.5");
  });
});

describe("parseSimpleRequest (simplified stand-in for custom_intent.py parse)", () => {
  it("recognizes an explicit pick count and sport", () => {
    const r = parseSimpleRequest("3 basketball picks");
    assert.ok(r.ok);
    assert.equal(r.request.pickCount, 3);
    assert.ok(r.request.sports.has("basketball"));
  });
  it("recognizes a target-odds request, including sport aliases", () => {
    const r = parseSimpleRequest("Safe 4.5 odds from soccer");
    assert.ok(r.ok);
    assert.equal(r.request.targetOdds, 4.5);
    assert.ok(r.request.sports.has("football"));
  });
  it("defers unrecognized free text to HELP rather than guessing", () => {
    const r = parseSimpleRequest("what's the weather like");
    assert.equal(r.ok, false);
    assert.equal(r.text, HELP);
  });
  it("routes a future-date request to TODAY_ONLY", () => {
    const r = parseSimpleRequest("picks for tomorrow");
    assert.equal(r.ok, false);
    assert.equal(r.text, TODAY_ONLY);
  });
  it("rejects empty text and oversized text as HELP, never a crash", () => {
    assert.equal(parseSimpleRequest("").ok, false);
    assert.equal(parseSimpleRequest("x".repeat(600)).ok, false);
  });
});

describe("buildTicket + formatTicket (ticket text mirrors custom_picks.py format_ticket)", () => {
  it("builds a ticket matching an explicit pick count, one leg per fixture", () => {
    const candidates = [
      normalizeCandidate(candidatePayload(), DAY, ZONE, NOW, NOW),
      normalizeCandidate(candidatePayload({ canonical_event_id: "fx-2", home: "Barca", away: "Real", price: 1.6, p_model: 0.65 }), DAY, ZONE, NOW, NOW),
      normalizeCandidate(candidatePayload({ canonical_event_id: "fx-3", home: "Bayern", away: "Dortmund", price: 1.5, p_model: 0.6 }), DAY, ZONE, NOW, NOW),
    ];
    const ticket = buildTicket(candidates, { targetOdds: null, pickCount: 2, sports: new Set() });
    assert.ok(ticket);
    assert.equal(ticket.picks.length, 2);
    const text = formatTicket(ticket);
    assert.match(text, /🔥 .+ ODDS/);
    assert.match(text, /Arsenal vs Chelsea/);
    assert.match(text, /Odds: 1\.80 · Confidence: 70%/);
  });
  it("never returns a ticket outside the target-odds tolerance band", () => {
    const candidates = [
      normalizeCandidate(candidatePayload({ price: 1.2, p_model: 0.9 }), DAY, ZONE, NOW, NOW),
    ];
    // Target far from anything achievable with one candidate at price 1.2.
    const ticket = buildTicket(candidates, { targetOdds: 10, pickCount: null, sports: new Set() });
    assert.equal(ticket, null);
  });
  it("returns null rather than fabricating a ticket when not enough fixtures exist", () => {
    const candidates = [normalizeCandidate(candidatePayload(), DAY, ZONE, NOW, NOW)];
    const ticket = buildTicket(candidates, { targetOdds: null, pickCount: 3, sports: new Set() });
    assert.equal(ticket, null);
  });
});

describe("respond() end-to-end contract", () => {
  function deps(pool, rows) {
    return { getPool: async () => pool, getCandidates: async () => rows };
  }
  it("replies UNAVAILABLE when no pool has been captured for today", async () => {
    const r = await respond(deps(null, []), "3 picks", ZONE, DAY, NOW);
    assert.equal(r.reason, "unavailable");
    assert.equal(r.text, UNAVAILABLE);
  });
  it("replies HELP without ever touching the pool for unparseable text", async () => {
    let touched = false;
    const d = { getPool: async () => { touched = true; return null; }, getCandidates: async () => [] };
    const r = await respond(d, "???", ZONE, DAY, NOW);
    assert.equal(r.reason, "help");
    assert.equal(touched, false);
  });
  it("builds and formats a real ticket from archived candidates", async () => {
    const rows = [
      { payload_json: JSON.stringify(candidatePayload()) },
      { payload_json: JSON.stringify(candidatePayload({ canonical_event_id: "fx-2", home: "Barca", away: "Real" })) },
    ];
    const pool = { status: "COMPLETE", generated_at: "2026-10-14T06:30:00Z" };
    const r = await respond(deps(pool, rows), "2 picks", ZONE, DAY, NOW);
    assert.equal(r.reason, "success");
    assert.match(r.text, /🔥/);
  });
  it("skips a malformed archive row instead of crashing", async () => {
    const rows = [{ payload_json: "not json" }, { payload_json: JSON.stringify(candidatePayload()) }];
    const pool = { status: "COMPLETE", generated_at: "2026-10-14T06:30:00Z" };
    const r = await respond(deps(pool, rows), "1 picks", ZONE, DAY, NOW);
    assert.equal(r.reason, "success");
  });
});
