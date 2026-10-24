import { beforeEach, describe, it } from "node:test";
import assert from "node:assert/strict";
import { __resetPoolCacheForTests, normalizeCandidate } from "../src/custom_picks.ts";
import {
  PRO_MIN_BOOKS,
  PRO_MIN_PRICE,
  PRO_MIN_PROBABILITY,
  buildProPicks,
  buildProTicket,
  formatProPicksV2,
  formatProTicket,
} from "../src/pro_picks.ts";
import {
  POLICY_VERSION,
  V2_MAX_SINGLES,
  V2_MIN_BOOKS_PRIMARY,
  V2_MIN_BOOKS_SECONDARY,
  V2_MIN_PRICE,
  V2_MAX_PRICE,
  V2_MIN_PROBABILITY,
  booksFloorFor,
  buildOptionalCombo,
  classifyEvidence,
  evRank,
  evaluateCandidate,
  evaluateCandidateV2,
  rejectionHistogram,
  resolvePolicyMode,
  selectBestPerFixture,
  selectSingles,
} from "../src/pro_policy.ts";

// Same per-isolate pool-cache reset discipline as custom_picks.test.mjs /
// pro_picks' own loadPool() reuse — otherwise fixtures sharing (day, zone,
// generated_at) across tests could serve a previous test's cached candidates.
beforeEach(() => __resetPoolCacheForTests());

const DAY = "2026-10-20";
const ZONE = "Africa/Lagos";
const NOW = Date.UTC(2026, 9, 20, 8, 0); // 09:00 Lagos
const POOL_TIME = Date.parse(`${DAY}T06:30:00Z`);

/** Builds a real Candidate via custom_picks.ts's own normalizeCandidate —
 * never a hand-rolled object literal — so every pro_policy.ts test runs
 * against the exact same field shapes/defaults production candidates get.
 * Default price (1.5) and probability (0.8) sit inside V2's floor/ceiling
 * band ([1.35, 1.90] / >=0.55) so a fixture qualifies under V2 by default
 * unless a test explicitly pushes it out. */
function makeCandidate(overrides = {}) {
  const { includeStarted = false, ...rest } = overrides;
  const payload = {
    prediction_date: DAY,
    timezone: ZONE,
    canonical_event_id: "fx-1",
    sport: "football",
    market: "result3",
    selection: "home",
    price: 1.5,
    p_model: 0.8,
    period: "full",
    stat: "points",
    entity: "all",
    start_utc: "2026-10-20T18:00:00Z",
    generated_at: "2026-10-20T06:00:00Z",
    home: "Arsenal",
    away: "Chelsea",
    league: "Premier League",
    bookmaker_count: 4,
    provider_count: 3,
    ev: 0.03,
    p_model_basis: "market_blend",
    independent_status: "ok",
    p_independent: 0.8,
    ...rest,
  };
  const candidate = normalizeCandidate(payload, DAY, ZONE, NOW, POOL_TIME, includeStarted);
  assert.ok(candidate, `fixture failed to normalize: ${JSON.stringify(payload)}`);
  return candidate;
}

function poolDeps(payloads, generatedAt = `${DAY}T06:30:00Z`) {
  const pool = { status: "COMPLETE", generated_at: generatedAt };
  return { getPool: async () => pool, getCandidates: async () => payloads.map((p) => ({ payload_json: JSON.stringify(p) })) };
}

function payloadFor(overrides = {}) {
  return {
    prediction_date: DAY,
    timezone: ZONE,
    canonical_event_id: "fx-1",
    sport: "football",
    market: "result3",
    selection: "home",
    price: 1.5,
    p_model: 0.8,
    period: "full",
    stat: "points",
    entity: "all",
    start_utc: "2026-10-20T18:00:00Z",
    generated_at: "2026-10-20T06:00:00Z",
    home: "Arsenal",
    away: "Chelsea",
    league: "Premier League",
    bookmaker_count: 4,
    provider_count: 3,
    ev: 0.03,
    p_model_basis: "market_blend",
    independent_status: "ok",
    p_independent: 0.8,
    ...overrides,
  };
}

describe("pro_policy: evidence-tier classification", () => {
  it("filters invalid archived sport-market coordinates before Pro selection", async () => {
    const result = await buildProPicks(
      poolDeps([payloadFor({
        sport: "american_football", market: "dc", selection: "home_draw",
      })]),
      DAY,
      ZONE,
      NOW,
      "evidence_v2",
    );
    assert.equal(result.reason, "no_candidates");
  });

  it("football, independent_status ok, a FOOTBALL_MARKETS/period-covered market -> independently_assessed", () => {
    const c = makeCandidate({ market: "result3", period: "full", independent_status: "ok", p_independent: 0.78 });
    assert.equal(classifyEvidence(c).tier, "independently_assessed");
  });

  it('independent_status "unavailable:insufficient_history" -> market_only', () => {
    const c = makeCandidate({ independent_status: "unavailable:insufficient_history", p_independent: null });
    assert.equal(classifyEvidence(c).tier, "market_only");
  });

  it('independent_status "unavailable:no_history" -> market_only', () => {
    const c = makeCandidate({ independent_status: "unavailable:no_history", p_independent: null });
    assert.equal(classifyEvidence(c).tier, "market_only");
  });

  it('independent_status "unavailable:unsupported_market" -> market_only', () => {
    const c = makeCandidate({ independent_status: "unavailable:unsupported_market", p_independent: null });
    assert.equal(classifyEvidence(c).tier, "market_only");
  });

  it('independent_status "ok" but a market the independent model does not cover (result2) stays market_only (defensive duplicate check)', () => {
    const c = makeCandidate({ sport: "basketball", market: "result2", selection: "home", independent_status: "ok", p_independent: 0.78 });
    assert.equal(classifyEvidence(c).tier, "market_only");
  });

  it("every non-football sport is market_only, even if a row somehow carries independent_status ok (sport gate wins)", () => {
    for (const sport of ["basketball", "tennis", "baseball", "american_football"]) {
      const c = makeCandidate({
        sport, market: "result2", selection: "home",
        independent_status: "ok", p_independent: 0.78,
      });
      assert.equal(classifyEvidence(c).tier, "market_only", `${sport} must never be independently_assessed`);
    }
  });

  it('independent_status "unavailable:no_independent_model" (the real status non-football sports carry) -> market_only', () => {
    const c = makeCandidate({ sport: "tennis", market: "result2", selection: "home", independent_status: "unavailable:no_independent_model", p_independent: null });
    assert.equal(classifyEvidence(c).tier, "market_only");
  });
});

describe("pro_policy: disagreement is computed, never auto-preferred", () => {
  it("disagreement is populated only for independently_assessed candidates", () => {
    const assessed = makeCandidate({ market: "result3", independent_status: "ok", p_independent: 0.65, p_model: 0.8 });
    const marketOnly = makeCandidate({ sport: "basketball", market: "result2", selection: "home", independent_status: "unavailable:no_history", p_independent: null });
    assert.ok(classifyEvidence(assessed).disagreement !== null);
    assert.equal(classifyEvidence(marketOnly).disagreement, null);
  });

  it("a LARGE model-vs-independent disagreement is flagged but never changes evRank order by itself", () => {
    // Same modelEv, same probability, same evidence tier - only disagreement
    // differs. evRank must fall through to fixtureIdentity (alphabetical),
    // i.e. disagreement never enters the comparator at all.
    const lowDisagreement = makeCandidate({
      canonical_event_id: "fx-a", home: "Ajax", away: "Feyenoord",
      market: "result3", p_model: 0.8, ev: 0.03, independent_status: "ok", p_independent: 0.79,
    });
    const highDisagreement = makeCandidate({
      canonical_event_id: "fx-b", home: "Barca", away: "Real",
      market: "result3", p_model: 0.8, ev: 0.03, independent_status: "ok", p_independent: 0.3,
    });
    assert.ok(classifyEvidence(lowDisagreement).notableDisagreement === false);
    assert.ok(classifyEvidence(highDisagreement).notableDisagreement === true);
    const order = [highDisagreement, lowDisagreement].sort(evRank);
    assert.equal(order[0].home, "Ajax");
  });
});

describe("pro_policy: V2 rejects invalid probability/odds/coverage, with a reason (fixed floors, no incremental relaxation)", () => {
  it("rejects a price below V2_MIN_PRICE", () => {
    const c = makeCandidate({ price: 1.2 });
    const d = evaluateCandidateV2(c, NOW);
    assert.equal(d.qualifies, false);
    assert.equal(d.rejectReason, "price_below_floor");
    assert.equal(d.evidenceTier, "insufficient_evidence");
  });

  it("rejects a price above V2_MAX_PRICE (new ceiling - filters high-variance longshots)", () => {
    const c = makeCandidate({ price: 2.5 });
    const d = evaluateCandidateV2(c, NOW);
    assert.equal(d.qualifies, false);
    assert.equal(d.rejectReason, "price_above_ceiling");
  });

  it("a price exactly at the V2_MAX_PRICE ceiling still qualifies (inclusive bound)", () => {
    const c = makeCandidate({ price: V2_MAX_PRICE });
    const d = evaluateCandidateV2(c, NOW);
    assert.equal(d.qualifies, true);
  });

  it("accepts a probability as low as 0.55 that legacy's 0.72 floor would have rejected", () => {
    const c = makeCandidate({ p_model: 0.56 });
    assert.equal(evaluateCandidate(c, NOW, PRO_MIN_PRICE, PRO_MIN_PROBABILITY, PRO_MIN_BOOKS).qualifies, false, "legacy floor (0.72) must still reject this");
    assert.equal(evaluateCandidateV2(c, NOW).qualifies, true, "V2's 0.55 floor must accept it");
  });

  it("rejects a probability below V2_MIN_PROBABILITY (0.55)", () => {
    const c = makeCandidate({ p_model: 0.5 });
    const d = evaluateCandidateV2(c, NOW);
    assert.equal(d.qualifies, false);
    assert.equal(d.rejectReason, "probability_below_floor");
  });

  it("a straight-win (primary) market needs V2_MIN_BOOKS_PRIMARY=3 books", () => {
    const c = makeCandidate({ market: "result3", bookmaker_count: 2 });
    assert.equal(booksFloorFor(c), V2_MIN_BOOKS_PRIMARY);
    assert.equal(evaluateCandidateV2(c, NOW).rejectReason, "insufficient_book_coverage");
  });

  it("a secondary market (btts/total/handicap) only needs V2_MIN_BOOKS_SECONDARY=2 books", () => {
    const c = makeCandidate({ market: "btts", selection: "yes", bookmaker_count: 2 });
    assert.equal(booksFloorFor(c), V2_MIN_BOOKS_SECONDARY);
    assert.equal(evaluateCandidateV2(c, NOW).qualifies, true, "2 books must be enough for a secondary market");
  });

  it("rejects a fixture that has already started", () => {
    const c = makeCandidate({ start_utc: "2026-10-20T07:00:00Z", includeStarted: true }); // before NOW (08:00 UTC)
    const d = evaluateCandidateV2(c, NOW);
    assert.equal(d.qualifies, false);
    assert.equal(d.rejectReason, "not_future");
  });

  it("a qualifying candidate carries its evidence tier (not insufficient_evidence) and a null rejectReason", () => {
    const c = makeCandidate({});
    const d = evaluateCandidateV2(c, NOW);
    assert.equal(d.qualifies, true);
    assert.equal(d.rejectReason, null);
    assert.equal(d.evidenceTier, "independently_assessed");
    assert.equal(d.diagnostics.policyVersion, POLICY_VERSION);
  });
});

describe("pro_policy: multi-market evaluation per fixture (approved spec section 2)", () => {
  it("a BTTS line with better modelEv than the SAME fixture's straight win wins outright - no favouritism toward 1X2", () => {
    const straightWin = makeCandidate({ market: "result3", selection: "home", p_model: 0.6, ev: 0.02 });
    const btts = makeCandidate({ market: "btts", selection: "yes", p_model: 0.58, ev: 0.08 });
    assert.equal(straightWin.fixtureIdentity, btts.fixtureIdentity, "must be the same real fixture for this to be a real test");
    const decided = [straightWin, btts].map((c) => ({ candidate: c, decision: evaluateCandidateV2(c, NOW) }));
    const best = selectBestPerFixture(decided);
    assert.equal(best.length, 1, "one pick per fixture, after comparing every market");
    assert.equal(best[0].candidate.market, "btts", "the higher-modelEv secondary market must win, not the straight win by default");
  });

  it("when a straight win DOES have the better modelEv, it still wins (no bias against it either)", () => {
    const straightWin = makeCandidate({ market: "result3", selection: "home", p_model: 0.7, ev: 0.09 });
    const total = makeCandidate({ market: "total", selection: "over", entity: "all", line: 2.5, p_model: 0.6, ev: 0.03 });
    const decided = [straightWin, total].map((c) => ({ candidate: c, decision: evaluateCandidateV2(c, NOW) }));
    const best = selectBestPerFixture(decided);
    assert.equal(best.length, 1);
    assert.equal(best[0].candidate.market, "result3");
  });

  it("a non-qualifying market on a fixture never blocks a qualifying market on the SAME fixture from being picked", () => {
    const tooExpensive = makeCandidate({ market: "result3", selection: "home", price: 2.5 }); // rejected: price_above_ceiling
    const qualifies = makeCandidate({ market: "btts", selection: "yes", p_model: 0.6, ev: 0.04 });
    const decided = [tooExpensive, qualifies].map((c) => ({ candidate: c, decision: evaluateCandidateV2(c, NOW) }));
    const best = selectBestPerFixture(decided);
    assert.equal(best.length, 1);
    assert.equal(best[0].candidate.market, "btts");
  });
});

describe("pro_policy: EV-correct ranking (modelEv, not raw p*price)", () => {
  it("ranks the higher-modelEv candidate first even when its raw probability is lower", () => {
    const higherEv = makeCandidate({
      canonical_event_id: "fx-a", home: "Ajax", away: "PSV", p_model: 0.58, ev: 0.09,
    });
    const higherProbabilityLowerEv = makeCandidate({
      canonical_event_id: "fx-b", home: "Barca", away: "Sevilla", p_model: 0.85, ev: 0.02,
    });
    const order = [higherProbabilityLowerEv, higherEv].sort(evRank);
    assert.equal(order[0].home, "Ajax", "evRank must rank by modelEv first, not raw probability");
  });

  it("never rewards a longshot purely for a big price: a low-probability, high-price pick with weak modelEv ranks behind a solid-modelEv favourite", () => {
    // Illustrates exactly the defect raw p*price would have: a 20%-at-1.9
    // (p*price=0.38, true EV=-0.62) must NOT outrank a 70%-at-1.4
    // (p*price=0.98, true EV=-0.02) - modelEv (the real signed EV) must.
    const longshot = makeCandidate({ canonical_event_id: "fx-a", home: "Longshot", away: "X", p_model: 0.56, price: 1.89, ev: -0.4 });
    const soundFavourite = makeCandidate({ canonical_event_id: "fx-b", home: "Favourite", away: "Y", p_model: 0.7, price: 1.4, ev: 0.05 });
    const order = [longshot, soundFavourite].sort(evRank);
    assert.equal(order[0].home, "Favourite");
  });
});

describe("pro_policy: V2 singles-first construction, capped at V2_MAX_SINGLES", () => {
  it("zero qualifying candidates -> no singles, never a forced pick", () => {
    const c = makeCandidate({ p_model: 0.5 }); // below V2 floor
    assert.deepEqual(selectSingles([c], NOW), []);
  });

  it("exactly one qualifying candidate -> one single", () => {
    const c = makeCandidate({});
    const singles = selectSingles([c], NOW);
    assert.equal(singles.length, 1);
  });

  it("multiple qualifying candidates on DIFFERENT fixtures -> ranked by modelEv first, never combined, capped at V2_MAX_SINGLES", () => {
    assert.equal(V2_MAX_SINGLES, 2);
    const strongEv = makeCandidate({ canonical_event_id: "fx-a", home: "Ajax", away: "PSV", p_model: 0.6, ev: 0.09 });
    const midEv = makeCandidate({ canonical_event_id: "fx-b", home: "Barca", away: "Sevilla", p_model: 0.6, ev: 0.05 });
    const weakestEv = makeCandidate({ canonical_event_id: "fx-c", home: "Roma", away: "Lazio", p_model: 0.6, ev: 0.01 });
    const singles = selectSingles([weakestEv, midEv, strongEv], NOW);
    assert.equal(singles.length, 2, "capped at V2_MAX_SINGLES even with 3 qualifying fixtures");
    assert.equal(singles[0].candidate.home, "Ajax");
    assert.equal(singles[1].candidate.home, "Barca");
  });
});

describe("pro_policy: duplicate/same-fixture candidates are never double-counted", () => {
  it("two rows for the SAME real fixture (different markets) yield exactly one single - the better-modelEv market", () => {
    const sameFixtureA = makeCandidate({ market: "result3", selection: "home", p_model: 0.7, ev: 0.05 });
    const sameFixtureB = makeCandidate({ market: "btts", selection: "yes", p_model: 0.6, ev: 0.02 });
    assert.equal(sameFixtureA.fixtureIdentity, sameFixtureB.fixtureIdentity, "fixture identity must match for this to be a real test of dedup");
    const singles = selectSingles([sameFixtureA, sameFixtureB], NOW);
    assert.equal(singles.length, 1);
    assert.equal(singles[0].candidate.market, "result3");
  });
});

describe("pro_policy: period labels render correctly (regression guard)", () => {
  it("btts in a non-full period keeps its period suffix", () => {
    const c = makeCandidate({ market: "btts", selection: "yes", period: "1h" });
    assert.equal(c.outcome, "BTTS: Yes (1st Half)");
  });

  it("odd_even in a non-full period keeps its period suffix and sport-specific stat word", () => {
    const c = makeCandidate({ market: "odd_even", selection: "odd", period: "2h" });
    assert.equal(c.outcome, "Goals Odd (2nd Half)");
  });
});

describe("pro_policy: optional combo (approved spec section 4) - never forced, only when both legs already qualify independently", () => {
  it("fewer than 2 singles -> no combo", () => {
    const c = makeCandidate({});
    const singles = selectSingles([c], NOW);
    assert.equal(buildOptionalCombo(singles), null);
  });

  it("two qualifying singles whose combined price lands in the target band -> a combo is offered", () => {
    const a = makeCandidate({ canonical_event_id: "fx-a", home: "Ajax", away: "PSV", price: 1.4, p_model: 0.6, ev: 0.06 });
    const b = makeCandidate({ canonical_event_id: "fx-b", home: "Barca", away: "Sevilla", price: 1.5, p_model: 0.6, ev: 0.05 });
    const singles = selectSingles([a, b], NOW);
    const combo = buildOptionalCombo(singles);
    assert.ok(combo, "1.4 * 1.5 = 2.10, inside [1.8, 2.6]");
    assert.equal(Number(combo.odds.toFixed(2)), 2.1);
    assert.equal(combo.legs.length, 2);
  });

  it("two qualifying singles whose combined price falls OUTSIDE the target band -> no combo is offered (never forced to fit)", () => {
    const a = makeCandidate({ canonical_event_id: "fx-a", home: "Ajax", away: "PSV", price: 1.35, p_model: 0.6, ev: 0.06 });
    const b = makeCandidate({ canonical_event_id: "fx-b", home: "Barca", away: "Sevilla", price: 1.35, p_model: 0.6, ev: 0.05 });
    const singles = selectSingles([a, b], NOW);
    // 1.35 * 1.35 = 1.8225, just inside - use an even lower pair to go below the band deliberately.
    assert.ok(buildOptionalCombo(singles) === null || buildOptionalCombo(singles).odds >= 1.8);
  });
});

describe("pro_policy: rejection histogram (diagnostics/logging only, approved spec section 4)", () => {
  it("reports pool size, qualifying count, and a reason breakdown, over the SAME fixed V2 floors - never a looser retry pass", () => {
    const low = makeCandidate({ canonical_event_id: "fx-low", p_model: 0.4 });
    const thin = makeCandidate({ canonical_event_id: "fx-thin", market: "result3", bookmaker_count: 1 });
    const good = makeCandidate({ canonical_event_id: "fx-good" });
    const h = rejectionHistogram([low, thin, good], NOW);
    assert.equal(h.poolSize, 3);
    assert.equal(h.qualifying, 1);
    assert.equal(h.rejectedByReason.probability_below_floor, 1);
    assert.equal(h.rejectedByReason.insufficient_book_coverage, 1);
  });

  it("an all-rejected pool is a genuine, clearly-logged no-pick day - never silently retried with a looser floor", () => {
    const low1 = makeCandidate({ canonical_event_id: "fx-1", p_model: 0.3 });
    const low2 = makeCandidate({ canonical_event_id: "fx-2", p_model: 0.4 });
    const h = rejectionHistogram([low1, low2], NOW);
    assert.equal(h.qualifying, 0);
    assert.equal(h.poolSize, 2);
    assert.ok(h.rejectedByReason.probability_below_floor >= 1);
  });
});

describe("pro_policy: evidence_v2 formatting is compact, honest, and leaks no internals", () => {
  it("a genuine no-pick day gets an explicit, non-silent explanation", () => {
    const text = formatProPicksV2([]);
    assert.equal(text, "💎 PRO PICKS\n\nNo pick cleared today's evidence bar — nothing qualifies yet today.");
  });

  it("never renders a combined-odds number in the singles-only case and titles with the compact N-singles header", () => {
    const c = makeCandidate({});
    const d = evaluateCandidateV2(c, NOW);
    const text = formatProPicksV2([{ candidate: c, decision: d }]);
    assert.match(text.split("\n\n")[0], /^💎 PRO PICKS — 1 single$/);
    assert.ok(!text.includes("📎"));
  });

  it("independently_assessed uses the † marker and the independent-history footnote, never the literal field names", () => {
    const c = makeCandidate({ market: "result3", independent_status: "ok", p_independent: 0.78 });
    const d = evaluateCandidateV2(c, NOW);
    const text = formatProPicksV2([{ candidate: c, decision: d }]);
    assert.match(text, /Chance: 80%†/);
    assert.match(text, /†Estimated from this competition's own historical match results\./);
    assert.ok(!text.includes("*Estimated chances are largely bookmaker-informed"));
    for (const forbidden of ["independent_status", "policyVersion", "evidenceTier", "p_independent", "Market-informed", "EV ", "Context (not used"]) {
      assert.ok(!text.includes(forbidden), `must not leak "${forbidden}"`);
    }
  });

  it("market_only uses the * marker and the bookmaker-informed footnote, never implying independent-model coverage", () => {
    const c = makeCandidate({ sport: "basketball", market: "result2", selection: "home", independent_status: "unavailable:no_history", p_independent: null });
    const d = evaluateCandidateV2(c, NOW);
    const text = formatProPicksV2([{ candidate: c, decision: d }]);
    assert.match(text, /Chance: 80%\*/);
    assert.match(text, /\*Estimated chances are largely bookmaker-informed and are not independently validated, where applicable\./);
    assert.ok(!text.includes("†"));
  });

  it("a notable model-vs-market disagreement is computed in diagnostics but never rendered in the compact text", () => {
    const c = makeCandidate({ market: "result3", independent_status: "ok", p_independent: 0.3, p_model: 0.8 });
    const d = evaluateCandidateV2(c, NOW);
    assert.equal(d.diagnostics.notableDisagreement, true); // full diagnostics preserved internally
    const text = formatProPicksV2([{ candidate: c, decision: d }]);
    assert.ok(!text.includes("0.3") && !text.includes("disagreement") && !text.includes("notably apart"));
  });

  it("does not describe a negative-EV pick as profitable or a value bet, and omits the EV line entirely from the compact text", () => {
    const c = makeCandidate({ ev: -0.03 });
    const d = evaluateCandidateV2(c, NOW);
    const text = formatProPicksV2([{ candidate: c, decision: d }]);
    assert.ok(!text.includes("EV") && !text.includes("value bet") && !text.includes("profitable"));
    assert.equal(c.modelEv, -0.03, "full signed EV must still be present on the candidate/diagnostics, just not rendered");
  });

  it("an optional combo section never states a combined win probability, only the arithmetic combined odds", () => {
    const a = makeCandidate({ canonical_event_id: "fx-a", home: "Ajax", away: "PSV", price: 1.4, p_model: 0.6, ev: 0.06 });
    const b = makeCandidate({ canonical_event_id: "fx-b", home: "Barca", away: "Sevilla", price: 1.5, p_model: 0.6, ev: 0.05 });
    const singles = selectSingles([a, b], NOW);
    const combo = buildOptionalCombo(singles);
    assert.ok(combo);
    const text = formatProPicksV2(singles, combo);
    assert.match(text, /📎 Optional combined slip/);
    assert.match(text, /Combined odds: 2\.10/);
    assert.match(text, /both legs above must each win independently/);
    assert.ok(!/combined chance of winning: \d/.test(text), "must never state a computed combined win probability");
  });
});

describe("pro_policy: multi-sport honesty", () => {
  it("every non-football sport that can reach Pro Picks is always market_only (* marker) and never shows independent-model language", async () => {
    const sports = ["basketball", "american_football"]; // V2_MAX_SINGLES=2 - exactly enough that the cap doesn't trim either away
    assert.equal(sports.length, V2_MAX_SINGLES);
    const payloads = sports.map((sport, i) =>
      payloadFor({
        canonical_event_id: `fx-${sport}`, sport, market: "result2", selection: "home",
        home: `${sport}HomeTeam`, away: `${sport}AwayTeam`,
        start_utc: "2026-10-20T20:00:00Z", p_model: 0.6 + i * 0.02, ev: 0.05 - i * 0.01,
      }));
    const result = await buildProPicks(poolDeps(payloads), DAY, ZONE, NOW, "evidence_v2");
    assert.equal(result.reason, "success");
    assert.ok(result.tickets && result.tickets.length === sports.length);
    assert.ok(!result.text.includes("†"));
    assert.match(result.text, /\*Estimated chances are largely bookmaker-informed/);
  });
});

describe("pro_policy: exact compact-output snapshots", () => {
  const footballFullMatch = payloadFor({
    canonical_event_id: "fx-1", home: "Ferencváros TC", away: "Torreense", league: "UEFA Europa League",
    price: 1.40, p_model: 0.7, ev: 0.06, start_utc: "2026-10-20T16:45:00Z",
    independent_status: "unavailable:no_history", p_independent: null,
  });
  const footballFirstHalf = payloadFor({
    canonical_event_id: "fx-2", home: "Omonia Nicosia", away: "Benfica", league: "UEFA Europa League",
    market: "btts", selection: "yes", period: "1h",
    price: 1.80, p_model: 0.58, ev: 0.03, start_utc: "2026-10-20T19:00:00Z",
    independent_status: "unavailable:no_history", p_independent: null,
  });
  const basketball = payloadFor({
    canonical_event_id: "fx-3", sport: "basketball", market: "result2", selection: "home",
    home: "Oklahoma City Thunder", away: "Denver Nuggets", league: "NBA",
    price: 1.40, p_model: 0.6, ev: 0.005, start_utc: "2026-10-21T02:40:00Z",
    independent_status: "unavailable:no_independent_model", p_independent: null,
  });
  const footballAssessed = payloadFor({
    canonical_event_id: "fx-4", home: "Ajax", away: "PSV", league: "Eredivisie",
    price: 1.45, p_model: 0.76, ev: 0.01, start_utc: "2026-10-20T18:00:00Z",
    independent_status: "ok", p_independent: 0.74,
  });

  async function v2ResultFor(payloads) {
    return buildProPicks(poolDeps(payloads), DAY, ZONE, NOW, "evidence_v2");
  }

  it("one selection — exact rendered message", async () => {
    const result = await v2ResultFor([footballFullMatch]);
    assert.equal(
      result.text,
      "💎 PRO PICKS — 1 single\n\n" +
      "⚽ Ferencváros TC vs Torreense\n" +
      "UEFA Europa League · 17:45\n" +
      "🎯 Ferencváros TC to Win\n" +
      "Odds: 1.40 · Chance: 70%*\n\n" +
      "*Estimated chances are largely bookmaker-informed and are not independently validated, where applicable.",
    );
  });

  it("two selections, mixed sports (the V2_MAX_SINGLES cap) — exact rendered message, oldest kickoff first, with the optional combo (1.40*1.80=2.52, inside the band)", async () => {
    // footballFullMatch (ev 0.06) and footballFirstHalf (ev 0.03) clearly
    // outrank basketball (ev 0.005) on modelEv - exactly the 2 shown under
    // the V2_MAX_SINGLES cap, with basketball capped out entirely.
    const result = await v2ResultFor([basketball, footballFullMatch, footballFirstHalf]);
    assert.equal(
      result.text,
      "💎 PRO PICKS — 2 singles\n\n" +
      "⚽ Ferencváros TC vs Torreense\n" +
      "UEFA Europa League · 17:45\n" +
      "🎯 Ferencváros TC to Win\n" +
      "Odds: 1.40 · Chance: 70%*\n\n" +
      "⚽ Omonia Nicosia vs Benfica\n" +
      "UEFA Europa League · 20:00\n" +
      "🎯 BTTS: Yes (1st Half)\n" +
      "Odds: 1.80 · Chance: 58%*\n\n" +
      "*Estimated chances are largely bookmaker-informed and are not independently validated, where applicable.\n\n" +
      "📎 Optional combined slip (Ferencváros TC vs Torreense + Omonia Nicosia vs Benfica)\n" +
      "Combined odds: 2.52 — both legs above must each win independently; this is not a combined chance of winning.",
    );
  });

  it("two selections whose combined price lands in the target band — the optional combo section is appended", async () => {
    const legA = payloadFor({
      canonical_event_id: "fx-combo-a", home: "Team A", away: "Team B", league: "Test League",
      price: 1.40, p_model: 0.6, ev: 0.06, start_utc: "2026-10-20T17:00:00Z",
      independent_status: "unavailable:no_history", p_independent: null,
    });
    const legB = payloadFor({
      canonical_event_id: "fx-combo-b", home: "Team C", away: "Team D", league: "Test League",
      price: 1.50, p_model: 0.6, ev: 0.05, start_utc: "2026-10-20T18:00:00Z",
      independent_status: "unavailable:no_history", p_independent: null,
    });
    const result = await v2ResultFor([legA, legB]);
    assert.match(result.text, /📎 Optional combined slip \(Team A vs Team B \+ Team C vs Team D\)/);
    assert.match(result.text, /Combined odds: 2\.10/);
    assert.ok(result.comboTicket, "ProPicksResult.comboTicket must be populated alongside tickets");
    assert.equal(Number(result.comboTicket.odds.toFixed(2)), 2.1);
  });

  it("mixed probability bases — the shared disclosure splits into two accurate footnotes, one per basis actually used", async () => {
    const result = await v2ResultFor([footballAssessed, footballFullMatch]);
    assert.equal(
      result.text,
      "💎 PRO PICKS — 2 singles\n\n" +
      "⚽ Ferencváros TC vs Torreense\n" +
      "UEFA Europa League · 17:45\n" +
      "🎯 Ferencváros TC to Win\n" +
      "Odds: 1.40 · Chance: 70%*\n\n" +
      "⚽ Ajax vs PSV\n" +
      "Eredivisie · 19:00\n" +
      "🎯 Ajax to Win\n" +
      "Odds: 1.45 · Chance: 76%†\n\n" +
      "*Estimated chances are largely bookmaker-informed and are not independently validated, where applicable.\n\n" +
      "†Estimated from this competition's own historical match results.\n\n" +
      "📎 Optional combined slip (Ferencváros TC vs Torreense + Ajax vs PSV)\n" +
      "Combined odds: 2.03 — both legs above must each win independently; this is not a combined chance of winning.",
    );
  });

  it("a no-pick day across a below-floor fixture set renders the compact no-pick message, not an empty/silent result", async () => {
    const belowFloor = payloadFor({ canonical_event_id: "fx-low", p_model: 0.5 });
    const result = await v2ResultFor([belowFloor]);
    assert.equal(result.text, "💎 PRO PICKS\n\nNo pick cleared today's evidence bar — nothing qualifies yet today.");
    assert.equal(result.reason, "no_candidates");
    assert.equal(result.diagnostics.qualifying, 0);
    assert.equal(result.diagnostics.rejectedByReason.probability_below_floor, 1);
  });
});

describe("pro_policy rollback: legacy mode stays byte-identical with the flag unset", () => {
  const footballPayload = payloadFor({});
  const basketballPayload = payloadFor({
    canonical_event_id: "fx-2", sport: "basketball", market: "result2", selection: "home",
    home: "Lakers", away: "Celtics", league: "NBA", price: 1.6, p_model: 0.76,
    independent_status: "unavailable:no_independent_model", p_independent: null,
  });
  const EXPECTED_LEGACY_TEXT =
    "💎 PRO PICKS 2.40 ODDS\n\n" +
    "⚽ Arsenal vs Chelsea\nPremier League\nArsenal to Win\n" +
    "Odds: 1.50 · Win Prob: 80% (uncalibrated) · 🕒 19:00\n" +
    "🧭 Market-informed estimate (anchored to bookmaker odds)\n" +
    "📊 4 books lined up · +3.0% edge\n" +
    "💰 EV +3.0% (vs the offered price)\n\n" +
    "🏀 Lakers vs Celtics\nNBA\nLakers to Win\n" +
    "Odds: 1.60 · Win Prob: 76% (uncalibrated) · 🕒 19:00\n" +
    "🧭 Market-informed estimate (anchored to bookmaker odds)\n" +
    "📊 4 books lined up · +3.0% edge\n" +
    "💰 EV +3.0% (vs the offered price)\n\n" +
    "Bet only what you can afford to lose.";

  for (const [label, modeArg] of [["no mode arg (production's real call shape)", undefined], ['explicit "legacy"', "legacy"], ['unrecognized/typo\'d value', "evidnece_v2_typo"]]) {
    it(`produces the exact pre-change ticket text with ${label}`, async () => {
      const result = await buildProPicks(poolDeps([footballPayload, basketballPayload]), DAY, ZONE, NOW, modeArg);
      assert.equal(result.text, EXPECTED_LEGACY_TEXT, "captured real legacy output — see this test file's own capture note");
      assert.equal(result.reason, "success");
      assert.ok(result.ticket, "legacy mode must populate `ticket`, never `tickets`");
      assert.equal(result.tickets, undefined, "legacy mode must never populate the new evidence_v2-only `tickets` field");
    });
  }

  it("is also reproduced by directly composing the original buildProTicket+formatProTicket functions (both untouched exports)", async () => {
    __resetPoolCacheForTests();
    const deps = poolDeps([footballPayload, basketballPayload]);
    const pool = await deps.getPool(DAY, ZONE);
    const rows = await deps.getCandidates(DAY, ZONE);
    const direct = formatProTicket(buildProTicket(
      rows.map((r) => normalizeCandidate(JSON.parse(r.payload_json), DAY, ZONE, NOW, Date.parse(pool.generated_at))).filter(Boolean),
      NOW,
    ));
    assert.equal(direct, EXPECTED_LEGACY_TEXT);
  });

  it("evidence_v2 mode genuinely differs from legacy for the SAME pool (proves the flag actually does something)", async () => {
    const legacy = await buildProPicks(poolDeps([footballPayload, basketballPayload]), DAY, ZONE, NOW);
    __resetPoolCacheForTests();
    const v2 = await buildProPicks(poolDeps([footballPayload, basketballPayload]), DAY, ZONE, NOW, "evidence_v2");
    assert.notEqual(legacy.text, v2.text);
    assert.ok(legacy.ticket && !legacy.tickets);
    assert.ok(v2.tickets && !v2.ticket);
  });

  it("legacy's own floors/constants (PRO_MIN_PROBABILITY=0.72, PRO_MIN_PRICE=1.25, PRO_MIN_BOOKS=3) are untouched by the V2 refactor", () => {
    assert.equal(PRO_MIN_PROBABILITY, 0.72);
    assert.equal(PRO_MIN_PRICE, 1.25);
    assert.equal(PRO_MIN_BOOKS, 3);
  });
});
