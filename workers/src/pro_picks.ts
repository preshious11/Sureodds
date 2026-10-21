/**
 * Pro Picks: a fully independent pipeline, not a mode of Custom Picks.
 *
 * It has no request-parsing, no conversational preference system
 * ("safe"/"strong"/"best"), no shortlist/beam-search ticket builder, and no
 * shared leg cap or probability floor — PRO_MIN_PROBABILITY/PRO_MAX_LEGS
 * below are this file's own constants. A change to Custom Picks'
 * MIN_PROBABILITY table or MAX_LEGS in custom_picks.ts has zero effect
 * here, and a change here has zero effect there. It is also not
 * conversational: there is nothing to refine, no session, no follow-up —
 * one call in, one ticket (or an honest "nothing qualifies yet") out.
 *
 * It DOES reuse `loadPool`/`Candidate`/`CustomPicksDeps` from
 * custom_picks.ts: that is the pure data-loading layer (D1 row parsing,
 * dead-fixture/stale-day rejection, duplicate-identity dedup) for the one
 * stored dataset (`custom_pick_candidates`) both pipelines read from — not
 * selection behavior. Re-deriving that parsing independently would only
 * create two places that could disagree about which stored rows are even
 * valid. Everything that decides WHICH picks qualify and HOW the ticket is
 * built is independent and lives only in this file.
 *
 * Selection isn't probability-only: `eligible()` also requires real
 * bookmaker coverage (PRO_MIN_BOOKS), and `byQuality()` ranks on
 * probability, then predbot's always-present, settlement-aware expected
 * value (`modelEv` — see its own doc comment), then cross-model agreement as
 * an uncertainty tiebreak — never price/odds direction, so nothing is ever
 * preferred for being cheap or expensive. See each constant/function below
 * for the full reasoning.
 */
import { SPORT_ICON, combinedOdds, escHtml } from "./format.ts";
import { loadPool, type Candidate, type CustomPicksDeps } from "./custom_picks.ts";

/** Independent of Custom Picks' MIN_PROBABILITY table (0.65/0.6/0.5) — this
 * number is never read by, or derived from, anything over there. */
export const PRO_MIN_PROBABILITY = 0.72;
/** Pro Picks is not about filling a slip — it's about how close to sure a
 * handful of picks are. A small cap, kept deliberately low so adding more
 * eligible fixtures on a busy day can never dilute the ticket with a
 * weaker 4th or 5th leg just to hit a bigger number. */
export const PRO_MAX_LEGS = 3;
/** A combined-odds ceiling, not a target to hit: the ticket is built from
 * the surest/best-evidenced picks first (see byQuality) and stops there, but a
 * pick is skipped (in favour of the next-surest one) if adding it would
 * push the running combined odds past this — so the ticket never balloons
 * into a long-shot parlay just because 3 slots are available. Applied
 * identically regardless of sport/market; the FIRST pick is always kept
 * even if its own price alone exceeds this, so a single very sure pick is
 * never rejected outright. */
const PRO_MAX_COMBINED_ODDS = 3.0;
/** Same floor-odds notion as Custom Picks' own MIN_PRICE, defined
 * independently so a change to that constant can never move this one. */
const PRO_MIN_PRICE = 1.25;
/** Minimum distinct-bookmaker coverage. Not an invented number: it reuses
 * predbot/config.py's own TOP_MIN_BOOKS (3) — Bet of the Day's strictest
 * existing book-coverage bar (predbot/selection/top.py's `need` check) —
 * so a single- or two-book quote, which is far more exposed to being
 * stale or simply wrong, can never qualify for Pro Picks regardless of
 * how high its stated probability is. This is an evidence-quality
 * requirement, not a probability/price policy change. */
const PRO_MIN_BOOKS = 3;

export interface ProTicket {
  picks: Candidate[];
  odds: number;
}

/** Real, still-future, priced-above-floor, probability-above-Pro's-own-floor,
 * covered by enough independent bookmakers to trust the quote — the only
 * conditions. No sport, league, market or outcome-type check anywhere: every
 * candidate competes on the same numbers regardless of what it is. */
function eligible(candidates: Candidate[], nowMs: number): Candidate[] {
  return candidates.filter(
    (c) =>
      c.startMs > nowMs &&
      c.price >= PRO_MIN_PRICE &&
      c.probability >= PRO_MIN_PROBABILITY &&
      c.books >= PRO_MIN_BOOKS,
  );
}

/**
 * Pro Picks' ranking, in priority order — never odds-direction-biased (price
 * appears nowhere in this comparator):
 *   1. probability, descending — still the primary "how close to sure is
 *      this" criterion established for Pro Picks; unchanged from before.
 *   2. modelEv, descending — predbot's ALWAYS-present settlement-aware EV
 *      (Scored.ev, ensemble.py:431: `payoff.ev(ml.price)`), not
 *      `referenceEv` (predbot's `edge_ev`): investigated and confirmed
 *      `edge_ev` requires a configured SHARP bookmaker (pinnacle/betfair
 *      exchange/matchbook) to quote the ENTIRE market, which real captured
 *      data shows essentially never happens for this deployment's provider
 *      mix (0 of 1779 real candidates on a sampled day, 0 of 213 in a
 *      separate local history sample) — so ranking on it was correctly
 *      wired but practically inert. `modelEv` has no such gating: it's
 *      computed for every scored candidate from the model's own calibrated
 *      probability against the real price, via the exact same
 *      settlement-aware Payoff machinery (push/void/half-win/half-loss
 *      handled correctly, never a naive binary formula). Nothing invented:
 *      it's predbot's own existing output, archived verbatim, just never
 *      read by the Worker before now.
 *   3. agreement, descending — cross-model estimate agreement (see
 *      pickReason's own doc comment below for what this number actually
 *      is). Used ONLY here, as an uncertainty tiebreak among
 *      near-identical probability/EV picks: a probability built from
 *      several agreeing internal estimates is less uncertain than an
 *      equally-stated probability from sources that disagree. Never
 *      surfaced to the owner as "confidence" — see pickReason.
 *   4. fixtureIdentity — final deterministic tiebreak only.
 * An unverified high probability is never preferred over a well-evidenced
 * lower one by virtue of criterion 1 alone winning outright on ties: ties
 * at the SAME probability are exactly where 2-4 take over.
 */
function byQuality(a: Candidate, b: Candidate): number {
  if (b.probability !== a.probability) return b.probability - a.probability;
  if (b.modelEv !== a.modelEv) return b.modelEv - a.modelEv;
  if (b.agreement !== a.agreement) return b.agreement - a.agreement;
  return a.fixtureIdentity < b.fixtureIdentity ? -1 : a.fixtureIdentity > b.fixtureIdentity ? 1 : 0;
}

/** One pick per fixture (never two correlated legs on the same game): up to
 * PRO_MAX_LEGS of the *surest, best-evidenced* eligible picks (see
 * byQuality), skipping over any pick that would push the combined odds past
 * PRO_MAX_COMBINED_ODDS (trying the next one down the ranking instead). On a
 * day with few eligible fixtures, or few that fit under the odds ceiling,
 * this returns fewer than PRO_MAX_LEGS — it never pads out a weaker or
 * longer-priced pick just to reach either cap. */
export function buildProTicket(pool: Candidate[], nowMs: number): ProTicket | null {
  const seenFixtures = new Set<string>();
  const picks: Candidate[] = [];
  let odds = 1;
  for (const c of eligible(pool, nowMs).sort(byQuality)) {
    if (picks.length >= PRO_MAX_LEGS) break;
    if (seenFixtures.has(c.fixtureIdentity)) continue;
    const nextOdds = odds * c.price;
    if (picks.length > 0 && nextOdds > PRO_MAX_COMBINED_ODDS) continue;
    seenFixtures.add(c.fixtureIdentity);
    picks.push(c);
    odds = nextOdds;
  }
  if (!picks.length) return null;
  return { picks, odds: combinedOdds(picks.map((p) => p.price)) };
}

/** "3W-2L" from a newest-first W/L list. */
function wlRecord(results: string[]): string {
  const wins = results.filter((r) => r === "W").length;
  return `${wins}W-${results.length - wins}L`;
}

/** Real stats/H2H when ESPN could identify this fixture (see
 * ESPNProvider.team_insight — ".homeForm"/".awayForm"/".h2h" on Candidate),
 * falling back to books/edge when it couldn't. Never fabricates a stats/H2H
 * line it doesn't actually have.
 *
 * Deliberately NEVER shows c.agreement as a reason: despite the name, it is
 * not a confidence/strength signal at all (predbot/model/ensemble.py) — it's
 * the spread between the model's own internal estimate SOURCES (market,
 * score model, Elo, sharp), defaulting to a flat 0.35 whenever fewer than 2
 * of them apply. Tennis has no scoreline/Elo model at all, so EVERY tennis
 * pick hits that floor regardless of how strong the pick actually is — and
 * any sport can be further discounted ×0.7 for merely incomplete book
 * coverage. Surfacing it as "confidence" produced a real, reproducible bug:
 * a 78%-probability pick showing "38% confidence" right next to it, because
 * the two numbers measure completely different things. */
function pickReason(c: Candidate): string {
  const parts: string[] = [];
  if (c.h2h.length) parts.push(`H2H (${c.home}): ${wlRecord(c.h2h)} last ${c.h2h.length}`);
  if (c.homeForm.length) parts.push(`${c.home} form: ${wlRecord(c.homeForm)}`);
  if (c.awayForm.length) parts.push(`${c.away} form: ${wlRecord(c.awayForm)}`);
  if (parts.length) return parts.join(" · ");
  const fallback: string[] = [];
  if (c.books >= 3) fallback.push(`${Math.round(c.books)} books lined up`);
  if (c.modelEv > 0.01) fallback.push(`+${(c.modelEv * 100).toFixed(1)}% edge`);
  if (fallback.length) return fallback.join(" · ");
  return "No match history on file for this fixture yet";
}

/** "HH:MM" in Africa/Lagos (UTC+1, no DST). */
function lagosTimeLabel(startMs: number): string {
  const local = new Date(startMs + 60 * 60_000);
  const hh = String(local.getUTCHours()).padStart(2, "0");
  const mm = String(local.getUTCMinutes()).padStart(2, "0");
  return `${hh}:${mm}`;
}

export function formatProTicket(ticket: ProTicket): string {
  const blocks = [`💎 PRO PICKS ${ticket.odds.toFixed(2)} ODDS`];
  const sorted = [...ticket.picks].sort((a, b) => a.startMs - b.startMs);
  for (const pick of sorted) {
    const icon = SPORT_ICON[pick.sport] ?? SPORT_ICON.other;
    const league = pick.competitionLabel ? `${escHtml(pick.competitionLabel)}\n` : "";
    blocks.push(
      `${icon} ${escHtml(pick.home)} vs ${escHtml(pick.away)}\n` +
        `${league}${escHtml(pick.outcome)}\n` +
        `Odds: ${pick.price.toFixed(2)} · Confidence: ${Math.round(pick.probability * 100)}% · 🕒 ${lagosTimeLabel(pick.startMs)}\n` +
        `📊 ${escHtml(pickReason(pick))}`,
    );
  }
  blocks.push("Bet only what you can afford to lose.");
  return blocks.join("\n\n");
}

export interface ProPicksResult {
  text: string;
  reason: "success" | "no_candidates" | "archive_unavailable";
  /** The actual ticket built, only on success — callers (index.ts,
   * cron.ts) use this to record the issued ticket in the ledger (see
   * ledger.ts) after a successful send; never set on failure, since
   * nothing was actually built or sent. */
  ticket?: ProTicket;
}

/** The one entry point: load today's stored candidates and deterministically
 * assemble a ticket, or report honestly why there isn't one yet. Never asks
 * a question, never starts a conversation, never touches a session. */
export async function buildProPicks(
  deps: CustomPicksDeps,
  day: string,
  zone: string,
  nowMs: number,
): Promise<ProPicksResult> {
  const loaded = await loadPool(deps, day, zone, nowMs);
  if (loaded === null) return { text: "", reason: "archive_unavailable" };
  const ticket = buildProTicket(loaded.candidates, nowMs);
  if (!ticket) return { text: "", reason: "no_candidates" };
  return { text: formatProTicket(ticket), reason: "success", ticket };
}
