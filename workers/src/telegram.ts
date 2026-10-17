// Timing-safe secret comparison + Telegram update helpers.
// Stage 1: webhook auth, update dedupe coordination, view routing tables.
// Mirrors predbot/bot/callbacks.py COMMANDS and predbot/bot/pagination.py
// callback codec (pg|view|page[|yyyymmdd]) so buttons stay compatible.

export const CALLBACK_PREFIX = "pg";
export const CALLBACK_NOOP = "noop";

export const COMMAND_TO_VIEW: Record<string, string> = {
  "/all": "all",
  "/top": "top",
  "/daily": "daily",
  "/today": "daily",
  "/stats": "stats",
  "/yesterday": "yesterday",
  "/stats_top": "stats_top",
  "/stats_daily": "stats_daily",
};

export function timingSafeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

export function isAuthorized(request: Request, envSecret: string): boolean {
  if (!envSecret) return false;
  const got = request.headers.get("X-Telegram-Bot-Api-Secret-Token") ?? "";
  return timingSafeEqual(got, envSecret);
}

export function normalizeCommand(text: string | undefined | null): string | null {
  if (!text) return null;
  const first = text.trim().split(/\s+/)[0] ?? "";
  // Strip @BotName suffix that Telegram adds in groups: /top@MyBot -> /top
  const bare = first.split("@")[0]?.toLowerCase() ?? "";
  if (!bare.startsWith("/")) return null;
  return COMMAND_TO_VIEW[bare] ? bare : null;
}

/**
 * ~ predbot/bot/commands.py split_command(): `"/top@MyBot extra"` ->
 * `{ cmd: "/top", args: ["extra"] }` (Telegram appends @bot in groups).
 * Only the command token is lowercased; args keep their original case.
 */
export function splitCommand(text: string | undefined | null): { cmd: string; args: string[] } {
  const parts = (text ?? "").trim().split(/\s+/).filter(Boolean);
  if (!parts.length) return { cmd: "", args: [] };
  const cmd = (parts[0] ?? "").split("@")[0]?.toLowerCase() ?? "";
  return { cmd, args: parts.slice(1) };
}

export function commandToView(cmd: string): string | null {
  return COMMAND_TO_VIEW[cmd.toLowerCase()] ?? null;
}

export function encodeCallback(view: string, page: number, day?: string): string {
  const base = `${CALLBACK_PREFIX}|${view}|${Math.max(0, Math.floor(page))}`;
  return day ? `${base}|${day.replace(/-/g, "")}` : base;
}

import { SPORT_ICON } from "./format.ts";

// Every view this Worker can actually render (mirrors renderView in
// views.ts). Anything else — including Python-only `results_*` views — is
// rejected so a forged/legacy button can never render an unrelated menu.
const CALLBACK_VIEWS = new Set([
  "all", "top", "daily", "stats", "yesterday", "stats_top", "stats_daily",
  ...Object.keys(SPORT_ICON).filter((s) => s !== "other").map((s) => `all_${s}`),
]);

export function parseCallback(
  data: string | undefined | null,
): { view: string; page: number; day: string | null } | null {
  if (!data || data === CALLBACK_NOOP) return null;
  const parts = data.split("|");
  // ~ pagination.py parse_callback(): exactly 3-4 segments; anything else
  // (extra segments, unknown view) is malformed and rejected outright.
  if (parts[0] !== CALLBACK_PREFIX || parts.length < 3 || parts.length > 4) return null;
  const view = parts[1] ?? "";
  if (!CALLBACK_VIEWS.has(view)) return null;
  // Number.parseInt("3abc") === 3 — a forged page must not parse, so pure
  // digits are required (negative pages are rejected outright rather than
  // clamped, matching the long-standing Worker contract).
  if (!/^\d+$/.test((parts[2] ?? "").trim())) return null;
  const page = Number.parseInt((parts[2] ?? "").trim(), 10);
  let day: string | null = null;
  if (parts.length === 4) {
    const d = parts[3] ?? "";
    if (!/^\d{8}$/.test(d)) return null;
    day = `${d.slice(0, 4)}-${d.slice(4, 6)}-${d.slice(6, 8)}`;
  }
  return { view, page, day };
}

// predbot/bot/pagination.py VIEW_ICON / VIEW_LABEL — button text is the icon
// and label only; production never highlights the active view in-place.
const VIEW_ICON: Record<string, string> = { all: "📚", top: "🔥", daily: "📋", stats: "📊", yesterday: "🗓" };
const VIEW_LABEL: Record<string, string> = {
  all: "All Predictions",
  top: "Top Picks",
  daily: "Daily Picks",
  stats: "Stats",
  yesterday: "Yesterday",
};

type SwitchButton = { text: string; callback_data?: string; url?: string };

// AllButtonMode's `{ url }` is always exactly this shape (allButtonModeFor,
// below) - pulling botUsername back out of it lets every OTHER view button
// (top/daily/stats/yesterday) build its own `?start=<view>` deep link from
// the same mode, without changing AllButtonMode's own public shape (pinned
// by an existing test asserting it with assert.deepEqual).
function usernameFromAllUrl(url: string): string | undefined {
  return /^https:\/\/t\.me\/([^/?]+)\?start=all$/.exec(url)?.[1];
}

/**
 * Bug 10: a group tap on Top/Daily/Stats/Yesterday used to be a callback
 * button, which posted a brand-new full card into the group (see
 * cron.ts/index.ts's own fix for the TAP side). `mode` lets every caller
 * that already threads an AllButtonMode through for the "all" button make
 * these four buttons deep links in a group too, the way "all" already does
 * - "callback" (private) is unchanged; "omit" (group, no username to build
 * a link with) falls back to callback rather than omitting the button
 * entirely, since index.ts's own redirect handler has a no-link fallback
 * alert for exactly that case.
 */
function viewButton(view: string, mode: AllButtonMode = "callback"): SwitchButton {
  const text = `${VIEW_ICON[view] ?? ""} ${VIEW_LABEL[view] ?? view}`.trim();
  const username = mode !== "callback" && mode !== "omit" ? usernameFromAllUrl(mode.url) : undefined;
  return username ? { text, url: `https://t.me/${username}?start=${view}` } : { text, callback_data: encodeCallback(view, 0) };
}
type KeyboardRow = SwitchButton[];

/**
 * All Predictions is a private-chat-only experience (the list is too long
 * for a broadcast group). Every keyboard that would otherwise carry the
 * callback-based All switch button into a group must instead use this to
 * decide what (if anything) to render there:
 *   - "callback": private chat — normal in-app pagination, unchanged.
 *   - { url }: group chat with TELEGRAM_BOT_USERNAME configured — a deep
 *     link to the private bot (never a callback that could publish the
 *     full list into the group).
 *   - "omit": group chat with no configured bot username — no dead link,
 *     no callback either; the button is simply left out.
 */
export type AllButtonMode = "callback" | { url: string } | "omit";

export function allButtonModeFor(chatType: string, botUsername: string | undefined): AllButtonMode {
  if (chatType === "private") return "callback";
  return botUsername ? { url: `https://t.me/${botUsername}?start=all` } : "omit";
}

function allButton(mode: AllButtonMode): SwitchButton | null {
  if (mode === "omit") return null;
  if (mode === "callback") return viewButton("all");
  return { text: `${VIEW_ICON.all} ${VIEW_LABEL.all}`, url: mode.url };
}

// predbot/bot/pagination.py SWITCH_ROWS: ((all,), (top, daily), (stats, yesterday)).
function switchRows(allMode: AllButtonMode = "callback"): KeyboardRow[] {
  const all = allButton(allMode);
  return [
    ...(all ? [[all]] : []),
    [viewButton("top", allMode), viewButton("daily", allMode)],
    [viewButton("stats", allMode), viewButton("yesterday", allMode)],
  ];
}

/** predbot/bot/callbacks.py menu_markup — identical rows to switchRows(), no pager. */
export function menuKeyboard(allMode: AllButtonMode = "callback"): Record<string, unknown> {
  return { inline_keyboard: switchRows(allMode) };
}

/**
 * predbot/bot/pagination.py build_keyboard: an optional pager row (shown
 * only when there's more than one page; ◀️ omitted on page 0, ▶️ omitted on
 * the last page, middle button is a static "page/pages" no-op), any
 * extra rows (e.g. a sport-menu back button), then the fixed view-switch
 * rows. `day`, when given, is carried on page-turn buttons only, so an old
 * message keeps paging the day it was opened for.
 */
export function buildKeyboard(
  view: string,
  page: number,
  pages: number,
  day: string | null = null,
  extraRows: KeyboardRow[] = [],
  allMode: AllButtonMode = "callback",
): Record<string, unknown> {
  const pagerRow: KeyboardRow = [];
  if (pages > 1) {
    if (page > 0) pagerRow.push({ text: "◀️", callback_data: encodeCallback(view, page - 1, day ?? undefined) });
    pagerRow.push({ text: `${page + 1}/${pages}`, callback_data: CALLBACK_NOOP });
    if (page < pages - 1) pagerRow.push({ text: "▶️", callback_data: encodeCallback(view, page + 1, day ?? undefined) });
  }
  return {
    inline_keyboard: [...(pagerRow.length ? [pagerRow] : []), ...extraRows, ...switchRows(allMode)],
  };
}

/** predbot/bot/pagination.py _sports_back_row — the /all sport-page back button. */
export function sportsBackRow(day: string): KeyboardRow {
  return [{ text: "◀️ Sports", callback_data: encodeCallback("all", 0, day) }];
}

/** predbot/bot/callbacks.py custom_picks_button — same deep link for the empty-Top card and the community menu. */
export function customPicksButton(botUsername: string): { text: string; url: string } {
  return { text: "✨ Custom Picks", url: `https://t.me/${botUsername}?start=custom_picks` };
}

/**
 * predbot/bot/callbacks.py top_empty_markup. `botUsername` is required to
 * build the deep link (production defers/retries the whole publication
 * rather than ever showing a dead link when the username isn't known yet —
 * callers here must do the same: omit this card, don't send it with no
 * button, if the username isn't configured).
 */
export function topEmptyKeyboard(botUsername: string, allMode: AllButtonMode = "callback"): Record<string, unknown> {
  const all = allButton(allMode);
  const daily = viewButton("daily", allMode);
  const topRow = all ? [daily, all] : [daily];
  return { inline_keyboard: [topRow, [customPicksButton(botUsername)]] };
}

/**
 * predbot/bot/callbacks.py community_menu_markup — posted to the group only
 * via the /menu admin command, so the All button here is always the
 * private deep link, never the in-group callback.
 */
export function communityMenuKeyboard(botUsername: string): Record<string, unknown> {
  const mode: AllButtonMode = { url: `https://t.me/${botUsername}?start=all` };
  return {
    inline_keyboard: [
      [viewButton("top", mode), viewButton("daily", mode)],
      [allButton(mode)!],
      [viewButton("stats", mode), viewButton("yesterday", mode)],
      [customPicksButton(botUsername)],
    ],
  };
}
