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
};

export const VIEW_COMMAND: Record<string, string> = {
  all: "/all",
  top: "/top",
  daily: "/daily",
  stats: "/stats",
  yesterday: "/yesterday",
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

export function commandToView(cmd: string): string | null {
  return COMMAND_TO_VIEW[cmd.toLowerCase()] ?? null;
}

export function encodeCallback(view: string, page: number, day?: string): string {
  const base = `${CALLBACK_PREFIX}|${view}|${Math.max(0, Math.floor(page))}`;
  return day ? `${base}|${day.replace(/-/g, "")}` : base;
}

export function parseCallback(
  data: string | undefined | null,
): { view: string; page: number; day: string | null } | null {
  if (!data || data === CALLBACK_NOOP) return null;
  const parts = data.split("|");
  if (parts[0] !== CALLBACK_PREFIX || parts.length < 3) return null;
  const view = parts[1] ?? "";
  const page = Number.parseInt(parts[2] ?? "", 10);
  if (!view || !Number.isFinite(page) || page < 0) return null;
  let day: string | null = null;
  if (parts[3]) {
    const d = parts[3];
    if (/^\d{8}$/.test(d)) {
      day = `${d.slice(0, 4)}-${d.slice(4, 6)}-${d.slice(6, 8)}`;
    }
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

function viewButton(view: string): { text: string; callback_data: string } {
  return { text: `${VIEW_ICON[view] ?? ""} ${VIEW_LABEL[view] ?? view}`.trim(), callback_data: encodeCallback(view, 0) };
}

// predbot/bot/pagination.py SWITCH_ROWS: ((all,), (top, daily), (stats, yesterday)).
function switchRows(): { text: string; callback_data: string }[][] {
  return [[viewButton("all")], [viewButton("top"), viewButton("daily")], [viewButton("stats"), viewButton("yesterday")]];
}

/** predbot/bot/callbacks.py menu_markup — identical rows to switchRows(), no pager. */
export function menuKeyboard(): Record<string, unknown> {
  return { inline_keyboard: switchRows() };
}

type KeyboardRow = { text: string; callback_data: string }[];

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
): Record<string, unknown> {
  const pagerRow: KeyboardRow = [];
  if (pages > 1) {
    if (page > 0) pagerRow.push({ text: "◀️", callback_data: encodeCallback(view, page - 1, day ?? undefined) });
    pagerRow.push({ text: `${page + 1}/${pages}`, callback_data: CALLBACK_NOOP });
    if (page < pages - 1) pagerRow.push({ text: "▶️", callback_data: encodeCallback(view, page + 1, day ?? undefined) });
  }
  return {
    inline_keyboard: [...(pagerRow.length ? [pagerRow] : []), ...extraRows, ...switchRows()],
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
export function topEmptyKeyboard(botUsername: string): Record<string, unknown> {
  return { inline_keyboard: [[viewButton("daily"), viewButton("all")], [customPicksButton(botUsername)]] };
}

/** predbot/bot/callbacks.py community_menu_markup — posted to the group only via the /menu admin command. */
export function communityMenuKeyboard(botUsername: string): Record<string, unknown> {
  return {
    inline_keyboard: [
      [viewButton("top"), viewButton("daily")],
      [viewButton("all")],
      [viewButton("stats"), viewButton("yesterday")],
      [customPicksButton(botUsername)],
    ],
  };
}
