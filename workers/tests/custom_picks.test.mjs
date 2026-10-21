import { beforeEach, describe, it } from "node:test";
import assert from "node:assert/strict";
import {
  HELP,
  INTRO,
  introText,
  TODAY_ONLY,
  UNAVAILABLE,
  __resetPoolCacheForTests,
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
  scoped,
  search,
  shortlist,
  ticketSignature,
  validTicket,
} from "../src/custom_picks.ts";

// See custom_picks_routing.test.mjs's identical comment: resets bug 13's
// per-isolate pool cache so fixtures reusing the same (day, zone,
// generated_at) across tests never leak cached candidates between them.
beforeEach(() => __resetPoolCacheForTests());

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
  // Bug 13: a per-isolate cache keyed by (day, timezone, pool.generated_at)
  // means a Worker isolate serving several requests back to back only pays
  // for getCandidates' full read once per 60s per pool generation.
  it("a repeat request within 60s reuses the cached candidates (getCandidates not called again)", async () => {
    const rows = [{ payload_json: JSON.stringify(candidatePayload()) }];
    const pool = { status: "COMPLETE", generated_at: "2026-10-14T06:30:00Z" };
    let getCandidatesCalls = 0;
    const d = { getPool: async () => pool, getCandidates: async () => { getCandidatesCalls += 1; return rows; } };
    await respond(d, "1 picks", ZONE, DAY, NOW, null);
    assert.equal(getCandidatesCalls, 1);
    await respond(d, "1 picks", ZONE, DAY, NOW + 30_000, null); // 30s later, still within the 60s TTL
    assert.equal(getCandidatesCalls, 1, "cache hit - candidates not re-read");
  });
  it("after the TTL elapses, candidates are re-read even with the same generated_at", async () => {
    const rows = [{ payload_json: JSON.stringify(candidatePayload()) }];
    const pool = { status: "COMPLETE", generated_at: "2026-10-14T06:30:00Z" };
    let getCandidatesCalls = 0;
    const d = { getPool: async () => pool, getCandidates: async () => { getCandidatesCalls += 1; return rows; } };
    await respond(d, "1 picks", ZONE, DAY, NOW, null);
    await respond(d, "1 picks", ZONE, DAY, NOW + 61_000, null); // past the 60s TTL
    assert.equal(getCandidatesCalls, 2);
  });
  it("a new generation (changed pool.generated_at) is never served stale cached candidates", async () => {
    const rowsV1 = [{ payload_json: JSON.stringify(candidatePayload()) }];
    const rowsV2 = [{ payload_json: JSON.stringify(candidatePayload({ canonical_event_id: "fx-2", home: "Barca", away: "Real" })) }];
    let pool = { status: "COMPLETE", generated_at: "2026-10-14T06:30:00Z" };
    let getCandidatesCalls = 0;
    const d = { getPool: async () => pool, getCandidates: async () => { getCandidatesCalls += 1; return pool.generated_at === "2026-10-14T06:30:00Z" ? rowsV1 : rowsV2; } };
    const first = await respond(d, "1 picks", ZONE, DAY, NOW, null);
    pool = { status: "COMPLETE", generated_at: "2026-10-14T07:00:00Z" }; // regeneration landed
    const second = await respond(d, "1 picks", ZONE, DAY, NOW + 1000, null);
    assert.equal(getCandidatesCalls, 2, "a changed generated_at must never be served the old cache entry");
    assert.notDeepEqual(first.result.text, second.result.text);
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

// Kovqi natural-language improvements: welcome message, sport/league/market
// recognition, unsupported-sport response, team names, explicit lines.
describe("INTRO lists exactly the currently ENABLED sports, nothing fabricated", () => {
  it("names every sport production's ALLOWED_SPORTS actually enables, and one example", () => {
    // Production's real ALLOWED_SPORTS default excludes MMA (2026-10 audit):
    // the welcome message must never advertise a sport that can't actually
    // be served, even though MMA stays fully recognized/parseable.
    for (const label of ["Football", "Basketball", "American Football", "Baseball", "Tennis"]) {
      assert.ok(INTRO.includes(label), `INTRO should list ${label}`);
    }
    assert.ok(!INTRO.includes("MMA"));
    assert.match(INTRO, /Example:/);
    for (const sport of ["hockey", "cricket", "boxing", "rugby"]) {
      assert.ok(!INTRO.toLowerCase().includes(sport));
    }
  });
  it("re-enabling a sport is a config change only, never a separately hardcoded list", () => {
    assert.ok(introText(new Set(["football", "basketball", "american_football", "baseball", "tennis", "mma"])).includes("MMA"));
  });
});

describe("parseRequest: expanded sport/league/market recognition", () => {
  it('"UCL" alone is recognized as the Champions League', () => {
    const r = parseRequest("3 picks UCL").request;
    assert.ok(r.competitions.has("ucl"));
  });
  it('"EPL BTTS" recognizes both the league and the market together', () => {
    const r = parseRequest("EPL BTTS").request;
    assert.ok(r.competitions.has("epl"));
    assert.ok(r.allowedMarkets.has("btts"));
  });
  it('an explicit "Over 1.5" names the total market and an exact line, never a nearby one', () => {
    const r = parseRequest("3 picks over 1.5").request;
    assert.ok(r.allowedMarkets.has("total"));
    assert.deepEqual(r.totalLine, { selection: "over", line: 1.5 });
  });
  it('"Under 2.5" is recognized the same way', () => {
    const r = parseRequest("under 2.5, 2 picks").request;
    assert.deepEqual(r.totalLine, { selection: "under", line: 2.5 });
  });
  it('an explicit line only matches the FULL MATCH total, never a team total or a period-scoped one', () => {
    const req = parseRequest("1 picks over 1.5").request;
    const matchTotal = candidate({ market: "total", selection: "over", line: 1.5, entity: "all" });
    const teamTotal = candidate({ canonical_event_id: "fx-2", market: "total", selection: "over", line: 1.5, entity: "home" });
    const halfTotal = candidate({ canonical_event_id: "fx-3", market: "total", selection: "over", line: 1.5, entity: "all", period: "1h" });
    assert.equal(scoped(matchTotal, req), true);
    assert.equal(scoped(teamTotal, req), false);
    assert.equal(scoped(halfTotal, req), false);
  });
  it("extra sport aliases: hoops/basket -> basketball", () => {
    assert.ok(parseRequest("3 hoops picks").request.sports.has("basketball"));
    assert.ok(parseRequest("3 basket picks").request.sports.has("basketball"));
  });
});

describe('unsupported-sport response: "We do not cover X yet"', () => {
  it("names the actual requested sport and lists the supported ones", () => {
    const r = parseRequest("3 hockey picks");
    assert.equal(r.ok, false);
    assert.equal(r.reason, "unsupported_sport");
    assert.match(r.text, /^We do not cover Hockey yet\. Please choose one of our supported sports\./);
    for (const label of ["Football", "Basketball", "Tennis"]) assert.ok(r.text.includes(label));
  });
  it("identifies a specific compound phrase correctly (ice hockey, not a bare sport guess)", () => {
    const r = parseRequest("ice hockey picks please");
    assert.equal(r.reason, "unsupported_sport");
    assert.match(r.text, /We do not cover Hockey yet/);
  });
  it("covers cricket, rugby, boxing, golf, darts as named, distinct sports", () => {
    for (const [word, label] of [["cricket", "Cricket"], ["rugby", "Rugby"], ["boxing", "Boxing"], ["golf", "Golf"], ["darts", "Darts"]]) {
      const r = parseRequest(`3 ${word} picks`);
      assert.equal(r.reason, "unsupported_sport");
      assert.match(r.text, new RegExp(`We do not cover ${label} yet`));
    }
  });
  it("never invents a sport name for text that isn't a recognized sport at all", () => {
    const r = parseRequest("asdkjfh picks please");
    assert.notEqual(r.reason, "unsupported_sport");
  });
  it("MMA is recognized by name but disabled by default (production's real ALLOWED_SPORTS)", () => {
    const r = parseRequest("3 MMA picks");
    assert.equal(r.reason, "unsupported_sport");
    assert.match(r.text, /^We do not cover MMA yet\. Please choose one of our supported sports\./);
    assert.ok(!r.text.split("\n\n").at(-1).includes("MMA")); // never listed among the supported sports itself
    // Recognition itself is untouched: explicitly enabling it makes it parse clean.
    const enabled = parseRequest("3 MMA picks", new Set(["mma"])).request;
    assert.ok(enabled.sports.has("mma"));
  });
  it("a supported sport with no candidates today is a DIFFERENT message (sport_unavailable), never the unsupported-sport text", () => {
    const pool = [candidate({ sport: "football" })];
    const req = parseRequest("3 tennis picks").request;
    assert.equal(req.sports.has("tennis"), true); // parsed fine - tennis IS supported
    const matches = eligible(pool, req, NOW);
    assert.equal(failureReason(pool, req, NOW, matches), "sport_unavailable");
  });
  it("a supported sport with candidates but no matching market is market_unavailable, not sport_unavailable", () => {
    const pool = [candidate({ sport: "tennis", market: "result2" })];
    const req = parseRequest("tennis picks, btts only").request;
    const matches = eligible(pool, req, NOW);
    assert.equal(failureReason(pool, req, NOW, matches), "market_unavailable");
  });
});

describe("team-name recognition (matched against today's pool, never a static catalog)", () => {
  function teamPool() {
    return [
      candidate({ canonical_event_id: "fx-arsenal", home: "Arsenal", away: "Chelsea" }),
      candidate({ canonical_event_id: "fx-barca", home: "Barcelona", away: "Real Madrid", price: 1.6, p_model: 0.65 }),
    ];
  }
  it("a named team scopes the ticket to its fixture only", async () => {
    const rows = teamPool().map((c) => ({
      payload_json: JSON.stringify(candidatePayload({
        canonical_event_id: c.fixtureId, home: c.home, away: c.away, price: c.price, p_model: c.probability,
      })),
    }));
    const pool = { status: "COMPLETE", generated_at: "2026-10-14T06:30:00Z" };
    const deps = { getPool: async () => pool, getCandidates: async () => rows };
    const { result } = await respond(deps, "1 picks Arsenal", ZONE, DAY, NOW, null);
    assert.equal(result.reason, "success");
    assert.match(result.text, /Arsenal/);
    assert.ok(!result.text.includes("Barcelona"));
  });
  it("a team mentioned in an earlier turn persists across a refinement that doesn't re-mention it", async () => {
    const rows = teamPool().map((c) => ({
      payload_json: JSON.stringify(candidatePayload({
        canonical_event_id: c.fixtureId, home: c.home, away: c.away, price: c.price, p_model: c.probability,
      })),
    }));
    const pool = { status: "COMPLETE", generated_at: "2026-10-14T06:30:00Z" };
    const deps = { getPool: async () => pool, getCandidates: async () => rows };
    const first = await respond(deps, "1 picks Arsenal", ZONE, DAY, NOW, null);
    assert.equal(first.result.reason, "success");
    const second = await respond(deps, "make it safer instead", ZONE, DAY, NOW, first.request);
    assert.equal(second.result.reason, "success");
    assert.match(second.result.text, /Arsenal/);
  });
});

// Regression: live screenshot, 2026-10-20. "1.5 options" got the generic
// HELP ("I didn't quite catch that"); "over 1.5 options" got "Nothing made
// the cut today" even though the real issue was the exact line not being
// offered. Root causes: (1) "options" wasn't in the request-word vocabulary
// at all, so a bare ambiguous line had no path but HELP; (2) the pick-count
// regex greedily claimed "2.5" in "under 2.5 games" BEFORE the over/under
// line regex ever ran, producing a bogus "I can offer 1-8 games" error;
// (3) failureReason() never distinguished "this market exists, but not at
// this exact line" from a generic confidence-floor miss.
describe("natural-language line/count disambiguation (2026-10-20 screenshot)", () => {
  it('"1.5 options" is recognized as an ordinary request, not gibberish, and asks Over or Under', async () => {
    const parsed = parseRequest("1.5 options");
    assert.equal(parsed.ok, true);
    assert.equal(parsed.request.ambiguousLine, 1.5);
    assert.equal(parsed.request.totalLine, null);
    const pool = { status: "COMPLETE", generated_at: "2026-10-14T06:30:00Z" };
    const deps = { getPool: async () => pool, getCandidates: async () => [] };
    const { result } = await respond(deps, "1.5 options", ZONE, DAY, NOW, null);
    assert.equal(result.reason, "ambiguous_line");
    assert.equal(result.text, "🤔 Over or under?\n\nDo you mean Over 1.5 or Under 1.5?");
  });
  it('a bare line is never silently defaulted to Over - a DIFFERENT prior line still asks again', async () => {
    const pool = { status: "COMPLETE", generated_at: "2026-10-14T06:30:00Z" };
    const deps = { getPool: async () => pool, getCandidates: async () => [] };
    const previous = parseRequest("under 2.5 games").request; // resolves 2.5, not 1.5
    const { result } = await respond(deps, "1.5 options", ZONE, DAY, NOW, previous);
    assert.equal(result.reason, "ambiguous_line");
  });
  it("previous conversation context resolving the SAME line skips re-asking", async () => {
    const rows = [{ payload_json: JSON.stringify(candidatePayload({ market: "total", selection: "over", line: 1.5 })) }];
    const pool = { status: "COMPLETE", generated_at: "2026-10-14T06:30:00Z" };
    const deps = { getPool: async () => pool, getCandidates: async () => rows };
    const previous = parseRequest("over 1.5 options").request; // already resolved: over 1.5
    const { result } = await respond(deps, "1.5 options", ZONE, DAY, NOW, previous);
    assert.notEqual(result.reason, "ambiguous_line");
  });
  it('"over 1.5 options" is interpreted as a real request for Over 1.5, not rejected as an unsupported market', () => {
    const parsed = parseRequest("over 1.5 options");
    assert.equal(parsed.ok, true);
    assert.deepEqual(parsed.request.totalLine, { selection: "over", line: 1.5 });
    assert.ok(parsed.request.allowedMarkets.has("total"));
  });
  it('"under 2.5 games" is a line, never a bogus "invalid pick count" error', () => {
    const parsed = parseRequest("under 2.5 games");
    assert.equal(parsed.ok, true);
    assert.deepEqual(parsed.request.totalLine, { selection: "under", line: 2.5 });
    assert.equal(parsed.request.pickCount, null);
  });
  it('"BTTS picks" and "UCL over 1.5" still parse exactly as before (not broken by the fix)', () => {
    const btts = parseRequest("BTTS picks").request;
    assert.ok(btts.allowedMarkets.has("btts"));
    const ucl = parseRequest("UCL over 1.5").request;
    assert.ok(ucl.competitions.has("ucl"));
    assert.deepEqual(ucl.totalLine, { selection: "over", line: 1.5 });
  });
  it('"GG" and "NG" (common BTTS slang) restrict the request to the btts market, never left unconstrained', () => {
    // Before this fix, "gg"/"ng" matched no known word at all: a request
    // like "GG picks" still passed the generic "is this a real request?"
    // check (via "picks") but with allowedMarkets left EMPTY, so the search
    // silently mixed in whatever market it liked (e.g. a 2.5 total) instead
    // of restricting to BTTS as the user actually asked.
    for (const text of ["GG", "gg picks", "give me some GG options", "3 GG picks"]) {
      const parsed = parseRequest(text);
      assert.equal(parsed.ok, true, text);
      assert.deepEqual([...parsed.request.allowedMarkets], ["btts"], text);
    }
    for (const text of ["NG", "ng picks"]) {
      const parsed = parseRequest(text);
      assert.equal(parsed.ok, true, text);
      assert.deepEqual([...parsed.request.allowedMarkets], ["btts"], text);
    }
    // Never a false positive on an unrelated word that merely CONTAINS "ng" —
    // \b word-boundary matching, not a bare substring search.
    const unrelated = parseRequest("going to watch the game tonight");
    assert.ok(!unrelated.ok || !unrelated.request.allowedMarkets.has("btts"));
  });
  it('an exact line nothing today offers is "line_unavailable", distinct from a generic confidence-floor miss', () => {
    const pool = [candidate({ market: "total", selection: "over", line: 2.5, entity: "all" })];
    const req = parseRequest("over 1.5 options").request;
    const matches = eligible(pool, req, NOW);
    assert.equal(matches.length, 0);
    assert.equal(failureReason(pool, req, NOW, matches), "line_unavailable");
  });
  it("a genuinely unsupported market is still reported as unavailable, not as a missing line", () => {
    const pool = [candidate({ market: "result2", selection: "home" })];
    const req = parseRequest("3 picks, double chance only").request;
    const matches = eligible(pool, req, NOW);
    assert.equal(failureReason(pool, req, NOW, matches), "market_unavailable");
  });
  it("3 or fewer legitimate integer pick counts are completely unaffected", () => {
    assert.equal(parseRequest("3 picks").request.pickCount, 3);
    assert.equal(parseRequest("2 predictions").request.pickCount, 2);
  });
});
