// View rendering for Stage 1. Reads D1-cached picks only — never calls a
// sports provider. Before the day's generation has been migrated/published,
// every view returns the honest not-ready empty state (same contract as
// UnifiedFetcher.get_daily_snapshot(allow_api_call=False) in Python).

import { getDayCache, getPicksPage, type Env } from "./db.ts";
import { switchRowKeyboard } from "./telegram.ts";

export const LAGOS_OFFSET_MINUTES = 60; // Africa/Lagos is UTC+1 year-round (no DST)

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
  all: "📚 ALL PREDICTIONS",
  top: "🔥 TOP PICKS",
  daily: "📋 DAILY PICKS",
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

function escHtml(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

export async function renderView(
  env: Env,
  view: string,
  page: number,
  day: string | null,
): Promise<{ text: string; replyMarkup: Record<string, unknown> }> {
  const timezone = env.TIMEZONE ?? "Africa/Lagos";
  const pageSize = Math.min(Math.max(Number.parseInt(env.PAGE_SIZE ?? "5", 10) || 5, 1), 25);
  const localDate = day ?? lagosDateString();
  const cache = await getDayCache(env.DB, localDate, timezone);

  if (!cache || cache.status !== "COMPLETE") {
    return { text: notReadyText(view, localDate), replyMarkup: switchRowKeyboard(view, 0) };
  }

  if (view === "stats" || view === "yesterday") {
    // Stage 1 serves stats/yesterday from the day-cache payload summary.
    // Full settlement history browsing lands in Stage 3 with job_state.
    let summary = "settlement summary pending";
    try {
      const payload = JSON.parse(cache.payload_json) as { stats_text?: string };
      if (payload.stats_text) summary = payload.stats_text;
    } catch {
      // keep fallback
    }
    return {
      text: `<b>${VIEW_TITLE[view]}</b> — ${localDate}\n\n${escHtml(summary)}`,
      replyMarkup: switchRowKeyboard(view, 0),
    };
  }

  const collection = view === "top" || view === "daily" || view === "all" ? view : "all";
  const picks = await getPicksPage(env.DB, localDate, collection, pageSize, page * pageSize);
  if (picks.length === 0) {
    const msg =
      page === 0
        ? `No qualified ${escHtml(collection)} picks for ${localDate} — the model found nothing above the quality floor. ` +
          `This is an honest empty, not an error; thresholds were not lowered to fill the card.`
        : `No further ${escHtml(collection)} picks on page ${page + 1} for ${localDate}.`;
    return { text: `<b>${VIEW_TITLE[collection] ?? "PICKS"}</b> — ${localDate}\n\n${msg}`, replyMarkup: switchRowKeyboard(view, page) };
  }
  const lines = picks.map(
    (p, i) =>
      `${page * pageSize + i + 1}. ${escHtml(p.home)} vs ${escHtml(p.away)} — ${escHtml(p.label)} @ ${p.price.toFixed(2)}`,
  );
  return {
    text: `<b>${VIEW_TITLE[collection] ?? "PICKS"}</b> — ${localDate} (page ${page + 1})\n\n${lines.join("\n")}`,
    replyMarkup: switchRowKeyboard(view, page),
  };
}
