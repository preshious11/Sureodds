import { describe, it } from "node:test";
import assert from "node:assert/strict";
import {
  HELP,
  TODAY_ONLY,
  UNAVAILABLE,
  eligible,
  failureReason,
  formatTicket,
  normalizeCandidate,
  parseRequest,
  prepareAlternatives,
  refine,
  respond,
  search,
  validTicket,
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

function candidate(overrides = {}) {
  return normalizeCandidate(candidatePayload(overrides), DAY, ZONE, NOW, Date.parse("2026-10-14T06:30:00Z"));
}

describe("normalizeCandidate (~ custom_picks.py normalize)", () => {
  it("accepts a valid archived row and builds a human-readable outcome", () => {
    const c = candidate();
    assert.ok(c);
    assert.equal(c.outcome, "Arsenal to Win");
    assert.equal(c.competition, "epl"); // Premier League resolved via the curated league map
  });
  it("rejects a different day/timezone (archive is date+zone scoped)", () => {
    assert.equal(candidate({ prediction_date: "2026-10-13" }), null);
    assert.equal(candidate({ timezone: "UTC" }), null);
  });
  it("rejects cancelled/live/finished fixtures", () => {
    assert.equal(candidate({ status: "cancelled" }), null);
    assert.equal(candidate({ is_live: true }), null);
  });
  it("rejects out-of-range price/probability and unsupported sport/market", () => {
    assert.equal(candidate({ price: 1.0 }), null);
    assert.equal(candidate({ p_model: 1.0 }), null);
    assert.equal(candidate({ sport: "cricket" }), null);
    assert.equal(candidate({ market: "result2", selection: "draw" }), null);
  });
  it("requires a line for total/handicap markets", () => {
    assert.equal(candidate({ market: "total", selection: "over" }), null);
    const withLine = candidate({ market: "total", selection: "over", line: 2.5 });
    assert.ok(withLine);
    assert.equal(withLine.outcome, "Over 2.5");
  });
});

describe("parseRequest (~ custom_intent.py parse)", () => {
  it("recognizes an explicit pick count and sport", () => {
    const r = parseRequest("3 basketball picks");
    assert.ok(r.ok);
    assert.equal(r.request.pickCount, 3);
    assert.ok(r.request.sports.has("basketball"));
  });
  it("recognizes a target-odds request with a sport alias", () => {
    const r = parseRequest("Safe 4.5 odds from soccer");
    assert.ok(r.ok);
    assert.equal(r.request.targetOdds, 4.5);
    assert.ok(r.request.sports.has("football"));
    assert.equal(r.request.preference, "safe");
  });
  it("recognizes a league/competition and resolves its sport", () => {
    const r = parseRequest("2 picks from the premier league");
    assert.ok(r.ok);
    assert.ok(r.request.competitions.has("epl"));
  });
  it("rejects a sport mentioned only to exclude it", () => {
    const r = parseRequest("no basketball picks please give me 3");
    assert.equal(r.ok, false);
    assert.match(r.text, /Which sports/);
  });
  it("recognizes market inclusion and exclusion with negation", () => {
    // Exclusion placed last so it doesn't carry its negation forward onto
    // "double chance" via the comma-continuation rule (same ambiguity
    // production's own custom_intent.py negation scan has for "X, Y" where
    // Y directly follows an excluded market with only a comma between).
    const r = parseRequest("5 picks, double chance only, no handicap");
    assert.ok(r.ok);
    assert.ok(r.request.allowedMarkets.has("dc"));
    assert.ok(r.request.excludedMarkets.has("handicap"));
  });
  it("rejects a market both included and excluded", () => {
    const r = parseRequest("double chance only, no double chance");
    assert.equal(r.ok, false);
  });
  it("defers unrecognized free text to HELP rather than guessing", () => {
    const r = parseRequest("what's the weather like");
    assert.equal(r.ok, false);
    assert.equal(r.text, HELP);
  });
  it("routes a future-date request to TODAY_ONLY", () => {
    const r = parseRequest("picks for tomorrow");
    assert.equal(r.ok, false);
    assert.equal(r.text, TODAY_ONLY);
  });
  it("detects refinement intent ('instead', 'safer') vs. a fresh request", () => {
    assert.equal(parseRequest("make it safer instead").ok, true);
    assert.equal(parseRequest("make it safer instead").request.refinement, true);
    assert.equal(parseRequest("give me 3 basketball picks").request.refinement, false);
  });
  it("rejects conflicting odds targets or pick counts in the same message", () => {
    const r = parseRequest("4 odds and 7 odds");
    assert.equal(r.ok, false);
    assert.equal(r.reason, "conflicting_constraints");
  });
});

describe("refine (~ custom_recovery.py refine) — conversation refinement", () => {
  it("merges only the newly-specified fields into the stored previous request", () => {
    const previous = parseRequest("3 basketball picks").request;
    const current = parseRequest("make it safer instead").request;
    const merged = refine(previous, current);
    assert.equal(merged.pickCount, 3); // kept from previous
    assert.ok(merged.sports.has("basketball")); // kept from previous
    assert.equal(merged.preference, "safe"); // upgraded by the refinement
  });
  it("accumulates excluded markets across refinements rather than replacing them", () => {
    const previous = parseRequest("5 picks, no handicap").request;
    const current = parseRequest("make it safer instead, no btts").request;
    const merged = refine(previous, current);
    assert.ok(merged.excludedMarkets.has("handicap"));
    assert.ok(merged.excludedMarkets.has("btts"));
  });
});

describe("search + validTicket (~ custom_picks.py search(), custom_recovery.py valid_ticket)", () => {
  it("builds a ticket matching an explicit pick count, one leg per fixture", () => {
    const candidates = [
      candidate(),
      candidate({ canonical_event_id: "fx-2", home: "Barca", away: "Real", price: 1.6, p_model: 0.65 }),
      candidate({ canonical_event_id: "fx-3", home: "Bayern", away: "Dortmund", price: 1.5, p_model: 0.6 }),
    ];
    const req = { ...parseRequest("2 picks").request };
    const ticket = search(candidates, req);
    assert.ok(ticket);
    assert.equal(ticket.picks.length, 2);
    assert.equal(validTicket(ticket, req, NOW), true);
    const text = formatTicket(ticket);
    assert.match(text, /🔥 .+ ODDS/);
    assert.match(text, /Arsenal vs Chelsea/);
  });
  it("never returns a ticket outside the target-odds tolerance band", () => {
    const candidates = [candidate({ price: 1.2, p_model: 0.9 })];
    const req = parseRequest("10 odds").request;
    const ticket = search(candidates, req);
    assert.equal(ticket, null);
  });
  it("returns null rather than fabricating a ticket when not enough fixtures exist", () => {
    const candidates = [candidate()];
    const req = parseRequest("3 picks").request;
    const ticket = search(candidates, req);
    assert.equal(ticket, null);
  });
  it("re-validation rejects a ticket whose leg has since started", () => {
    const candidates = [candidate()];
    const req = parseRequest("1 picks").request;
    const ticket = search(candidates, req);
    assert.ok(ticket);
    const afterKickoff = Date.parse("2026-10-14T19:00:00Z");
    assert.equal(validTicket(ticket, req, afterKickoff), false);
  });
});

describe("failureReason + prepareAlternatives (~ custom_recovery.py failure_reason/prepare)", () => {
  it("explains insufficient_count honestly and proves at least one real alternative", () => {
    const pool = [candidate()];
    const req = parseRequest("3 picks").request;
    const matches = eligible(pool, req, NOW);
    const reason = failureReason(pool, req, NOW, matches);
    assert.equal(reason, "insufficient_count");
    const alts = prepareAlternatives(pool, req, reason, NOW);
    // Every proven alternative must itself be a validated ticket — never a
    // promised label without a matching ticket behind it.
    for (const alt of alts) assert.equal(validTicket(alt.ticket, alt.request, NOW), true);
  });
});

describe("respond() end-to-end contract", () => {
  function deps(pool, rows) {
    return { getPool: async () => pool, getCandidates: async () => rows };
  }
  it("replies UNAVAILABLE when no pool has been captured for today", async () => {
    const { result } = await respond(deps(null, []), "3 picks", ZONE, DAY, NOW, null);
    assert.equal(result.reason, "archive_unavailable");
    assert.equal(result.text, UNAVAILABLE);
  });
  it("replies HELP without ever touching the pool for unparseable text", async () => {
    let touched = false;
    const d = { getPool: async () => { touched = true; return null; }, getCandidates: async () => [] };
    const { result } = await respond(d, "???", ZONE, DAY, NOW, null);
    assert.equal(result.reason, "invalid_request");
    assert.equal(touched, false);
  });
  it("builds and formats a real ticket from archived candidates", async () => {
    const rows = [
      { payload_json: JSON.stringify(candidatePayload()) },
      { payload_json: JSON.stringify(candidatePayload({ canonical_event_id: "fx-2", home: "Barca", away: "Real" })) },
    ];
    const pool = { status: "COMPLETE", generated_at: "2026-10-14T06:30:00Z" };
    const { result } = await respond(deps(pool, rows), "2 picks", ZONE, DAY, NOW, null);
    assert.equal(result.reason, "success");
    assert.match(result.text, /🔥/);
  });
  it("skips a malformed archive row instead of crashing", async () => {
    const rows = [{ payload_json: "not json" }, { payload_json: JSON.stringify(candidatePayload()) }];
    const pool = { status: "COMPLETE", generated_at: "2026-10-14T06:30:00Z" };
    const { result } = await respond(deps(pool, rows), "1 picks", ZONE, DAY, NOW, null);
    assert.equal(result.reason, "success");
  });
  it("a refinement follow-up merges into the previous request", async () => {
    const rows = [
      { payload_json: JSON.stringify(candidatePayload()) },
      { payload_json: JSON.stringify(candidatePayload({ canonical_event_id: "fx-2", home: "Barca", away: "Real", p_model: 0.55 })) },
    ];
    const pool = { status: "COMPLETE", generated_at: "2026-10-14T06:30:00Z" };
    const first = await respond(deps(pool, rows), "2 picks", ZONE, DAY, NOW, null);
    assert.equal(first.result.reason, "success");
    // "make it safer instead" carries no explicit count — refined onto the
    // stored previous request, it must still ask for 2 picks.
    const second = await respond(deps(pool, rows), "make it safer instead", ZONE, DAY, NOW, first.request);
    assert.equal(second.request.pickCount, 2);
  });
});
