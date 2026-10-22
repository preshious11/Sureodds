import { beforeEach, describe, it } from "node:test";
import assert from "node:assert/strict";
import { __resetPoolCacheForTests, normalizeCandidate } from "../src/custom_picks.ts";
import {
  PRO_MAX_LEGS,
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
  classifyEvidence,
  evaluateCandidate,
  evidenceRank,
  resolvePolicyMode,
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
 * against the exact same field shapes/defaults production candidates get. */
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
    const c = makeCandidate({ market: "result2", selection: "home", independent_status: "ok", p_independent: 0.78 });
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
    const marketOnly = makeCandidate({ market: "result2", selection: "home", independent_status: "unavailable:no_history", p_independent: null });
    assert.ok(classifyEvidence(assessed).disagreement !== null);
    assert.equal(classifyEvidence(marketOnly).disagreement, null);
  });

  it("a LARGE model-vs-independent disagreement is flagged but never changes the rank order by itself", () => {
    // Same probability, same modelEv, same agreement, same evidence tier -
    // only disagreement differs. evidenceRank must fall through to
    // fixtureIdentity (alphabetical), i.e. disagreement never enters the
    // comparator at all.
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
    // "Ajax" < "Barca" lexically via fixtureIdentity - that tiebreak (not
    // disagreement) decides order here.
    const order = [highDisagreement, lowDisagreement].sort(evidenceRank);
    assert.equal(order[0].home, "Ajax");
  });

  it("evidenceTier only breaks a tie once probability AND modelEv are themselves within epsilon", () => {
    // Names picked so alphabetical fixtureIdentity order (Alpha < Zeta)
    // DISAGREES with the expected probability-driven order - if this test
    // passed for the wrong reason (fixtureIdentity, not probability), it
    // would show "Alpha" first instead of "Zeta".
    const higherProbabilityMarketOnly = makeCandidate({
      canonical_event_id: "fx-a", home: "Zeta", away: "PSV",
      market: "result2", selection: "home", p_model: 0.9, ev: 0.03,
      independent_status: "unavailable:no_history", p_independent: null, // market_only
    });
    const lowerProbabilityButAssessed = makeCandidate({
      canonical_event_id: "fx-b", home: "Alpha", away: "Sevilla",
      market: "result3", p_model: 0.73, ev: 0.03,
      independent_status: "ok", p_independent: 0.7, // independently_assessed
    });
    // 0.9 vs 0.73 is far outside PROBABILITY_TIE_EPSILON - the higher raw
    // probability wins outright, regardless of evidence tier.
    const order = [lowerProbabilityButAssessed, higherProbabilityMarketOnly].sort(evidenceRank);
    assert.equal(order[0].home, "Zeta");
  });
});

describe("pro_policy: rejects invalid probability/odds/coverage, with a reason", () => {
  it("rejects a price below PRO_MIN_PRICE", () => {
    const c = makeCandidate({ price: 1.1 });
    const d = evaluateCandidate(c, NOW, PRO_MIN_PRICE, PRO_MIN_PROBABILITY, PRO_MIN_BOOKS);
    assert.equal(d.qualifies, false);
    assert.equal(d.rejectReason, "price_below_floor");
    assert.equal(d.evidenceTier, "insufficient_evidence");
  });

  it("rejects a probability below PRO_MIN_PROBABILITY", () => {
    const c = makeCandidate({ p_model: 0.5 });
    const d = evaluateCandidate(c, NOW, PRO_MIN_PRICE, PRO_MIN_PROBABILITY, PRO_MIN_BOOKS);
    assert.equal(d.qualifies, false);
    assert.equal(d.rejectReason, "probability_below_floor");
  });

  it("rejects insufficient bookmaker coverage even at a very high probability", () => {
    const c = makeCandidate({ p_model: 0.95, bookmaker_count: 2 });
    const d = evaluateCandidate(c, NOW, PRO_MIN_PRICE, PRO_MIN_PROBABILITY, PRO_MIN_BOOKS);
    assert.equal(d.qualifies, false);
    assert.equal(d.rejectReason, "insufficient_book_coverage");
  });

  it("rejects a fixture that has already started", () => {
    const c = makeCandidate({ start_utc: "2026-10-20T07:00:00Z", includeStarted: true }); // before NOW (08:00 UTC)
    const d = evaluateCandidate(c, NOW, PRO_MIN_PRICE, PRO_MIN_PROBABILITY, PRO_MIN_BOOKS);
    assert.equal(d.qualifies, false);
    assert.equal(d.rejectReason, "not_future");
  });

  it("a qualifying candidate carries its evidence tier (not insufficient_evidence) and a null rejectReason", () => {
    const c = makeCandidate({});
    const d = evaluateCandidate(c, NOW, PRO_MIN_PRICE, PRO_MIN_PROBABILITY, PRO_MIN_BOOKS);
    assert.equal(d.qualifies, true);
    assert.equal(d.rejectReason, null);
    assert.equal(d.evidenceTier, "independently_assessed");
    assert.equal(d.diagnostics.policyVersion, POLICY_VERSION);
  });
});

describe("pro_policy: duplicate/same-fixture candidates are never double-counted", () => {
  it("two rows for the SAME real fixture (different markets) yield exactly one single", () => {
    const sameFixtureA = makeCandidate({ market: "result3", selection: "home", p_model: 0.85, ev: 0.05 });
    const sameFixtureB = makeCandidate({ market: "btts", selection: "yes", p_model: 0.8, ev: 0.02 });
    assert.equal(sameFixtureA.fixtureIdentity, sameFixtureB.fixtureIdentity, "fixture identity must match for this to be a real test of dedup");
    const singles = selectSingles([sameFixtureA, sameFixtureB], NOW, PRO_MIN_PRICE, PRO_MIN_PROBABILITY, PRO_MIN_BOOKS, PRO_MAX_LEGS);
    assert.equal(singles.length, 1);
    // The higher-probability leg (result3 @ .85) outranks btts @ .80.
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

describe("pro_policy: singles-first construction (0 / 1 / multiple qualifying)", () => {
  it("zero qualifying candidates -> no singles, never a forced pick", () => {
    const c = makeCandidate({ p_model: 0.5 }); // below floor
    assert.deepEqual(selectSingles([c], NOW, PRO_MIN_PRICE, PRO_MIN_PROBABILITY, PRO_MIN_BOOKS, PRO_MAX_LEGS), []);
  });

  it("exactly one qualifying candidate -> one single", () => {
    const c = makeCandidate({});
    const singles = selectSingles([c], NOW, PRO_MIN_PRICE, PRO_MIN_PROBABILITY, PRO_MIN_BOOKS, PRO_MAX_LEGS);
    assert.equal(singles.length, 1);
  });

  it("multiple qualifying candidates on DIFFERENT fixtures -> multiple singles, ranked best probability first, never combined", () => {
    const strong = makeCandidate({ canonical_event_id: "fx-a", home: "Ajax", away: "PSV", p_model: 0.9, ev: 0.04 });
    const weaker = makeCandidate({ canonical_event_id: "fx-b", home: "Barca", away: "Sevilla", p_model: 0.75, ev: 0.01 });
    const singles = selectSingles([weaker, strong], NOW, PRO_MIN_PRICE, PRO_MIN_PROBABILITY, PRO_MIN_BOOKS, PRO_MAX_LEGS);
    assert.equal(singles.length, 2);
    assert.equal(singles[0].candidate.home, "Ajax"); // higher probability ranked first
  });

  it("never exceeds PRO_MAX_LEGS separate singles even with many qualifying fixtures", () => {
    const many = Array.from({ length: PRO_MAX_LEGS + 5 }, (_, i) =>
      makeCandidate({ canonical_event_id: `fx-${i}`, home: `Team${i}A`, away: `Team${i}B`, p_model: 0.73 + i * 0.001 }));
    const singles = selectSingles(many, NOW, PRO_MIN_PRICE, PRO_MIN_PROBABILITY, PRO_MIN_BOOKS, PRO_MAX_LEGS);
    assert.equal(singles.length, PRO_MAX_LEGS);
  });
});

describe("pro_policy: evidence_v2 formatting is compact, honest, and leaks no internals", () => {
  it("a genuine no-pick day gets an explicit, non-silent explanation", () => {
    const text = formatProPicksV2([]);
    assert.equal(text, "💎 PRO PICKS\n\nNo pick cleared today's evidence bar — nothing qualifies yet today.");
  });

  it("never renders a combined-odds number (singles, not a combo) and titles with the compact N-singles header", () => {
    const c = makeCandidate({});
    const d = evaluateCandidate(c, NOW, PRO_MIN_PRICE, PRO_MIN_PROBABILITY, PRO_MIN_BOOKS);
    const text = formatProPicksV2([{ candidate: c, decision: d }]);
    assert.match(text.split("\n\n")[0], /^💎 PRO PICKS — 1 single$/);
  });

  it("independently_assessed uses the † marker and the independent-history footnote, never the literal field names", () => {
    const c = makeCandidate({ market: "result3", independent_status: "ok", p_independent: 0.78 });
    const d = evaluateCandidate(c, NOW, PRO_MIN_PRICE, PRO_MIN_PROBABILITY, PRO_MIN_BOOKS);
    const text = formatProPicksV2([{ candidate: c, decision: d }]);
    assert.match(text, /Chance: 80%†/);
    assert.match(text, /†Estimated from this competition's own historical match results\./);
    assert.ok(!text.includes("*Estimated chances are largely bookmaker-informed"));
    for (const forbidden of ["independent_status", "policyVersion", "evidenceTier", "p_independent", "Market-informed", "EV ", "Context (not used"]) {
      assert.ok(!text.includes(forbidden), `must not leak "${forbidden}"`);
    }
  });

  it("market_only uses the * marker and the bookmaker-informed footnote, never implying independent-model coverage", () => {
    const c = makeCandidate({ market: "result2", selection: "home", independent_status: "unavailable:no_history", p_independent: null });
    const d = evaluateCandidate(c, NOW, PRO_MIN_PRICE, PRO_MIN_PROBABILITY, PRO_MIN_BOOKS);
    const text = formatProPicksV2([{ candidate: c, decision: d }]);
    assert.match(text, /Chance: 80%\*/);
    assert.match(text, /\*Estimated chances are largely bookmaker-informed and are not independently validated, where applicable\./);
    assert.ok(!text.includes("†"));
    for (const forbidden of ["independent_status", "policyVersion", "evidenceTier"]) {
      assert.ok(!text.includes(forbidden));
    }
  });

  it("a notable model-vs-market disagreement is computed in diagnostics but never rendered in the compact text", () => {
    const c = makeCandidate({ market: "result3", independent_status: "ok", p_independent: 0.3, p_model: 0.8 });
    const d = evaluateCandidate(c, NOW, PRO_MIN_PRICE, PRO_MIN_PROBABILITY, PRO_MIN_BOOKS);
    assert.equal(d.diagnostics.notableDisagreement, true); // full diagnostics preserved internally
    const text = formatProPicksV2([{ candidate: c, decision: d }]);
    assert.ok(!text.includes("0.3") && !text.includes("disagreement") && !text.includes("notably apart"));
  });

  it("does not describe a negative-EV pick as profitable or a value bet, and omits the EV line entirely from the compact text", () => {
    const c = makeCandidate({ ev: -0.03 });
    const d = evaluateCandidate(c, NOW, PRO_MIN_PRICE, PRO_MIN_PROBABILITY, PRO_MIN_BOOKS);
    const text = formatProPicksV2([{ candidate: c, decision: d }]);
    assert.ok(!text.includes("EV") && !text.includes("value bet") && !text.includes("profitable"));
    assert.equal(c.modelEv, -0.03, "full signed EV must still be present on the candidate/diagnostics, just not rendered");
  });
});

describe("pro_policy: multi-sport honesty", () => {
  it("every non-football sport that can reach Pro Picks is always market_only (* marker) and never shows independent-model language", async () => {
    // Exactly PRO_MAX_LEGS sports so the day's cap doesn't trim any of them
    // away - this test is about evidence-tier labeling, not the cap itself
    // (see the dedicated "never exceeds PRO_MAX_LEGS" test above for that).
    const sports = ["basketball", "american_football", "baseball"];
    assert.equal(sports.length, PRO_MAX_LEGS);
    const payloads = sports.map((sport, i) =>
      payloadFor({
        canonical_event_id: `fx-${sport}`, sport, market: "result2", selection: "home",
        home: `${sport}HomeTeam`, away: `${sport}AwayTeam`,
        start_utc: "2026-10-20T20:00:00Z", p_model: 0.8 + i * 0.001,
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
    price: 1.3, p_model: 0.75, start_utc: "2026-10-20T16:45:00Z",
    independent_status: "unavailable:no_history", p_independent: null,
  });
  const footballFirstHalf = payloadFor({
    canonical_event_id: "fx-2", home: "Omonia Nicosia", away: "Benfica", league: "UEFA Europa League",
    market: "btts", selection: "yes", period: "1h",
    price: 1.9, p_model: 0.73, start_utc: "2026-10-20T19:00:00Z",
    independent_status: "unavailable:no_history", p_independent: null,
  });
  const basketball = payloadFor({
    canonical_event_id: "fx-3", sport: "basketball", market: "result2", selection: "home",
    home: "Oklahoma City Thunder", away: "Denver Nuggets", league: "NBA",
    price: 1.31, p_model: 0.74, start_utc: "2026-10-21T02:40:00Z",
    independent_status: "unavailable:no_independent_model", p_independent: null,
  });
  const footballAssessed = payloadFor({
    canonical_event_id: "fx-4", home: "Ajax", away: "PSV", league: "Eredivisie",
    price: 1.45, p_model: 0.76, start_utc: "2026-10-20T18:00:00Z",
    independent_status: "ok", p_independent: 0.74,
  });

  async function v2SinglesFor(payloads) {
    const result = await buildProPicks(poolDeps(payloads), DAY, ZONE, NOW, "evidence_v2");
    return result.text;
  }

  it("one selection — exact rendered message", async () => {
    const text = await v2SinglesFor([footballFullMatch]);
    assert.equal(
      text,
      "💎 PRO PICKS — 1 single\n\n" +
      "⚽ Ferencváros TC vs Torreense\n" +
      "UEFA Europa League · 17:45\n" +
      "🎯 Ferencváros TC to Win\n" +
      "Odds: 1.30 · Chance: 75%*\n\n" +
      "*Estimated chances are largely bookmaker-informed and are not independently validated, where applicable.",
    );
  });

  it("two selections, mixed sports — exact rendered message, oldest kickoff first", async () => {
    const text = await v2SinglesFor([basketball, footballFullMatch]);
    assert.equal(
      text,
      "💎 PRO PICKS — 2 singles\n\n" +
      "⚽ Ferencváros TC vs Torreense\n" +
      "UEFA Europa League · 17:45\n" +
      "🎯 Ferencváros TC to Win\n" +
      "Odds: 1.30 · Chance: 75%*\n\n" +
      "🏀 Oklahoma City Thunder vs Denver Nuggets\n" +
      "NBA · 03:40\n" +
      "🎯 Oklahoma City Thunder to Win\n" +
      "Odds: 1.31 · Chance: 74%*\n\n" +
      "*Estimated chances are largely bookmaker-informed and are not independently validated, where applicable.",
    );
  });

  it("three selections including a first-half market — exact rendered message, period distinction preserved", async () => {
    const text = await v2SinglesFor([basketball, footballFullMatch, footballFirstHalf]);
    assert.equal(
      text,
      "💎 PRO PICKS — 3 singles\n\n" +
      "⚽ Ferencváros TC vs Torreense\n" +
      "UEFA Europa League · 17:45\n" +
      "🎯 Ferencváros TC to Win\n" +
      "Odds: 1.30 · Chance: 75%*\n\n" +
      "⚽ Omonia Nicosia vs Benfica\n" +
      "UEFA Europa League · 20:00\n" +
      "🎯 BTTS: Yes (1st Half)\n" +
      "Odds: 1.90 · Chance: 73%*\n\n" +
      "🏀 Oklahoma City Thunder vs Denver Nuggets\n" +
      "NBA · 03:40\n" +
      "🎯 Oklahoma City Thunder to Win\n" +
      "Odds: 1.31 · Chance: 74%*\n\n" +
      "*Estimated chances are largely bookmaker-informed and are not independently validated, where applicable.",
    );
  });

  it("mixed probability bases — the shared disclosure splits into two accurate footnotes, one per basis actually used", async () => {
    const text = await v2SinglesFor([footballAssessed, footballFullMatch]);
    assert.equal(
      text,
      "💎 PRO PICKS — 2 singles\n\n" +
      "⚽ Ferencváros TC vs Torreense\n" +
      "UEFA Europa League · 17:45\n" +
      "🎯 Ferencváros TC to Win\n" +
      "Odds: 1.30 · Chance: 75%*\n\n" +
      "⚽ Ajax vs PSV\n" +
      "Eredivisie · 19:00\n" +
      "🎯 Ajax to Win\n" +
      "Odds: 1.45 · Chance: 76%†\n\n" +
      "*Estimated chances are largely bookmaker-informed and are not independently validated, where applicable.\n\n" +
      "†Estimated from this competition's own historical match results.",
    );
  });

  it("a no-pick day across this same fixture set renders the compact no-pick message, not an empty/silent result", async () => {
    const belowFloor = payloadFor({ canonical_event_id: "fx-low", p_model: 0.5 });
    const text = await v2SinglesFor([belowFloor]);
    assert.equal(text, "💎 PRO PICKS\n\nNo pick cleared today's evidence bar — nothing qualifies yet today.");
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
    // Re-run through the real loadPool-equivalent path via buildProPicks's
    // own legacy branch is already covered above; this additionally proves
    // buildProTicket/formatProTicket (still exported, still used by the
    // legacy branch) themselves produce the identical text independently.
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
});
