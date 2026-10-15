import { describe, it } from "node:test";
import assert from "node:assert/strict";
import {
  HELP,
  TODAY_ONLY,
  UNAVAILABLE,
  chooseTicket,
  claimedCount,
  dropConflictingIdentities,
  eligible,
  failureReason,
  formatTicket,
  labelFor,
  normalizeCandidate,
  normalizeCandidateWithReason,
  parseRequest,
  prepareAlternatives,
  refine,
  requestKey,
  respond,
  search,
  shortlist,
  ticketSignature,
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
    assert.equal(withLine.outcome, "Over 2.5 Total Goals");
    const teamTotal = candidate({ market: "total", selection: "over", line: 1.5, entity: "home" });
    assert.equal(teamTotal.outcome, "Arsenal Over 1.5 Goals");
    const handicap = candidate({ market: "handicap", selection: "home", line: -1.5 });
    assert.equal(handicap.outcome, "Handicap: Arsenal -1.5");
    assert.equal(candidate({ market: "total", selection: "over", line: 2.5, entity: "player" }), null);
  });

  // Regression: 2026-10-15 live screenshot. A football total/handicap rendered
  // with "Points" instead of "Goals", and a 1st-quarter basketball total
  // dropped its "Total" scope word, both traced to outcomeLabel/statWord bugs
  // in this file (not the archived payload, which already carries the correct
  // label — these assert the WORKER recomputes it correctly from raw fields).
  describe("sport-specific market units and period scope (2026-10-15 screenshot)", () => {
    it('FK Riga vs Kairat Almaty: a football total/handicap says "Goals", never "Points"', () => {
      const base = { sport: "football", home: "FK Riga", away: "Kairat Almaty", stat: "points" };
      const total = candidate({ ...base, market: "total", selection: "under", line: 1.5, entity: "home" });
      assert.equal(total.outcome, "FK Riga Under 1.5 Goals");
      const matchTotal = candidate({ ...base, market: "total", selection: "over", line: 2.5, entity: "all" });
      assert.equal(matchTotal.outcome, "Over 2.5 Total Goals");
      assert.ok(!total.outcome.includes("Points"));
      assert.ok(!matchTotal.outcome.includes("Points"));
    });
    it("a 1st-quarter basketball total keeps its explicit Total scope, never collapsing to a bare line", () => {
      const c = candidate({
        sport: "basketball", home: "Vilniaus Kibirkstis-MRU Women", away: "Athinaikos Women",
        market: "total", selection: "under", line: 37.5, entity: "all", period: "1q", stat: "points",
      });
      assert.equal(c.outcome, "Under 37.5 Total Points (1st Qtr)");
    });
    it("a quarter-scoped team handicap still names the team, the signed line and the period", () => {
      const c = candidate({
        sport: "basketball", home: "Vilniaus Kibirkstis-MRU Women", away: "Athinaikos Women",
        market: "handicap", selection: "home", line: 2.5, period: "1q", stat: "points",
      });
      assert.equal(c.outcome, "Handicap: Vilniaus Kibirkstis-MRU Women +2.5 (1st Qtr)");
    });
    it("a period-scoped prop names the side, line, stat and period - never a bare fragment", () => {
      const c = candidate({
        sport: "basketball", home: "Lakers", away: "Bulls",
        market: "prop", selection: "over", line: 12.5, period: "1q", stat: "points", entity: "home",
      });
      assert.equal(c.outcome, "Lakers Over 12.5 Points (1st Qtr)");
    });
    it("an unresolvable prop (no attributable side, or no line) is rejected rather than shown as a bare fragment", () => {
      assert.equal(candidate({ market: "prop", selection: "over", line: 12.5, entity: "neither" }), null);
      assert.equal(candidate({ market: "prop", selection: "over", entity: "home" }), null);
    });
  });

  // Regression: production has 2,441 archived candidates whose payload_json
  // never carried generated_at (older archive writer), even though the D1
  // row's own generated_at column is always set. normalizeCandidate must
  // fall back to the row's timestamp instead of rejecting these wholesale.
  describe("generated_at fallback to the D1 row (production archive-writer gap)", () => {
    function payloadWithoutGeneratedAt(overrides = {}) {
      const payload = candidatePayload(overrides);
      delete payload.generated_at;
      return payload;
    }
    it("falls back to the row's generated_at when the payload lacks it entirely", () => {
      const c = normalizeCandidate(
        payloadWithoutGeneratedAt(),
        DAY, ZONE, NOW, Date.parse("2026-10-14T06:30:00Z"), false,
        "2026-10-14T06:00:00Z",
      );
      assert.ok(c, "candidate must be accepted using the row's generated_at as fallback");
    });
    it("still rejects when the row's fallback generated_at is itself after pool time or in the future", () => {
      const afterPool = normalizeCandidate(
        payloadWithoutGeneratedAt(),
        DAY, ZONE, NOW, Date.parse("2026-10-14T06:30:00Z"), false,
        "2026-10-14T07:00:00Z", // after poolTimeMs
      );
      assert.equal(afterPool, null);
      const future = normalizeCandidate(
        payloadWithoutGeneratedAt(),
        DAY, ZONE, NOW, Date.parse("2026-10-14T09:00:00Z"), false,
        "2026-10-14T09:00:00Z", // after nowMs
      );
      assert.equal(future, null);
    });
    it("rejects when neither the payload nor the row supplies a generated_at", () => {
      const c = normalizeCandidate(
        payloadWithoutGeneratedAt(),
        DAY, ZONE, NOW, Date.parse("2026-10-14T06:30:00Z"), false,
        undefined,
      );
      assert.equal(c, null);
    });
    it("never uses the row's generated_at to rescue a present-but-invalid payload timestamp", () => {
      const c = normalizeCandidate(
        candidatePayload({ generated_at: "not-a-date" }),
        DAY, ZONE, NOW, Date.parse("2026-10-14T06:30:00Z"), false,
        "2026-10-14T06:00:00Z", // valid row fallback, must NOT be used
      );
      assert.equal(c, null, "a present but malformed payload timestamp must reject, not silently fall back to the row");
    });
    it("prefers a valid payload timestamp over the row's when both are present", () => {
      // Payload says generated after pool time (invalid); row says well before it
      // (valid). The payload value must win since it IS present — this proves
      // the fallback only engages on absence, not merely on availability.
      const c = normalizeCandidate(
        candidatePayload({ generated_at: "2026-10-14T07:00:00Z" }),
        DAY, ZONE, NOW, Date.parse("2026-10-14T06:30:00Z"), false,
        "2026-10-14T05:00:00Z",
      );
      assert.equal(c, null, "payload's own (invalid-for-this-pool) timestamp must be honored over the row fallback");
    });
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
  it("builds a ticket from production-shaped rows: payload lacks generated_at, row supplies it", async () => {
    function payloadWithoutGeneratedAt(overrides = {}) {
      const payload = candidatePayload(overrides);
      delete payload.generated_at;
      return payload;
    }
    const rows = [
      { payload_json: JSON.stringify(payloadWithoutGeneratedAt()), generated_at: "2026-10-14T06:00:00Z" },
      {
        payload_json: JSON.stringify(payloadWithoutGeneratedAt({ canonical_event_id: "fx-2", home: "Barca", away: "Real" })),
        generated_at: "2026-10-14T06:00:00Z",
      },
    ];
    const pool = { status: "COMPLETE", generated_at: "2026-10-14T06:30:00Z" };
    const { result } = await respond(deps(pool, rows), "2 picks", ZONE, DAY, NOW, null);
    assert.equal(result.reason, "success");
    assert.match(result.text, /🔥/);
  });
  it("excludes a production-shaped row whose row generated_at is stale/expired (after pool time)", async () => {
    function payloadWithoutGeneratedAt(overrides = {}) {
      const payload = candidatePayload(overrides);
      delete payload.generated_at;
      return payload;
    }
    const rows = [{ payload_json: JSON.stringify(payloadWithoutGeneratedAt()), generated_at: "2026-10-14T07:00:00Z" }];
    const pool = { status: "COMPLETE", generated_at: "2026-10-14T06:30:00Z" };
    const { result } = await respond(deps(pool, rows), "1 picks", ZONE, DAY, NOW, null);
    assert.notEqual(result.reason, "success");
  });
  // Production-archive shape: capture_candidate() serializes ONLY the
  // evaluated event/market/score fields into payload_json. prediction_date,
  // timezone and generated_at live exclusively in the D1 row columns (see
  // predbot/storage/db.py get_custom_pick_candidates, which merges the
  // columns over the parsed JSON). These fixtures reproduce that exact
  // shape — any test embedding the scoping keys inside payload_json is NOT
  // production-compatible and masks the 2026-10-14 empty-pool defect.
  function productionPayload(overrides = {}) {
    return {
      provider: "sgo",
      provider_event_id: "ev-1",
      sport: "football",
      league: "Premier League",
      home: "Arsenal",
      away: "Chelsea",
      start_utc: "2026-10-14T18:00:00Z",
      league_id: "epl",
      extra: {},
      event_sources: ["sgo"],
      canonical_event_id: "fx-1",
      canonical_competition_id: "epl",
      home_id: "ars",
      away_id: "che",
      market: "result2",
      selection: "home",
      price: 1.8,
      period: "full",
      stat: "points",
      entity: "all",
      line: null,
      market_label: "Home",
      p_market: 0.55,
      p_model: 0.7,
      sources: { sgo: 0.7 },
      agreement: 0.8,
      ev: 0.05,
      edge: 0.15,
      label: "Arsenal to Win",
      liquidity: 0.9,
      edge_ev: 0.03,
      bookmaker_count: 5,
      provider_count: 1,
      rank_score: null,
      quality: null,
      se: null,
      p_lcb: null,
      ...overrides,
    };
  }
  function productionRow(overrides = {}, rowOverrides = {}) {
    return {
      payload_json: JSON.stringify(productionPayload(overrides)),
      generated_at: "2026-10-14T06:00:00Z",
      prediction_date: DAY,
      timezone: ZONE,
      ...rowOverrides,
    };
  }
  describe("production archive shape (row columns merged over payload_json)", () => {
    const POOL = { status: "COMPLETE", generated_at: "2026-10-14T06:30:00Z" };
    function deps(rows) {
      return { getPool: async () => POOL, getCandidates: async () => rows };
    }
    it("accepts a production-shaped row: scoping comes from the row columns, not the payload", async () => {
      assert.ok(!("prediction_date" in productionPayload()), "fixture must not embed scoping keys in payload_json");
      assert.ok(!("timezone" in productionPayload()), "fixture must not embed scoping keys in payload_json");
      assert.ok(!("generated_at" in productionPayload()), "fixture must not embed scoping keys in payload_json");
      const rows = [
        productionRow(),
        productionRow({ canonical_event_id: "fx-2", home: "Barca", away: "Real", home_id: "bar", away_id: "rea" }),
      ];
      const { result } = await respond(deps(rows), "2 picks", ZONE, DAY, NOW, null);
      assert.equal(result.reason, "success");
      assert.match(result.text, /🔥/);
    });
    it("reports day_zone_mismatch (not silent empty) when the row columns are missing entirely", () => {
      const { candidate, reason } = normalizeCandidateWithReason(
        productionPayload(), DAY, ZONE, NOW, Date.parse("2026-10-14T06:30:00Z"), true, "2026-10-14T06:00:00Z",
      );
      assert.equal(candidate, null);
      assert.equal(reason, "day_zone_mismatch");
    });
    it("rejects a stale evaluation whose Lagos day differs from the pool day", () => {
      const { candidate, reason } = normalizeCandidateWithReason(
        { ...productionPayload(), prediction_date: DAY, timezone: ZONE, generated_at: "2026-10-13T22:00:00Z" },
        DAY, ZONE, NOW, Date.parse("2026-10-14T06:30:00Z"),
      );
      assert.equal(candidate, null);
      assert.equal(reason, "stale_evaluated_day");
    });
    it("rejects a dead fixture reported only inside the nested extra dict", () => {
      const { candidate, reason } = normalizeCandidateWithReason(
        { ...productionPayload(), prediction_date: DAY, timezone: ZONE, generated_at: "2026-10-14T06:00:00Z", extra: { status: "final" } },
        DAY, ZONE, NOW, Date.parse("2026-10-14T06:30:00Z"),
      );
      assert.equal(candidate, null);
      assert.equal(reason, "bad_status");
    });
    it("reaches the ticket builder for odds targets: 30 odds fails honestly as unreachable, never below_quality_floor", async () => {
      const rows = [
        productionRow(),
        productionRow({ canonical_event_id: "fx-2", home: "Barca", away: "Real", home_id: "bar", away_id: "rea", price: 1.6, p_model: 0.65 }),
      ];
      const { result } = await respond(deps(rows), "Give me 30 odds", ZONE, DAY, NOW, null);
      assert.notEqual(result.reason, "below_quality_floor", "pool must be non-empty so the builder is actually reached");
      assert.equal(result.reason, "target_unreachable");
    });
    it("builds a real ticket for an attainable odds target from production-shaped rows", async () => {
      // 1.8 × 1.6 × 1.9 = 5.47, inside the ±10% band around 5.
      const rows = [
        productionRow({ price: 1.8, p_model: 0.7 }),
        productionRow({ canonical_event_id: "fx-2", home: "Barca", away: "Real", home_id: "bar", away_id: "rea", price: 1.6, p_model: 0.65 }),
        productionRow({ canonical_event_id: "fx-3", home: "Bayern", away: "Dortmund", home_id: "bay", away_id: "dor", price: 1.9, p_model: 0.68 }),
      ];
      const { result } = await respond(deps(rows), "give me 5 odds", ZONE, DAY, NOW, null);
      assert.equal(result.reason, "success");
      assert.match(result.text, /🔥/);
    });
    it("parses both reported Telegram messages as target-odds requests", () => {
      for (const text of ["Give me 30 odds", "give me 5 odds"]) {
        const parsed = parseRequest(text);
        assert.ok(parsed.ok, `${text} must parse`);
        assert.equal(parsed.request.pickCount, null);
        assert.equal(parsed.request.refinement, false);
      }
      assert.equal(parseRequest("Give me 30 odds").request.targetOdds, 30);
      assert.equal(parseRequest("give me 5 odds").request.targetOdds, 5);
    });
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

describe("reported-issue regressions (strict floor, labels, counts, fixtures, variety)", () => {
  function pricedPool(prices, startId = 1) {
    return prices.map((price, i) =>
      candidate({
        canonical_event_id: `fx-${startId + i}`,
        home: `Home${startId + i}`,
        away: `Away${startId + i}`,
        home_id: `h${startId + i}`,
        away_id: `a${startId + i}`,
        price,
        p_model: 0.7,
      }),
    );
  }

  it("a 1.03 selection can never appear in a 15-odds ticket", () => {
    const pool = [
      ...pricedPool([1.03]),
      ...pricedPool([1.5, 1.6, 1.7, 1.8, 1.9, 2.0], 2),
    ];
    const req = parseRequest("15 odds").request;
    const ticket = search(pool, req);
    assert.ok(ticket);
    assert.ok(ticket.picks.every((c) => c.price >= 1.25));
    assert.ok(!ticket.picks.some((c) => c.price === 1.03));
  });

  it("no selection below 1.25 appears at any target, count, or sport", () => {
    for (const price of [1.0, 1.03, 1.08, 1.12, 1.19, 1.24]) {
      const only = pricedPool([price, price + 0.01, price + 0.02]).filter(Boolean);
      for (const text of ["15 odds", "5 odds", "2 odds", "3 picks", "2 basketball picks"]) {
        const req = parseRequest(text).request;
        if (!req) continue;
        const ticket = search(eligible(only, req, NOW), req);
        if (ticket) assert.ok(ticket.picks.every((c) => c.price >= 1.25), `${text} @ ${price}`);
      }
    }
    // A pool of only sub-floor legs yields no ticket at all, never filler.
    const lowOnly = pricedPool([1.12, 1.15, 1.19]);
    assert.equal(search(lowOnly, parseRequest("2 odds").request), null);
    const lowTicket = { picks: lowOnly.slice(0, 2), odds: 1.12 * 1.15 };
    assert.equal(validTicket(lowTicket, parseRequest("2 odds").request, NOW), false);
  });

  it("the 15-odds button reads 'Best 15 Odds', never '15g'", () => {
    assert.equal(labelFor(parseRequest("15 odds").request), "🔥 Best 15 Odds");
    assert.equal(labelFor(parseRequest("safe 4 odds football").request), "🛡️ Safe 4 Odds · Football");
    const both = parseRequest("15 odds").request;
    assert.equal(labelFor({ ...both, pickCount: 2 }), "🔥 Best 15 Odds · 2 Picks");
  });

  it("a three-pick request returns exactly three picks with a matching heading", () => {
    const pool = pricedPool([1.6, 1.6, 1.6, 1.6]);
    const req = parseRequest("3 picks").request;
    const ticket = search(pool, req);
    assert.equal(ticket.picks.length, 3);
    const text = formatTicket(ticket);
    assert.match(text, /🔥 4\.10 ODDS/);
    assert.equal((text.match(/Confidence:/g) ?? []).length, 3);
  });

  it("a 15-odds request is never mistaken for a 15-leg request", () => {
    const pool = pricedPool([1.8, 1.7, 1.6, 1.5, 1.4, 1.9, 2.0, 2.1, 2.2]);
    const req = parseRequest("15 odds").request;
    assert.equal(req.pickCount, null);
    assert.equal(req.targetOdds, 15);
    const ticket = search(pool, req);
    if (ticket) {
      assert.ok(ticket.picks.length <= 8);
      assert.ok(ticket.picks.length < 15);
      assert.ok(ticket.picks.every((c) => c.price >= 1.25));
    }
  });

  it("count-promising alternative buttons carry exactly that many picks", () => {
    const pool = pricedPool([1.6, 1.6, 1.6, 1.6, 1.6]);
    const req = parseRequest("100 odds").request;
    const alts = prepareAlternatives(pool, req, "target_unreachable", NOW);
    assert.ok(alts.length);
    for (const alt of alts) {
      assert.equal(validTicket(alt.ticket, alt.request, NOW), true);
      const claimed = claimedCount(alt.request);
      if (claimed) assert.equal(alt.ticket.picks.length, claimed, alt.label);
    }
  });

  it("different users can receive different qualified tickets; repeats rotate", () => {
    const pool = pricedPool([1.7, 1.7, 1.7, 1.7, 1.7]);
    const req = parseRequest("5 odds").request;
    const options = shortlist(pool, req);
    assert.ok(options.length > 1);
    const seen = new Set(options.map((t) => ticketSignature(t)));
    assert.ok(seen.size > 1);
    const picks = new Set();
    for (let u = 0; u < 10; u++) picks.add(ticketSignature(chooseTicket(options, `user-${u}`, DAY, req)));
    assert.ok(picks.size > 1, "rotation must spread users across qualified tickets");
    const first = ticketSignature(chooseTicket(options, "repeat-user", DAY, req));
    const second = ticketSignature(chooseTicket(options, "repeat-user", DAY, req));
    assert.notEqual(second, first, "a repeat request avoids the just-served ticket when alternatives exist");
    // A lone qualified ticket is still served to everyone.
    const single = chooseTicket(options.slice(0, 1), "anyone", DAY, req);
    assert.equal(ticketSignature(single), ticketSignature(options[0]));
  });

  it("conflicting fixture identities are dropped before search", () => {
    const a = candidate({ home_id: "x", away_id: "y" });
    const b = candidate({ home: "Brighton", away: "Kauno Zalgiris", home_id: "bri", away_id: "kau" });
    assert.equal(dropConflictingIdentities([a, b]).length, 0);
    const ok = candidate({ canonical_event_id: "fx-2", home: "Barca", away: "Real", home_id: "bar", away_id: "rea" });
    assert.equal(dropConflictingIdentities([a, ok]).length, 2);
  });

  it("same-team and unattributable-market rows are rejected", () => {
    assert.equal(candidate({ home: "Arsenal", away: "arsenal" }), null);
    assert.equal(candidate({ home_id: "same", away_id: "same" }), null);
    assert.equal(candidate({ market: "total", selection: "over", line: 2.5, entity: "player" }), null);
  });

  it("basketball totals and handicaps carry scope, line and side", () => {
    const total = candidate({ sport: "basketball", market: "total", selection: "over", line: 220.5, home: "Lakers", away: "Bulls" });
    assert.ok(total.outcome.includes("220.5") && total.outcome.includes("Points"));
    const teamTotal = candidate({ market: "total", selection: "under", line: 1.5, entity: "home", stat: "goals" });
    assert.equal(teamTotal.outcome, "Arsenal Under 1.5 Goals");
    const half = candidate({ market: "total", selection: "over", line: 1.5, period: "1h", stat: "goals" });
    assert.ok(half.outcome.includes("1st Half"));
    const handicap = candidate({ market: "handicap", selection: "away", line: 4.5, home: "Lakers", away: "Bulls" });
    assert.equal(handicap.outcome, "Handicap: Bulls +4.5");
  });

  it("a 50-odds request fails honestly with only proven, above-floor alternatives", async () => {
    const rows = pricedPool([1.8, 1.6, 1.9]).map((c, i) => ({
      payload_json: JSON.stringify({
        prediction_date: DAY, timezone: ZONE, canonical_event_id: c.fixtureId,
        sport: "football", market: "result2", selection: "home", price: c.price, p_model: 0.7,
        start_utc: "2026-10-14T18:00:00Z", generated_at: "2026-10-14T06:00:00Z",
        home: c.home, away: c.away, home_id: `h${i}`, away_id: `a${i}`, league: "Premier League",
      }),
      generated_at: "2026-10-14T06:00:00Z", prediction_date: DAY, timezone: ZONE,
    }));
    const pool = { status: "COMPLETE", generated_at: "2026-10-14T06:30:00Z" };
    const deps = { getPool: async () => pool, getCandidates: async () => rows };
    const { result } = await respond(deps, "50 odds", ZONE, DAY, NOW, null);
    assert.notEqual(result.reason, "success");
    for (const alt of result.suggestions) {
      assert.equal(validTicket(alt.ticket, alt.request, NOW), true);
      assert.ok(alt.ticket.picks.every((c) => c.price >= 1.25));
    }
  });

  it("ticket headings always match the combined odds of the legs shown", () => {
    const pool = pricedPool([1.6, 1.6, 1.6]);
    const ticket = search(pool, parseRequest("3 picks").request);
    const text = formatTicket(ticket);
    const product = ticket.picks.reduce((a, c) => a * c.price, 1);
    assert.ok(text.startsWith(`🔥 ${product.toFixed(2)} ODDS`));
  });
});
