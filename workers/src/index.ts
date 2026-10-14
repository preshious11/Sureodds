// Stage 1 Worker entrypoint: secure Telegram webhook + health + Cron stub.
// Intrusion surface is intentionally small: no provider calls, no generation,
// no spending. Slow work belongs in GitHub Actions (Stage 2) or later Crons.

import {
  QuotaExhausted,
  completeUpdate,
  enterCustomSession,
  failUpdate,
  getConversationRequest,
  getCustomPickCandidates,
  getCustomPickPool,
  getSuggestion,
  isCustomOnCooldown,
  isCustomSessionActive,
  leaveCustomSession,
  listActiveSuggestions,
  recordCustomRequest,
  setConversationRequest,
  storeSuggestion,
  tryClaimUpdate,
  upsertSubscriber,
  type Env,
  type SuggestionRow,
} from "./db.ts";
import {
  BUSY,
  COPY_EXPIRED,
  COPY_EXPIRED_EMPTY,
  CUSTOM_PICKS_TTL_MS,
  INTRO,
  deserializeRequest,
  formatTicket,
  respond as customPicksRespond,
  serializeRequest,
  type Suggestion,
  validTicket,
} from "./custom_picks.ts";
import {
  allButtonModeFor,
  commandToView,
  communityMenuKeyboard,
  isAuthorized,
  menuKeyboard,
  normalizeCommand,
  parseCallback,
} from "./telegram.ts";
import { lagosDateString, lagosDayBounds, renderView } from "./views.ts";

const CUSTOM_PICKS_COOLDOWN_MS = 3_000;

// All Predictions is a private-chat-only experience: the group is for
// broadcasts, not a long paginated list. Two distinct group-facing exits,
// chosen so NEITHER ever edits/replaces an existing group message (editing
// a shared message's keyboard previously wiped out the other switch
// buttons sitting in it — see answerCallbackWithDeepLink below):
//   - the /all command (plain text): sendGroupAllRedirect sends a fresh,
//     brand-new message — nothing existing to disturb.
//   - a pg|all*|... callback tap (new keyboards never carry one, see
//     telegram.ts allButtonModeFor; legacy pre-deploy messages still can):
//     answerCallbackWithDeepLink opens the private chat straight from the
//     callback answer itself, touching no message at all.
const ALL_GROUP_TEXT = "📚 All Predictions is a lot to show here — tap below to see it privately.";
const ALL_GROUP_TEXT_NO_LINK = "📚 All Predictions is available in a private chat with me — open a DM and send /all.";
const LOADING_TEXT = "⏳ Building your picks...";

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

/**
 * Opens a deep link directly from the callback answer, exactly like a URL
 * inline button would (Telegram Bot API: answerCallbackQuery's own `url`
 * field is documented for opening "t.me/your_bot?start=XXXX" links). This
 * is the fix for the regression in 4135f81: that commit instead called
 * editMessageText on the tapped group message to turn it into a redirect —
 * which silently replaced that message's ENTIRE keyboard (Top/Daily/Stats/
 * Yesterday sitting right alongside the All button), making the rest of
 * the group menu vanish. Answering the callback this way touches nothing
 * about the original message — same no-edit guarantee a plain URL button
 * (e.g. the Custom Picks button) already gives for free.
 */
async function answerCallbackWithDeepLink(env: Env, id: string, startParam: string): Promise<void> {
  if (!id) return;
  try {
    if (env.TELEGRAM_BOT_USERNAME) {
      await tgApi(env, "answerCallbackQuery", {
        callback_query_id: id,
        url: `https://t.me/${env.TELEGRAM_BOT_USERNAME}?start=${startParam}`,
      });
    } else {
      await tgApi(env, "answerCallbackQuery", { callback_query_id: id, text: ALL_GROUP_TEXT_NO_LINK, show_alert: true });
    }
  } catch {
    // swallowed deliberately
  }
}

function allRedirectKeyboard(botUsername: string | undefined): Record<string, unknown> | undefined {
  if (!botUsername) return undefined;
  return { inline_keyboard: [[{ text: "📚 Open All Predictions", url: `https://t.me/${botUsername}?start=all` }]] };
}

/** /all typed as a plain group command: a brand-new short message, never an edit — nothing to preserve yet. */
async function sendGroupAllRedirect(env: Env, chatId: string): Promise<void> {
  const replyMarkup = allRedirectKeyboard(env.TELEGRAM_BOT_USERNAME);
  const text = replyMarkup ? ALL_GROUP_TEXT : ALL_GROUP_TEXT_NO_LINK;
  await tgApi(env, "sendMessage", { chat_id: chatId, text, ...(replyMarkup ? { reply_markup: replyMarkup } : {}) });
}

/** Best-effort "⏳ Building your picks..." indicator; a failure here must never block the real result. */
async function sendLoadingIndicator(env: Env, chatId: string): Promise<number | undefined> {
  try {
    const res = await tgApi(env, "sendMessage", { chat_id: chatId, text: LOADING_TEXT });
    const body = (await res.json()) as { result?: { message_id?: number } };
    return body.result?.message_id;
  } catch {
    return undefined;
  }
}

/** Best-effort cleanup; an already-gone/edited loading message must never block the real result. */
async function clearLoadingIndicator(env: Env, chatId: string, messageId: number | undefined): Promise<void> {
  if (messageId == null) return;
  try {
    await tgApi(env, "deleteMessage", { chat_id: chatId, message_id: messageId });
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
    const msg = cb.message as Record<string, unknown> | undefined;
    const chatId = String((msg?.chat as Record<string, unknown> | undefined)?.id ?? (cb.from as Record<string, unknown> | undefined)?.id ?? "");
    if (data.startsWith("cp|")) {
      await handleCustomPicksSelect(env, cb, cbId, chatId, msg);
      return;
    }
    const parsed = parseCallback(data);
    if (!parsed) {
      await answerCallback(env, cbId);
      return;
    }
    const chatType = String((msg?.chat as Record<string, unknown> | undefined)?.type ?? "private");

    // All Predictions never renders in a group — whether this is a brand
    // new tap (shouldn't happen once the keyboard itself is fixed, see
    // telegram.ts allButtonModeFor) or a tap on an OLD group message from
    // before this redirect existed, still carrying a live pg|all*|... button.
    // Answered via the callback's own deep link (never an edit/send into the
    // group): editing the tapped message would also overwrite every OTHER
    // button sharing that same keyboard (Top/Daily/Stats/Yesterday), which
    // is exactly the menu-disappearing regression this fixes — the rest of
    // the group menu, on that message and everywhere else, is left intact.
    if (chatType !== "private" && (parsed.view === "all" || parsed.view.startsWith("all_"))) {
      await answerCallbackWithDeepLink(env, cbId, "all");
      return;
    }

    // Group taps on paged views are acknowledged without re-render spam;
    // private chats get the page edit. (Closest clean alternative to
    // per-member group welcomes, which read-only groups cannot do.)
    const { text, replyMarkup } = await renderView(env, parsed.view, parsed.page, parsed.day, chatType);
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

  // /start all (the group-facing All Predictions deep link,
  // https://t.me/<bot>?start=all) — Telegram always sends this as a plain
  // text message in the private chat it just opened, whether or not the
  // user had ever started the bot before (deep links don't need a prior
  // /start). Shows today's All Predictions immediately, same pagination/
  // formatting as typing /all directly.
  if (text.startsWith("/start")) {
    const startParam = text.trim().split(/\s+/)[1];
    if (startParam === "all") {
      if (chatType !== "private") return; // this deep link only ever opens a private chat.
      if (userId) await leaveCustomSession(env.DB, userId, chatId);
      await upsertSubscriber(env.DB, chatId, {
        username: (from.username as string) ?? null,
        firstName: (from.first_name as string) ?? null,
      });
      const { text: body, replyMarkup } = await renderView(env, "all", 0, null, chatType);
      await tgApi(env, "sendMessage", { chat_id: chatId, text: body, parse_mode: "HTML", reply_markup: replyMarkup });
      return;
    }
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
      reply_markup: menuKeyboard(allButtonModeFor(chatType, env.TELEGRAM_BOT_USERNAME)),
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
      const nowMs = Date.now();
      await recordCustomRequest(env.DB, userId, chatId, nowMs);
      const zone = env.TIMEZONE ?? "Africa/Lagos";
      const day = lagosDateString(nowMs);
      const previousJson = await getConversationRequest(env.DB, userId, chatId, nowMs);
      const previous = previousJson ? deserializeRequest(JSON.parse(previousJson)) : null;
      // A visible "building your picks" status while the request is
      // processed — best-effort (see sendLoadingIndicator): a failure here
      // must never block the real ticket. Cleaned up on both the success
      // and error paths so a crashed/retried attempt never leaves a stuck
      // "⏳ Building..." message sitting in the chat.
      const loadingMessageId = await sendLoadingIndicator(env, chatId);
      try {
        const { result, request } = await customPicksRespond(
          {
            getPool: (d, z) => getCustomPickPool(env.DB, d, z),
            getCandidates: (d, z) => getCustomPickCandidates(env.DB, d, z),
          },
          text,
          zone,
          day,
          nowMs,
          previous,
        );
        if (request) {
          const expiresAtMs = Math.min(nowMs + CUSTOM_PICKS_TTL_MS, lagosDayBounds(day).endUtcMs);
          await setConversationRequest(env.DB, userId, chatId, JSON.stringify(serializeRequest(request)), expiresAtMs);
        }
        const replyMarkup = await storeAndBuildSuggestionKeyboard(env, result.suggestions, userId, chatId, nowMs, day);
        await clearLoadingIndicator(env, chatId, loadingMessageId);
        await tgApi(env, "sendMessage", {
          chat_id: chatId,
          text: result.text,
          parse_mode: "HTML",
          ...(replyMarkup ? { reply_markup: replyMarkup } : {}),
        });
      } catch (err) {
        await clearLoadingIndicator(env, chatId, loadingMessageId);
        throw err;
      }
      return;
    }
  }

  // Custom Picks alternative-suggestion button tap (~ custom_recovery.py
  // Recovery.select, callback_data "cp|<token>"). Handled here rather than
  // the generic pg|view|... callback router, which is a different codec.

  const cmd = normalizeCommand(text);
  if (cmd) {
    const view = commandToView(cmd) ?? "top";
    // /all typed directly in a group: redirect to the private bot instead
    // of rendering the (long) prediction list into the group.
    if (view === "all" && chatType !== "private") {
      await sendGroupAllRedirect(env, chatId);
      return;
    }
    if (chatType === "private") {
      await upsertSubscriber(env.DB, chatId, {
        username: (from.username as string) ?? null,
        firstName: (from.first_name as string) ?? null,
      });
    }
    const { text: body, replyMarkup } = await renderView(env, view, 0, null, chatType);
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

const CALLBACK_PREFIX = "cp|";

/** Persists each proven Suggestion as a redeemable button (~ custom_recovery.py prepare()'s token row). */
async function storeAndBuildSuggestionKeyboard(
  env: Env,
  suggestions: Suggestion[],
  userId: string,
  chatId: string,
  nowMs: number,
  day: string,
): Promise<Record<string, unknown> | undefined> {
  if (!suggestions.length) return undefined;
  const expiresAtMs = Math.min(
    nowMs + CUSTOM_PICKS_TTL_MS,
    lagosDayBounds(day).endUtcMs,
    ...suggestions.flatMap((s) => s.ticket.picks.map((p) => p.startMs)),
  );
  const rows: { text: string; callback_data: string }[][] = [];
  for (const s of suggestions) {
    const token = crypto.randomUUID();
    await storeSuggestion(
      env.DB,
      token,
      userId,
      chatId,
      s.label,
      JSON.stringify(serializeRequest(s.request)),
      JSON.stringify(s.ticket),
      nowMs,
      expiresAtMs,
    );
    rows.push([{ text: s.label, callback_data: `${CALLBACK_PREFIX}${token}` }]);
  }
  return { inline_keyboard: rows };
}

/**
 * ~ custom_recovery.py Recovery.select(): redeem a tapped suggestion
 * button. Ownership-checked (private chat, same user+chat the button was
 * prepared for), re-validated (never serves a ticket that's gone stale —
 * e.g. a leg has since kicked off), and if the specific token is gone,
 * offers whichever OTHER suggestions from the same prepare() call are
 * still valid, exactly as production does, rather than silently
 * substituting a different ticket.
 */
async function handleCustomPicksSelect(
  env: Env,
  cb: Record<string, unknown>,
  cbId: string,
  chatId: string,
  msg: Record<string, unknown> | undefined,
): Promise<void> {
  const userId = String((cb.from as Record<string, unknown> | undefined)?.id ?? "");
  const chatType = String((msg?.chat as Record<string, unknown> | undefined)?.type ?? "private");
  const token = String((cb.data as string) ?? "").slice(CALLBACK_PREFIX.length);
  const nowMs = Date.now();
  if (chatType !== "private" || !userId || !chatId) {
    await answerCallback(env, cbId, "Open your own Custom Picks options in a private chat with the bot.");
    return;
  }
  const row = await getSuggestion(env.DB, token);
  if (row && (row.user_id !== userId || row.chat_id !== chatId)) {
    await answerCallback(env, cbId, "Open your own Custom Picks options in a private chat with the bot.");
    return;
  }
  if (row && Date.parse(row.expires_at) > nowMs) {
    const ticket = JSON.parse(row.ticket_json);
    const request = deserializeRequest(JSON.parse(row.request_json));
    if (validTicket(ticket, request, nowMs)) {
      await answerCallback(env, cbId);
      await tgApi(env, "sendMessage", { chat_id: chatId, text: formatTicket(ticket), parse_mode: "HTML" });
      return;
    }
  }
  // Token missing/expired/stale: offer any other still-valid suggestions
  // from this same user+chat's last prepare() call instead of a dead end.
  const replacements = (await listActiveSuggestions(env.DB, userId, chatId, nowMs)).filter((s: SuggestionRow) => {
    try {
      return validTicket(JSON.parse(s.ticket_json), deserializeRequest(JSON.parse(s.request_json)), nowMs);
    } catch {
      return false;
    }
  });
  await answerCallback(env, cbId);
  if (replacements.length) {
    const keyboard = {
      inline_keyboard: replacements.slice(0, 3).map((s) => [{ text: s.label, callback_data: `${CALLBACK_PREFIX}${s.token}` }]),
    };
    await tgApi(env, "sendMessage", { chat_id: chatId, text: COPY_EXPIRED, reply_markup: keyboard });
    return;
  }
  await tgApi(env, "sendMessage", { chat_id: chatId, text: COPY_EXPIRED_EMPTY });
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
