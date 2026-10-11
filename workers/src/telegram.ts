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

export function menuKeyboard(): Record<string, unknown> {
  return {
    inline_keyboard: [
      [
        { text: "📚 All Predictions", callback_data: encodeCallback("all", 0) },
        { text: "🔥 Top Picks", callback_data: encodeCallback("top", 0) },
      ],
      [
        { text: "📋 Daily Picks", callback_data: encodeCallback("daily", 0) },
        { text: "📊 Stats", callback_data: encodeCallback("stats", 0) },
      ],
      [{ text: "🗓 Yesterday", callback_data: encodeCallback("yesterday", 0) }],
    ],
  };
}

export function switchRowKeyboard(view: string, page: number): Record<string, unknown> {
  const btn = (v: string, label: string) => ({
    text: v === view ? `• ${label} •` : label,
    callback_data: encodeCallback(v, 0),
  });
  const prev = page > 0 ? [{ text: "◀ Prev", callback_data: encodeCallback(view, page - 1) }] : [];
  const next = [{ text: "Next ▶", callback_data: encodeCallback(view, page + 1) }];
  return {
    inline_keyboard: [
      [btn("all", "📚 All"), btn("top", "🔥 Top"), btn("daily", "📋 Daily")],
      [btn("stats", "📊 Stats"), btn("yesterday", "🗓 Yesterday")],
      [...prev, ...next],
    ],
  };
}
