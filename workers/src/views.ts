import { restrictedView, welcomeMessage } from "./telegram.ts";
// View rendering, ported to match predbot/bot/pagination.py + format.py
// (production) rather than the earlier ad-hoc Stage 1 format. Reads
// D1-cached picks only — never calls a sports provider. See
// workers/PRODUCTION_PARITY.md for the full production-vs-staging
// comparison and the gaps intentionally not ported in this pass.

import {
  getAllPicksForCollection,
  getDayCache,
  getFirstPickDate,
  getItemizedStatsRowsForDate,
  getRecapStatuses,
  getStatsAggregateForRange,
  getStatsRowsForDate,
  getStatsRowsForDateByCollection,
  getStatsRowsForRange,
  hasPicksForDate,
  type Env,
  type CachedPick,
} from "./db.ts";
import {
  allButtonModeFor,
  buildKeyboard,
  menuKeyboard,
  sportsBackRow,
  topEmptyKeyboard,
  type AllButtonMode,
} from "./telegram.ts";
import {
  ALL_TITLE,
  DAILY_TITLE,
  NO_RESULTS_YET,
  SPORT_ICON,
  SPORT_LABEL,
  TOP_TITLE,
  ZONE_LABEL,
  OUTCOME_MARK,
  bySportLines,
  compactRecapText,
  computeStatsBlock,
  dayLabel,
  escHtml,
  fixtureBlock,
  hitRate,
  lastDaysBlock,
  lastDaysLines,
  nPicks,
  outcomeOf,
  picksCard,
  shiftDate,
  sportsDayBlock,
  statsBlockFromAggregate,
  yesterdayLines,
  type PickLineInput,
} from "./format.ts";

export const LAGOS_OFFSET_MINUTES = 60; // Africa/Lagos is UTC+1 year-round (no DST)

/**
 * Interactive page size, from PAGE_SIZE (wrangler.toml) with production's
 * PAGE_SIZE_DEFAULT as the fallback. Clamped to the same 1–25 range the D1
 * reads use so a misconfigured value can neither unpage nor explode.
 */
export function picksPerPage(env: { PAGE_SIZE?: string }): number {
  const n = Number.parseInt(env.PAGE_SIZE ?? "", 10);
  if (!Number.isFinite(n) || n < 1) return 5; // predbot/bot/pagination.py PAGE_SIZE_DEFAULT
  return Math.min(n, 25);
}

export function lagosDateString(nowMs = Date.now()): string {
  const lagos = new Date(nowMs + LAGOS_OFFSET_MINUTES * 60_000);
  const y = lagos.getUTCFullYear();
  const m = String(lagos.getUTCMonth() + 1).padStart(2, "0");
  const d = String(lagos.getUTCDate()).padStart(2, "0");
  return `${y}-${m}-${d}`;
}

export function lagosDayBounds(localDate: string): { startUtcMs: number; endUtcMs: number } {
  // localDate YYYY-MM-DD in Lagos; window runs to local midnight + 3h
  // (matches DAY_WINDOW_END_OFFSET_HOURS=3).
  const [y, m, d] = localDate.split("-").map(Number);
  const startUtcMs = Date.UTC(y, (m as number) - 1, d) - LAGOS_OFFSET_MINUTES * 60_000;
  const endUtcMs = startUtcMs + 27 * 3_600_000;
  return { startUtcMs, endUtcMs };
}

const VIEW_TITLE: Record<string, string> = {
  all: ALL_TITLE,
  top: TOP_TITLE,
  daily: DAILY_TITLE,
  stats: "📊 STATS",
  yesterday: "🗓 YESTERDAY",
};

// Matches predbot/bot/pagination.py empty_view_message's "awaiting scheduled
// fetch" copy exactly (UI-parity review, 2026-10-15) — the previous text
// here ("Generation runs at 09:00...", "No provider was queried...") leaked
// internal scheduling/provider details the Python bot's own user-facing
// text never mentions. Ordinary users only ever see this friendly line;
// cache/provider/generation state stays in logs and admin tooling only.
export function notReadyText(view: string, localDate: string): string {
  const title = VIEW_TITLE[view] ?? "📋 PICKS";
  return `<b>${title}</b> — ${localDate}\n\n` + "📋 Today's predictions have not been generated yet.";
}

/** Bug 12: a PAST day genuinely has nothing recorded — distinct from notReadyText's "not generated YET" (today only). */
export function noDataForDateText(view: string, localDate: string): string {
  const title = VIEW_TITLE[view] ?? "📋 PICKS";
  return `<b>${title}</b> — ${localDate}\n\n` + "📋 No predictions were recorded for this date.";
}

function toPickLineInput(p: CachedPick): PickLineInput {
  return { sport: p.sport, league: p.league, home: p.home, away: p.away, label: p.label, price: p.price, prob: p.prob, startUtc: p.start_utc };
}

function totalPages(count: number, size: number): number {
  return count <= 0 ? 1 : Math.ceil(count / size);
}

function clampPage(page: number, count: number, size: number): number {
  return Math.min(Math.max(0, page), totalPages(count, size) - 1);
}

/**
 * ~ predbot/bot/broadcast.py picks_messages/summary_card: the automatic
 * group post is the WHOLE ticket in one message, never paginated — unlike
 * the interactive /top /daily views (renderTicket below), which page at
 * PICKS_PER_PAGE. Reusing renderTicket's paginated output here would have
 * silently posted only the first page of a Daily ticket with more than
 * PICKS_PER_PAGE legs to the group, with a "Page 1 of N" footer that
 * production's automatic post never has.
 */
export async function renderGroupCard(
  env: Env,
  kind: "top" | "daily",
  localDate: string,
): Promise<{ text: string; isEmpty: boolean }> {
  const all = await getAllPicksForCollection(env.DB, localDate, kind);
  const title = kind === "top" ? TOP_TITLE : DAILY_TITLE;
  if (all.length === 0) {
    return { text: wrapTitleHtml(picksCard([], localDate, title, { zone: ZONE_LABEL }), title), isEmpty: true };
  }
  const sorted = all
    .map(toPickLineInput)
    .sort((a, b) => new Date(a.startUtc).getTime() - new Date(b.startUtc).getTime());
  const body = picksCard(sorted, localDate, title, { zone: ZONE_LABEL });
  return { text: wrapTitleHtml(body, title), isEmpty: false };
}

async function renderTicket(
  env: Env,
  view: "top" | "daily",
  page: number,
  localDate: string,
  allMode: AllButtonMode,
): Promise<{ text: string; replyMarkup: Record<string, unknown> }> {
  const all = await getAllPicksForCollection(env.DB, localDate, view);
  const title = view === "top" ? TOP_TITLE : DAILY_TITLE;
  if (all.length === 0) {
    const body = picksCard([], localDate, title, { zone: ZONE_LABEL });
    // predbot/bot/callbacks.py open_view: the Custom Picks deep-link button
    // only replaces the normal keyboard for a genuinely-empty Top (evaluated,
    // zero qualifying picks) — Daily's empty state keeps the plain pager/
    // switch keyboard. If the bot username isn't configured, production
    // would refuse to show a dead link; here that means falling back to the
    // plain keyboard rather than omitting the card (never leaves /top
    // silently unanswered).
    const keyboard =
      view === "top" && env.TELEGRAM_BOT_USERNAME
        ? topEmptyKeyboard(env.TELEGRAM_BOT_USERNAME, allMode)
        : buildKeyboard(view, 0, 1, localDate, [], allMode);
    return { text: wrapTitleHtml(body, title), replyMarkup: keyboard };
  }
  const pageSize = picksPerPage(env);
  const pages = totalPages(all.length, pageSize);
  const safePage = clampPage(page, all.length, pageSize);
  // The combined-odds/pick-count header must reflect the WHOLE ticket, not
  // just this page (predbot/bot/pagination.py passes total_prices for the
  // same reason). picksCard() sorts by kickoff internally for the header's
  // own ordering guarantee; sort here too so the slice matches the page the
  // keyboard claims to show.
  const sorted = all
    .map(toPickLineInput)
    .sort((a, b) => new Date(a.startUtc).getTime() - new Date(b.startUtc).getTime());
  const slice = sorted.slice(safePage * pageSize, (safePage + 1) * pageSize);
  const body = picksCard(slice, localDate, title, {
    totalPrices: all.map((p) => p.price),
    zone: ZONE_LABEL,
    page: safePage + 1,
    pages,
  });
  return { text: wrapTitleHtml(body, title), replyMarkup: buildKeyboard(view, safePage, pages, localDate, [], allMode) };
}

// picksCard() returns plain text with the title as its first line's leading
// segment (e.g. "🔥 BET OF THE DAY · Sat 19 Sep"); Telegram HTML needs that title
// bolded like every other view, without bolding the rest of the line.
function wrapTitleHtml(body: string, title: string): string {
  if (body.startsWith(title)) {
    return `<b>${title}</b>${body.slice(title.length)}`;
  }
  return body;
}

async function renderAllSportMenu(
  env: Env,
  localDate: string,
  allMode: AllButtonMode,
): Promise<{ text: string; replyMarkup: Record<string, unknown> }> {
  const rows = await getAllPicksForCollection(env.DB, localDate, "all");
  if (rows.length === 0) {
    // Matches predbot/bot/pagination.py empty_view_message's genuine-empty
    // case exactly (UI-parity review, 2026-10-15) — the previous text here
    // ("the model found nothing above the quality floor", "thresholds were
    // not lowered") invented new copy that exposed internal selection-
    // threshold concepts no Python-bot user ever saw.
    return {
      text: `<b>${ALL_TITLE}</b> — ${localDate}\n\nNo valid model predictions were generated today.`,
      replyMarkup: buildKeyboard("all", 0, 1, localDate, [], allMode),
    };
  }
  const bySport = new Map<string, { count: number; fixtures: Set<string> }>();
  for (const r of rows) {
    const key = `${r.home}|${r.away}|${r.start_utc}`;
    const bucket = bySport.get(r.sport) ?? { count: 0, fixtures: new Set<string>() };
    bucket.count += 1;
    bucket.fixtures.add(key);
    bySport.set(r.sport, bucket);
  }
  const totalFixtures = new Set(rows.map((r) => `${r.home}|${r.away}|${r.start_utc}`)).size;
  const lines = [
    `<b>${ALL_TITLE}</b> — ${dayLabel(localDate)}`,
    `${nPicks(rows.length).replace("pick", "prediction")} · ${totalFixtures} ${totalFixtures === 1 ? "match" : "matches"}`,
    "",
    "Pick a sport:",
  ];
  const sports = [...bySport.keys()].sort((a, b) => bySport.get(b)!.count - bySport.get(a)!.count || a.localeCompare(b));
  const buttonRows: { text: string; callback_data: string }[][] = [];
  for (let i = 0; i < sports.length; i += 2) {
    const chunk = sports.slice(i, i + 2).map((s) => {
      const b = bySport.get(s)!;
      const icon = SPORT_ICON[s] ?? SPORT_ICON.other;
      const label = SPORT_LABEL[s] ?? s;
      return {
        text: `${icon} ${label} (${b.count} · ${b.fixtures.size} ${b.fixtures.size === 1 ? "match" : "matches"})`,
        callback_data: `pg|all_${s}|0|${localDate.replace(/-/g, "")}`,
      };
    });
    buttonRows.push(chunk);
  }
  return {
    text: lines.join("\n"),
    replyMarkup: { inline_keyboard: [...buttonRows, ...(menuKeyboard(allMode).inline_keyboard as unknown[][])] },
  };
}

async function renderAllSportPages(
  env: Env,
  sport: string,
  page: number,
  localDate: string,
  allMode: AllButtonMode,
): Promise<{ text: string; replyMarkup: Record<string, unknown> }> {
  const all = (await getAllPicksForCollection(env.DB, localDate, "all")).filter((r) => r.sport === sport);
  const view = `all_${sport}`;
  if (all.length === 0) {
    return {
      text: `<b>${ALL_TITLE}</b> — ${localDate}\n\nNo ${SPORT_LABEL[sport] ?? sport} predictions today.`,
      replyMarkup: { inline_keyboard: [sportsBackRow(localDate), ...(menuKeyboard(allMode).inline_keyboard as unknown[][])] },
    };
  }
  const groups = new Map<string, CachedPick[]>();
  for (const r of all) {
    const key = `${r.home}|${r.away}|${r.start_utc}`;
    const g = groups.get(key) ?? [];
    g.push(r);
    groups.set(key, g);
  }
  const fixtures = [...groups.values()].sort(
    (a, b) => new Date(a[0].start_utc).getTime() - new Date(b[0].start_utc).getTime(),
  );
  const pageSize = picksPerPage(env);
  const pages = totalPages(fixtures.length, pageSize);
  const safePage = clampPage(page, fixtures.length, pageSize);
  const pageFixtures = fixtures.slice(safePage * pageSize, (safePage + 1) * pageSize);
  const matches = fixtures.length;
  const header = [
    `${ALL_TITLE} · ${dayLabel(localDate)}`,
    `${SPORT_ICON[sport] ?? SPORT_ICON.other} ${SPORT_LABEL[sport] ?? sport} · ${nPicks(all.length).replace("pick", "prediction")} · ${matches} ${matches === 1 ? "match" : "matches"} · 🕘 times in ${ZONE_LABEL}`,
  ];
  const blocks = pageFixtures.map((rows) => fixtureBlock(rows.map(toPickLineInput)).join("\n"));
  const lines = [...header, ...blocks.flatMap((b) => ["", b])];
  if (pages > 1) lines.push("", `Page ${safePage + 1} of ${pages}`);
  const body = lines.join("\n");
  const keyboard = buildKeyboard(view, safePage, pages, localDate, [sportsBackRow(localDate)], allMode);
  return { text: wrapTitleHtml(body, ALL_TITLE), replyMarkup: keyboard };
}

/** ~ production's itemized /yesterday: one ✅/❌/➖/↩️/❔ line per leg, Top legs before Daily. */
function itemizedYesterdayLines(rows: { tier: string; home: string; away: string; label: string; status: string; result_json: string | null }[]): string[] {
  const pending = rows.filter((r) => outcomeOf(r.status, r.result_json) === null);
  const settled = rows.filter((r) => outcomeOf(r.status, r.result_json) !== null);
  const lines: string[] = [];
  let currentTier: string | null = null;
  for (const r of settled) {
    if (r.tier !== currentTier) {
      currentTier = r.tier;
      lines.push(currentTier === "top" ? "🔥 Top" : "📋 Daily");
    }
    const mark = OUTCOME_MARK[outcomeOf(r.status, r.result_json) as string];
    lines.push(`${mark} ${escHtml(r.home)} vs ${escHtml(r.away)} — ${escHtml(r.label)}`);
  }
  if (pending.length) lines.push("", `⏳ ${pending.length} leg${pending.length === 1 ? "" : "s"} still pending`);
  return lines;
}

/**
 * 30-day / all-time blocks (~ predbot/jobs/stats.py compute()'s `last30`/
 * `all` — same `_block()` shape as the yesterday card, just over a wider
 * range). Appended only to /stats, not /yesterday (which gets the itemized
 * per-leg list instead) or the collection-specific views (kept to their
 * single aggregate for now — see PRODUCTION_PARITY.md).
 */
function historicalLines(label: string, block: ReturnType<typeof computeStatsBlock>): string[] {
  if (block.n === 0 && block.won + block.lost + block.push + block.void + block.unresolved === 0) return [];
  const rate = hitRate(block);
  const parts = [countsLineSafe(block)];
  if (rate !== null) parts.push(`🎯 ${Math.round(rate)}%`);
  if (block.n) parts.push(`💹 ROI ${block.roi >= 0 ? "+" : ""}${block.roi.toFixed(0)}%`);
  return [label, parts.join("  ·  ")];
}
function countsLineSafe(b: ReturnType<typeof computeStatsBlock>): string {
  const parts = [`✅ ${b.won}`, `❌ ${b.lost}`, `➖ ${b.void}`];
  if (b.push) parts.push(`↩️ ${b.push}`);
  return parts.join("  ");
}

async function renderStatsLike(
  env: Env,
  title: string,
  localDate: string,
  kind: "stats" | "yesterday" | "top" | "daily" = "stats",
  allMode: AllButtonMode = "callback",
): Promise<{ text: string; replyMarkup: Record<string, unknown> }> {
  const yesterday = shiftDate(localDate, -1);
  const rows = kind === "top" || kind === "daily" ? await getStatsRowsForDateByCollection(env.DB, yesterday, kind) : await getStatsRowsForDate(env.DB, yesterday);
  if (rows.length === 0) {
    return { text: `<b>${title}</b>\n\n${NO_RESULTS_YET}`, replyMarkup: menuKeyboard(allMode) };
  }
  const block = computeStatsBlock(rows);
  const issued = rows.length;
  const settledYesterday = block.won + block.lost + block.push + block.void + block.unresolved;

  const blocks: string[][] = [yesterdayLines(yesterday, block, issued).map(escHtml)];

  if (kind === "yesterday") {
    const itemized = await getItemizedStatsRowsForDate(env.DB, yesterday);
    const itemLines = itemizedYesterdayLines(itemized);
    if (itemLines.length) blocks.push(itemLines); // already escHtml'd per-field inside
  }

  if (kind === "stats" || kind === "yesterday") {
    // ~ predbot/jobs/stats.py compute(): 7-day window capped to how long the
    // engine has actually been running (never claims "Last 7 days" on day 2).
    const since7 = shiftDate(localDate, -7);
    const [rows7, firstPickDate] = await Promise.all([
      getStatsRowsForRange(env.DB, since7, localDate),
      getFirstPickDate(env.DB),
    ]);
    let windowDays = 0;
    if (firstPickDate) {
      const days = Math.round((Date.parse(localDate) - Date.parse(firstPickDate)) / 86_400_000);
      windowDays = Math.max(0, Math.min(7, days));
    }
    if (settledYesterday > 0) {
      const sportRows = rows7.filter((r) => r.pick_date === yesterday);
      const sportLines = bySportLines(sportsDayBlock(sportRows));
      if (sportLines.length) blocks.push(sportLines.map(escHtml));
    }
    const sevenBlock = lastDaysBlock(rows7, windowDays);
    const sevenLines = lastDaysLines(sevenBlock);
    if (sevenLines.length) blocks.push(sevenLines.map(escHtml));
  }

  if (kind === "stats") {
    const since30 = shiftDate(localDate, -30);
    const [rows30, firstPickDate] = await Promise.all([
      getStatsRowsForRange(env.DB, since30, localDate),
      getFirstPickDate(env.DB),
    ]);
    const thirtyLines = historicalLines("📈 Last 30 days", computeStatsBlock(rows30));
    if (thirtyLines.length) blocks.push(thirtyLines.map(escHtml));
    if (firstPickDate) {
      // Bug 13: unlike Last 7/30 days (already bounded reads), "All time"
      // used to read EVERY settled Top/Daily row ever via getStatsRowsForRange
      // just to feed computeStatsBlock - an unbounded, ever-growing read.
      // getStatsAggregateForRange computes the identical won/lost/push/void/
      // unresolved/profit/bestWin in SQL instead (two point queries).
      const agg = await getStatsAggregateForRange(env.DB, firstPickDate, shiftDate(localDate, 1));
      const allLines = historicalLines("📈 All time", statsBlockFromAggregate(agg));
      if (allLines.length) blocks.push(allLines.map(escHtml));
    }
  }

  const body = [`<b>${title}</b> · ${dayLabel(localDate)}`, "", ...blocks.flatMap((b, i) => (i === 0 ? b : ["", ...b]))].join(
    "\n",
  );
  return { text: body, replyMarkup: menuKeyboard(allMode) };
}

/**
 * The automatic group recap — predbot/jobs/recap.py's recap_message() uses
 * "📊 RESULTS" as its title (_results_card("📊 RESULTS", s,
 * describes_yesterday=True)), distinct from the interactive /yesterday
 * button's "🗓 YESTERDAY" card, even though both describe the same day's
 * results. Fixed (UI-parity audit, 2026-10-15): the automatic post used to
 * reuse the interactive view's title verbatim.
 */
export async function renderGroupRecap(env: Env, localDate: string): Promise<{ text: string }> {
  // Compact marks-only recap of YESTERDAY's published Top and Daily picks.
  // Membership-scoped queries: an All-only prediction can never appear.
  const yesterday = shiftDate(localDate, -1);
  const [top, daily] = await Promise.all([
    getRecapStatuses(env.DB, yesterday, "top"),
    getRecapStatuses(env.DB, yesterday, "daily"),
  ]);
  return { text: compactRecapText(top, daily) ?? `<b>📊 SUREODDS RESULTS</b>\n\n${NO_RESULTS_YET}` };
}

export async function renderView(
  env: Env,
  view: string,
  page: number,
  day: string | null,
  chatType = "private",
  admin = false,
): Promise<{ text: string; replyMarkup: Record<string, unknown> }> {
  // WHY: every stats entry path, including old callbacks, fails closed here.
  if (restrictedView(view) && !admin) return { text: welcomeMessage('there'), replyMarkup: menuKeyboard() };
  const timezone = env.TIMEZONE ?? "Africa/Lagos";
  const localDate = day ?? lagosDateString();
  const allMode = allButtonModeFor(chatType, env.TELEGRAM_BOT_USERNAME, admin);

  // Stats/Yesterday read PAST, already-settled dates (yesterday + rolling
  // windows) from their own tables (picks/prediction_memberships/stats
  // rows) — never today's prediction_cache. Gating them on today's cache
  // being COMPLETE (as every other view correctly is) meant a slow/missed
  // generation for TODAY made yesterday's already-settled results
  // unreachable too, even though nothing about them was actually pending.
  // renderStatsLike() already answers its own genuinely-empty case
  // (NO_RESULTS_YET) honestly — it never fabricates data.
  if (view === "stats") return renderStatsLike(env, "📊 STATS", localDate, "stats", allMode);
  if (view === "yesterday") return renderStatsLike(env, "🗓 YESTERDAY", localDate, "yesterday", allMode);
  if (view === "stats_top") return renderStatsLike(env, "🔥 TOP STATS", localDate, "top", allMode);
  if (view === "stats_daily") return renderStatsLike(env, "📋 DAILY STATS", localDate, "daily", allMode);

  // Today requires a COMPLETE prediction_cache row (the actual readiness
  // signal: generation may still be running). A PAST day instead checks
  // for picks/prediction_memberships rows directly (bug 12): its cache row
  // is retention-pruned after a few days even though the picks themselves
  // never are, so gating a page-turn on an old message's cache row the same
  // way went dark on perfectly good historical data once that row expired.
  const isToday = localDate === lagosDateString();
  const ready = isToday
    ? (await getDayCache(env.DB, localDate, timezone))?.status === "COMPLETE"
    : await hasPicksForDate(env.DB, localDate);
  if (!ready) {
    const keyboard = view === "all" || view.startsWith("all_") ? menuKeyboard(allMode) : buildKeyboard(view, 0, 1, localDate, [], allMode);
    const baseView = view.startsWith("all_") ? "all" : view;
    const text = isToday ? notReadyText(baseView, localDate) : noDataForDateText(baseView, localDate);
    return { text, replyMarkup: keyboard };
  }

  if (view === "top" || view === "daily") return renderTicket(env, view, page, localDate, allMode);

  if (view === "all") return renderAllSportMenu(env, localDate, allMode);
  if (view.startsWith("all_")) return renderAllSportPages(env, view.slice(4), page, localDate, allMode);

  // Unroutable collection falls back to /all's current day, matching the
  // previous Stage 1 default.
  return renderAllSportMenu(env, localDate, allMode);
}


export async function renderRecapStats(env: Env, day: string): Promise<string> {
  // WHY: reuse the existing aggregate and formatter without rendering a user-facing stats route.
  const [yesterday, seven] = await Promise.all([
    getStatsAggregateForRange(env.DB,shiftDate(day,-1),day),
    getStatsAggregateForRange(env.DB,shiftDate(day,-7),day),
  ]);
  const block = (label: string, aggregate: typeof yesterday) => {
    const lines = historicalLines(label, statsBlockFromAggregate(aggregate));
    // WHY: a forced recap with pending legs must still identify both statistics windows.
    return lines.length ? lines : [label, 'No settled results yet.'];
  };
  return [...block('Yesterday', yesterday), '', ...block('Last 7 days', seven)].map(escHtml).join('\n');
}
