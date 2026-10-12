// Stage 1 Worker entrypoint: secure Telegram webhook + health + Cron stub.
// Intrusion surface is intentionally small: no provider calls, no generation,
// no spending. Slow work belongs in GitHub Actions (Stage 2) or later Crons.

import { QuotaExhausted, completeUpdate, failUpdate, tryClaimUpdate, upsertSubscriber, type Env } from "./db.ts";
import {
  commandToView,
  isAuthorized,
  menuKeyboard,
  normalizeCommand,
  parseCallback,
} from "./telegram.ts";
import { lagosDateString, renderView } from "./views.ts";

const WELCOME_COMMANDS =
  "<b>🏆 /top</b> – Top picks of the day\n" +
  "<b>📚 /all</b> – All predictions\n" +
  "<b>📋 /daily</b> – Daily picks · Accumulator slip\n" +
  "<b>📊 /stats</b> – See prediction results";

function welcomeMessage(firstName: string): string {
  const safe = firstName.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  return `👋 Hi, ${safe}\n\nUse the menu below or these commands:\n\n${WELCOME_COMMANDS}`;
}

async function tgApi(env: Env, method: string, payload: Record<string, unknown>): Promise<Response> {
  const res = await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/${method}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
  return res;
}

async function answerCallback(env: Env, id: string, text?: string): Promise<void> {
  if (!id) return;
  await tgApi(env, "answerCallbackQuery", { callback_query_id: id, text: text ?? undefined });
}

async function handleUpdate(env: Env, update: Record<string, unknown>): Promise<void> {
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

  // /start subscribes (private) and always shows the menu; group /start
  // shows the menu without subscribing the group.
  if (text.startsWith("/start")) {
    if (chatType === "private") {
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
