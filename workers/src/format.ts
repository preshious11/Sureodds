// Faithful port of predbot/bot/format.py's presentation primitives. Any
// change here must mirror the Python source exactly (text, spacing, emoji) —
// this file is not a redesign, it is the production bot's card format
// reproduced for the Worker. See predictions-bot/predbot/bot/format.py.

export const SPORT_ICON: Record<string, string> = {
  football: "⚽",
  basketball: "🏀",
  american_football: "🏈",
  baseball: "⚾",
  tennis: "🎾",
  mma: "🥊",
  other: "🎯",
};

export const SPORT_LABEL: Record<string, string> = {
  football: "Football",
  basketball: "Basketball",
  american_football: "American Football",
  baseball: "Baseball",
  tennis: "Tennis",
  mma: "MMA",
  other: "Other",
};

// predbot/util/duration.py DURATION_H — typical match length per sport, used
// only for the started/finished estimate on a pick line (not a live feed).
const DURATION_H: Record<string, number> = {
  football: 2.2,
  basketball: 2.8,
  american_football: 3.6,
  baseball: 3.6,
  tennis: 3.0,
  mma: 3.0,
  other: 3.0,
};

const STARTED_MARK = " 🔴";
const FINISHED_MARK = " · Finished";

export const TOP_TITLE = "🔥 TOP PICKS";
export const DAILY_TITLE = "📋 DAILY PICKS";
export const ALL_TITLE = "📚 ALL PREDICTIONS";

// Africa/Lagos is UTC+1 year-round (no DST); WAT is its fixed abbreviation.
export const ZONE_LABEL = "WAT";
export const ZONE_OFFSET_MINUTES = 60;

export function escHtml(s: string): string {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

const WEEKDAY_ABBR = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTH_ABBR = [
  "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
];

/** "YYYY-MM-DD" -> "Sat 19 Sep" (predbot/bot/format.py day_label). */
export function dayLabel(dateStr: string): string {
  const [y, m, d] = dateStr.split("-").map(Number);
  const utcMs = Date.UTC(y, (m as number) - 1, d);
  const weekday = WEEKDAY_ABBR[new Date(utcMs).getUTCDay()];
  return `${weekday} ${d} ${MONTH_ABBR[(m as number) - 1]}`;
}

/** ISO UTC instant + zone offset -> "7:45 PM" (predbot/bot/format.py clock_label). */
export function clockLabel(startUtcIso: string, offsetMinutes = ZONE_OFFSET_MINUTES): string {
  const t = new Date(new Date(startUtcIso).getTime() + offsetMinutes * 60_000);
  let h = t.getUTCHours();
  const min = String(t.getUTCMinutes()).padStart(2, "0");
  const ampm = h >= 12 ? "PM" : "AM";
  h = h % 12;
  if (h === 0) h = 12;
  return `${h}:${min} ${ampm}`;
}

function legState(startUtcIso: string, sport: string, nowMs: number): "" | "started" | "finished" {
  const start = new Date(startUtcIso).getTime();
  if (!Number.isFinite(start)) return "";
  const elapsedMs = nowMs - start;
  if (elapsedMs < 0) return "";
  const durationMs = (DURATION_H[sport] ?? DURATION_H.other) * 3_600_000;
  return elapsedMs < durationMs ? "started" : "finished";
}

export interface PickLineInput {
  sport: string;
  league: string | null;
  home: string;
  away: string;
  label: string;
  price: number;
  prob: number;
  startUtc: string;
}

/**
 * The one canonical 3-line pick block (predbot/bot/format.py pick_lines):
 *   ⚽ England • Premier League · Chelsea vs Man United
 *   🎯 Chelsea to Win
 *   💰 1.31  ·  📊 76%  ·  🕘 12:30 AM
 * `label` is used as-is: the engine already writes the human-readable
 * outcome text (the same column the production bot itself reads), so no
 * market-code translation is reimplemented here.
 */
export function pickLines(p: PickLineInput, nowMs = Date.now()): string[] {
  const icon = SPORT_ICON[p.sport] ?? SPORT_ICON.other;
  const league = escHtml(p.league ?? "");
  const match = `${escHtml(p.home)} vs ${escHtml(p.away)}`;
  const state = legState(p.startUtc, p.sport, nowMs);
  const mark = state === "started" ? STARTED_MARK : state === "finished" ? FINISHED_MARK : "";
  const when = clockLabel(p.startUtc);
  return [
    `${icon} ${league}${league ? " · " : ""}${match}`,
    `🎯 ${escHtml(p.label)}`,
    `💰 ${p.price.toFixed(2)}  ·  📊 ${Math.round(p.prob * 100)}%  ·  🕘 ${when}${mark}`,
  ];
}

/** Decimal odds multiply, never add (predbot/bot/format.py combined_odds). */
export function combinedOdds(prices: number[]): number {
  let total = 1;
  for (const raw of prices) {
    const p = Number(raw);
    if (!(p > 1.0) || Number.isNaN(p)) {
      throw new Error(`invalid price for combined odds: ${raw}`);
    }
    total *= p;
  }
  return total;
}

export function nPicks(n: number): string {
  return `${n} pick${n === 1 ? "" : "s"}`;
}

/** predbot/bot/format.py card_header. */
export function cardHeader(title: string, day: string, prices: number[], zone = ""): string[] {
  const lines = [day ? `${title} · ${dayLabel(day)}` : title];
  if (prices.length) {
    let odds = `🎟 Combined odds ${combinedOdds(prices).toFixed(2)} · ${nPicks(prices.length)}`;
    if (zone) odds += ` · 🕘 times in ${zone}`;
    lines.push(odds);
  }
  return lines;
}

export function noSelectionMessage(): string {
  return "No selections qualified today.";
}

function byKickoff<T extends { startUtc: string }>(picks: T[]): T[] {
  const withIndex = picks.map((p, i) => ({ p, i }));
  withIndex.sort((a, b) => {
    const ta = new Date(a.p.startUtc).getTime();
    const tb = new Date(b.p.startUtc).getTime();
    const va = Number.isFinite(ta), vb = Number.isFinite(tb);
    if (va && vb && ta !== tb) return ta - tb;
    if (va !== vb) return va ? -1 : 1; // unknown-time picks sort last
    return a.i - b.i; // stable: keep original slate order on ties
  });
  return withIndex.map((w) => w.p);
}

/**
 * predbot/bot/format.py picks_card: the full ticket text for one collection
 * (Top or Daily). `totalPrices`, when given, drives the combined-odds/pick
 * count header from the WHOLE ticket even when only one page is rendered.
 */
export function picksCard(
  picks: PickLineInput[],
  day: string,
  title: string,
  opts: {
    totalPrices?: number[];
    zone?: string;
    page?: number;
    pages?: number;
    nowMs?: number;
  } = {},
): string {
  const sorted = byKickoff(picks);
  const prices = opts.totalPrices ?? sorted.map((p) => p.price);
  const lines = cardHeader(title, day, sorted.length ? prices : [], opts.zone ?? "");
  if (!sorted.length) {
    lines.push(
      title === TOP_TITLE ? "No Top Picks for today.\n\nSee other picks:" : noSelectionMessage(),
    );
  }
  for (const p of sorted) {
    lines.push("", ...pickLines(p, opts.nowMs));
  }
  if (opts.pages && opts.pages > 1 && opts.page) {
    lines.push("", `Page ${opts.page} of ${opts.pages}`);
  }
  return lines.join("\n");
}

// -------------------------------------------------------------------------
// Yesterday-results block (predbot/bot/format.py counts_line, _best_line,
// yesterday_lines) — the "today" window from predbot/jobs/stats.py _block,
// ported 1:1. 7-day history, by-sport breakdown and streaks are NOT ported
// yet (see PRODUCTION_PARITY notes); this covers the primary settled-results
// content of /stats and /yesterday.
export interface StatsBlock {
  won: number;
  lost: number;
  push: number;
  void: number;
  unresolved: number;
  n: number; // staked (won+lost+push, half-legs counted whole)
  roi: number;
  profit: number;
  bestWin: { label: string; price: number; home: string; away: string } | null;
}

export function countsLine(b: StatsBlock, sep = "  "): string {
  const parts = [`✅ ${b.won}`, `❌ ${b.lost}`, `➖ ${b.void}`];
  if (b.push) parts.push(`↩️ ${b.push}`);
  if (b.unresolved) parts.push(`❔ ${b.unresolved} unresolved`);
  return parts.join(sep);
}

export function hitRate(b: StatsBlock): number | null {
  const decided = b.won + b.lost;
  return decided ? (100 * b.won) / decided : null;
}

export function bestLine(best: StatsBlock["bestWin"], final: boolean): string | null {
  if (!best) return null;
  const label = final ? "Best" : "Best so far";
  return `🏆 ${label}: ${escHtml(best.label)} · ${escHtml(best.home)} vs ${escHtml(best.away)} · @${best.price.toFixed(2)}`;
}

/**
 * predbot/bot/format.py yesterday_lines, for a day whose results are fully
 * settled (the common case once the day's last match ends). `issued` is the
 * number of picks that existed for the day (won+lost+push+void+unresolved,
 * i.e. everything that isn't still pending).
 */
export function yesterdayLines(day: string, b: StatsBlock, issued: number): string[] {
  if (issued === 0) {
    return [`🗓 Yesterday · ${dayLabel(day)} — no picks were issued.`];
  }
  const settled = b.won + b.lost + b.push + b.void + b.unresolved;
  const lines = [`🗓 Yesterday · ${dayLabel(day)} · ${nPicks(issued)}`];
  if (settled < issued) {
    lines.push(`${settled} of ${issued} settled · final card after the last result`);
    return lines;
  }
  lines.push(countsLine(b));
  const rate = hitRate(b);
  const rateText = rate === null ? "🎯 –" : `🎯 ${Math.round(rate)}%`;
  lines.push(
    b.n ? `${rateText}  ·  💹 ROI ${b.roi >= 0 ? "+" : ""}${b.roi.toFixed(0)}%` : rateText,
  );
  const best = bestLine(b.bestWin, true);
  if (best) lines.push(best);
  return lines;
}

export const NO_RESULTS_YET = "📊 No results yet — the first recap arrives the morning after the first slate.";

/** "YYYY-MM-DD" shifted by `deltaDays` (UTC-safe; local date arithmetic only). */
export function shiftDate(dateStr: string, deltaDays: number): string {
  const [y, m, d] = dateStr.split("-").map(Number);
  const t = new Date(Date.UTC(y, (m as number) - 1, (d as number) + deltaDays));
  const yy = t.getUTCFullYear();
  const mm = String(t.getUTCMonth() + 1).padStart(2, "0");
  const dd = String(t.getUTCDate()).padStart(2, "0");
  return `${yy}-${mm}-${dd}`;
}

/**
 * predbot/bot/format.py fixture_block: one fixture's header line, then each
 * of its predictions' outcome + price/confidence/kickoff lines (no repeated
 * header). Used by the /all per-sport fixture pages.
 */
export function fixtureBlock(rows: PickLineInput[], nowMs = Date.now()): string[] {
  if (!rows.length) return [];
  const icon = SPORT_ICON[rows[0].sport] ?? SPORT_ICON.other;
  const league = escHtml(rows[0].league ?? "");
  const match = `${escHtml(rows[0].home)} vs ${escHtml(rows[0].away)}`;
  const lines = [`${icon} ${league}${league ? " · " : ""}${match}`];
  for (const r of rows) {
    lines.push(`🎯 ${escHtml(r.label)}`);
    lines.push(`💰 ${r.price.toFixed(2)}  ·  📊 ${Math.round(r.prob * 100)}%  ·  🕘 ${clockLabel(r.startUtc, ZONE_OFFSET_MINUTES)}${""}`);
  }
  return lines;
}

/** predbot/bot/stats.py _block, ported 1:1 (flat 1-unit stakes). */
export function computeStatsBlock(rows: {
  status: string;
  price: number;
  label: string;
  home: string;
  away: string;
  result_json: string | null;
}[]): StatsBlock {
  let won = 0, lost = 0, push = 0, voidN = 0, unresolved = 0, staked = 0, profit = 0;
  let best: StatsBlock["bestWin"] = null;
  const UNRESOLVED_REASONS = new Set(["settlement deadline expired"]);
  for (const r of rows) {
    const st = r.status;
    if (st === "pending") continue;
    if (st === "void") {
      let isUnresolved = false;
      if (r.result_json) {
        try {
          const data = JSON.parse(r.result_json);
          isUnresolved = data && typeof data === "object" && UNRESOLVED_REASONS.has(data.reason);
        } catch {
          // keep isUnresolved false
        }
      }
      if (isUnresolved) unresolved += 1;
      else voidN += 1;
      continue;
    }
    staked += 1;
    const price = Number(r.price);
    if (st === "won") {
      profit += price - 1.0;
      won += 1;
      if (!best || price > best.price) best = { label: r.label, price, home: r.home, away: r.away };
    } else if (st === "half_won") {
      profit += (price - 1.0) / 2;
      won += 1;
    } else if (st === "lost") {
      profit += -1.0;
      lost += 1;
    } else if (st === "half_lost") {
      profit += -0.5;
      lost += 1;
    } else {
      push += 1; // st === "push"
    }
  }
  return {
    won, lost, push, void: voidN, unresolved, n: staked,
    roi: staked ? (100 * profit) / staked : 0,
    profit: Math.round(profit * 100) / 100,
    bestWin: best,
  };
}
