// View rendering, ported to match predbot/bot/pagination.py + format.py
// (production) rather than the earlier ad-hoc Stage 1 format. Reads
// D1-cached picks only — never calls a sports provider. See
// workers/PRODUCTION_PARITY.md for the full production-vs-staging
// comparison and the gaps intentionally not ported in this pass.

import { getAllPicksForCollection, getDayCache, getStatsRowsForDate, type Env, type CachedPick } from "./db.ts";
import {
  buildKeyboard,
  menuKeyboard,
  sportsBackRow,
  topEmptyKeyboard,
} from "./telegram.ts";
import {
  ALL_TITLE,
  DAILY_TITLE,
  NO_RESULTS_YET,
  SPORT_ICON,
  SPORT_LABEL,
  TOP_TITLE,
  ZONE_LABEL,
  computeStatsBlock,
  dayLabel,
  escHtml,
  fixtureBlock,
  nPicks,
  picksCard,
  shiftDate,
  yesterdayLines,
  type PickLineInput,
} from "./format.ts";

export const LAGOS_OFFSET_MINUTES = 60; // Africa/Lagos is UTC+1 year-round (no DST)
const PICKS_PER_PAGE = 5; // predbot/bot/pagination.py PAGE_SIZE_DEFAULT

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

export function notReadyText(view: string, localDate: string): string {
  const title = VIEW_TITLE[view] ?? "📋 PICKS";
  return (
    `<b>${title}</b> — ${localDate}\n\n` +
    `Today's picks are not ready yet. Generation runs at <b>09:00 Africa/Lagos</b>; ` +
    `subscriber delivery follows at <b>09:30</b>.\n\n` +
    `No provider was queried for this response — please check back after generation.`
  );
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

async function renderTicket(
  env: Env,
  view: "top" | "daily",
  page: number,
  localDate: string,
): Promise<{ text: string; replyMarkup: Record<string, unknown> }> {
  const all = await getAllPicksForCollection(env.DB, localDate, view);
  const title = view === "top" ? TOP_TITLE : DAILY_TITLE;
  if (all.length === 0) {
    const body = picksCard([], localDate, title, { zone: ZONE_LABEL });
    const keyboard = view === "top" ? topEmptyKeyboard() : buildKeyboard(view, 0, 1, localDate);
    return { text: wrapTitleHtml(body, title), replyMarkup: keyboard };
  }
  const pages = totalPages(all.length, PICKS_PER_PAGE);
  const safePage = clampPage(page, all.length, PICKS_PER_PAGE);
  // The combined-odds/pick-count header must reflect the WHOLE ticket, not
  // just this page (predbot/bot/pagination.py passes total_prices for the
  // same reason). picksCard() sorts by kickoff internally for the header's
  // own ordering guarantee; sort here too so the slice matches the page the
  // keyboard claims to show.
  const sorted = all
    .map(toPickLineInput)
    .sort((a, b) => new Date(a.startUtc).getTime() - new Date(b.startUtc).getTime());
  const slice = sorted.slice(safePage * PICKS_PER_PAGE, (safePage + 1) * PICKS_PER_PAGE);
  const body = picksCard(slice, localDate, title, {
    totalPrices: all.map((p) => p.price),
    zone: ZONE_LABEL,
    page: safePage + 1,
    pages,
  });
  return { text: wrapTitleHtml(body, title), replyMarkup: buildKeyboard(view, safePage, pages, localDate) };
}

// picksCard() returns plain text with the title as its first line's leading
// segment (e.g. "🔥 TOP PICKS · Sat 19 Sep"); Telegram HTML needs that title
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
): Promise<{ text: string; replyMarkup: Record<string, unknown> }> {
  const rows = await getAllPicksForCollection(env.DB, localDate, "all");
  if (rows.length === 0) {
    return {
      text: `<b>${ALL_TITLE}</b> — ${localDate}\n\n` +
        `No qualified all picks for ${localDate} — the model found nothing above the quality floor. ` +
        `This is an honest empty, not an error; thresholds were not lowered to fill the card.`,
      replyMarkup: buildKeyboard("all", 0, 1, localDate),
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
    replyMarkup: { inline_keyboard: [...buttonRows, ...(menuKeyboard().inline_keyboard as unknown[][])] },
  };
}

async function renderAllSportPages(
  env: Env,
  sport: string,
  page: number,
  localDate: string,
): Promise<{ text: string; replyMarkup: Record<string, unknown> }> {
  const all = (await getAllPicksForCollection(env.DB, localDate, "all")).filter((r) => r.sport === sport);
  const view = `all_${sport}`;
  if (all.length === 0) {
    return {
      text: `<b>${ALL_TITLE}</b> — ${localDate}\n\nNo ${SPORT_LABEL[sport] ?? sport} predictions today.`,
      replyMarkup: { inline_keyboard: [sportsBackRow(localDate), ...(menuKeyboard().inline_keyboard as unknown[][])] },
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
  const pages = totalPages(fixtures.length, PICKS_PER_PAGE);
  const safePage = clampPage(page, fixtures.length, PICKS_PER_PAGE);
  const pageFixtures = fixtures.slice(safePage * PICKS_PER_PAGE, (safePage + 1) * PICKS_PER_PAGE);
  const matches = fixtures.length;
  const header = [
    `${ALL_TITLE} · ${dayLabel(localDate)}`,
    `${SPORT_ICON[sport] ?? SPORT_ICON.other} ${SPORT_LABEL[sport] ?? sport} · ${nPicks(all.length).replace("pick", "prediction")} · ${matches} ${matches === 1 ? "match" : "matches"} · 🕘 times in ${ZONE_LABEL}`,
  ];
  const blocks = pageFixtures.map((rows) => fixtureBlock(rows.map(toPickLineInput)).join("\n"));
  const lines = [...header, ...blocks.flatMap((b) => ["", b])];
  if (pages > 1) lines.push("", `Page ${safePage + 1} of ${pages}`);
  const body = lines.join("\n");
  const keyboard = buildKeyboard(view, safePage, pages, localDate, [sportsBackRow(localDate)]);
  return { text: wrapTitleHtml(body, ALL_TITLE), replyMarkup: keyboard };
}

async function renderStatsLike(
  env: Env,
  title: string,
  localDate: string,
): Promise<{ text: string; replyMarkup: Record<string, unknown> }> {
  const yesterday = shiftDate(localDate, -1);
  const rows = await getStatsRowsForDate(env.DB, yesterday);
  if (rows.length === 0) {
    return { text: `<b>${title}</b>\n\n${NO_RESULTS_YET}`, replyMarkup: menuKeyboard() };
  }
  const block = computeStatsBlock(rows);
  const issued = rows.length;
  const body = [
    `<b>${title}</b> · ${dayLabel(localDate)}`,
    "",
    ...yesterdayLines(yesterday, block, issued).map(escHtml),
  ].join("\n");
  return { text: body, replyMarkup: menuKeyboard() };
}

export async function renderView(
  env: Env,
  view: string,
  page: number,
  day: string | null,
): Promise<{ text: string; replyMarkup: Record<string, unknown> }> {
  const timezone = env.TIMEZONE ?? "Africa/Lagos";
  const localDate = day ?? lagosDateString();
  const cache = await getDayCache(env.DB, localDate, timezone);

  if (!cache || cache.status !== "COMPLETE") {
    const keyboard = view === "all" || view.startsWith("all_") ? menuKeyboard() : buildKeyboard(view, 0, 1, localDate);
    return { text: notReadyText(view.startsWith("all_") ? "all" : view, localDate), replyMarkup: keyboard };
  }

  if (view === "stats") return renderStatsLike(env, "📊 STATS", localDate);
  if (view === "yesterday") return renderStatsLike(env, "🗓 YESTERDAY", localDate);

  if (view === "top" || view === "daily") return renderTicket(env, view, page, localDate);

  if (view === "all") return renderAllSportMenu(env, localDate);
  if (view.startsWith("all_")) return renderAllSportPages(env, view.slice(4), page, localDate);

  // Unroutable collection falls back to /all's current day, matching the
  // previous Stage 1 default.
  return renderAllSportMenu(env, localDate);
}
