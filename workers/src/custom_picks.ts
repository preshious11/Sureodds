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

// INTRO is assembled further down (introText(), after enabledSports() is
// defined) from the SAME enabled-sports rule parseRequest uses - the
// welcome message can never drift out of sync with what's actually enabled.
export const UNAVAILABLE = "👨‍🍳 Still cooking\n\nToday's picks aren't ready yet. Check back in a bit.";
export const TODAY_ONLY = "😅 I'm living in today for now\n\nCustom Picks currently works with today's games only.";
export const HELP = "😅 I didn't quite catch that\n\nWhat odds or how many games would you like?";
export const BUSY = "⏳ Still working on your last request — one moment.";
export const COPY_EXPIRED = "⏰ That option has expired\n\nTry these instead:";
export const COPY_EXPIRED_EMPTY = "⏰ That option has expired\n\nThere aren't enough upcoming picks left to rebuild it right now.";
export const CUSTOM_PICKS_TTL_MS = 10 * 60_000; // ~ custom_recovery.py TTL = timedelta(minutes=10)

const DEFAULT_COUNT = 3;
const MAX_LEGS = 8;
/** Pro Picks (admin-only preview, see db.ts isProSession): a higher leg cap
 * than the free tier. Threaded through as ParsedRequest.maxLegs rather than
 * read as a module constant, so a single process still serves both free and
 * pro sessions correctly at once (Workers are multi-tenant per isolate). */
export const MAX_LEGS_PRO = 12;
/** Strict minimum individual price for EVERY Custom Picks selection, at any
 * target odds, in any sport, including fallback and recovery tickets. Never
 * relaxed to complete a ticket. */
const MIN_PRICE = 1.25;
const LOW_TARGET_CEILING = 2.5;
const LOW_TARGET_MAX_LEGS = 3;
/** Shortlist size for ticket variety: the top-N distinct feasible tickets are
 * ranked, then rotation picks one per user. search() still returns rank 0. */
const SHORTLIST_LIMIT = 5;
const TOLERANCE = 0.1;
const MAX_FIXTURES = 60;
const ALTERNATIVES = 4;
const BEAM_PER_COUNT = 48;
const ODDS_BUCKETS = 12;
const MAX_EXPANSIONS = 100_000;
const ODDS_LADDER = [20, 15, 12, 10, 8, 6, 5, 4, 3, 2];
const MAX_SUGGESTIONS = 3;
// "pro" is never user-typed — respond() forces it for an admin Pro Picks
// session (see RespondOptions.preference) regardless of whatever preference
// word the text itself contains, so a Pro ticket is ALWAYS held to a
// stricter floor than both Custom Picks' own strictest tier ("safe", 0.65)
// and predbot's Bet of the Day floor (TOP_MIN_PROB, 0.62 - config.py).
const MIN_PROBABILITY: Record<string, number> = { safe: 0.65, strong: 0.6, best: 0.5, pro: 0.72 };
export const SPORT_LABEL: Record<string, string> = {
  football: "Football", basketball: "Basketball", american_football: "American Football",
  baseball: "Baseball", tennis: "Tennis", mma: "MMA",
};
// Every sport the parser recognizes names/aliases for - this NEVER narrows,
// so "mma"/"ufc" stay fully parseable even while disabled in production
// (see ENABLED_SPORTS below). Deterministic order (SPORT_ICON's own
// insertion order): football, basketball, american_football, baseball,
// tennis, mma.
const SUPPORTED_SPORTS = new Set(Object.keys(SPORT_ICON).filter((s) => s !== "other"));

/** SUPPORTED_SPORTS actually enabled in a deployment: the real generation
 * config (predbot's ALLOWED_SPORTS, mirrored here - see DEFAULT_ENABLED_SPORTS)
 * intersected with what the parser knows by name. A sport can be recognized
 * (SUPPORTED_SPORTS) without being enabled, which is exactly MMA's current
 * state: parseable, not yet advertised or servable (2026-10 production
 * audit: ALLOWED_SPORTS doesn't include it). */
function enabledSports(allowed: Iterable<string>): Set<string> {
  const allowedSet = new Set(allowed);
  return new Set([...SUPPORTED_SPORTS].filter((s) => allowedSet.has(s)));
}

function enabledSportsLabel(enabled: Set<string>): string {
  return [...SUPPORTED_SPORTS].filter((s) => enabled.has(s)).map((s) => SPORT_LABEL[s] ?? s).join(", ");
}

/** Mirrors predbot/config.py's own ALLOWED_SPORTS default - the one place
 * this file hardcodes an enabled-sports list, matching production's real
 * configured value rather than inventing a separate one. The Worker has no
 * shared runtime config store with the Python generation pipeline, so this
 * default is kept in lockstep by hand (same pattern already used for
 * TIMEZONE/POST_TIME/etc. between wrangler.toml and predbot/config.py).
 * Real callers (index.ts) pass env.ALLOWED_SPORTS instead when configured. */
const DEFAULT_ENABLED_SPORTS = enabledSports(["football", "basketball", "american_football", "baseball", "tennis"]);

/** env.ALLOWED_SPORTS ("football,basketball,...") -> the enabled-sports set,
 * same CSV shape as predbot's own ALLOWED_SPORTS env var. Falls back to
 * DEFAULT_ENABLED_SPORTS when unset, never to "everything recognized". */
export function parseEnabledSports(csv: string | undefined): Set<string> {
  if (!csv || !csv.trim()) return DEFAULT_ENABLED_SPORTS;
  return enabledSports(csv.split(",").map((s) => s.trim().toLowerCase()).filter(Boolean));
}

/** The welcome message, built fresh from whatever's actually enabled -
 * never a separately hardcoded sport list, so it can never advertise a
 * sport the real pipeline isn't producing. */
export function introText(enabled: Set<string> = DEFAULT_ENABLED_SPORTS): string {
  return (
    "✨ Custom Picks\n\nTell me what you want.\n\n" +
    `Supported sports: ${enabledSportsLabel(enabled)}\n\n` +
    'Example: "3 basketball picks at 5 odds"'
  );
}
export const INTRO = introText();

const SPORT_ALIASES: Record<string, string> = {
  soccer: "football", "association football": "football",
  "american football": "american_football", nfl: "american_football",
  gridiron: "american_football", pigskin: "american_football",
  nba: "basketball", wnba: "basketball", hoops: "basketball", basket: "basketball",
  mlb: "baseball", atp: "tennis", wta: "tennis", ufc: "mma", "mixed martial arts": "mma",
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
  "champions league": { id: "ucl", sport: "football" }, ucl: { id: "ucl", sport: "football" },
  "europa league": { id: "uel", sport: "football" }, uel: { id: "uel", sport: "football" },
  "conference league": { id: "uecl", sport: "football" }, uecl: { id: "uecl", sport: "football" },
};

/** League id -> a phrase LEAGUES itself recognizes, so a competition button's
 * reply round-trips through parseRequest into the SAME competition (never a
 * different one) - same discipline as MARKET_REPLY. Picks the FIRST key that
 * maps to each id, in LEAGUES's own declaration order. */
export const LEAGUE_REPLY: Record<string, string> = (() => {
  const out: Record<string, string> = {};
  for (const [phrase, { id }] of Object.entries(LEAGUES)) if (!(id in out)) out[id] = phrase;
  return out;
})();

/** Display label for a competition button. */
export const LEAGUE_LABEL: Record<string, string> = {
  epl: "Premier League", la_liga: "La Liga", serie_a: "Serie A", ligue_1: "Ligue 1",
  ucl: "Champions League", uel: "Europa League", uecl: "Conference League",
};
// Sports recognized by NAME but not currently offered, mapped to their
// canonical display name for the "We do not cover X yet" response (sorted
// longest-phrase-first so e.g. "ice hockey" is identified before the bare
// "hockey" substring it contains). Curated, like SPORT_ALIASES/LEAGUES - an
// unlisted word is never guessed at; it falls through to the generic HELP
// clarifying question instead of inventing a sport name.
const UNSUPPORTED_SPORTS: [string, string][] = Object.entries({
  "ice hockey": "Hockey", "field hockey": "Hockey", hockey: "Hockey",
  "rugby league": "Rugby", "rugby union": "Rugby", rugby: "Rugby",
  cricket: "Cricket", volleyball: "Volleyball", handball: "Handball", golf: "Golf",
  boxing: "Boxing", darts: "Darts", snooker: "Snooker", "table tennis": "Table Tennis",
  badminton: "Badminton", cycling: "Cycling", "formula 1": "Formula 1", "formula one": "Formula 1",
  f1: "Formula 1", esports: "Esports", "e-sports": "Esports", swimming: "Swimming",
  athletics: "Athletics", motorsport: "Motorsport",
}).sort((a, b) => b[0].length - a[0].length);

const MARKET_ALIASES: Record<string, string> = {
  result2: "result2", result3: "result3", dc: "dc", dnb: "dnb", btts: "btts", total: "total", handicap: "handicap",
  "double chance": "dc", "draw no bet": "dnb", moneyline: "result2", "money line": "result2", ml: "result2",
  totals: "total", "over under": "total", "over/under": "total", "both teams to score": "btts",
  handicaps: "handicap", spread: "handicap", spreads: "handicap", "1x2": "result3", "match result": "result3",
  // Common betting-slang shorthand for BTTS: "GG" (Goal-Goal, both teams
  // score) / "NG" (No Goal, at least one team doesn't) - both name the SAME
  // market, just opposite sides of it, same as "btts" itself names the
  // market without picking yes/no.
  gg: "btts", ng: "btts",
};

// Ordinary request nouns: a number next to any of these is a pick count or
// a goal/point/set line, never an "unsupported market" or unrecognized
// word. Shared between the count regex and the general-request signal so
// "1.5 options"/"3 predictions" are never mistaken for gibberish.
const REQUEST_WORDS = "picks?|games?|matches?|selections?|options?|predictions?";

const VALID_SELECTIONS: Record<string, Set<string>> = {
  result3: new Set(["home", "draw", "away"]),
  result2: new Set(["home", "away"]),
  dnb: new Set(["home", "away"]),
  dc: new Set(["home_draw", "away_draw", "home_away"]),
  btts: new Set(["yes", "no"]),
  total: new Set(["over", "under"]),
  handicap: new Set(["home", "away"]),
  odd_even: new Set(["odd", "even"]),
  prop: new Set(["over", "under", "yes", "no", "odd", "even"]),
};

function validSelection(market: string, selection: string): boolean {
  return VALID_SELECTIONS[market]?.has(selection) ?? false;
}

function finite(value: unknown): number | null {
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

const RESULT2_LABELS = (home: string, away: string): Record<string, string> => ({
  home: `${home} to Win`, away: `${away} to Win`,
});
const RESULT3_LABELS = (home: string, away: string): Record<string, string> => ({
  home: `${home} to Win`, draw: "Draw", away: `${away} to Win`,
});
const DNB_LABELS = (home: string, away: string): Record<string, string> => ({
  home: `${home} (Draw No Bet)`, away: `${away} (Draw No Bet)`,
});
const DC_LABELS: Record<string, string> = {
  home_draw: "Double Chance: 1X", away_draw: "Double Chance: X2", home_away: "Double Chance: 12",
};
const BTTS_LABELS: Record<string, string> = { yes: "BTTS: Yes", no: "BTTS: No" };

/**
 * ~ custom_picks.py normalize() + catalog.py selection_label(): every total
 * names side + line + stat + scope, every handicap names side + signed line
 * (+ stat/scope). Returns "" when the coordinates cannot be described
 * accurately — the caller drops such candidates instead of guessing.
 */
function outcomeLabel(
  market: string, selection: string, home: string, away: string,
  line: number | null, period: string, stat: string, entity: string, sport: string,
): string {
  const team: Record<string, string> = { home, away };
  const word = statWord(stat, sport);
  const suffix = periodSuffix(period);
  if (market === "result3" || market === "result2") {
    const base = market === "result3" ? RESULT3_LABELS(home, away) : RESULT2_LABELS(home, away);
    let core = base[selection] ?? `${market}: ${selection}`;
    if (stat !== "points") core += ` (${word})`;
    if (market === "result3" && selection === "draw") core = "Match Result: " + core;
    return core + suffix;
  }
  if (market === "dnb") {
    return (DNB_LABELS(home, away)[selection] ?? `${market}: ${selection}`) + (stat !== "points" ? ` (${word})` : "");
  }
  if (market === "dc") {
    let core = DC_LABELS[selection] ?? `${market}: ${selection}`;
    if (stat !== "points") core += ` (${readableStat(stat) || word})`;
    if (period !== "full" && period !== "reg") core += periodSuffix(period);
    return core;
  }
  if (market === "btts") {
    return BTTS_LABELS[selection] ?? `${market}: ${selection}`;
  }
  if (market === "total") {
    if (line == null || (entity !== "all" && entity !== "home" && entity !== "away")) return "";
    let core: string;
    if (entity === "all") {
      // ~ catalog.py selection_label(): "Over 0.5 Goals" alone doesn't say
      // whose goals - a match total must keep the explicit "Total" word
      // (previously stripped here right after being built, which is the
      // "Basketball first-quarter totals missing team/market scope" bug:
      // "Under 37.5 Total Points (1st Qtr)" rendered as the ambiguous
      // "Under 37.5 Points (1st Qtr)").
      core = `${selection === "over" ? "Over" : "Under"} ${fmtLine(line)} Total ${word}`.trim();
    } else {
      core = `${team[entity]} ${selection === "over" ? "Over" : "Under"} ${fmtLine(line)} ${word}`.trim();
    }
    return core + suffix;
  }
  if (market === "handicap") {
    if (line == null) return "";
    let core = `${team[selection] ?? selection} ${fmtLine(line, true)}`;
    if (stat !== "points") core += ` ${word}`;
    return "Handicap: " + core + suffix;
  }
  if (market === "odd_even") {
    return `${word} ${selection === "odd" ? "Odd" : "Even"}`;
  }
  if (market === "prop") {
    return propOutcome(selection, home, away, line, period, stat, entity);
  }
  return "";
}

/** ~ catalog.py prop_outcome(): ONE canonical outcome builder for prop markets
 * (player props AND period-scoped team/match props, e.g. a 1st-quarter
 * basketball total modeled as entity="home"/"away", stat="points",
 * period="1q"). Every branch names the explicit side/player, the line and
 * the stat, and — unlike catalog.py's current prop_outcome — the period, so
 * a quarter/half total is never shown as if it were full-game. Returns ""
 * when the coordinates can't produce an unambiguous outcome (no line, no
 * recognizable side, no stat): the caller drops such a candidate rather than
 * ever rendering a bare "Over"/"Yes" or a raw entity key. */
function propOutcome(
  selection: string, home: string, away: string,
  line: number | null, period: string, stat: string, entity: string,
): string {
  const s = selection.trim().toLowerCase();
  const team: Record<string, string> = { home, away };
  const who = entity in team ? team[entity] : "";
  const word = readableStat(stat);
  const suffix = periodSuffix(period);
  if (s === "over" || s === "under") {
    if (line == null || !word || !who) return "";
    const finalWord = entity in team && word.toLowerCase() === "hits" ? "Team Hits" : word;
    return `${who} ${s === "over" ? "Over" : "Under"} ${fmtLine(line)} ${finalWord}`.trim() + suffix;
  }
  if (s === "yes" || s === "no") {
    if (!word || !who) return "";
    return `${who} ${word} ${s === "yes" ? "Yes" : "No"}` + suffix;
  }
  if ((s === "even" || s === "odd") && word && who) {
    return `${who} ${word} ${s === "even" ? "Even" : "Odd"}` + suffix;
  }
  return "";
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
  line: number | null;
  period: string;
  stat: string;
  entity: string;
  /** Stable fixture identity (ids when archived, else teams): one leg per
   * real fixture even if fixture-id strings drift, and conflicting rows for
   * one id are dropped before search. */
  fixtureIdentity: string;
}

/** ~ predbot/markets/catalog.py STAT_NAMES / PERIOD_NAMES / SPORT_POINTS_WORD. */
const STAT_NAMES: Record<string, string> = {
  points: "", goals: "Goals", corners: "Corners", cards: "Cards", sets: "Sets",
  games: "Games", rounds: "Rounds", hits: "Hits", shots: "Shots",
};
const PERIOD_NAMES: Record<string, string> = {
  full: "", reg: "Regulation", "1h": "1st Half", "2h": "2nd Half",
  "1q": "1st Qtr", "2q": "2nd Qtr", "3q": "3rd Qtr", "4q": "4th Qtr",
  "1s": "1st Set", "2s": "2nd Set", "1i5": "First 5 Innings",
};
const SPORT_POINTS_WORD: Record<string, string> = {
  football: "Goals", basketball: "Points", american_football: "Points",
  baseball: "Runs", tennis: "Games", mma: "Rounds",
};
const PROP_STAT_WORDS: Record<string, string> = {
  hits: "Hits", batting_hits: "Hits", total_bases: "Total Bases", totalbases: "Total Bases",
  runs: "Runs", rbi: "RBIs", rbis: "RBIs", runs_batted_in: "RBIs", home_runs: "Home Runs",
  homeruns: "Home Runs", strikeouts: "Strikeouts", strikeouts_thrown: "Strikeouts",
  pitcher_strikeouts: "Strikeouts", batter_strikeouts: "Strikeouts", walks: "Walks",
  doubles: "Doubles", triples: "Triples", earned_runs: "Earned Runs", points: "Points",
  rebounds: "Rebounds", assists: "Assists", three_pointers: "Three-Pointers",
  threepointers: "Three-Pointers", three_pointers_made: "Three-Pointers", steals: "Steals",
  blocks: "Blocks", goals: "Goals", shots: "Shots", shots_on_target: "Shots on Target",
  corners: "Corners", cards: "Cards", saves: "Saves", touchdowns: "Touchdowns",
  passing_yards: "Passing Yards", rushing_yards: "Rushing Yards", receiving_yards: "Receiving Yards",
  receptions: "Receptions", passing_tds: "Passing TDs", field_goals: "Field Goals",
  made_field_goals: "Field Goals",
};

/** ~ catalog.py readable_stat(): a raw stat id as plain words, never underscores. */
function readableStat(stat: string): string {
  const s = String(stat ?? "").trim();
  if (!s) return "";
  const key = s.replace(/[\s-]+/g, "_").toLowerCase();
  if (PROP_STAT_WORDS[key]) return PROP_STAT_WORDS[key];
  if (STAT_NAMES[key]) return STAT_NAMES[key];
  const words = s.replace(/(?<!^)(?=[A-Z])/g, " ").replace(/_/g, " ").split(/\s+/).filter(Boolean);
  const out = words.join(" ").trim();
  return out ? toTitle(out) : s;
}

function toTitle(out: string): string {
  return out.toLowerCase().replace(/\b\w/g, (c) => c.toUpperCase());
}

/** ~ catalog.py _fmt_line(): 2.0 -> "2", -1.5 signed -> "-1.5", +4.5 -> "+4.5". */
function fmtLine(line: number | null, signed = false): string {
  if (line == null) return "";
  const s = Number.isInteger(line) ? String(line) : String(line);
  return signed && line > 0 ? "+" + s : s;
}

/** ~ catalog.py selection_label(): `STAT_NAMES.get(ml.stat, readable_stat(ml.stat)) or pts`.
 * dict.get() only falls through to readable_stat when the key is MISSING, not
 * when its value is falsy — "points" IS a key (value ""), so it must resolve
 * straight to the sport word, never through readableStat()'s own
 * PROP_STAT_WORDS["points"] = "Points", which would silently defeat the
 * sport-specific unit for every non-basketball sport (the "Under 1.5 Points"
 * on a football total bug). */
function statWord(stat: string, sport: string): string {
  const named = stat in STAT_NAMES ? STAT_NAMES[stat] : readableStat(stat);
  return named || SPORT_POINTS_WORD[sport] || "Points";
}

function periodSuffix(period: string): string {
  const per = PERIOD_NAMES[period] ?? period;
  return per ? ` (${per})` : "";
}

const DC_SHORT: Record<string, string> = { home_draw: "1X", away_draw: "X2", home_away: "12" };

/**
 * ~ custom_picks.py normalize(): validate an archived candidate row without altering the stored evaluation.
 *
 * NOTE on row vs payload fields: production's archive writer
 * (predbot/model/candidates.py capture_candidate) serializes ONLY the
 * evaluated event/market/score fields into payload_json. prediction_date,
 * timezone and generated_at live exclusively in the D1 row's own columns
 * (mirrored by predbot/storage/db.py get_custom_pick_candidates, which
 * merges the columns over the parsed JSON). Callers MUST therefore merge
 * the row columns in before calling this — see loadPool(). A payload that
 * already carries these keys (older test fixtures) keeps working because
 * the row values are applied only when present, payload-first otherwise.
 */
export function normalizeCandidate(
  payload: Record<string, unknown>,
  day: string,
  zone: string,
  nowMs: number,
  poolTimeMs: number,
  includeStarted = false,
  rowGeneratedAt?: string,
): Candidate | null {
  return normalizeCandidateWithReason(payload, day, zone, nowMs, poolTimeMs, includeStarted, rowGeneratedAt).candidate;
}

/** Same validation as normalizeCandidate, plus a machine-readable rejection reason for diagnostics. */
export function normalizeCandidateWithReason(
  payload: Record<string, unknown>,
  day: string,
  zone: string,
  nowMs: number,
  poolTimeMs: number,
  includeStarted = false,
  rowGeneratedAt?: string,
): { candidate: Candidate | null; reason: string } {
  if (payload.prediction_date !== day || payload.timezone !== zone) return { candidate: null, reason: "day_zone_mismatch" };
  // ~ custom_picks.py normalize(): statuses live on the row AND on its
  // nested `extra` dict (event.extra) — either may report a dead fixture.
  const extra = (payload.extra as Record<string, unknown> | null) ?? null;
  const extraObj = extra && typeof extra === "object" && !Array.isArray(extra) ? extra : {};
  const statuses = [payload, extraObj]
    .flatMap((source) => ["status", "event_status", "fixture_status"].map((k) => String(source[k] ?? "").toLowerCase()));
  const badStatuses = new Set(["cancelled", "canceled", "postponed", "abandoned", "final", "finished", "completed", "live", "in_progress", "suspended"]);
  if (statuses.some((s) => badStatuses.has(s))) return { candidate: null, reason: "bad_status" };
  if (["cancelled", "canceled", "postponed", "started", "finished", "is_live"].some((k) => payload[k] === true || extraObj[k] === true)) {
    return { candidate: null, reason: "bad_status" };
  }

  const startMs = Date.parse(String(payload.start_utc ?? ""));
  // Archived candidates may predate the payload carrying its own
  // `generated_at` (production: 2,441 candidates, all missing it from the
  // JSON). The D1 row always has its own `generated_at` column, set at
  // archive time — use it ONLY when the payload field is absent, never to
  // silently override a payload value that's present but fails to parse.
  const payloadGeneratedAt = payload.generated_at;
  const hasPayloadTimestamp = payloadGeneratedAt != null && String(payloadGeneratedAt).trim() !== "";
  const evaluatedMs = Date.parse(hasPayloadTimestamp ? String(payloadGeneratedAt) : String(rowGeneratedAt ?? ""));
  if (!Number.isFinite(startMs)) return { candidate: null, reason: "bad_start" };
  if (startMs <= nowMs && !includeStarted) return { candidate: null, reason: "started" };
  if (!Number.isFinite(evaluatedMs) || evaluatedMs > poolTimeMs || evaluatedMs > nowMs) {
    return { candidate: null, reason: "bad_evaluated" };
  }
  // ~ custom_picks.py normalize(): the evaluation must belong to the
  // request's Lagos day — a stale row (evaluated yesterday, read today)
  // is never served as today's pick. Africa/Lagos is UTC+1 year-round.
  const evaluatedDay = new Date(evaluatedMs + 60 * 60_000).toISOString().slice(0, 10);
  if (evaluatedDay !== day) return { candidate: null, reason: "stale_evaluated_day" };

  const price = finite(payload.price);
  const probability = finite(payload.p_model);
  if (price == null || price <= 1 || probability == null || probability <= 0 || probability >= 1) {
    return { candidate: null, reason: "bad_price_prob" };
  }

  const fixtureId = String(payload.canonical_event_id ?? payload.fixture_key ?? "");
  const sport = String(payload.sport ?? "");
  if (!fixtureId.trim() || !SUPPORTED_SPORTS.has(sport)) return { candidate: null, reason: "bad_fixture_sport" };

  const market = String(payload.market ?? "");
  const selection = String(payload.selection ?? "");
  if (!validSelection(market, selection)) return { candidate: null, reason: "bad_market_selection" };

  const home = String(payload.home ?? "");
  const away = String(payload.away ?? "");
  if (!home.trim() || !away.trim()) return { candidate: null, reason: "bad_teams" };
  if (home.trim().toLowerCase() === away.trim().toLowerCase()) {
    return { candidate: null, reason: "same_teams" }; // A team cannot play itself: incorrectly mapped fixture.
  }
  const homeId = typeof payload.home_id === "string" && payload.home_id.trim() ? payload.home_id.trim() : null;
  const awayId = typeof payload.away_id === "string" && payload.away_id.trim() ? payload.away_id.trim() : null;
  if (homeId && awayId && homeId === awayId) {
    return { candidate: null, reason: "same_teams" };
  }

  const line = payload.line == null ? null : finite(payload.line);
  if (payload.line != null && line == null) return { candidate: null, reason: "bad_line" };
  if ((market === "total" || market === "handicap") && line == null) return { candidate: null, reason: "bad_line" };

  // Market coordinates. Legacy rows may omit them; they default to the
  // full-game match scope (as production's archive writer always records the
  // real coordinates, an omitted field is a full-game row, not an unknown).
  // A total attributed to anything other than a team or the match cannot be
  // described accurately and is excluded rather than guessed at.
  const rawPeriod = payload.period, rawStat = payload.stat, rawEntity = payload.entity;
  const period = typeof rawPeriod === "string" && rawPeriod.trim() ? rawPeriod.trim() : "full";
  const stat = typeof rawStat === "string" && rawStat.trim() ? rawStat.trim() : "points";
  const entity = typeof rawEntity === "string" && rawEntity.trim() ? rawEntity.trim() : "all";
  if (market === "total" && entity !== "all" && entity !== "home" && entity !== "away") {
    return { candidate: null, reason: "bad_entity" };
  }

  const outcome = outcomeLabel(market, selection, home, away, line, period, stat, entity, sport);
  if (!outcome) return { candidate: null, reason: "bad_outcome" };
  const key = JSON.stringify([market, period, stat, entity, selection, line]);
  const leagueRaw = payload.league;
  const competitionLabel = typeof leagueRaw === "string" && leagueRaw.trim() && !/[_:]/.test(leagueRaw) ? leagueRaw.trim() : null;
  const competition =
    typeof payload.canonical_competition_id === "string"
      ? payload.canonical_competition_id
      : (competitionLabel && LEAGUES[competitionLabel.toLowerCase()]?.id) || null;
  // Stable fixture identity (ids when archived, else teams + start + sport):
  // one leg per real fixture even if fixture-id strings drift, and rows that
  // disagree about a fixture's identity are dropped before search.
  const idParts = homeId && awayId ? [homeId, awayId].sort() : [home.trim().toLowerCase(), away.trim().toLowerCase()].sort();
  const fixtureIdentity = `${sport}|${competition ?? ""}|${startMs}|${idParts.join("~")}`;

  const candidate: Candidate = {
    fixtureId, key, sport, market, selection, price, probability, startMs, home, away, outcome,
    competition, competitionLabel,
    agreement: Math.max(0, Math.min(1, finite(payload.agreement) ?? 0)),
    liquidity: Math.max(0, Math.min(1, finite(payload.liquidity) ?? 0)),
    books: Math.max(0, Math.min(10, finite(payload.bookmaker_count) ?? 0)),
    referenceEv: Math.max(-0.1, Math.min(0.1, finite(payload.edge_ev) ?? -0.1)),
    line, period, stat, entity, fixtureIdentity,
  };
  return { candidate, reason: "accepted" };
}

/** Drop every candidate of a fixture id whose rows disagree about the real
 * fixture (different teams/starts/competitions under one id) — a plausible
 * wrong fixture must never be served. Distinct market alternatives for one
 * fixture remain. ~ custom_recovery.py Recovery.load(). */
export function dropConflictingIdentities(candidates: Candidate[]): Candidate[] {
  const identities = new Map<string, Set<string>>();
  for (const c of candidates) {
    const set = identities.get(c.fixtureId) ?? new Set<string>();
    set.add(c.fixtureIdentity);
    identities.set(c.fixtureId, set);
  }
  return candidates.filter((c) => (identities.get(c.fixtureId)?.size ?? 0) === 1);
}

export interface ParsedRequest {
  targetOdds: number | null;
  pickCount: number | null;
  sports: Set<string>;
  allowedMarkets: Set<string>;
  excludedMarkets: Set<string>;
  /** "pro" is never typed by a user — only respond() sets it, forced for
   * an admin Pro Picks session (see RespondOptions.preference). */
  preference: "safe" | "strong" | "best" | "pro";
  competitions: Set<string>;
  timeIntent: "today" | "tonight" | null;
  specifiedFields: Set<string>;
  refinement: boolean;
  qualityUpgrade: boolean;
  /** Lower-cased team names mentioned by name, matched against the loaded
   * pool (there's no static team catalog - teams change daily), applied in
   * scoped(). Populated AFTER parseRequest, in respond(), since it needs the
   * pool; see matchTeamsInPool(). Persists across refinement turns like any
   * other field unless the CURRENT message names different teams. */
  teams: Set<string>;
  /** An explicit "Over 1.5" / "Under 2.5" style line request: the total
   * market, a specific side and a specific line value - never relaxed to a
   * nearby line, so a request for a line nothing today offers fails
   * honestly (market_unavailable) instead of silently substituting a
   * different line. */
  totalLine: { selection: "over" | "under"; line: number } | null;
  /** A bare decimal that reads like a goal/point/set line ("1.5 options")
   * but named no side - set fresh on EVERY parse (never carried forward by
   * refine() itself; see its "ambiguous_line" case), so respond() can ask
   * "Over or Under?" unless the conversation already resolved this exact
   * number via totalLine (carried forward normally). Never silently
   * defaulted to Over. */
  ambiguousLine: number | null;
  /** The leg cap THIS parse was allowed to validate against (MAX_LEGS for a
   * free session, MAX_LEGS_PRO for an admin Pro Picks session - see
   * parseRequest's `maxLegs` param). Not a user-specified field: refine()
   * always takes the CURRENT turn's value, never a stale one carried over
   * from an earlier turn in a different session mode. */
  maxLegs: number;
}

/** JSON-safe form of a ParsedRequest (Sets -> arrays), for D1 persistence across stateless invocations. */
export function serializeRequest(req: ParsedRequest): Record<string, unknown> {
  return {
    targetOdds: req.targetOdds, pickCount: req.pickCount, sports: [...req.sports],
    allowedMarkets: [...req.allowedMarkets], excludedMarkets: [...req.excludedMarkets],
    preference: req.preference, competitions: [...req.competitions], timeIntent: req.timeIntent,
    specifiedFields: [...req.specifiedFields], refinement: req.refinement, qualityUpgrade: req.qualityUpgrade,
    teams: [...req.teams], totalLine: req.totalLine, ambiguousLine: req.ambiguousLine, maxLegs: req.maxLegs,
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
    teams: new Set((raw.teams as string[]) ?? []),
    totalLine: (raw.totalLine as ParsedRequest["totalLine"]) ?? null,
    ambiguousLine: (raw.ambiguousLine as number) ?? null,
    maxLegs: (raw.maxLegs as number) ?? MAX_LEGS,
  };
}

export function freshRequest(maxLegs: number = MAX_LEGS): ParsedRequest {
  return {
    targetOdds: null, pickCount: null, sports: new Set(), allowedMarkets: new Set(), excludedMarkets: new Set(),
    preference: "best", competitions: new Set(), timeIntent: null, specifiedFields: new Set(),
    refinement: false, qualityUpgrade: false, teams: new Set(), totalLine: null, ambiguousLine: null, maxLegs,
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
export function parseRequest(rawText: string, enabled: Set<string> = DEFAULT_ENABLED_SPORTS, maxLegs: number = MAX_LEGS): ParseResult {
  const trimmed = rawText.trim();
  if (!trimmed || trimmed.length > 512) return err(HELP);
  const text = trimmed.toLowerCase();

  if (/\b(tomorrow|yesterday|next|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b|\b\d{4}-\d{2}-\d{2}\b/.test(text)) {
    return err(TODAY_ONLY, "future_date");
  }

  const req = freshRequest(maxLegs);
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
        return err("Which sports would you like to include?", "ask_sport");
      }
      req.sports.add(canonical);
    }
  }
  if (req.sports.size) req.specifiedFields.add("sports");
  if (/\ball sports\b/.test(text)) {
    if (req.sports.size) return err(HELP, "conflicting_constraints");
    req.specifiedFields.add("sports"); // sports stays empty: explicit "all"
  }
  // A recognized sport that isn't ENABLED (currently just "mma" - see
  // DEFAULT_ENABLED_SPORTS) gets the same "we don't cover it yet" message as
  // a genuinely unrecognized sport, never silently dropped or accepted.
  const disabled = [...req.sports].filter((s) => SUPPORTED_SPORTS.has(s) && !enabled.has(s)).sort();
  if (disabled.length) {
    return err(
      `We do not cover ${SPORT_LABEL[disabled[0]] ?? disabled[0]} yet. Please choose one of our supported sports.\n\nSupported sports: ${enabledSportsLabel(enabled)}`,
      "unsupported_sport",
    );
  }
  if (req.competitions.size && req.sports.size) {
    const leagueSports = new Set([...req.competitions].map((id) => Object.values(LEAGUES).find((l) => l.id === id)?.sport));
    if (![...leagueSports].every((s) => req.sports.has(s as string))) {
      return err("Those leagues and sports don't match. Which should I use?", "conflicting_constraints");
    }
  }
  for (const [word, label] of UNSUPPORTED_SPORTS) {
    if (word in sportWords) continue; // never shadow a genuinely supported alias
    if (new RegExp(`\\b${word.replace(/\s+/g, "\\s+")}\\b`).test(text)) {
      return err(
        `We do not cover ${label} yet. Please choose one of our supported sports.\n\nSupported sports: ${enabledSportsLabel(enabled)}`,
        "unsupported_sport",
      );
    }
  }

  // Claimed EARLY (before odds-target/count scanning) so "under 2.5 games"
  // is never misread as a (non-integer, erroring) pick count: "2.5" sits
  // right where a count regex would also look, so the explicit over/under
  // phrase must claim its span first. The actual req mutation (needs
  // excludedMarkets, computed later) stays below, unchanged.
  const lineMatch = /\b(over|under)\s+(\d+(?:\.\d+)?)\b/.exec(text);
  if (lineMatch) spans.push([lineMatch.index, lineMatch.index + lineMatch[0].length]);

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
    `(?<![\\w.])(\\d+(?:\\.\\d+)?)\\s+(?:(?:${qualifierWords.join("|")})\\s+)*(?:${REQUEST_WORDS})\\b`,
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
    // A non-integer "count" (no over/under claimed it above, or it would
    // never have reached here) is never a pick-count typo - it's a
    // goal/point/set LINE with no named side ("1.5 options"). Never
    // silently defaulted to Over: respond() asks, unless the conversation
    // already resolved this exact number (see ambiguousLine).
    if (!Number.isInteger(count)) {
      req.ambiguousLine = count;
      req.specifiedFields.add("ambiguous_line");
    } else if (count < 1 || count > maxLegs) {
      return err(`I can offer 1–${maxLegs} different games. How many would you like?`, "invalid_count");
    } else {
      req.pickCount = count;
      req.specifiedFields.add("pick_count");
    }
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

  // ~ catalog.py selection_label()'s total/handicap line, read back the
  // other way: "Over 1.5" / "Under 2.5" names an explicit total-market line
  // (goals/points/sets, whichever the matched sport uses — the stat word
  // itself is never parsed from text, only rendered). Never widened to a
  // nearby line: scoped() requires an exact match, so a line nothing today
  // offers fails honestly instead of silently substituting a close one.
  // (lineMatch itself was computed earlier, before odds-target/count
  // scanning, so its span is already claimed - see its definition above.)
  if (lineMatch && !req.excludedMarkets.has("total")) {
    req.totalLine = { selection: lineMatch[1] as "over" | "under", line: Number(lineMatch[2]) };
    req.allowedMarkets.add("total");
    req.specifiedFields.add("total_line");
  }
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
  const general = new RegExp(`\\b(?:${REQUEST_WORDS})\\b`).test(text);
  const hasSignal = req.specifiedFields.size > (req.specifiedFields.has("time_intent") ? 1 : 0);
  if (!hasSignal && !general) return err(HELP);
  if (target == null && count == null && !req.allowedMarkets.size && !req.excludedMarkets.size && /\b(scores?|results?|news|weather|payment)\b/.test(text)) {
    return err(HELP);
  }

  req.refinement =
    /\b(make|change|switch|instead|same|safer|stronger)\b/.test(text) ||
    // An ambiguous bare line always checks the previous turn - that's the
    // ONLY way "unless previous context already resolves it" can work.
    req.ambiguousLine != null ||
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
  if (req.teams.size && !req.teams.has(c.home.toLowerCase()) && !req.teams.has(c.away.toLowerCase())) return false;
  if (req.totalLine) {
    // ~ custom_recovery.py scoped()'s market_line check: an explicit "Over
    // 1.5" means the FULL MATCH total specifically — never a team total or
    // a period-scoped one (a 1st-half "Over 1.5" is a different market the
    // user didn't ask for), so period/stat/entity are pinned too.
    const t = req.totalLine;
    if (c.market !== "total" || c.selection !== t.selection || c.line !== t.line) return false;
    if (c.entity !== "all" || c.stat !== "points" || (c.period !== "full" && c.period !== "reg")) return false;
  }
  return true;
}

/** ~ custom_intent.py parse()'s team-name recognition: there's no static
 * team catalog (teams change daily), so a team mention is matched against
 * the names actually IN today's pool, not guessed from free text alone.
 * Only returns names the pool itself contains - never invents a team. */
function matchTeamsInPool(text: string, pool: Candidate[]): Set<string> {
  const lower = text.toLowerCase();
  const seen = new Set<string>();
  const matched = new Set<string>();
  for (const c of pool) {
    for (const name of [c.home, c.away]) {
      const key = name.toLowerCase();
      if (seen.has(key)) continue;
      seen.add(key);
      const escaped = key.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      if (new RegExp(`\\b${escaped}\\b`).test(lower)) matched.add(key);
    }
  }
  return matched;
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

/**
 * Dynamic clarification-button options (~ spec "Dynamic options": never
 * advertise a sport/market/line nothing today actually has). Each relaxes
 * exactly ONE dimension of `req` and re-runs the same eligible() pool
 * filter already used for real search, so a button is never offered for
 * something `eligible()` itself wouldn't actually find.
 */
export function availableSports(pool: Candidate[], req: ParsedRequest, nowMs: number, enabled: Set<string>): string[] {
  const relaxed: ParsedRequest = { ...req, sports: new Set() };
  const present = new Set(eligible(pool, relaxed, nowMs).map((c) => c.sport));
  return [...enabled].filter((s) => present.has(s));
}

export function availableMarkets(pool: Candidate[], req: ParsedRequest, nowMs: number): string[] {
  const relaxed: ParsedRequest = { ...req, allowedMarkets: new Set(), excludedMarkets: new Set(), totalLine: null };
  return [...new Set(eligible(pool, relaxed, nowMs).map((c) => c.market))];
}

/** Competitions (LEAGUE_REPLY-known ones only) actually on offer today -
 * used by the "I don't recognize that league" clarification, so a button
 * never names a competition today's pool doesn't have a single fixture in. */
export function availableCompetitions(pool: Candidate[], req: ParsedRequest, nowMs: number): string[] {
  const relaxed: ParsedRequest = { ...req, competitions: new Set() };
  const present = new Set(eligible(pool, relaxed, nowMs).map((c) => c.competition).filter((id): id is string => !!id));
  return Object.keys(LEAGUE_REPLY).filter((id) => present.has(id));
}

/** Alternative Over/Under total lines actually on offer today, for the
 * SAME full-match scoping scoped()'s totalLine branch requires (never a
 * period/team total masquerading as a full-match line). */
export function availableTotalLines(
  pool: Candidate[],
  req: ParsedRequest,
  nowMs: number,
): { selection: "over" | "under"; line: number }[] {
  const relaxed: ParsedRequest = { ...req, totalLine: null, allowedMarkets: new Set(["total"]), excludedMarkets: new Set() };
  const seen = new Map<string, { selection: "over" | "under"; line: number }>();
  for (const c of eligible(pool, relaxed, nowMs)) {
    if (c.market !== "total" || c.line == null) continue;
    if (c.entity !== "all" || c.stat !== "points" || (c.period !== "full" && c.period !== "reg")) continue;
    const selection = c.selection as "over" | "under";
    seen.set(`${selection}:${c.line}`, { selection, line: c.line });
  }
  return [...seen.values()].sort((a, b) => a.line - b.line || a.selection.localeCompare(b.selection));
}

/** Market code -> a phrase MARKET_ALIASES itself recognizes, so a button's
 * reply round-trips through parseRequest into the SAME market (never a
 * different one). result3 has no natural standalone phrase (its only alias
 * is the literal word "result3") and is deliberately left out of button
 * offers; it's unaffected as a typed request. */
export const MARKET_REPLY: Record<string, string> = {
  result2: "moneyline", dc: "double chance", dnb: "draw no bet",
  btts: "both teams to score", total: "over/under", handicap: "handicap",
};

/** Display label for a market button — kept in sync with marketName()'s
 * phrasing below by hand (both are small, static, rarely-changed maps). */
export const MARKET_LABEL: Record<string, string> = {
  result2: "To Win", result3: "Match Result", dc: "Double Chance",
  dnb: "Draw No Bet", btts: "BTTS", total: "Over/Under", handicap: "Handicap",
};

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

function minPriceFor(_req: ParsedRequest): number {
  return MIN_PRICE;
}

function maxLegsFor(req: ParsedRequest): number {
  if (req.pickCount) return req.pickCount;
  if (req.targetOdds != null && req.targetOdds <= LOW_TARGET_CEILING) return LOW_TARGET_MAX_LEGS;
  if (req.targetOdds) return req.maxLegs;
  return DEFAULT_COUNT;
}

/**
 * ~ custom_picks.py search(): the top-ranked feasible ticket, or null.
 * Deterministic: the best ticket always wins, so per-user variety is layered
 * on top via shortlist(), never inside search(). Selections below the strict
 * minimum price never participate, at any target odds.
 */
export function search(candidates: Candidate[], req: ParsedRequest): Ticket | null {
  const ranked = shortlist(candidates, req, SHORTLIST_LIMIT);
  return ranked.length ? { picks: ranked[0].picks, odds: ranked[0].odds } : null;
}

interface BeamResult {
  beams: Map<number, SearchState[]>;
  upper: number;
  limit: number;
  groups: Candidate[][];
}

function runBeam(pool: Candidate[], req: ParsedRequest): BeamResult {
  const grouped = new Map<string, Candidate[]>();
  for (const c of [...pool].sort((a, b) => cmp(rankKey(a), rankKey(b)))) {
    const key = c.fixtureIdentity || c.fixtureId;
    const list = grouped.get(key) ?? [];
    list.push(c);
    grouped.set(key, list);
  }
  const groups = [...grouped.values()].sort((a, b) => cmp(rankKey(a[0]), rankKey(b[0]))).slice(0, MAX_FIXTURES);
  const limit = maxLegsFor(req);
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
  return { beams, upper, limit, groups };
}

function feasibleStates(beams: Map<number, SearchState[]>, upper: number, limit: number, groups: Candidate[][], req: ParsedRequest): SearchState[] {
  const counts = req.pickCount ? [req.pickCount] : req.targetOdds ? Array.from({ length: limit }, (_, i) => i + 1) : [Math.min(DEFAULT_COUNT, groups.length)];
  const out: SearchState[] = [];
  for (const count of counts) {
    for (const s of beams.get(count) ?? []) {
      if (!req.targetOdds || (req.targetOdds * (1 - TOLERANCE) <= s.odds && s.odds <= upper)) out.push(s);
    }
  }
  return out;
}

/**
 * ~ custom_picks.py shortlist(): the top-N distinct feasible tickets, best
 * first. Every entry satisfies the same constraints search() enforces
 * (price floor, one leg per fixture, target tolerance, exact count when
 * requested). Low targets stay compact via the price floor + 3-leg cap;
 * one- and two-leg states are ranked alongside three-leg states (quality
 * first, then leg count, then proximity), so fewer legs win ties but
 * confidence is never traded for fewer legs.
 */
export function shortlist(candidates: Candidate[], req: ParsedRequest, limit = SHORTLIST_LIMIT): Ticket[] {
  if (limit <= 0) return [];
  const pool = candidates.filter((c) => c.price >= minPriceFor(req));
  const { beams, upper, limit: legLimit, groups } = runBeam(pool, req);
  if (groups.length < (req.pickCount ?? 1)) return [];
  const feasible = feasibleStates(beams, upper, legLimit, groups, req)
    .sort((a, b) => cmp(stateRank(a, req), stateRank(b, req)));
  const ranked: Ticket[] = [];
  const seen = new Set<string>();
  for (const s of feasible) {
    const signature = ticketSignature({ picks: s.picks, odds: s.odds });
    if (seen.has(signature)) continue;
    seen.add(signature);
    ranked.push({ picks: s.picks, odds: s.odds });
    if (ranked.length >= limit) break;
  }
  return ranked;
}

/** Stable identity of a ticket's fixture set and combined odds. */
export function ticketSignature(ticket: Ticket): string {
  return [...ticket.picks].map((c) => c.fixtureIdentity || c.fixtureId).sort().join(",") + `@${ticket.odds.toFixed(2)}`;
}

/** Stable identity of a request's constraints for variety rotation. */
export function requestKey(req: ParsedRequest): string {
  return [
    req.targetOdds ?? "", req.pickCount ?? "", [...req.sports].sort().join(","),
    [...req.allowedMarkets].sort().join(","), [...req.excludedMarkets].sort().join(","),
    req.preference, [...req.competitions].sort().join(","),
    req.timeIntent ?? "",
  ].join("|");
}

/** Synchronous 53-bit hash (cyrb53) for rotation indexes: no async crypto
 * needed, and raw user ids never leave the request scope unhashed. */
export function hash53(input: string): number {
  let h1 = 0xdeadbeef, h2 = 0x41c6ce57;
  for (let i = 0; i < input.length; i++) {
    const ch = input.charCodeAt(i);
    h1 = Math.imul(h1 ^ ch, 2654435761);
    h2 = Math.imul(h2 ^ ch, 1597334677);
  }
  h1 = Math.imul(h1 ^ (h1 >>> 16), 2246822507) ^ Math.imul(h2 ^ (h2 >>> 13), 3266489909);
  h2 = Math.imul(h2 ^ (h2 >>> 16), 2246822507) ^ Math.imul(h1 ^ (h1 >>> 13), 3266489909);
  return 4294967296 * (2097151 & h2) + (h1 >>> 0);
}

/** Recently served ticket signatures per (hashed user, day, request): bounded
 * in-process rotation memory so identical requests need not serve the same
 * ticket every time. Never persisted, never logged; a restart simply starts
 * rotation over. Only hashes are kept, never raw ids. */
const recentTickets = new Map<string, string[]>();
const RECENT_LIMIT = 512;

function recentKey(userHash: string, day: string, req: ParsedRequest): string {
  return `${userHash}|${day}|${requestKey(req)}`;
}

/**
 * Pick one shortlisted ticket with per-user rotation. The shortlist is
 * already ranked best-first under identical quality rules, so rotation only
 * moves within qualified tickets: confidence, price floor, constraints and
 * fixture validity are never sacrificed for diversity. A lone qualified
 * ticket is always served.
 */
export function chooseTicket(options: Ticket[], userId: string, day: string, req: ParsedRequest): Ticket | null {
  if (!options.length) return null;
  const userHash = hash53(`custom-picks|${userId}`).toString(36);
  const key = recentKey(userHash, day, req);
  const rotation = hash53(key) % options.length;
  const recent = recentTickets.get(key) ?? [];
  const ordered = options.map((_, i) => options[(rotation + i) % options.length]);
  const ticket = ordered.find((t) => !recent.includes(hash53(ticketSignature(t)).toString(36))) ?? ordered[0];
  recentTickets.set(key, [...recent, hash53(ticketSignature(ticket)).toString(36)].slice(-4));
  while (recentTickets.size > RECENT_LIMIT) {
    const oldest = recentTickets.keys().next();
    if (oldest.done) break;
    recentTickets.delete(oldest.value);
  }
  return ticket;
}

/** ~ custom_recovery.py valid_ticket() + market_complete(): the final
 * delivery boundary before anything reaches Telegram — minimum individual
 * odds, fixture identity, market completeness, leg count, combined-odds
 * maths, target tolerance, sport/market constraints and confidence
 * requirements. Applies to normal generation, callbacks, recovery and stored
 * suggestions alike: legacy stored rows lacking market coordinates fail here
 * and fall through to the expired-suggestion path, never to a guessed label. */
export function validTicket(ticket: Ticket | null, req: ParsedRequest, nowMs: number): boolean {
  const cap = req.targetOdds && !req.pickCount ? maxLegsFor(req) : req.maxLegs;
  if (!ticket || ticket.picks.length < 1 || ticket.picks.length > cap) return false;
  if (new Set(ticket.picks.map((c) => c.fixtureId)).size !== ticket.picks.length) return false;
  if (new Set(ticket.picks.map((c) => c.fixtureIdentity || c.fixtureId)).size !== ticket.picks.length) return false;
  if (req.pickCount && ticket.picks.length !== req.pickCount) return false;
  if (ticket.picks.some((c) => !Number.isFinite(c.price) || c.price < minPriceFor(req))) return false;
  if (!marketComplete(ticket)) return false;
  if (eligible(ticket.picks, req, nowMs).length !== ticket.picks.length) return false;
  const product = ticket.picks.reduce((a, c) => a * c.price, 1);
  if (!Number.isFinite(product) || Math.abs(product - ticket.odds) > 1e-6 * Math.max(1, Math.abs(ticket.odds))) return false;
  if (req.targetOdds && !(req.targetOdds * (1 - TOLERANCE) <= product && product <= req.targetOdds * (1 + TOLERANCE))) return false;
  return true;
}

/** Every leg carries the market detail needed to display it accurately.
 * Totals/handicap-style markets without a line, legs with an empty outcome,
 * an unattributable total entity, or missing market coordinates can never be
 * described unambiguously — excluded, never guessed at. */
export function marketComplete(ticket: Ticket): boolean {
  return ticket.picks.every((c) => {
    if ((c.market === "total" || c.market === "handicap") && c.line == null) return false;
    if (c.market === "total" && c.entity !== "all" && c.entity !== "home" && c.entity !== "away") return false;
    if (!c.outcome) return false;
    if (!c.period?.trim() || !c.stat?.trim() || !c.entity?.trim()) return false;
    return true;
  });
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
  blocks.push("Bet only what you can afford to lose.");
  return blocks.join("\n\n");
}

const COPY: Record<string, string> = {
  archive_unavailable: UNAVAILABLE,
  sport_unavailable: "👀 Nothing cooking today\n\nI don't have usable picks for that sport right now.",
  market_unavailable: "😅 That market is a little dry today\n\nI couldn't find enough matching picks that meet the mark.",
  line_unavailable: "📏 That exact line isn't up today\n\nTry a different line, or drop it and I'll use what's available.",
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
  // The "total" market itself exists today, but not at the EXACT requested
  // line ("Over 1.5" when only 2.5 is offered) - distinct from a generic
  // confidence-floor miss, so the user isn't told their picks were "too
  // weak" when the real issue is simply that line isn't offered.
  if (req.totalLine && !sportsFuture.some((c) => scoped(c, req))) return "line_unavailable";
  if (!matches.length) return "below_quality_floor";
  if (req.pickCount && new Set(matches.map((c) => c.fixtureId)).size < req.pickCount) return "insufficient_count";
  if (req.targetOdds) {
    const priced = matches.filter((c) => c.price >= minPriceFor(req));
    // Reachability is judged on usable (above-floor) legs. When the floor
    // removes everything, fall back to the raw pool so the reason still
    // describes the odds gap honestly instead of a generic failure.
    const usable = priced.length ? priced : matches;
    const perFixtureMin = new Map<string, number>();
    for (const c of usable) perFixtureMin.set(c.fixtureId, Math.min(perFixtureMin.get(c.fixtureId) ?? Infinity, c.price));
    const minimum = [...perFixtureMin.values()].sort((a, b) => a - b).slice(0, req.pickCount ?? 1).reduce((a, b) => a * b, 1);
    if (minimum > req.targetOdds * (1 + TOLERANCE)) return "target_too_low";
    const perFixtureMax = new Map<string, number>();
    for (const c of usable) perFixtureMax.set(c.fixtureId, Math.max(perFixtureMax.get(c.fixtureId) ?? 0, c.price));
    const maximum = [...perFixtureMax.values()].sort((a, b) => b - a).slice(0, req.pickCount ?? maxLegsFor(req)).reduce((a, b) => a * b, 1);
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
  out.push({ ...freshRequest(req.maxLegs), sports: new Set(req.sports) });
  out.push(freshRequest(req.maxLegs));
  return out.slice(0, MAX_SUGGESTIONS);
}

/** Format an odds target the way Python's `:g` spec does: 15, never "15.0" or "15g". */
export function formatOddsTarget(target: number): string {
  if (!Number.isFinite(target) || Number.isInteger(target)) return String(target);
  // Round to 6 significant digits (Python's "%g" default) and round-trip through
  // Number so toPrecision's trailing zeros are dropped — without this, float noise
  // from the odds-ladder/beam search (e.g. 2.1000000000000005) renders verbatim in
  // a user-facing label instead of the clean "2.1".
  return String(Number(target.toPrecision(6)));
}

/**
 * ~ custom_recovery.py claimed_count(): the number of picks a recovery
 * button's label promises, or null when it promises none. The all-sports
 * label always names a count ("3 Best Picks · All Sports" — DEFAULT_COUNT
 * when the request has no explicit one) so that count must be exact.
 */
export function claimedCount(req: ParsedRequest): number | null {
  if (req.pickCount) return req.pickCount;
  if (req.targetOdds || sportName(req.sports) || req.preference === "best") return null;
  return DEFAULT_COUNT;
}

function sportName(sports: Set<string>): string {
  return [...sports].map((s) => SPORT_LABEL[s] ?? s).join(" / ");
}

function marketName(markets: Set<string>): string {
  const names: Record<string, string> = {
    result2: "To Win", result3: "Match Result", dc: "Double Chance",
    dnb: "Draw No Bet", btts: "BTTS", total: "Over/Under", handicap: "Handicap",
  };
  return [...markets].map((m) => names[m] ?? m).join(" / ");
}

/** ~ custom_recovery.py label_for(): a grammatically correct label that always
 * matches the request it advertises — odds targets never gain a stray
 * suffix, and a promised pick count is pinned on the request itself (see
 * prepareAlternatives) so the ticket behind the button always matches. */
export function labelFor(req: ParsedRequest): string {
  const icon = req.preference === "safe" ? "🛡️" : req.preference === "strong" ? "⭐" : "🔥";
  const prefix = req.preference[0].toUpperCase() + req.preference.slice(1);
  const sport = sportName(req.sports);
  let label: string;
  if (req.targetOdds) {
    label = `${icon} ${prefix} ${formatOddsTarget(req.targetOdds)} Odds` + (sport ? ` · ${sport}` : "");
    if (req.pickCount) label += ` · ${req.pickCount} Picks`;
  } else if (sport) {
    const firstSport = [...req.sports].sort()[0];
    const sportIcon = SPORT_ICON[firstSport] ?? "🎯";
    label = req.pickCount
      ? `${sportIcon} ${req.pickCount} ${sport} Picks`
      : `${sportIcon} ${prefix} ${sport} Picks`;
    label += ` · ${prefix}`;
  } else {
    label = req.preference === "best" && !req.pickCount
      ? "🌍 Best Across All Sports"
      : `${icon} ${req.pickCount ?? DEFAULT_COUNT} ${prefix} Picks · All Sports`;
  }
  if (req.allowedMarkets.size) label += " · " + marketName(req.allowedMarkets);
  if (req.excludedMarkets.size) label += " · No " + marketName(req.excludedMarkets);
  if (req.timeIntent === "tonight") label += " · Tonight";
  return label;
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
  const seenRequests = new Set<string>();
  for (const raw of alternatives(req, reason)) {
    if (proven.length >= MAX_SUGGESTIONS) break;
    // A label that names a count is a promise of EXACTLY that many picks:
    // pin it on the request, so the search must find that many (a shorter
    // ticket is never prepared) and every later validation — the tap
    // included — checks it. ~ custom_recovery.py prepare().
    let option = raw;
    const claimed = claimedCount(option);
    if (claimed && !option.pickCount) option = { ...option, pickCount: claimed };
    const key = requestKey(option);
    if (seenRequests.has(key)) continue;
    seenRequests.add(key);
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
  // maxLegs is never user-specified - it's a per-call capability (which
  // session mode THIS turn parsed under), so the current turn's value
  // always wins, never a stale one a prior (possibly different-mode) turn
  // left behind.
  const next: ParsedRequest = { ...previous, maxLegs: current.maxLegs };
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
      case "total_line": next.totalLine = current.totalLine; break;
      // ambiguousLine is THIS message's own bare number, always taken fresh
      // (never inherited from a stale previous turn) - respond() is what
      // decides whether previous.totalLine (preserved above, untouched
      // unless "total_line" is also in current.specifiedFields) resolves it.
      case "ambiguous_line": next.ambiguousLine = current.ambiguousLine; break;
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
  getCandidates: (day: string, zone: string) => Promise<{
    payload_json: string; generated_at: string; prediction_date?: string; timezone?: string;
  }[]>;
}

export interface PoolDiagnostics {
  rows: number;
  accepted: number;
  jsonErrors: number;
  rejectedByReason: Record<string, number>;
}

export type LoadedPool = { candidates: Candidate[]; diagnostics: PoolDiagnostics; poolStatus: string; poolGeneratedAt: string };

// Per-isolate cache (bug 13): a Worker isolate can serve several Custom
// Picks requests back to back, each previously re-running getCandidates'
// full-table scan (every candidate row for the day) from scratch. Keyed by
// (day, timezone, pool.generated_at) - the pool row itself is always read
// fresh (cheap, one row) so a NEW generation run (a changed generated_at)
// is never served stale candidates; only the expensive candidates read is
// skipped on a hit. 60s TTL, not unbounded by generated_at alone, since a
// candidate archive row could theoretically be corrected without the
// pool's own generated_at changing.
const POOL_CACHE_TTL_MS = 60_000;
const poolCache = new Map<string, { value: LoadedPool; cachedAtMs: number }>();

/**
 * Test-only escape hatch: a real Worker isolate never needs this (the cache
 * existing for its whole lifetime is the point), but a test process is one
 * long-lived "isolate" across many independent test cases that reuse the
 * same (day, timezone, generated_at) fixture values on purpose — without
 * this, a later test would silently see an earlier test's cached candidates
 * instead of its own freshly-seeded ones.
 */
export function __resetPoolCacheForTests(): void {
  poolCache.clear();
}

/** Exported so a clarification keyboard can be built straight from the pool
 * when parseRequest itself produced no usable `request` (the bare HELP
 * case) — a plain D1 read, same cache, never a provider call. */
export async function loadPool(
  deps: CustomPicksDeps,
  day: string,
  zone: string,
  nowMs: number,
): Promise<LoadedPool | null> {
  const pool = await deps.getPool(day, zone);
  if (!pool) return null;
  const cacheKey = `${day}|${zone}|${pool.generated_at}`;
  const cached = poolCache.get(cacheKey);
  if (cached && nowMs - cached.cachedAtMs < POOL_CACHE_TTL_MS) return cached.value;
  const poolTimeMs = Date.parse(pool.generated_at);
  const rows = await deps.getCandidates(day, zone);
  const candidates: Candidate[] = [];
  const diagnostics: PoolDiagnostics = { rows: rows.length, accepted: 0, jsonErrors: 0, rejectedByReason: {} };
  for (const row of rows) {
    let payload: Record<string, unknown>;
    try {
      payload = JSON.parse(row.payload_json) as Record<string, unknown>;
    } catch {
      diagnostics.jsonErrors += 1; // malformed archive row — skip, never fabricate.
      continue;
    }
    // The archived JSON never carries the pool's scoping columns (see
    // normalizeCandidate's docstring): merge the row's own columns over the
    // payload, exactly as production's get_custom_pick_candidates does.
    // generated_at keeps its fallback-only semantic (see normalizeCandidate).
    const merged: Record<string, unknown> = {
      ...payload,
      prediction_date: row.prediction_date ?? payload.prediction_date,
      timezone: row.timezone ?? payload.timezone,
    };
    const { candidate, reason } = normalizeCandidateWithReason(merged, day, zone, nowMs, poolTimeMs, true, row.generated_at);
    if (candidate) {
      candidates.push(candidate);
      diagnostics.accepted += 1;
    } else {
      diagnostics.rejectedByReason[reason] = (diagnostics.rejectedByReason[reason] ?? 0) + 1;
    }
  }
  const before = candidates.length;
  const deduped = dropConflictingIdentities(candidates);
  const dropped = before - deduped.length;
  if (dropped) diagnostics.rejectedByReason["conflicting_identity"] = (diagnostics.rejectedByReason["conflicting_identity"] ?? 0) + dropped;
  const result: LoadedPool = { candidates: deduped, diagnostics, poolStatus: pool.status, poolGeneratedAt: pool.generated_at };
  // Bound the cache to live entries only — isolates are short-lived, but an
  // isolate that happens to serve several different (day, timezone) combos
  // (e.g. spanning a Lagos midnight rollover) must not accumulate entries
  // forever within its own lifetime.
  for (const [k, v] of poolCache) {
    if (nowMs - v.cachedAtMs >= POOL_CACHE_TTL_MS) poolCache.delete(k);
  }
  poolCache.set(cacheKey, { value: result, cachedAtMs: nowMs });
  return result;
}

export interface RespondResult {
  text: string;
  reason: string;
  suggestions: Suggestion[];
}

export interface RespondOptions {
  /** Pro Picks leg cap (MAX_LEGS_PRO) for an admin Pro session; omitted/undefined uses the free-tier MAX_LEGS default. */
  maxLegs?: number;
  /** Pro Picks only: forces the "pro" confidence floor (MIN_PROBABILITY.pro,
   * stricter than Bet of the Day's own floor — see MIN_PROBABILITY's
   * comment) regardless of whatever preference word the text itself
   * contains. Never set for a free session. */
  preference?: "pro";
}

export async function respond(
  deps: CustomPicksDeps,
  text: string,
  zone: string,
  day: string,
  nowMs: number,
  previous: ParsedRequest | null,
  userId = "",
  enabled: Set<string> = DEFAULT_ENABLED_SPORTS,
  options: RespondOptions = {},
): Promise<{ result: RespondResult; request: ParsedRequest | null }> {
  const parsed = parseRequest(text, enabled, options.maxLegs);
  if (!parsed.ok) return { result: { text: parsed.text, reason: parsed.reason, suggestions: [] }, request: null };
  let request = parsed.request;
  if (request.refinement && previous) request = refine(previous, request);
  if (options.preference) request.preference = options.preference;

  // A bare "1.5 options" names no side - ask, unless the conversation
  // already resolved THIS exact number (request.totalLine, carried
  // forward by refine() above, matches it). Resolved before touching the
  // pool: this is a pure clarification, never a search.
  if (request.ambiguousLine != null) {
    const resolved = request.totalLine != null && request.totalLine.line === request.ambiguousLine;
    if (!resolved) {
      const line = request.ambiguousLine;
      return {
        result: {
          text: `🤔 Over or under?\n\nDo you mean Over ${line} or Under ${line}?`,
          reason: "ambiguous_line",
          suggestions: [],
        },
        request,
      };
    }
  }

  return searchWithRequest(deps, request, text, zone, day, nowMs, userId);
}

/**
 * The second half of respond(): given an already-resolved ParsedRequest
 * (either freshly parsed+refined text, or one a clarification BUTTON built
 * directly — see index.ts's "cq|z|" actions), loads the pool and searches.
 * Split out so a button action that isn't literally "replay this text"
 * (e.g. "drop the stuck exact line but keep the market") can still reuse
 * the exact same search/failure-reason/alternatives tail respond() uses,
 * instead of re-deriving it through parseRequest.
 */
/** "Any available line" (line_unavailable keyboard): clears the stuck exact
 * line while KEEPING the total market, bypassing parseRequest/refine
 * entirely — no typed phrase can express "drop just this one field" (see
 * refine()'s per-field merge), so this is a direct, structured request edit
 * instead of a text replay. */
export function clearTotalLine(request: ParsedRequest): ParsedRequest {
  return { ...request, totalLine: null, allowedMarkets: new Set(["total"]) };
}

export async function searchWithRequest(
  deps: CustomPicksDeps,
  request: ParsedRequest,
  text: string,
  zone: string,
  day: string,
  nowMs: number,
  userId = "",
): Promise<{ result: RespondResult; request: ParsedRequest | null }> {
  const loaded = await loadPool(deps, day, zone, nowMs);
  if (loaded === null) return { result: { text: UNAVAILABLE, reason: "archive_unavailable", suggestions: [] }, request };
  const { candidates: pool, diagnostics } = loaded;
  // Team names mentioned in THIS turn's text win outright (a fresh mention
  // always means the user is naming a team right now); otherwise whatever
  // refine() already carried over from the previous turn stands.
  const mentionedTeams = matchTeamsInPool(text, pool);
  if (mentionedTeams.size) request.teams = mentionedTeams;

  const matches = eligible(pool, request, nowMs);
  const options = matches.length ? shortlist(matches, request) : [];
  const ticket = chooseTicket(options, userId, day, request);
  // Operational diagnostics only: pool/request shapes and funnel counts.
  // Never the user's raw text, identity, or any secret.
  console.log(JSON.stringify({
    event: "custom_picks",
    day,
    zone,
    poolStatus: loaded.poolStatus,
    rows: diagnostics.rows,
    accepted: diagnostics.accepted,
    jsonErrors: diagnostics.jsonErrors,
    rejectedByReason: diagnostics.rejectedByReason,
    fixtures: new Set(pool.map((c) => c.fixtureId)).size,
    targetOdds: request.targetOdds,
    pickCount: request.pickCount,
    sports: request.sports.size,
    allowedMarkets: request.allowedMarkets.size,
    excludedMarkets: request.excludedMarkets.size,
    preference: request.preference,
    matches: matches.length,
    matchFixtures: new Set(matches.map((c) => c.fixtureId)).size,
  }));
  if (validTicket(ticket, request, nowMs)) {
    return { result: { text: formatTicket(ticket!), reason: "success", suggestions: [] }, request };
  }
  const reason = failureReason(pool, request, nowMs, matches);
  const suggestions = prepareAlternatives(pool, request, reason, nowMs);
  const copy = COPY[reason] ?? COPY.search_failed;
  const text2 = copy + (suggestions.length ? "\n\nTry these instead:" : "");
  return { result: { text: text2, reason, suggestions }, request };
}
