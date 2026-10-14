// Deeper port of predbot/custom_picks.py + custom_recovery.py + custom_intent.py
// (production's "Custom Picks" private-chat feature). See
// CUSTOM_PICKS_PARITY section of PRODUCTION_PARITY.md for the exact list
// of what's a faithful algorithmic port vs. a documented simplification.
// Faithful ports: the beam search (search()), the eligibility/scoping
// filters (eligible/scoped), ticket re-validation (validTicket), the
// canned copy strings, candidate validation (normalizeCandidate), ticket
// text format, access rules, and conversation refinement (refine()).
// Simplified (never fabricated — every gap fails to HELP/UNAVAILABLE/no
// ticket, never a wrong answer): the league/competition catalog is a
// curated subset of production's ~100-entry BY_ID catalog, not the full
// thing; no fuzzy typo-correction pass; the alternatives ladder covers the
// three most common failure shapes, not production's full branching.

import { SPORT_ICON, combinedOdds, escHtml } from "./format.ts";

export const INTRO =
  "✨ Custom Picks\n\nTell me what you want.\n\n" +
  "• Safe 4 odds\n• 3 basketball picks\n• 5 odds, double chance only\n" +
  "• 2 strong tennis picks\n• 6 odds from football and basketball";
export const UNAVAILABLE = "👨‍🍳 Still cooking\n\nToday's picks aren't ready yet. Check back in a bit.";
export const TODAY_ONLY = "😅 I'm living in today for now\n\nCustom Picks currently works with today's games only.";
export const HELP = "😅 I didn't quite catch that\n\nWhat odds or how many games would you like?";
export const BUSY = "⏳ Still working on your last request — one moment.";
export const COPY_EXPIRED = "⏰ That option has expired\n\nTry these instead:";
export const COPY_EXPIRED_EMPTY = "⏰ That option has expired\n\nThere aren't enough upcoming picks left to rebuild it right now.";
export const CUSTOM_PICKS_TTL_MS = 10 * 60_000; // ~ custom_recovery.py TTL = timedelta(minutes=10)

const DEFAULT_COUNT = 3;
const MAX_LEGS = 8;
const TOLERANCE = 0.1;
const MAX_FIXTURES = 60;
const ALTERNATIVES = 4;
const BEAM_PER_COUNT = 48;
const ODDS_BUCKETS = 12;
const MAX_EXPANSIONS = 100_000;
const ODDS_LADDER = [20, 15, 12, 10, 8, 6, 5, 4, 3, 2];
const MAX_SUGGESTIONS = 3;
const MIN_PROBABILITY: Record<string, number> = { safe: 0.65, strong: 0.6, best: 0.5 };
const SPORT_LABEL: Record<string, string> = {
  football: "Football", basketball: "Basketball", american_football: "American Football",
  baseball: "Baseball", tennis: "Tennis", mma: "MMA",
};
const SUPPORTED_SPORTS = new Set(Object.keys(SPORT_ICON).filter((s) => s !== "other"));

const SPORT_ALIASES: Record<string, string> = {
  soccer: "football", "american football": "american_football", nfl: "american_football",
  nba: "basketball", wnba: "basketball", mlb: "baseball", atp: "tennis", wta: "tennis", ufc: "mma",
};
// Curated subset of predbot/markets/competitions.py BY_ID — the handful
// production hardcodes by name in custom_intent.py's LEAGUES.update(). The
// full ~100-competition catalog (with per-competition sport mapping) isn't
// ported; an unrecognized league name falls through to HELP, same fail-safe
// shape production uses for its own unrecognized-league case.
const LEAGUES: Record<string, { id: string; sport: string }> = {
  "premier league": { id: "epl", sport: "football" }, epl: { id: "epl", sport: "football" },
  "english premier league": { id: "epl", sport: "football" },
  "la liga": { id: "la_liga", sport: "football" }, laliga: { id: "la_liga", sport: "football" },
  "serie a": { id: "serie_a", sport: "football" },
  "ligue 1": { id: "ligue_1", sport: "football" }, "ligue one": { id: "ligue_1", sport: "football" },
  "champions league": { id: "ucl", sport: "football" },
  "europa league": { id: "uel", sport: "football" },
  "conference league": { id: "uecl", sport: "football" },
};

const MARKET_ALIASES: Record<string, string> = {
  result2: "result2", result3: "result3", dc: "dc", dnb: "dnb", btts: "btts", total: "total", handicap: "handicap",
  "double chance": "dc", "draw no bet": "dnb", moneyline: "result2", "money line": "result2", ml: "result2",
  totals: "total", total: "total", "over under": "total", "over/under": "total", "both teams to score": "btts",
  handicaps: "handicap", spread: "handicap", spreads: "handicap", "1x2": "result3", "match result": "result3",
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
  total: () => ({ over: "Over", under: "Under" }),
  handicap: (home, away) => ({ home: `Handicap: ${home}`, away: `Handicap: ${away}` }),
  odd_even: () => ({ odd: "Odd", even: "Even" }),
};

function outcomeLabel(market: string, selection: string, home: string, away: string, line: number | null): string {
  const base = OUTCOME_LABELS[market]?.(home, away)[selection] ?? `${market}: ${selection}`;
  return market === "total" && line != null ? `${base} ${line}` : base;
}

export interface Candidate {
  fixtureId: string;
  key: string;
  sport: string;
  market: string;
  selection: string;
  price: number;
  probability: number;
  startMs: number;
  home: string;
  away: string;
  outcome: string;
  competition: string | null;
  competitionLabel: string | null;
  agreement: number;
  liquidity: number;
  books: number;
  referenceEv: number;
}

/** ~ custom_picks.py normalize(): validate an archived candidate row without altering the stored evaluation. */
export function normalizeCandidate(
  payload: Record<string, unknown>,
  day: string,
  zone: string,
  nowMs: number,
  poolTimeMs: number,
  includeStarted = false,
): Candidate | null {
  if (payload.prediction_date !== day || payload.timezone !== zone) return null;
  const statuses = ["status", "event_status", "fixture_status"].map((k) => String(payload[k] ?? "").toLowerCase());
  const badStatuses = new Set(["cancelled", "canceled", "postponed", "abandoned", "final", "finished", "completed", "live", "in_progress", "suspended"]);
  if (statuses.some((s) => badStatuses.has(s))) return null;
  if (["cancelled", "canceled", "postponed", "started", "finished", "is_live"].some((k) => payload[k] === true)) return null;

  const startMs = Date.parse(String(payload.start_utc ?? ""));
  const evaluatedMs = Date.parse(String(payload.generated_at ?? ""));
  if (!Number.isFinite(startMs) || (startMs <= nowMs && !includeStarted)) return null;
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
  const competition =
    typeof payload.canonical_competition_id === "string"
      ? payload.canonical_competition_id
      : (competitionLabel && LEAGUES[competitionLabel.toLowerCase()]?.id) || null;

  return {
    fixtureId, key, sport, market, selection, price, probability, startMs, home, away, outcome,
    competition, competitionLabel,
    agreement: Math.max(0, Math.min(1, finite(payload.agreement) ?? 0)),
    liquidity: Math.max(0, Math.min(1, finite(payload.liquidity) ?? 0)),
    books: Math.max(0, Math.min(10, finite(payload.bookmaker_count) ?? 0)),
    referenceEv: Math.max(-0.1, Math.min(0.1, finite(payload.edge_ev) ?? -0.1)),
  };
}

export interface ParsedRequest {
  targetOdds: number | null;
  pickCount: number | null;
  sports: Set<string>;
  allowedMarkets: Set<string>;
  excludedMarkets: Set<string>;
  preference: "safe" | "strong" | "best";
  competitions: Set<string>;
  timeIntent: "today" | "tonight" | null;
  specifiedFields: Set<string>;
  refinement: boolean;
  qualityUpgrade: boolean;
}

/** JSON-safe form of a ParsedRequest (Sets -> arrays), for D1 persistence across stateless invocations. */
export function serializeRequest(req: ParsedRequest): Record<string, unknown> {
  return {
    targetOdds: req.targetOdds, pickCount: req.pickCount, sports: [...req.sports],
    allowedMarkets: [...req.allowedMarkets], excludedMarkets: [...req.excludedMarkets],
    preference: req.preference, competitions: [...req.competitions], timeIntent: req.timeIntent,
    specifiedFields: [...req.specifiedFields], refinement: req.refinement, qualityUpgrade: req.qualityUpgrade,
  };
}

export function deserializeRequest(raw: Record<string, unknown>): ParsedRequest {
  return {
    targetOdds: (raw.targetOdds as number) ?? null,
    pickCount: (raw.pickCount as number) ?? null,
    sports: new Set((raw.sports as string[]) ?? []),
    allowedMarkets: new Set((raw.allowedMarkets as string[]) ?? []),
    excludedMarkets: new Set((raw.excludedMarkets as string[]) ?? []),
    preference: (raw.preference as ParsedRequest["preference"]) ?? "best",
    competitions: new Set((raw.competitions as string[]) ?? []),
    timeIntent: (raw.timeIntent as ParsedRequest["timeIntent"]) ?? null,
    specifiedFields: new Set((raw.specifiedFields as string[]) ?? []),
    refinement: !!raw.refinement,
    qualityUpgrade: !!raw.qualityUpgrade,
  };
}

function freshRequest(): ParsedRequest {
  return {
    targetOdds: null, pickCount: null, sports: new Set(), allowedMarkets: new Set(), excludedMarkets: new Set(),
    preference: "best", competitions: new Set(), timeIntent: null, specifiedFields: new Set(),
    refinement: false, qualityUpgrade: false,
  };
}

export type ParseResult = { ok: true; request: ParsedRequest } | { ok: false; text: string; reason: string };
const err = (text: string, reason = "invalid_request"): ParseResult => ({ ok: false, text, reason });

/**
 * ~ custom_intent.py parse(): a fuller port than a pure keyword matcher —
 * detects leagues/competitions, sport aliases (with "no/without X" negation
 * rejection), "all sports", an odds target OR a pick count (conflicting
 * numbers rejected), market allow/exclude via negation-aware scanning,
 * preference qualifiers, and refinement/quality-upgrade intent. NOT ported:
 * the full ~100-competition catalog (see LEAGUES above), fuzzy
 * typo-correction, and the over/under line-market detection (total market
 * without an explicit line word is still supported via MARKET_ALIASES).
 */
export function parseRequest(rawText: string): ParseResult {
  const trimmed = rawText.trim();
  if (!trimmed || trimmed.length > 512) return err(HELP);
  const text = trimmed.toLowerCase();

  if (/\b(tomorrow|yesterday|next|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b|\b\d{4}-\d{2}-\d{2}\b/.test(text)) {
    return err(TODAY_ONLY, "future_date");
  }

  const req = freshRequest();
  const spans: [number, number][] = [];

  if (/\b(tonight|this evening)\b/.test(text)) { req.timeIntent = "tonight"; req.specifiedFields.add("time_intent"); }
  else if (/\btoday'?s?\b/.test(text)) { req.timeIntent = "today"; req.specifiedFields.add("time_intent"); }

  for (const [phrase, league] of Object.entries(LEAGUES)) {
    const idx = text.indexOf(phrase);
    if (idx >= 0) { req.competitions.add(league.id); spans.push([idx, idx + phrase.length]); }
  }
  if (req.competitions.size) req.specifiedFields.add("competitions");

  const sportWords = { ...SPORT_ALIASES, ...Object.fromEntries([...SUPPORTED_SPORTS].map((s) => [s, s])) };
  for (const [word, canonical] of Object.entries(sportWords)) {
    const re = new RegExp(`\\b${word.replace(/\s+/g, "\\s+")}\\b`, "g");
    let m: RegExpExecArray | null;
    while ((m = re.exec(text))) {
      if (/\b(no|without|avoid|exclude)\s*$/.test(text.slice(0, m.index))) {
        return err("Which sports would you like to include?");
      }
      req.sports.add(canonical);
    }
  }
  if (req.sports.size) req.specifiedFields.add("sports");
  if (/\ball sports\b/.test(text)) {
    if (req.sports.size) return err(HELP, "conflicting_constraints");
    req.specifiedFields.add("sports"); // sports stays empty: explicit "all"
  }
  if (req.competitions.size && req.sports.size) {
    const leagueSports = new Set([...req.competitions].map((id) => Object.values(LEAGUES).find((l) => l.id === id)?.sport));
    if (![...leagueSports].every((s) => req.sports.has(s as string))) {
      return err("Those leagues and sports don't match. Which should I use?", "conflicting_constraints");
    }
  }
  const unsupported = ["hockey", "rugby", "cricket", "volleyball", "handball"].filter((s) => !(s in sportWords));
  if (unsupported.some((s) => new RegExp(`\\b${s}\\b`).test(text))) {
    return err("I can't offer that sport right now. Which other sport would you like?", "unsupported_sport");
  }

  const num = "([+-]?\\d+(?:\\.\\d+)?)";
  const targets: number[] = [];
  const counts: number[] = [];
  const inLeagueSpan = (pos: number) => spans.some(([a, b]) => a <= pos && pos < b);
  for (const pattern of [
    new RegExp(`(?<![\\w.])${num}\\s*(?:(?:combined|total)\\s+)?odds\\b`, "g"),
    new RegExp(`\\bodds\\s*(?:(?:of|around|about|near|like)\\s+|[:=]\\s*)?${num}(?![\\w.])`, "g"),
  ]) {
    let m: RegExpExecArray | null;
    while ((m = pattern.exec(text))) {
      if (inLeagueSpan(m.index)) continue;
      targets.push(Number(m[1]));
      spans.push([m.index, m.index + m[0].length]);
    }
  }
  // ~ custom_intent.py's `qualifiers` group: sport-alias words AND quality
  // words may sit between the count and "picks" ("3 basketball picks").
  const qualifierWords = [...Object.keys(sportWords), "safe", "safest", "safer", "sure", "strong", "strongest", "stronger", "best", "and"]
    .sort((a, b) => b.length - a.length)
    .map((w) => w.replace(/\s+/g, "\\s+"));
  const countPattern = new RegExp(
    `(?<![\\w.])(\\d+(?:\\.\\d+)?)\\s+(?:(?:${qualifierWords.join("|")})\\s+)*(?:picks?|games?|matches?|selections?)\\b`,
    "g",
  );
  let cm: RegExpExecArray | null;
  while ((cm = countPattern.exec(text))) {
    if (inLeagueSpan(cm.index)) continue;
    counts.push(Number(cm[1]));
    spans.push([cm.index, cm.index + cm[0].length]);
  }
  if (new Set(targets).size > 1 || new Set(counts).size > 1) return err("Which odds target and number of picks should I use?", "conflicting_constraints");
  const target = targets[0] ?? null;
  const count = counts[0] ?? null;
  if (target != null) {
    if (!Number.isFinite(target) || target <= 1) return err("What combined odds would you like? Choose a number above 1.");
    req.targetOdds = target;
    req.specifiedFields.add("target_odds");
  }
  if (count != null) {
    if (!Number.isInteger(count) || count < 1 || count > MAX_LEGS) return err(`I can offer 1–${MAX_LEGS} different games. How many would you like?`);
    req.pickCount = count;
    req.specifiedFields.add("pick_count");
  }

  // Scan ALL market-alias phrases as one alternation so matches come back
  // in TEXT order (left to right), not MARKET_ALIASES's object-definition
  // order — iterating the dict directly would silently reorder matches
  // whenever a later-defined alias appears earlier in the message, which
  // also corrupts the negation-prefix calculation below.
  const marketPhrases = Object.keys(MARKET_ALIASES).sort((a, b) => b.length - a.length).map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&").replace(/\s+/g, "\\s+"));
  const marketPattern = new RegExp(`\\b(?:${marketPhrases.join("|")})\\b`, "g");
  const marketMatches: { index: number; phrase: string }[] = [];
  let mm: RegExpExecArray | null;
  while ((mm = marketPattern.exec(text))) marketMatches.push({ index: mm.index, phrase: mm[0] });

  let negative = false;
  let previousEnd = 0;
  for (const { index, phrase } of marketMatches) {
    const market = MARKET_ALIASES[phrase];
    if (!market) continue;
    const prefix = text.slice(previousEnd, index);
    negative =
      /\b(?:no|without|avoid|exclude)\s*(?:(?:any|more|all)\s+)?$/.test(prefix) ||
      (negative && /^[\s,]*(?:(?:and|or)\s+)?$/.test(prefix));
    (negative ? req.excludedMarkets : req.allowedMarkets).add(market);
    previousEnd = index + phrase.length;
  }
  if ([...req.allowedMarkets].some((m) => req.excludedMarkets.has(m))) return err(HELP, "conflicting_constraints");
  if (req.allowedMarkets.size) req.specifiedFields.add("allowed_markets");
  if (req.excludedMarkets.size) req.specifiedFields.add("excluded_markets");

  const qualityWords: Record<string, "safe" | "strong" | "best"> = {
    safe: "safe", safest: "safe", safer: "safe", sure: "safe",
    strong: "strong", strongest: "strong", stronger: "strong", best: "best",
  };
  let sawQuality = false;
  for (const [word, q] of Object.entries(qualityWords)) {
    if (new RegExp(`\\b${word}\\b`).test(text)) { req.preference = q; sawQuality = true; }
  }
  if (sawQuality) req.specifiedFields.add("preference");

  if (/\b(league|cup|liga)\b/.test(text) && req.competitions.size === 0) {
    return err("I don't recognize that league. Which competition would you like?", "unsupported_competition");
  }
  const general = /\b(picks?|games?|matches?|selections?)\b/.test(text);
  const hasSignal = req.specifiedFields.size > (req.specifiedFields.has("time_intent") ? 1 : 0);
  if (!hasSignal && !general) return err(HELP);
  if (target == null && count == null && !req.allowedMarkets.size && !req.excludedMarkets.size && /\b(scores?|results?|news|weather|payment)\b/.test(text)) {
    return err(HELP);
  }

  req.refinement =
    /\b(make|change|switch|instead|same|safer|stronger)\b/.test(text) ||
    (target == null && count == null && req.specifiedFields.size > 0 && !/\b(give|find|research|check|need|want)\b/.test(text));
  req.qualityUpgrade = /\b(safer|stronger)\b/.test(text);
  return { ok: true, request: req };
}

/** ~ custom_recovery.py scoped(): a candidate matches a request's hard constraints. */
export function scoped(c: Candidate, req: ParsedRequest): boolean {
  if (req.sports.size && !req.sports.has(c.sport)) return false;
  if (req.competitions.size && !(c.competition && req.competitions.has(c.competition))) return false;
  if (req.allowedMarkets.size && !req.allowedMarkets.has(c.market)) return false;
  if (req.excludedMarkets.has(c.market)) return false;
  return true;
}

/** ~ custom_recovery.py eligible(): scoped candidates that are future, quality-floor, and (if "tonight") evening. */
export function eligible(candidates: Candidate[], req: ParsedRequest, nowMs: number, tzOffsetMinutes = 60): Candidate[] {
  return candidates.filter((c) => {
    if (c.startMs <= nowMs) return false;
    if (c.probability < MIN_PROBABILITY[req.preference]) return false;
    if (!scoped(c, req)) return false;
    if (req.timeIntent === "tonight") {
      const localHour = new Date(c.startMs + tzOffsetMinutes * 60_000).getUTCHours();
      if (localHour < 18) return false;
    }
    return true;
  });
}

export interface Ticket {
  picks: Candidate[];
  odds: number;
}

function rankKey(c: Candidate): unknown[] {
  return [-c.probability, -c.agreement, -c.liquidity, -c.books, -c.referenceEv, c.price, c.startMs, c.fixtureId, c.key];
}
function cmp(a: unknown[], b: unknown[]): number {
  for (let i = 0; i < a.length; i++) {
    if ((a[i] as any) < (b[i] as any)) return -1;
    if ((a[i] as any) > (b[i] as any)) return 1;
  }
  return 0;
}

interface SearchState {
  picks: Candidate[];
  odds: number;
}
function stateRank(s: SearchState, req: ParsedRequest): unknown[] {
  const distance = req.targetOdds ? Math.abs(Math.log(s.odds / req.targetOdds)) : 0;
  const quality = s.picks.reduce((a, c) => a + c.probability, 0) / s.picks.length;
  const agreement = s.picks.reduce((a, c) => a + c.agreement, 0) / s.picks.length;
  const liquidity = s.picks.reduce((a, c) => a + c.liquidity, 0) / s.picks.length;
  const books = s.picks.reduce((a, c) => a + c.books, 0) / s.picks.length;
  const ev = s.picks.reduce((a, c) => a + c.referenceEv, 0) / s.picks.length;
  return [
    -Math.round(quality * 1e12) / 1e12,
    req.targetOdds ? s.picks.length : distance,
    req.targetOdds ? distance : s.picks.length,
    -agreement, -liquidity, -books, -ev,
  ];
}

/**
 * ~ custom_picks.py search(): bounded per-fixture beam. Faithful port of
 * production's algorithm — one leg per fixture, beam retention per leg
 * count (odds-bucketed when a target is given), capped expansions.
 */
export function search(candidates: Candidate[], req: ParsedRequest): Ticket | null {
  const grouped = new Map<string, Candidate[]>();
  for (const c of [...candidates].sort((a, b) => cmp(rankKey(a), rankKey(b)))) {
    const key = c.fixtureId;
    const list = grouped.get(key) ?? [];
    list.push(c);
    grouped.set(key, list);
  }
  const groups = [...grouped.values()].sort((a, b) => cmp(rankKey(a[0]), rankKey(b[0]))).slice(0, MAX_FIXTURES);
  const limit = req.pickCount ?? (req.targetOdds ? MAX_LEGS : DEFAULT_COUNT);
  if (groups.length < (req.pickCount ?? 1)) return null;

  const upper = req.targetOdds ? req.targetOdds * (1 + TOLERANCE) : Infinity;
  let beams = new Map<number, SearchState[]>([[0, [{ picks: [], odds: 1 }]]]);
  let expansions = 0;

  for (const group of groups) {
    const options: Candidate[] = [group[0]];
    const families = new Set([group[0].market]);
    for (const c of group.slice(1)) {
      if (!families.has(c.market) && options.length < ALTERNATIVES) {
        options.push(c);
        families.add(c.market);
      }
    }
    for (const c of group) {
      if (options.length >= ALTERNATIVES) break;
      if (!options.includes(c)) options.push(c);
    }
    const updated = new Map<number, SearchState[]>();
    for (const [count, states] of beams) updated.set(count, [...states]);
    outer: for (const [count, states] of beams) {
      if (count >= limit) continue;
      for (const state of states) {
        for (const c of options) {
          expansions += 1;
          if (expansions > MAX_EXPANSIONS) break outer;
          const odds = state.odds * c.price;
          if (Number.isFinite(odds) && odds <= upper) {
            const list = updated.get(count + 1) ?? [];
            list.push({ picks: [...state.picks, c], odds });
            updated.set(count + 1, list);
          }
        }
      }
    }
    const nextBeams = new Map<number, SearchState[]>([[0, updated.get(0) ?? []]]);
    for (const [count, states] of updated) {
      if (count === 0) continue;
      if (req.targetOdds) {
        const buckets = new Map<number, SearchState[]>();
        for (const s of states) {
          const bucket = Math.min(ODDS_BUCKETS - 1, Math.floor((Math.log(s.odds) / Math.log(upper)) * ODDS_BUCKETS));
          const list = buckets.get(bucket) ?? [];
          list.push(s);
          buckets.set(bucket, list);
        }
        const retained: SearchState[] = [];
        for (const bucket of [...buckets.keys()].sort((a, b) => a - b)) {
          retained.push(...buckets.get(bucket)!.sort((a, b) => cmp(stateRank(a, req), stateRank(b, req))).slice(0, Math.floor(BEAM_PER_COUNT / ODDS_BUCKETS)));
        }
        nextBeams.set(count, retained);
      } else {
        nextBeams.set(count, states.sort((a, b) => cmp(stateRank(a, req), stateRank(b, req))).slice(0, BEAM_PER_COUNT));
      }
    }
    beams = nextBeams;
    if (expansions > MAX_EXPANSIONS) break;
  }

  const counts = req.pickCount ? [req.pickCount] : req.targetOdds ? Array.from({ length: limit }, (_, i) => i + 1) : [Math.min(DEFAULT_COUNT, groups.length)];
  const feasible: SearchState[] = [];
  for (const count of counts) {
    for (const s of beams.get(count) ?? []) {
      if (!req.targetOdds || (req.targetOdds * (1 - TOLERANCE) <= s.odds && s.odds <= upper)) feasible.push(s);
    }
  }
  if (!feasible.length) return null;
  const best = feasible.reduce((a, b) => (cmp(stateRank(a, req), stateRank(b, req)) <= 0 ? a : b));
  return { picks: best.picks, odds: best.odds };
}

/** ~ custom_recovery.py valid_ticket(): independent re-verification before serving/storing. */
export function validTicket(ticket: Ticket | null, req: ParsedRequest, nowMs: number): boolean {
  if (!ticket || ticket.picks.length < 1 || ticket.picks.length > MAX_LEGS) return false;
  if (new Set(ticket.picks.map((c) => c.fixtureId)).size !== ticket.picks.length) return false;
  if (req.pickCount && ticket.picks.length !== req.pickCount) return false;
  if (eligible(ticket.picks, req, nowMs).length !== ticket.picks.length) return false;
  const product = ticket.picks.reduce((a, c) => a * c.price, 1);
  if (!Number.isFinite(product) || Math.abs(product - ticket.odds) > 1e-6 * Math.max(1, Math.abs(ticket.odds))) return false;
  if (req.targetOdds && !(req.targetOdds * (1 - TOLERANCE) <= product && product <= req.targetOdds * (1 + TOLERANCE))) return false;
  return true;
}

/** ~ custom_picks.py format_ticket(). */
export function formatTicket(ticket: Ticket): string {
  const blocks = [`🔥 ${combinedOdds(ticket.picks.map((p) => p.price)).toFixed(2)} ODDS`];
  const sorted = [...ticket.picks].sort((a, b) => a.startMs - b.startMs);
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

const COPY: Record<string, string> = {
  archive_unavailable: UNAVAILABLE,
  sport_unavailable: "👀 Nothing cooking today\n\nI don't have usable picks for that sport right now.",
  market_unavailable: "😅 That market is a little dry today\n\nI couldn't find enough matching picks that meet the mark.",
  insufficient_count: "😅 We're a few games short\n\nI couldn't find enough strong picks for that request.",
  target_unreachable: "😅 Couldn't stretch it that far\n\nI won't force weaker picks just to hit the number.",
  target_too_low: "👀 That's a tight squeeze\n\nI couldn't build a solid ticket close to that target from what's available.",
  conflicting_constraints: "😂 You boxed me in there\n\nNothing available matches all those filters.",
  all_started: "⏰ We missed the window\n\nThe matching games have already started.",
  below_quality_floor: "✂️ Nothing made the cut today\n\nI'd rather give you nothing than force weak picks.",
  search_failed: "😅 Almost, but I won't force it\n\nI couldn't build a solid ticket around that request from today's available picks.",
  expired: "⏰ That option has expired\n\nTry these instead:",
  expired_empty: "⏰ That option has expired\n\nThere aren't enough upcoming picks left to rebuild it right now.",
};

/** ~ custom_recovery.py failure_reason(): why no ticket could be built, for an honest (not generic) reply. */
export function failureReason(pool: Candidate[], req: ParsedRequest, nowMs: number, matches: Candidate[]): string {
  const structural = pool.filter((c) => scoped(c, req));
  const good = structural.filter((c) => c.probability >= MIN_PROBABILITY[req.preference]);
  if (good.length && !good.some((c) => c.startMs > nowMs)) return "all_started";
  const future = pool.filter((c) => c.startMs > nowMs);
  const sportsFuture = future.filter((c) => !req.sports.size || req.sports.has(c.sport));
  if (req.sports.size && !sportsFuture.length) return "sport_unavailable";
  if (req.allowedMarkets.size && !sportsFuture.some((c) => req.allowedMarkets.has(c.market))) return "market_unavailable";
  if (!matches.length) return "below_quality_floor";
  if (req.pickCount && new Set(matches.map((c) => c.fixtureId)).size < req.pickCount) return "insufficient_count";
  if (req.targetOdds) {
    const perFixtureMin = new Map<string, number>();
    for (const c of matches) perFixtureMin.set(c.fixtureId, Math.min(perFixtureMin.get(c.fixtureId) ?? Infinity, c.price));
    const minimum = [...perFixtureMin.values()].sort((a, b) => a - b).slice(0, req.pickCount ?? 1).reduce((a, b) => a * b, 1);
    if (minimum > req.targetOdds * (1 + TOLERANCE)) return "target_too_low";
    const perFixtureMax = new Map<string, number>();
    for (const c of matches) perFixtureMax.set(c.fixtureId, Math.max(perFixtureMax.get(c.fixtureId) ?? 0, c.price));
    const maximum = [...perFixtureMax.values()].sort((a, b) => b - a).slice(0, req.pickCount ?? MAX_LEGS).reduce((a, b) => a * b, 1);
    if (maximum < req.targetOdds * (1 - TOLERANCE)) return "target_unreachable";
  }
  return "search_failed";
}

/**
 * ~ custom_recovery.py alternatives() (reduced): the three most common
 * recovery shapes — relax the pick count, step down the odds-target ladder,
 * or drop every filter and use the default request. Production's fuller
 * alternatives generator (per-market relaxation, per-sport breakdown) is
 * not ported.
 */
function alternatives(req: ParsedRequest, reason: string): ParsedRequest[] {
  const out: ParsedRequest[] = [];
  if (req.targetOdds) {
    const ladder = reason === "target_too_low" ? [...ODDS_LADDER].reverse().filter((n) => n > req.targetOdds!) : ODDS_LADDER.filter((n) => n < req.targetOdds!);
    for (const target of ladder.slice(0, 2)) out.push({ ...req, targetOdds: target });
  }
  out.push({ ...freshRequest(), sports: new Set(req.sports) });
  out.push(freshRequest());
  return out.slice(0, MAX_SUGGESTIONS);
}

function labelFor(req: ParsedRequest): string {
  const icon = req.preference === "safe" ? "🛡️" : req.preference === "strong" ? "⭐" : "🔥";
  const prefix = req.preference[0].toUpperCase() + req.preference.slice(1);
  const sport = [...req.sports].map((s) => SPORT_LABEL[s] ?? s).join(" / ");
  if (req.targetOdds) return `${icon} ${prefix} ${req.targetOdds}g Odds${sport ? ` · ${sport}` : ""}`;
  if (sport) return `${icon} ${req.pickCount ?? DEFAULT_COUNT} ${sport} Picks`;
  return `${icon} ${req.pickCount ?? DEFAULT_COUNT} ${prefix} Picks · All Sports`;
}

export interface Suggestion {
  label: string;
  request: ParsedRequest;
  ticket: Ticket;
}

/** ~ custom_recovery.py prepare(): proves up to MAX_SUGGESTIONS real alternative tickets, never a promise it can't keep. */
export function prepareAlternatives(pool: Candidate[], req: ParsedRequest, reason: string, nowMs: number): Suggestion[] {
  const proven: Suggestion[] = [];
  const seenFixtureSets = new Set<string>();
  for (const option of alternatives(req, reason)) {
    if (proven.length >= MAX_SUGGESTIONS) break;
    const ticket = search(eligible(pool, option, nowMs), option);
    if (!validTicket(ticket, option, nowMs)) continue;
    const signature = [...ticket!.picks.map((c) => c.fixtureId)].sort().join(",");
    if (seenFixtureSets.has(signature)) continue;
    seenFixtureSets.add(signature);
    proven.push({ label: labelFor(option), request: option, ticket: ticket! });
  }
  return proven;
}

/** ~ custom_recovery.py refine(): merge a follow-up message into the stored prior request. */
export function refine(previous: ParsedRequest, current: ParsedRequest): ParsedRequest {
  const next: ParsedRequest = { ...previous };
  for (const field of current.specifiedFields) {
    switch (field) {
      case "target_odds": next.targetOdds = current.targetOdds; break;
      case "pick_count": next.pickCount = current.pickCount; break;
      case "sports": next.sports = current.sports; break;
      case "competitions": next.competitions = current.competitions; break;
      case "allowed_markets": next.allowedMarkets = current.allowedMarkets; break;
      case "excluded_markets": next.excludedMarkets = new Set([...current.excludedMarkets, ...previous.excludedMarkets]); break;
      case "preference": next.preference = current.preference; break;
      case "time_intent": next.timeIntent = current.timeIntent; break;
    }
  }
  if (current.specifiedFields.has("sports") && !current.specifiedFields.has("competitions")) {
    next.competitions = new Set(); // a changed sport invalidates a prior league filter (no catalog to re-check against)
  }
  if (current.qualityUpgrade) {
    next.preference = MIN_PROBABILITY[previous.preference] >= MIN_PROBABILITY[current.preference] ? previous.preference : current.preference;
  }
  next.specifiedFields = current.specifiedFields;
  next.refinement = true;
  return next;
}

export interface CustomPicksDeps {
  getPool: (day: string, zone: string) => Promise<{ status: string; generated_at: string } | null>;
  getCandidates: (day: string, zone: string) => Promise<{ payload_json: string }[]>;
}

export interface RespondResult {
  text: string;
  reason: string;
  suggestions: Suggestion[];
}

async function loadPool(deps: CustomPicksDeps, day: string, zone: string, nowMs: number): Promise<Candidate[] | null> {
  const pool = await deps.getPool(day, zone);
  if (!pool) return null;
  const poolTimeMs = Date.parse(pool.generated_at);
  const rows = await deps.getCandidates(day, zone);
  const candidates: Candidate[] = [];
  for (const row of rows) {
    try {
      const payload = JSON.parse(row.payload_json) as Record<string, unknown>;
      const c = normalizeCandidate(payload, day, zone, nowMs, poolTimeMs, true);
      if (c) candidates.push(c);
    } catch {
      continue; // malformed archive row — skip, never fabricate.
    }
  }
  return candidates;
}

export async function respond(
  deps: CustomPicksDeps,
  text: string,
  zone: string,
  day: string,
  nowMs: number,
  previous: ParsedRequest | null,
): Promise<{ result: RespondResult; request: ParsedRequest | null }> {
  const parsed = parseRequest(text);
  if (!parsed.ok) return { result: { text: parsed.text, reason: parsed.reason, suggestions: [] }, request: null };
  let request = parsed.request;
  if (request.refinement && previous) request = refine(previous, request);

  const pool = await loadPool(deps, day, zone, nowMs);
  if (pool === null) return { result: { text: UNAVAILABLE, reason: "archive_unavailable", suggestions: [] }, request };

  const matches = eligible(pool, request, nowMs);
  const ticket = matches.length ? search(matches, request) : null;
  if (validTicket(ticket, request, nowMs)) {
    return { result: { text: formatTicket(ticket!), reason: "success", suggestions: [] }, request };
  }
  const reason = failureReason(pool, request, nowMs, matches);
  const suggestions = prepareAlternatives(pool, request, reason, nowMs);
  const copy = COPY[reason] ?? COPY.search_failed;
  const text2 = copy + (suggestions.length ? "\n\nTry these instead:" : "");
  return { result: { text: text2, reason, suggestions }, request };
}
