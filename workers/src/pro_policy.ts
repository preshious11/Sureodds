/**
 * Pro Picks' evidence-aware selection policy — an isolated, unit-testable
 * layer pro_picks.ts calls into. It never substitutes the published
 * probability: `c.probability` (predbot's p_model) is ALWAYS what is ranked
 * and shown, in every mode, for every candidate. What this module decides
 * is (1) whether a candidate qualifies at all (it can reject on
 * data-quality grounds even when probability alone clears
 * PRO_MIN_PROBABILITY), (2) an evidence-tier label to show the user in
 * plain language, and (3) a conservative ranking tie-break between
 * candidates whose probability/EV are already close.
 *
 * ROLLBACK: every export here is additive — pro_picks.ts only calls into
 * this module when PRO_POLICY_MODE=="evidence_v2" (see resolvePolicyMode
 * below; the hard default is "legacy"). With the flag unset or any other
 * value, pro_picks.ts's existing eligible()/byQuality()/buildProTicket()/
 * formatProTicket() run exactly as before this file existed — see
 * workers/tests/pro_policy.test.mjs's "legacy byte-identical" test, which
 * diffs real captured pre-change output against the post-change output
 * with the flag unset.
 *
 * FIELD-NAME NOTE (read this before touching the classification below):
 * the task that produced this file assumed independent_status's values
 * were bare reason strings like "insufficient_history"/"no_history"/
 * "unsupported_market". Reading predbot/model/ensemble.py's Scored
 * dataclass and predbot/model/independent.py directly (both archived
 * verbatim into payload_json by predbot/model/candidates.py's
 * capture_candidate — it copies every Scored field except
 * event/line/rank_score/quality/se/p_lcb/tier) shows the REAL values are:
 *   "ok"                                   - the independent model priced this exact line
 *   "unavailable:no_independent_model"     - this sport has no independent model (today: everything but football)
 *   "unavailable:unresolved_participant"   - event has no canonical participant ids
 *   "unavailable:no_history"               - no stored results for this competition at all
 *   "unavailable:unknown_team"             - one side has zero results in the store
 *   "unavailable:insufficient_history"     - a side has too few (recency-weighted) results
 *   "unavailable:unsupported_market"       - a fit exists but this market/period/stat combo can't be priced
 *   "unavailable:not_configured"           - the row predates this field (old archive row); Scored's own default
 * Classification below checks for the exact "ok" value and treats every
 * other string (the whole "unavailable:*" family, and anything
 * unrecognized) as "no independent coverage" — never pattern-matching a
 * guessed bare-word enum that doesn't actually exist in the data.
 */
import type { Candidate } from "./custom_picks.ts";

export const POLICY_VERSION = "evidence_v2";

export type PolicyMode = "legacy" | "evidence_v2";

/** env.PRO_POLICY_MODE -> the active mode. Hard default "legacy" for
 * anything unset/unrecognized — this is the rollback mechanism itself, not
 * just documentation of one: an unset or typo'd binding can never
 * accidentally activate the new policy. */
export function resolvePolicyMode(raw: string | undefined): PolicyMode {
  return raw === "evidence_v2" ? "evidence_v2" : "legacy";
}

/**
 * predbot/model/independent.py's FOOTBALL_MARKETS frozenset, duplicated
 * here because the Worker (TypeScript) cannot import a Python module.
 * KEEP IN SYNC BY HAND: if independent.py's FOOTBALL_MARKETS set or its
 * IndependentEstimate.price() period/stat gate ever changes, update both
 * constants below to match. Searching this repo for "FOOTBALL_MARKETS"
 * finds both sides.
 */
const FOOTBALL_INDEPENDENT_MARKETS = new Set(["result3", "dc", "dnb", "total", "btts", "handicap"]);
/** independent.py's IndependentEstimate.price(): only "full"/"reg" period,
 * stat=="points", not a player prop, and (for total) entity=="all" are
 * ever priced — half/quarter/set lines and team totals stay market-derived
 * even when the SAME fixture's full-match line is independently assessed. */
const FOOTBALL_INDEPENDENT_PERIODS = new Set(["full", "reg"]);

/**
 * V2 eligibility floors (multi-market refactor, approved spec). These are
 * DELIBERATELY SEPARATE from pro_picks.ts's PRO_MIN_PROBABILITY/
 * PRO_MIN_PRICE/PRO_MIN_BOOKS/PRO_MAX_LEGS constants, which legacy mode's
 * eligible()/byQuality()/buildProTicket() still read UNCHANGED — this is
 * the rollback guarantee (see pro_picks.ts's own header comment and
 * workers/tests/pro_policy.test.mjs's "legacy byte-identical" test): legacy
 * must keep behaving exactly as it did before evidence_v2 existed, so none
 * of these numbers are allowed to touch pro_picks.ts's own constants.
 */
export const V2_MIN_PROBABILITY = 0.55;
export const V2_MIN_PRICE = 1.35;
/** New ceiling (the original floor-only design had none): filters out
 * high-variance longshots that would otherwise still clear the probability
 * floor on a mispriced line. */
export const V2_MAX_PRICE = 1.90;
/** Straight-win markets (1X2/moneyline/double-chance/draw-no-bet) keep the
 * stricter book-coverage bar; goal-line/BTTS/handicap markets, which are
 * frequently quoted by fewer books even when genuinely liquid, get a lower
 * one so thin coverage alone doesn't reject an otherwise-viable secondary-
 * market play. */
export const V2_MIN_BOOKS_PRIMARY = 3;
export const V2_MIN_BOOKS_SECONDARY = 2;
/** "1 to 2 high-confidence single value plays" (approved spec, output
 * structure). Distinct from pro_picks.ts's PRO_MAX_LEGS (legacy's combo-leg
 * cap, still 3, untouched). */
export const V2_MAX_SINGLES = 2;
/** Goal-line/BTTS/handicap-family markets — the ones V2_MIN_BOOKS_SECONDARY
 * applies to, and the ones multi-market evaluation (selectBestPerFixture
 * below) actively competes against straight-win markets for the same
 * fixture. Spread across predbot's own market vocabulary (markets/
 * catalog.py): "total" is Over/Under goals, "btts" is Both Teams To Score,
 * "handicap" is Asian handicap/spread. Everything else (result3/result2/
 * dc/dnb/odd_even/etc.) is "primary" for this classification. */
export const SECONDARY_MARKETS = new Set(["total", "btts", "handicap"]);
/** Combined-odds band an OPTIONAL 2-leg combo (built only from singles that
 * already independently qualify on their own) must land in to be offered
 * at all — centred on the approved spec's "~2.00" target, wide enough that
 * two real, already-qualifying prices (1.35-1.90 each) can plausibly land
 * inside it without being artificially forced there. */
export const V2_COMBO_MIN_ODDS = 1.8;
export const V2_COMBO_MAX_ODDS = 2.6;

export type EvidenceTier = "independently_assessed" | "market_only" | "insufficient_evidence";

/** A candidate's real-world probability/EV are indistinguishable at this
 * resolution — used only to decide when the evidence-tier tie-break (not
 * probability/EV themselves) gets to break a near-tie. Deliberately NOT
 * tuned against the offline evaluation sample (see the task's own
 * instruction not to curve-fit this policy's thresholds) — picked as a
 * small, conservative value in the same spirit as the existing TOLERANCE/
 * epsilon constants already in this codebase. */
const PROBABILITY_TIE_EPSILON = 0.01;
const EV_TIE_EPSILON = 0.01;
/** abs(p_independent - p_model) at or above this is "notable disagreement"
 * — surfaced to the user as an uncertainty note, in plain language, NEVER
 * as a reason to prefer or reject the pick (see the module header comment
 * and classify()'s own doc comment). An arbitrary, conservative, round
 * number — not fit to any sample. */
const NOTABLE_DISAGREEMENT = 0.10;

export interface PolicyDiagnostics {
  policyVersion: string;
  evidenceTier: EvidenceTier;
  independentStatus: string;
  disagreement: number | null;
  notableDisagreement: boolean;
  providerCount: number;
  books: number;
  rejectReason: string | null;
}

export interface PolicyDecision {
  qualifies: boolean;
  evidenceTier: EvidenceTier;
  rejectReason: string | null;
  diagnostics: PolicyDiagnostics;
}

/**
 * Evidence-reliability classification (task step 1b), using ONLY fields
 * already archived on Candidate — nothing new is read from the payload
 * beyond what custom_picks.ts's normalizeCandidateWithReason already
 * parses (independentStatus/pIndependent were added there specifically to
 * support this module; see that file's Candidate interface doc comments).
 *
 * "independently_assessed" requires ALL of: sport=="football",
 * independentStatus=="ok" (the archived per-line status predbot itself
 * computed), AND the market/period actually being one the independent
 * model covers per FOOTBALL_INDEPENDENT_MARKETS/PERIODS above. That last
 * check is a defensive duplicate of what independent_status=="ok" should
 * already guarantee (ensemble.py only ever reports "ok" when
 * IndependentEstimate.price() returned a real number, which itself already
 * enforces the market/period/stat/entity gate) — kept so this module never
 * blindly trusts an archived status string it cannot independently verify
 * from the row's own coordinates, and so it behaves correctly even against
 * a hand-built test fixture that never went through the real pipeline.
 *
 * Every non-football sport, and every football candidate without a
 * fully-"ok" independent estimate for this exact market/period, is
 * "market_only" — never shown independent-model language (task step 4).
 */
export function classifyEvidence(c: Candidate): { tier: EvidenceTier; disagreement: number | null; notableDisagreement: boolean } {
  const independentlyAssessed =
    c.sport === "football" &&
    c.independentStatus === "ok" &&
    FOOTBALL_INDEPENDENT_MARKETS.has(c.market) &&
    FOOTBALL_INDEPENDENT_PERIODS.has(c.period) &&
    c.pIndependent != null;
  if (!independentlyAssessed) return { tier: "market_only", disagreement: null, notableDisagreement: false };
  // Computed but NEVER used to prefer/reject (task step 1c) - only surfaced
  // as a plain-language uncertainty note by the formatter.
  const disagreement = Math.abs((c.pIndependent as number) - c.probability);
  return { tier: "independently_assessed", disagreement, notableDisagreement: disagreement >= NOTABLE_DISAGREEMENT };
}

/**
 * Full eligibility decision for one candidate (task step 1, end to end).
 * `nowMs`/the price-odds-probability-books floors are the SAME values
 * pro_picks.ts's own eligible() already enforces (PRO_MIN_PRICE,
 * PRO_MIN_PROBABILITY, PRO_MIN_BOOKS) - passed in rather than re-imported
 * so this module stays independently testable without constructing a full
 * pro_picks.ts environment, while pro_picks.ts itself is the one place that
 * decides which concrete numbers apply (see its own doc comments for why
 * each constant is what it is).
 *
 * Rejection is always RECORDED (rejectReason), never a silent drop - a day
 * where every candidate is rejected is `evidenceTier: "insufficient_evidence"`
 * for every one of them, which is a valid, intended "no-pick" day, not an
 * error. There is no incremental/degrading retry here: callers always pass
 * the SAME fixed floors (V2_MIN_PROBABILITY/V2_MIN_PRICE/V2_MAX_PRICE/
 * V2_MIN_BOOKS_*) - a day where nothing clears them stays a genuine
 * no-pick day, by explicit design (approved spec, section 4: "Preserve
 * honest no-pick / single-pick days ... do not degrade the threshold
 * further").
 *
 * `maxPrice` is a ceiling (new in the multi-market refactor): filters out
 * high-variance longshots that would otherwise still clear the
 * probability floor. `minBooks` is the market-appropriate floor the
 * caller has already chosen (V2_MIN_BOOKS_PRIMARY for straight-win
 * markets, V2_MIN_BOOKS_SECONDARY for goal-line/BTTS/handicap - see
 * SECONDARY_MARKETS and selectBestPerFixture below for where that choice
 * is made).
 */
export function evaluateCandidate(
  c: Candidate,
  nowMs: number,
  minPrice: number,
  minProbability: number,
  minBooks: number,
  maxPrice: number = Infinity,
): PolicyDecision {
  const { tier: evidenceTierIfOk, disagreement, notableDisagreement } = classifyEvidence(c);
  let rejectReason: string | null = null;
  // (a) Market/settlement validity - the same checks pro_picks.ts's own
  // eligible() already makes (reused, not reinvented): real/future,
  // priced-above-floor, probability-above-floor, enough independent
  // bookmaker coverage. By the time a Candidate exists at all it has
  // already passed custom_picks.ts's normalizeCandidateWithReason (valid
  // odds, a supported market/selection, a describable outcome) and
  // loadPool's own disabled-market/disabled-sport blocklist — these are
  // defensive re-checks for this module's own standalone testability, not
  // a second gate the real pipeline doesn't already have.
  if (!(c.startMs > nowMs)) rejectReason = "not_future";
  else if (!Number.isFinite(c.price) || c.price < minPrice) rejectReason = "price_below_floor";
  else if (c.price > maxPrice) rejectReason = "price_above_ceiling";
  else if (!Number.isFinite(c.probability) || c.probability <= 0 || c.probability >= 1) rejectReason = "invalid_probability";
  else if (c.probability < minProbability) rejectReason = "probability_below_floor";
  else if (!Number.isFinite(c.books) || c.books < minBooks) rejectReason = "insufficient_book_coverage";
  else if (!c.fixtureIdentity || !c.home.trim() || !c.away.trim()) rejectReason = "missing_critical_data";

  const diagnostics: PolicyDiagnostics = {
    policyVersion: POLICY_VERSION,
    evidenceTier: rejectReason ? "insufficient_evidence" : evidenceTierIfOk,
    independentStatus: c.independentStatus,
    disagreement,
    notableDisagreement,
    providerCount: c.providerCount,
    books: c.books,
    rejectReason,
  };
  return { qualifies: rejectReason === null, evidenceTier: diagnostics.evidenceTier, rejectReason, diagnostics };
}

/** The market-appropriate books floor for one candidate (approved spec
 * section 1: "Adjust PRO_MIN_BOOKS from 3 to 2 specifically for secondary
 * goal/handicap markets"). */
export function booksFloorFor(c: Candidate): number {
  return SECONDARY_MARKETS.has(c.market) ? V2_MIN_BOOKS_SECONDARY : V2_MIN_BOOKS_PRIMARY;
}

/** V2's own eligibility call, with the fixed floors baked in (no caller can
 * accidentally pass legacy's numbers here, and no incremental-relaxation
 * loop exists to pass a second, looser set on retry). */
export function evaluateCandidateV2(c: Candidate, nowMs: number): PolicyDecision {
  return evaluateCandidate(c, nowMs, V2_MIN_PRICE, V2_MIN_PROBABILITY, booksFloorFor(c), V2_MAX_PRICE);
}

const TIER_RANK: Record<EvidenceTier, number> = { independently_assessed: 0, market_only: 1, insufficient_evidence: 2 };

/**
 * Ranking tie-break (task step 1d/4): probability first, then modelEv, both
 * descending exactly like pro_picks.ts's own byQuality() - but a difference
 * smaller than PROBABILITY_TIE_EPSILON/EV_TIE_EPSILON is treated as a tie,
 * at which point (ONLY then) evidenceTier breaks it - independently_assessed
 * before market_only - before falling through to the same agreement/
 * fixtureIdentity tiebreaks byQuality() already uses. This never lets a
 * worse-evidenced pick jump ahead of a meaningfully higher-probability or
 * higher-EV one; it only decides between picks that are ALREADY
 * indistinguishable on the numbers that matter most.
 */
export function evidenceRank(a: Candidate, b: Candidate): number {
  if (Math.abs(b.probability - a.probability) > PROBABILITY_TIE_EPSILON) return b.probability - a.probability;
  if (Math.abs(b.modelEv - a.modelEv) > EV_TIE_EPSILON) return b.modelEv - a.modelEv;
  const ta = TIER_RANK[classifyEvidence(a).tier];
  const tb = TIER_RANK[classifyEvidence(b).tier];
  if (ta !== tb) return ta - tb;
  if (b.agreement !== a.agreement) return b.agreement - a.agreement;
  return a.fixtureIdentity < b.fixtureIdentity ? -1 : a.fixtureIdentity > b.fixtureIdentity ? 1 : 0;
}

export interface PolicySingle {
  candidate: Candidate;
  decision: PolicyDecision;
}

/**
 * EV-correct ranking (approved spec section 3): `modelEv` IS `p*price - 1`
 * already (predbot's settlement-aware Payoff machinery - see pro_picks.ts's
 * byQuality doc comment for the full provenance), so sorting on it directly
 * is the real expected-value ordering, never the raw `p*price` the
 * original ask proposed (which equally ranks a 20%-chance-at-5.00 and a
 * 50%-chance-at-2.00 and - since bookmaker margin usually makes true EV
 * negative - tends to reward longshots instead of real edges). Probability
 * is the first tiebreak (not the primary key, unlike legacy's byQuality) so
 * two near-identical-EV picks still prefer the surer one; evidenceTier and
 * fixtureIdentity remain final tiebreaks exactly as evidenceRank's did.
 */
export function evRank(a: Candidate, b: Candidate): number {
  if (Math.abs(b.modelEv - a.modelEv) > EV_TIE_EPSILON) return b.modelEv - a.modelEv;
  if (Math.abs(b.probability - a.probability) > PROBABILITY_TIE_EPSILON) return b.probability - a.probability;
  const ta = TIER_RANK[classifyEvidence(a).tier];
  const tb = TIER_RANK[classifyEvidence(b).tier];
  if (ta !== tb) return ta - tb;
  return a.fixtureIdentity < b.fixtureIdentity ? -1 : a.fixtureIdentity > b.fixtureIdentity ? 1 : 0;
}

/**
 * Multi-market evaluation (approved spec section 2): for EVERY fixture,
 * every qualifying market (straight win, totals, BTTS, handicap, ...) is
 * evaluated side by side BEFORE anything is discarded. Only after picking
 * the single best-EV market per fixture does the one-pick-per-fixture
 * dedup rule apply - so a fixture's straight win is no longer favoured by
 * construction; a BTTS or Over 1.5/2.5 line on the same game can now win
 * outright if its modelEv is genuinely better. Ties within EV_TIE_EPSILON
 * fall through evRank's own probability/evidenceTier/fixtureIdentity
 * tiebreaks (so "best risk-adjusted profile" is decided by the same real
 * numbers everywhere else in this module, never a second ad hoc rule).
 */
export function selectBestPerFixture(decided: PolicySingle[]): PolicySingle[] {
  const qualifying = decided.filter((d) => d.decision.qualifies);
  const byFixture = new Map<string, PolicySingle>();
  for (const entry of qualifying) {
    const key = entry.candidate.fixtureIdentity;
    const current = byFixture.get(key);
    if (!current || evRank(entry.candidate, current.candidate) < 0) byFixture.set(key, entry);
  }
  return [...byFixture.values()].sort((a, b) => evRank(a.candidate, b.candidate));
}

/**
 * Singles-first construction (approved spec sections 2-4), rebuilt around
 * multi-market evaluation and EV-correct ranking:
 *   1. Evaluate every candidate against V2's fixed floors (evaluateCandidateV2
 *      - no incremental relaxation, ever: see that function's own doc
 *      comment).
 *   2. Reduce to one candidate per fixture, picking the best-EV qualifying
 *      market for that fixture (selectBestPerFixture).
 *   3. Rank the (now one-per-fixture) survivors by evRank and take the top
 *      V2_MAX_SINGLES as the day's single-bet recommendations.
 * Returns [] on a legitimate no-pick day - never padded, never forced, and
 * never retried with looser floors.
 */
export function selectSingles(pool: Candidate[], nowMs: number): PolicySingle[] {
  const decided = pool.map((candidate) => ({ candidate, decision: evaluateCandidateV2(candidate, nowMs) }));
  return selectBestPerFixture(decided).slice(0, V2_MAX_SINGLES);
}

export interface OptionalCombo {
  legs: PolicySingle[];
  odds: number;
}

/**
 * Approved spec section 4: "allow pairing the top 2 non-correlated
 * selections into a combined slip (~2.00 odds) if both independently pass
 * all qualification criteria." This NEVER forces a combo into existence -
 * it only ever looks at the top 2 already-independently-qualifying singles
 * (same ones `selectSingles` would show on their own) and offers their
 * product as an OPTIONAL extra, solely when that product lands inside
 * [V2_COMBO_MIN_ODDS, V2_COMBO_MAX_ODDS]. "Non-correlated" here means
 * different real fixtures - guaranteed already, since `ranked` is one
 * entry per fixtureIdentity. No joint win probability is computed or
 * implied anywhere - `odds` is pure arithmetic on two real market prices,
 * never framed as a combined chance of winning (see formatProPicksV2's own
 * rendering of this - each leg's own real probability is shown
 * separately, and the combo section states plainly that both legs must
 * each win on their own).
 */
export function buildOptionalCombo(ranked: PolicySingle[]): OptionalCombo | null {
  if (ranked.length < 2) return null;
  const [first, second] = ranked;
  const odds = first.candidate.price * second.candidate.price;
  if (odds < V2_COMBO_MIN_ODDS || odds > V2_COMBO_MAX_ODDS) return null;
  return { legs: [first, second], odds };
}

/**
 * Diagnostics-only rejection histogram (approved spec section 4: "Log the
 * rejection histogram clearly"), over the SAME fixed-floor evaluation
 * `selectSingles` uses - never rendered in the Telegram text, never fed
 * back into eligibility. `poolSize`/`qualifying` let a caller confirm
 * candidate-pool size before/after filtering without re-deriving it.
 */
export function rejectionHistogram(pool: Candidate[], nowMs: number): { poolSize: number; qualifying: number; rejectedByReason: Record<string, number> } {
  const decided = pool.map((candidate) => evaluateCandidateV2(candidate, nowMs));
  const rejectedByReason: Record<string, number> = {};
  let qualifying = 0;
  for (const d of decided) {
    if (d.qualifies) qualifying += 1;
    else rejectedByReason[d.rejectReason as string] = (rejectedByReason[d.rejectReason as string] ?? 0) + 1;
  }
  return { poolSize: pool.length, qualifying, rejectedByReason };
}
