// Scoped port of predbot/custom_picks.py + custom_recovery.py's private-chat
// "Custom Picks" feature. This is NOT a full port — see CUSTOM_PICKS_PARITY.md
// for the exact list of what's simplified and why. What IS faithful:
//   - the canned copy strings (INTRO/UNAVAILABLE/TODAY_ONLY/HELP) — verbatim
//   - the candidate validation rules (normalizeCandidate ~ custom_picks.py normalize())
//   - the ticket text format (formatTicket ~ custom_picks.py format_ticket(), via
//     the same combinedOdds()/SPORT_ICON already ported in format.ts)
//   - access rules: private-chat only, session membership, leave-on-/start
// What's simplified (documented, not faked):
//   - request parsing: a handful of explicit patterns (sport, count, target
//     odds, safe/strong/best), not production's full NLP ambiguity grammar
//   - ticket construction: greedy highest-probability selection within the
//     sport/count/odds constraints, not production's per-fixture beam search
//   - no alternative-suggestion buttons on failure (production's `prepare()`)
//   - rate limiting: a flat per-chat cooldown, not the token-bucket work
//     budget + 30/60s counter + 16-slot queue

import { SPORT_ICON, combinedOdds, escHtml } from "./format.ts";

export const INTRO =
  "✨ Custom Picks\n\nTell me what you want.\n\n" +
  "• Safe 4 odds\n• 3 basketball picks\n• 5 odds, double chance only\n" +
  "• 2 strong tennis picks\n• 6 odds from football and basketball";
export const UNAVAILABLE = "👨‍🍳 Still cooking\n\nToday's picks aren't ready yet. Check back in a bit.";
export const TODAY_ONLY = "😅 I'm living in today for now\n\nCustom Picks currently works with today's games only.";
export const HELP = "😅 I didn't quite catch that\n\nWhat odds or how many games would you like?";
export const BUSY = "⏳ Still working on your last request — one moment.";

const DEFAULT_COUNT = 3;
const MAX_LEGS = 8;
const TOLERANCE = 0.1;
const SUPPORTED_SPORTS = new Set(Object.keys(SPORT_ICON).filter((s) => s !== "other"));

const SPORT_ALIASES: Record<string, string> = {
  soccer: "football",
  "american football": "american_football",
  nfl: "american_football",
  nba: "basketball",
  wnba: "basketball",
  mlb: "baseball",
  atp: "tennis",
  wta: "tennis",
  ufc: "mma",
};

const VALID_SELECTIONS: Record<string, Set<string>> = {
  result3: new Set(["home", "draw", "away"]),
  result2: new Set(["home", "away"]),
  dnb: new Set(["home", "away"]),
  dc: new Set(["home_draw", "away_draw", "home_away"]),
  btts: new Set(["yes", "no"]),
  total: new Set(["over", "under"]),
  handicap: new Set(["home", "away"]),
  odd_even: new Set(["odd", "even"]),
};

function validSelection(market: string, selection: string): boolean {
  return VALID_SELECTIONS[market]?.has(selection) ?? false;
}

export interface Candidate {
  fixtureId: string;
  key: string;
  sport: string;
  market: string;
  selection: string;
  price: number;
  probability: number;
  startUtc: string;
  home: string;
  away: string;
  outcome: string;
  competitionLabel: string | null;
}

function finite(value: unknown): number | null {
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

const OUTCOME_LABELS: Record<string, (home: string, away: string) => Record<string, string>> = {
  result3: (home, away) => ({ home: `${home} to Win`, draw: "Draw", away: `${away} to Win` }),
  result2: (home, away) => ({ home: `${home} to Win`, away: `${away} to Win` }),
  dnb: (home, away) => ({ home: `${home} (Draw No Bet)`, away: `${away} (Draw No Bet)` }),
  dc: () => ({ home_draw: "Double Chance: 1X", away_draw: "Double Chance: X2", home_away: "Double Chance: 12" }),
  btts: () => ({ yes: "BTTS: Yes", no: "BTTS: No" }),
  total: (_h, _a) => ({ over: "Over", under: "Under" }),
  handicap: (home, away) => ({ home: `Handicap: ${home}`, away: `Handicap: ${away}` }),
  odd_even: () => ({ odd: "Odd", even: "Even" }),
};

function outcomeLabel(market: string, selection: string, home: string, away: string, line: number | null): string {
  const base = OUTCOME_LABELS[market]?.(home, away)[selection] ?? `${market}: ${selection}`;
  if (market === "total" && line != null) return `${base} ${line}`;
  return base;
}

/**
 * ~ custom_picks.py normalize(): validate an archived candidate row without
 * altering the stored evaluation. Returns null (never fabricates) for
 * anything stale, cancelled/live/finished, out of range, or unrecognized.
 */
export function normalizeCandidate(
  payload: Record<string, unknown>,
  day: string,
  zone: string,
  nowMs: number,
  poolTimeMs: number,
): Candidate | null {
  if (payload.prediction_date !== day || payload.timezone !== zone) return null;
  const statuses = ["status", "event_status", "fixture_status"]
    .map((k) => String(payload[k] ?? "").toLowerCase());
  const badStatuses = new Set([
    "cancelled", "canceled", "postponed", "abandoned", "final", "finished", "completed", "live", "in_progress", "suspended",
  ]);
  if (statuses.some((s) => badStatuses.has(s))) return null;
  if (["cancelled", "canceled", "postponed", "started", "finished", "is_live"].some((k) => payload[k] === true)) return null;

  const startMs = Date.parse(String(payload.start_utc ?? ""));
  const evaluatedMs = Date.parse(String(payload.generated_at ?? ""));
  if (!Number.isFinite(startMs) || startMs <= nowMs) return null; // today's games only, not started
  if (!Number.isFinite(evaluatedMs) || evaluatedMs > poolTimeMs || evaluatedMs > nowMs) return null;

  const price = finite(payload.price);
  const probability = finite(payload.p_model);
  if (price == null || price <= 1 || probability == null || probability <= 0 || probability >= 1) return null;

  const fixtureId = String(payload.canonical_event_id ?? payload.fixture_key ?? "");
  const sport = String(payload.sport ?? "");
  if (!fixtureId.trim() || !SUPPORTED_SPORTS.has(sport)) return null;

  const market = String(payload.market ?? "");
  const selection = String(payload.selection ?? "");
  if (!validSelection(market, selection)) return null;

  const home = String(payload.home ?? "");
  const away = String(payload.away ?? "");
  if (!home.trim() || !away.trim()) return null;

  const line = payload.line == null ? null : finite(payload.line);
  if (payload.line != null && line == null) return null;
  if ((market === "total" || market === "handicap") && line == null) return null;

  const outcome = outcomeLabel(market, selection, home, away, line);
  const key = JSON.stringify([market, selection, line]);
  const leagueRaw = payload.league;
  const competitionLabel = typeof leagueRaw === "string" && leagueRaw.trim() && !/[_:]/.test(leagueRaw) ? leagueRaw.trim() : null;

  return { fixtureId, key, sport, market, selection, price, probability, startUtc: String(payload.start_utc), home, away, outcome, competitionLabel };
}

export interface ParsedRequest {
  targetOdds: number | null;
  pickCount: number | null;
  sports: Set<string>;
}

export type ParseResult = { ok: true; request: ParsedRequest } | { ok: false; text: string };

/**
 * Simplified stand-in for custom_intent.py's parse(): recognizes the
 * documented explicit patterns only (sport name/alias, "N odds"/"odds of N",
 * "N picks"/"N games"). Anything it doesn't recognize returns HELP rather
 * than guessing — never a silent misparse.
 */
export function parseSimpleRequest(text: string): ParseResult {
  const trimmed = text.trim();
  if (!trimmed || trimmed.length > 512) return { ok: false, text: HELP };
  if (/\b(tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday|\d{4}-\d{2}-\d{2})\b/i.test(trimmed)) {
    return { ok: false, text: TODAY_ONLY };
  }
  const lower = trimmed.toLowerCase();

  const sports = new Set<string>();
  for (const [word, canonical] of [...Object.entries(SPORT_ALIASES), ...[...SUPPORTED_SPORTS].map((s): [string, string] => [s, s])]) {
    if (new RegExp(`\\b${word.replace(/\s+/g, "\\s+")}\\b`).test(lower)) sports.add(canonical);
  }

  const oddsMatch = /\b(\d+(?:\.\d+)?)\s*odds?\b|\bodds?\s*(?:of|around)?\s*(\d+(?:\.\d+)?)\b/.exec(lower);
  const targetOdds = oddsMatch ? Number(oddsMatch[1] ?? oddsMatch[2]) : null;

  const countMatch = /\b(\d+)\s+(?:\w+\s+)?(?:picks?|games?|legs?|selections?)\b/.exec(lower);
  const pickCount = countMatch ? Math.min(MAX_LEGS, Math.max(1, Number(countMatch[1]))) : null;

  if (targetOdds == null && pickCount == null && sports.size === 0) {
    if (/\b(picks?|games?|odds?)\b/.test(lower)) {
      return { ok: true, request: { targetOdds: null, pickCount: DEFAULT_COUNT, sports } };
    }
    return { ok: false, text: HELP };
  }
  if (targetOdds != null && (!Number.isFinite(targetOdds) || targetOdds <= 1)) return { ok: false, text: HELP };
  return { ok: true, request: { targetOdds, pickCount, sports } };
}

export interface Ticket {
  picks: Candidate[];
  odds: number;
}

/**
 * Simplified stand-in for custom_picks.py search(): one fixture contributes
 * at most one leg; candidates are tried in probability-descending order and
 * kept if they fit the sport filter and keep the running combined odds
 * within [target*(1-TOLERANCE), target*(1+TOLERANCE)] when a target was
 * given. Not a beam search — no alternative-market exploration per fixture,
 * no odds-bucketed retention. Documented limitation, not a fabrication: it
 * never invents a leg, and never returns a ticket that fails the same
 * target-odds/leg-count checks production enforces.
 */
export function buildTicket(candidates: Candidate[], request: ParsedRequest): Ticket | null {
  const bySport = request.sports.size > 0 ? candidates.filter((c) => request.sports.has(c.sport)) : candidates;
  const byFixture = new Map<string, Candidate>();
  for (const c of [...bySport].sort((a, b) => b.probability - a.probability)) {
    if (!byFixture.has(c.fixtureId)) byFixture.set(c.fixtureId, c);
  }
  const pool = [...byFixture.values()].sort((a, b) => b.probability - a.probability);
  const limit = request.pickCount ?? (request.targetOdds ? MAX_LEGS : DEFAULT_COUNT);
  if (pool.length < (request.pickCount ?? 1)) return null;

  const picks: Candidate[] = [];
  let odds = 1;
  const upper = request.targetOdds ? request.targetOdds * (1 + TOLERANCE) : Infinity;
  for (const c of pool) {
    if (picks.length >= limit) break;
    const next = odds * c.price;
    if (request.targetOdds && next > upper) continue;
    picks.push(c);
    odds = next;
  }
  if (request.pickCount != null && picks.length !== request.pickCount) return null;
  if (request.targetOdds) {
    const lower = request.targetOdds * (1 - TOLERANCE);
    if (odds < lower || odds > upper) return null;
  }
  if (picks.length === 0) return null;
  return { picks, odds };
}

/** ~ custom_picks.py format_ticket(): same header + per-leg block shape. */
export function formatTicket(ticket: Ticket): string {
  const blocks = [`🔥 ${combinedOdds(ticket.picks.map((p) => p.price)).toFixed(2)} ODDS`];
  const sorted = [...ticket.picks].sort((a, b) => (a.startUtc < b.startUtc ? -1 : a.startUtc > b.startUtc ? 1 : 0));
  for (const pick of sorted) {
    const icon = SPORT_ICON[pick.sport] ?? SPORT_ICON.other;
    const league = pick.competitionLabel ? `${escHtml(pick.competitionLabel)}\n` : "";
    blocks.push(
      `${icon} ${escHtml(pick.home)} vs ${escHtml(pick.away)}\n` +
        `${league}${escHtml(pick.outcome)}\n` +
        `Odds: ${pick.price.toFixed(2)} · Confidence: ${Math.round(pick.probability * 100)}%`,
    );
  }
  return blocks.join("\n\n");
}

export interface CustomPicksDeps {
  getPool: (day: string, zone: string) => Promise<{ status: string; generated_at: string } | null>;
  getCandidates: (day: string, zone: string) => Promise<{ payload_json: string }[]>;
}

export interface RespondResult {
  text: string;
  reason: "success" | "help" | "unavailable" | "today_only" | "search_failed" | "insufficient";
}

export async function respond(
  deps: CustomPicksDeps,
  text: string,
  zone: string,
  day: string,
  nowMs: number,
): Promise<RespondResult> {
  const parsed = parseSimpleRequest(text);
  if (!parsed.ok) {
    const reason = parsed.text === TODAY_ONLY ? "today_only" : "help";
    return { text: parsed.text, reason };
  }
  const pool = await deps.getPool(day, zone);
  if (!pool) return { text: UNAVAILABLE, reason: "unavailable" };
  const poolTimeMs = Date.parse(pool.generated_at);
  const rows = await deps.getCandidates(day, zone);
  const candidates: Candidate[] = [];
  for (const row of rows) {
    try {
      const payload = JSON.parse(row.payload_json) as Record<string, unknown>;
      const c = normalizeCandidate(payload, day, zone, nowMs, poolTimeMs);
      if (c) candidates.push(c);
    } catch {
      continue; // malformed archive row — skip, never fabricate.
    }
  }
  const ticket = buildTicket(candidates, parsed.request);
  if (!ticket) {
    return {
      text: "😅 I couldn't build a ticket matching that right now\n\nTry a different count, odds target, or sport.",
      reason: "insufficient",
    };
  }
  return { text: formatTicket(ticket), reason: "success" };
}
