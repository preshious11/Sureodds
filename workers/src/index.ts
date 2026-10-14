// Stage 1 Worker entrypoint: secure Telegram webhook + health + Cron stub.
// Intrusion surface is intentionally small: no provider calls, no generation,
// no spending. Slow work belongs in GitHub Actions (Stage 2) or later Crons.

import {
  QuotaExhausted,
  completeUpdate,
  enterCustomSession,
  failUpdate,
  getCustomPickCandidates,
  getCustomPickPool,
  isCustomOnCooldown,
  isCustomSessionActive,
  leaveCustomSession,
  recordCustomRequest,
  tryClaimUpdate,
  upsertSubscriber,
  type Env,
} from "./db.ts";
import { BUSY, INTRO, respond as customPicksRespond } from "./custom_picks.ts";
import {
  commandToView,
  communityMenuKeyboard,
  isAuthorized,
  menuKeyboard,
  normalizeCommand,
  parseCallback,
} from "./telegram.ts";
import { lagosDateString, renderView } from "./views.ts";

const CUSTOM_PICKS_COOLDOWN_MS = 3_000;

const WELCOME_COMMANDS =
  "<b>🏆 /top</b> – Top picks of the day\n" +
  "<b>📚 /all</b> – All predictions\n" +
  "<b>📋 /daily</b> – Daily picks · Accumulator slip\n" +
  "<b>📊 /stats</b> – See prediction results";

const COMMUNITY_MENU_TEXT = "⚽ Predictions\n\nToday's picks, results and more.";

function welcomeMessage(firstName: string): string {
  const safe = firstName.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  return `👋 Hi, ${safe}\n\nUse the menu below or these commands:\n\n${WELCOME_COMMANDS}`;
}

// Throws on a non-2xx response instead of returning it silently: previously
// routeUpdate never inspected the Response it got back, so a failed
// sendMessage/editMessageText (Telegram 5xx/4xx) was treated as success —
// the update was marked `done` even though the user was never actually
// messaged, and no retry ever happened. Throwing routes the failure through
// handleUpdate's existing catch -> failUpdate path, so a transient Telegram
// failure is retried exactly like a D1 failure already is.
async function tgApi(env: Env, method: string, payload: Record<string, unknown>): Promise<Response> {
  const res = await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/${method}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    throw new Error(`telegram ${method} failed: ${res.status}`);
  }
  return res;
}

async function answerCallback(env: Env, id: string, text?: string): Promise<void> {
  if (!id) return;
  // Best-effort only: a failed toast must never fail the whole update (the
  // page render/edit it accompanies may already have succeeded).
  try {
    await tgApi(env, "answerCallbackQuery", { callback_query_id: id, text: text ?? undefined });
  } catch {
    // swallowed deliberately
  }
}

export async function handleUpdate(env: Env, update: Record<string, unknown>): Promise<void> {
  const updateId = update.update_id as number;
  const claim = await tryClaimUpdate(env.DB, updateId);
  if (claim === "duplicate") return; // already done (or exhausted retries) — never reprocess.
  try {
    await routeUpdate(env, update);
    await completeUpdate(env.DB, updateId);
  } catch (err) {
    if (err instanceof QuotaExhausted) {
      // Quota outage: record NOTHING (not even a failure row — the write
      // itself would fail). The update stays pending/undone, Telegram gets a
      // 500 and redelivers later, and deduplication is intact because no done
      // row exists. Zero additional writes are spent on the retry path.
      throw err;
    }
    // Persist the failure and leave the update retryable: a Telegram retry
    // or the Stage-3 sweeper reaps `failed` rows. The update is NOT lost.
    // (If failUpdate itself hits quota, it throws QuotaExhausted: same
    // fail-closed outcome as above.)
    const message = err instanceof Error ? err.message : String(err);
    await failUpdate(env.DB, updateId, message);
    throw err;
  }
}

async function routeUpdate(env: Env, update: Record<string, unknown>): Promise<void> {
  // Callback buttons share the view router with typed commands.
  const cb = update.callback_query as Record<string, unknown> | undefined;
  if (cb) {
    const cbId = String(cb.id ?? "");
    const data = String((cb.data as string) ?? "");
    const parsed = parseCallback(data);
    const msg = cb.message as Record<string, unknown> | undefined;
    const chatId = String((msg?.chat as Record<string, unknown> | undefined)?.id ?? (cb.from as Record<string, unknown> | undefined)?.id ?? "");
    if (!parsed) {
      await answerCallback(env, cbId);
      return;
    }
    // Group taps on paged views are acknowledged without re-render spam;
    // private chats get the page edit. (Closest clean alternative to
    // per-member group welcomes, which read-only groups cannot do.)
    const chatType = String((msg?.chat as Record<string, unknown> | undefined)?.type ?? "private");
    const { text, replyMarkup } = await renderView(env, parsed.view, parsed.page, parsed.day);
    await answerCallback(env, cbId);
    if (!chatId) return;
    if (chatType !== "private") {
      await tgApi(env, "sendMessage", {
        chat_id: chatId,
        text,
        parse_mode: "HTML",
        reply_markup: replyMarkup,
      });
      return;
    }
    const messageId = (msg as Record<string, unknown> | undefined)?.message_id;
    if (messageId) {
      await tgApi(env, "editMessageText", {
        chat_id: chatId,
        message_id: messageId,
        text,
        parse_mode: "HTML",
        reply_markup: replyMarkup,
      });
    } else {
      await tgApi(env, "sendMessage", { chat_id: chatId, text, parse_mode: "HTML", reply_markup: replyMarkup });
    }
    return;
  }

  const msg = update.message as Record<string, unknown> | undefined;
  if (!msg) return;
  const chat = (msg.chat as Record<string, unknown> | undefined) ?? {};
  const from = (msg.from as Record<string, unknown> | undefined) ?? {};
  const chatId = String(chat.id ?? "");
  const chatType = String(chat.type ?? "private");
  if (!chatId) return;
  const text = String((msg as Record<string, unknown>).text ?? "");

  // /start custom_picks (the "✨ Custom Picks" deep link) enters a private,
  // session-scoped mode (predbot/custom_recovery.py's Recovery) BEFORE the
  // generic /start handling below — mirrors commands.py:378-390.
  const userId = String(from.id ?? "");
  if (text.startsWith("/start") && text.includes("custom_picks")) {
    if (chatType !== "private" || !userId) return; // no group/channel path exists for Custom Picks.
    await enterCustomSession(env.DB, userId, chatId);
    await tgApi(env, "sendMessage", { chat_id: chatId, text: INTRO });
    return;
  }

  // /start subscribes (private) and always shows the menu; group /start
  // shows the menu without subscribing the group. Also leaves any active
  // Custom Picks session (predbot's `_custom_leave` on plain /start).
  if (text.startsWith("/start")) {
    if (chatType === "private") {
      if (userId) await leaveCustomSession(env.DB, userId, chatId);
      await upsertSubscriber(env.DB, chatId, {
        username: (from.username as string) ?? null,
        firstName: (from.first_name as string) ?? null,
      });
    }
    const name = String(from.first_name ?? "there");
    await tgApi(env, "sendMessage", {
      chat_id: chatId,
      text: welcomeMessage(name),
      parse_mode: "HTML",
      reply_markup: menuKeyboard(),
    });
    return;
  }

  // /menu (admin-only): posts the community menu — including the Custom
  // Picks deep-link button — to the GROUP, regardless of which chat /menu
  // was typed in (predbot/bot/commands.py:542-545, _post_community_menu).
  // This is the group's normal (non-empty-Top-card) Custom Picks entry
  // point; the empty-Top-card keyboard (above) is the other one.
  if (text.trim() === "/menu") {
    const adminIds = (env.TELEGRAM_ADMIN_IDS ?? "").split(",").map((s) => s.trim()).filter(Boolean);
    if (!adminIds.includes(String(from.id ?? ""))) {
      await tgApi(env, "sendMessage", { chat_id: chatId, text: "Not allowed." });
      return;
    }
    if (!env.TELEGRAM_CHAT_ID) {
      await tgApi(env, "sendMessage", { chat_id: chatId, text: "Community destination is not configured. Set TELEGRAM_CHAT_ID." });
      return;
    }
    if (!env.TELEGRAM_BOT_USERNAME) {
      await tgApi(env, "sendMessage", {
        chat_id: chatId,
        text: "Could not resolve this bot's username. Community menu was not posted. Try /menu again.",
      });
      return;
    }
    await tgApi(env, "sendMessage", {
      chat_id: env.TELEGRAM_CHAT_ID,
      text: COMMUNITY_MENU_TEXT,
      reply_markup: communityMenuKeyboard(env.TELEGRAM_BOT_USERNAME),
    });
    await tgApi(env, "sendMessage", { chat_id: chatId, text: "Community menu posted." });
    return;
  }

  // /stop leaves any active Custom Picks session (production also
  // unsubscribes from group broadcasts via db.subscriber_remove — not
  // ported here since this Worker's group delivery isn't subscriber-based).
  if (text.trim() === "/stop") {
    if (chatType === "private" && userId) await leaveCustomSession(env.DB, userId, chatId);
    return;
  }

  // Free-text routing into an active Custom Picks session — private chat
  // only, mirrors commands.py:356-358. Checked before the generic command
  // lookup so a Custom Picks reply (e.g. "3 basketball picks") is never
  // mistaken for an unknown command.
  if (chatType === "private" && userId && text.trim() !== "" && !text.startsWith("/")) {
    if (await isCustomSessionActive(env.DB, userId, chatId)) {
      if (await isCustomOnCooldown(env.DB, userId, chatId, Date.now(), CUSTOM_PICKS_COOLDOWN_MS)) {
        await tgApi(env, "sendMessage", { chat_id: chatId, text: BUSY });
        return;
      }
      await recordCustomRequest(env.DB, userId, chatId, Date.now());
      const zone = env.TIMEZONE ?? "Africa/Lagos";
      const day = lagosDateString(Date.now());
      const result = await customPicksRespond(
        {
          getPool: (d, z) => getCustomPickPool(env.DB, d, z),
          getCandidates: (d, z) => getCustomPickCandidates(env.DB, d, z),
        },
        text,
        zone,
        day,
        Date.now(),
      );
      await tgApi(env, "sendMessage", { chat_id: chatId, text: result.text, parse_mode: "HTML" });
      return;
    }
  }

  const cmd = normalizeCommand(text);
  if (cmd) {
    const view = commandToView(cmd) ?? "top";
    if (chatType === "private") {
      await upsertSubscriber(env.DB, chatId, {
        username: (from.username as string) ?? null,
        firstName: (from.first_name as string) ?? null,
      });
    }
    const { text: body, replyMarkup } = await renderView(env, view, 0, null);
    await tgApi(env, "sendMessage", {
      chat_id: chatId,
      text: body,
      parse_mode: "HTML",
      reply_markup: replyMarkup,
    });
    return;
  }
  // Non-command chatter is ignored in groups to avoid clutter; in private
  // chats we point back to the menu once.
  if (chatType === "private" && text.trim() !== "") {
    await tgApi(env, "sendMessage", {
      chat_id: chatId,
      text: welcomeMessage(String(from.first_name ?? "there")),
      parse_mode: "HTML",
      reply_markup: menuKeyboard(),
    });
  }
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    if (request.method === "GET" && url.pathname === "/healthz") {
      return Response.json({ ok: true, stage: 1, lagos_date: lagosDateString() });
    }
    if (request.method === "POST" && url.pathname === "/telegram/webhook") {
      if (!env.TELEGRAM_BOT_TOKEN || !env.TELEGRAM_WEBHOOK_SECRET) {
        return Response.json({ ok: false, error: "unconfigured" }, { status: 500 });
      }
      if (!isAuthorized(request, env.TELEGRAM_WEBHOOK_SECRET)) {
        return Response.json({ ok: false }, { status: 401 });
      }
      let update: Record<string, unknown>;
      try {
        update = (await request.json()) as Record<string, unknown>;
      } catch {
        return Response.json({ ok: false }, { status: 400 });
      }
      if (typeof update.update_id !== "number") {
        return Response.json({ ok: false }, { status: 400 });
      }
      // Respond fast; Telegram retries otherwise. Work is a D1 read + at
      // most one send — no provider calls by construction.
      await handleUpdate(env, update);
      return Response.json({ ok: true });
    }
    return Response.json({ ok: false, error: "not_found" }, { status: 404 });
  },

  // Stage 3 Cron entrypoint: handleScheduled is master-switched OFF
  // (STAGE3_CRON_ENABLED) and no triggers are attached yet, so this is a
  // verified no-op until staging enables it explicitly.
  async scheduled(event: ScheduledEvent, env: Env, ctx: ExecutionContext): Promise<void> {
    const { handleScheduled } = await import("./cron.ts");
    ctx.waitUntil(handleScheduled(env, event.cron, Date.now()));
  },
};
