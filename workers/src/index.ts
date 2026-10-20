// Stage 1 Worker entrypoint: secure Telegram webhook + health + Cron stub.
// Intrusion surface is intentionally small: no provider calls, no generation,
// no spending. Slow work belongs in GitHub Actions (Stage 2) or later Crons.

import {
  QuotaExhausted,
  completeUpdate,
  deactivateSubscriber,
  enterCustomSession,
  failUpdate,
  getConversationRequest,
  getCustomPickCandidates,
  getCustomPickPool,
  getPinnedMenu,
  getSuggestion,
  isCustomOnCooldown,
  isCustomSessionActive,
  leaveCustomSession,
  listActiveSuggestions,
  recordCustomRequest,
  setConversationRequest,
  setPinnedMenu,
  storeSuggestion,
  tryClaimUpdate,
  upsertSubscriber,
  type Env,
  type SuggestionRow,
} from "./db.ts";
import {
  availableMarkets,
  availableSports,
  availableTotalLines,
  BUSY,
  clearTotalLine,
  COPY_EXPIRED,
  COPY_EXPIRED_EMPTY,
  CUSTOM_PICKS_TTL_MS,
  deserializeRequest,
  formatTicket,
  freshRequest,
  introText,
  loadPool,
  MARKET_LABEL,
  MARKET_REPLY,
  parseEnabledSports,
  respond as customPicksRespond,
  searchWithRequest,
  serializeRequest,
  SPORT_LABEL,
  type ParsedRequest,
  type Suggestion,
  validTicket,
} from "./custom_picks.ts";
import {
  isAdmin, restrictedView, welcomeMessage,
  allButtonModeFor,
  commandToView,
  communityMenuKeyboard,
  customPicksButton,
  isAuthorized,
  menuKeyboard,
  normalizeCommand,
  parseCallback,
  splitCommand,
} from "./telegram.ts";
import { lagosDateString, lagosDayBounds, renderView } from "./views.ts";
import { splitMessage } from "./format.ts";
import { postGroupPicks } from "./cron.ts";

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

/** The /command a group tap's no-username fallback alert points to, per view (bug 10). */
const VIEW_FALLBACK_COMMAND: Record<string, string> = {
  all: "/all",
  top: "/top",
  daily: "/daily",
  stats: "/stats",
  yesterday: "/yesterday",
};
const LOADING_TEXT = "⏳ Building your picks...";

const COMMUNITY_MENU_TEXT = "⚽ Predictions\n\nToday's picks, results and more.";

export { welcomeMessage } from "./telegram.ts";

// Splits an over-long sendMessage (Telegram's 4096-char limit) into several
// calls, and redirects an over-long editMessageText into split sendMessage
// calls, before handing off to tgApiOnce (below) for the actual HTTP work
// and its failure semantics — see that function's own comment.
export async function tgApi(env: Env, method: string, payload: Record<string, unknown>): Promise<Response> {
  const text = payload.text;
  if (method === "editMessageText" && typeof text === "string" && text.length > 4000) {
    // Telegram's 4096-char limit can't be worked around by editing one message
    // into several - fall back to sending new (possibly itself split, below)
    // messages in the same chat rather than attempting an edit that would
    // fail or silently truncate. message_id refers to the message being
    // edited, which no longer applies once this becomes a fresh send.
    const { message_id, ...rest } = payload;
    return tgApi(env, "sendMessage", rest);
  }
  if (method === "sendMessage" && typeof text === "string") {
    const chunks = splitMessage(text);
    if (chunks.length > 1) {
      // reply_markup refers to the whole message, which the reader only has
      // once every chunk has arrived - attach it to the LAST chunk only. The
      // returned Response is the FIRST chunk's, so a caller reading
      // message_id (e.g. pinCommunityMenu) gets the same receipt-relevant id
      // a single send always returned.
      let first: Response | undefined;
      for (let i = 0; i < chunks.length; i++) {
        const chunkPayload = { ...payload, text: chunks[i] };
        if (i < chunks.length - 1) delete chunkPayload.reply_markup;
        const res = await tgApiOnce(env, method, chunkPayload);
        if (i === 0) first = res;
      }
      return first as Response;
    }
  }
  return tgApiOnce(env, method, payload);
}

// Throws on a non-2xx response instead of returning it silently: previously
// routeUpdate never inspected the Response it got back, so a failed
// sendMessage/editMessageText (Telegram 5xx/4xx) was treated as success —
// the update was marked `done` even though the user was never actually
// messaged, and no retry ever happened. Throwing routes the failure through
// handleUpdate's existing catch -> failUpdate path, so a transient Telegram
// failure is retried exactly like a D1 failure already is.
async function tgApiOnce(env: Env, method: string, payload: Record<string, unknown>): Promise<Response> {
  const res = await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/${method}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    // Telegram's error body (e.g. "Bad Request: chat not found", "Forbidden:
    // bot was kicked from the group chat") is far more actionable than the
    // bare HTTP status — surfaced here so every caller's failure message
    // (admin confirmations in particular) can explain WHY, not just that it
    // failed. Reading the body can itself throw on a malformed response;
    // that's swallowed, falling back to the status alone.
    let description = "";
    try {
      const body = (await res.json()) as { description?: string };
      description = body.description ?? "";
    } catch {
      // not JSON — fall back to the status code alone. (Safe to read here:
      // tgApi throws immediately after, so nothing downstream still needs
      // this response's body.)
    }
    throw new TelegramApiError(res.status, description, method);
  }
  return res;
}

/**
 * Carries the HTTP status and Telegram's own description so a caller (just
 * handleUpdate, today) can tell a PERMANENT failure (403 bot-blocked/kicked,
 * 400 bad-request) from a RETRYABLE one (429 rate limit, 5xx outage)
 * without re-parsing a formatted message string.
 */
export class TelegramApiError extends Error {
  status: number;
  description: string;
  constructor(status: number, description: string, method: string) {
    super(`telegram ${method} failed: ${status}${description ? ` (${description})` : ""}`);
    this.name = "TelegramApiError";
    this.status = status;
    this.description = description;
  }
}

/**
 * Production incident (2026-10-15, first real Cron-triggered traffic after
 * activation): a user re-tapped a button whose re-render produced content
 * byte-identical to what was already shown (e.g. re-tapping the current
 * page). Telegram correctly rejects that edit with 400 "message is not
 * modified" — but the update was then recorded as `failed` and retried,
 * and every retry hit the exact same error forever, since the one thing
 * that would fix it (the content changing) never happens on a retry.
 * This is not a delivery failure: the user already sees the correct
 * content. Treating it as success is what makes the update completable.
 */
async function editMessageTextIdempotent(env: Env, payload: Record<string, unknown>): Promise<void> {
  try {
    await tgApi(env, "editMessageText", payload);
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    if (/message is not modified/i.test(message)) return;
    throw err;
  }
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
 * field is documented for opening "t.me/your_bot?start=XXXX" links) —
 * touches nothing about the original group message, unlike the regression
 * in 4135f81 (editMessageText on the tapped message, which silently
 * replaced its ENTIRE keyboard — Top/Daily/Stats/Yesterday sitting right
 * alongside All — making the rest of the group menu vanish).
 *
 * Used for EVERY view a group keyboard button can point at (bug 10: a tap
 * used to post a brand new full card into the group on every tap, for
 * every view except "all", which this generalizes to). `fallbackCommand`
 * (e.g. "/top") is shown only when no TELEGRAM_BOT_USERNAME is configured,
 * so the alert is never a dead link.
 */
async function answerCallbackWithDeepLink(env: Env, id: string, startParam: string, fallbackCommand: string): Promise<void> {
  if (!id) return;
  try {
    if (env.TELEGRAM_BOT_USERNAME) {
      await tgApi(env, "answerCallbackQuery", {
        callback_query_id: id,
        url: `https://t.me/${env.TELEGRAM_BOT_USERNAME}?start=${startParam}`,
      });
    } else {
      await tgApi(env, "answerCallbackQuery", {
        callback_query_id: id,
        text: `This is available in a private chat with me — open a DM and send ${fallbackCommand}.`,
        show_alert: true,
      });
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
  const replyMarkup = env.TELEGRAM_BOT_USERNAME ? communityMenuKeyboard(env.TELEGRAM_BOT_USERNAME) : undefined;
  const text = "Open a private chat using the prediction buttons below.";
  await tgApi(env, "sendMessage", { chat_id: chatId, text, ...(replyMarkup ? { reply_markup: replyMarkup } : {}) });
}

const PRIVATE_CHAT_TEXT = "Use this command in a private chat with the bot.";

/**
 * A group-typed private-only deep link (/start custom_picks): a short,
 * fresh redirect message — never an edit, never a session. Mirrors
 * production's "Use this command in a private chat with the bot." reply,
 * plus the Custom Picks deep-link button when the username is configured
 * (plain text otherwise, so no dead link is ever shown).
 */
async function sendPrivateChatRedirect(env: Env, chatId: string, startParam: string): Promise<void> {
  await sendGroupAllRedirect(env, chatId);
}

/**
 * /pinmenu's implementation: posts (or refreshes in place) the exact same
 * community menu /menu already sends, then pins it — reporting the result
 * back to the admin privately. Reuses communityMenuKeyboard/COMMUNITY_MENU_TEXT
 * verbatim (same labels, same group-to-private All/Custom Picks buttons).
 *
 * Duplicate-post prevention + "replace safely" (requirements 7/8): if a
 * previous /pinmenu run is on record for this group, the SAME message is
 * edited in place rather than posting a new one — editing a pinned message
 * never unpins it, so nothing needs re-pinning and no other message or
 * admin's pin is ever touched. Only when that edit fails (message deleted,
 * bot removed, etc.) does this fall back to posting + pinning a fresh one.
 */
async function pinCommunityMenu(env: Env, adminChatId: string): Promise<void> {
  const groupChatId = env.TELEGRAM_CHAT_ID as string;
  const botUsername = env.TELEGRAM_BOT_USERNAME as string;
  const previous = await getPinnedMenu(env.DB, groupChatId);

  if (previous) {
    try {
      await editMessageTextIdempotent(env, {
        chat_id: groupChatId,
        message_id: previous.message_id,
        text: COMMUNITY_MENU_TEXT,
        reply_markup: communityMenuKeyboard(botUsername),
      });
      await tgApi(env, "sendMessage", { chat_id: adminChatId, text: "✅ Menu refreshed." });
      return;
    } catch {
      // The previously pinned message is gone/unpinned/uneditable — fall
      // through and post+pin a fresh one below.
    }
  }

  let messageId: number | undefined;
  try {
    const res = await tgApi(env, "sendMessage", {
      chat_id: groupChatId,
      text: COMMUNITY_MENU_TEXT,
      reply_markup: communityMenuKeyboard(botUsername),
    });
    const body = (await res.json()) as { result?: { message_id?: number } };
    messageId = body.result?.message_id;
  } catch (err) {
    // The detailed Telegram error (chat not found, bot not a member, etc.)
    // is logged for operator visibility but kept out of the admin-facing
    // message, which stays a short, simple failure notice.
    console.error("pinCommunityMenu: group post failed:", err instanceof Error ? err.message : err);
    await tgApi(env, "sendMessage", { chat_id: adminChatId, text: "❌ Failed to post the menu." });
    return;
  }
  if (messageId == null) {
    await tgApi(env, "sendMessage", { chat_id: adminChatId, text: "❌ Failed to post the menu." });
    return;
  }
  try {
    await tgApi(env, "pinChatMessage", { chat_id: groupChatId, message_id: messageId, disable_notification: true });
  } catch (err) {
    console.error("pinCommunityMenu: pin failed:", err instanceof Error ? err.message : err);
    await tgApi(env, "sendMessage", { chat_id: adminChatId, text: "⚠️ Menu posted, but I need the 'Pin Messages' permission to pin it." });
    return;
  }
  await setPinnedMenu(env.DB, groupChatId, messageId, adminChatId, new Date().toISOString());
  await tgApi(env, "sendMessage", { chat_id: adminChatId, text: "✅ Menu posted and pinned." });
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

/** The chat an update is about, same resolution routeUpdate's callback branch already uses. */
function extractChat(update: Record<string, unknown>): { chatId: string; chatType: string } {
  const cb = update.callback_query as Record<string, unknown> | undefined;
  const msg = (cb?.message ?? update.message) as Record<string, unknown> | undefined;
  const chat = msg?.chat as Record<string, unknown> | undefined;
  const chatId = String(chat?.id ?? (cb?.from as Record<string, unknown> | undefined)?.id ?? "");
  const chatType = String(chat?.type ?? "private");
  return { chatId, chatType };
}

// Telegram error descriptions that mean the user is permanently unreachable
// (blocked the bot / deleted their account) rather than some transient chat
// issue (e.g. "chat not found" from a deleted group, which is also a
// permanent 400 but not a reason to deactivate a PRIVATE-chat subscriber).
const USER_GONE = /blocked by the user|user is deactivated/i;

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
    // A PERMANENT Telegram failure (403 bot-blocked/kicked, or 400 other than
    // "message is not modified", which editMessageTextIdempotent already
    // handles before this ever sees it) will never succeed on retry — the
    // thing that would fix it (the user unblocking the bot, the chat
    // existing again) can't happen from here. Retrying it 10 times the way a
    // transient 429/5xx is retried just wastes the whole retry budget on a
    // guaranteed failure. Mark it done instead: there is nothing more this
    // Worker can do for this update.
    if (err instanceof TelegramApiError && (err.status === 403 || err.status === 400)) {
      console.error(JSON.stringify({ event: "permanent_telegram_error", status: err.status, description: err.description }));
      const { chatId, chatType } = extractChat(update);
      if (chatType === "private" && chatId && USER_GONE.test(err.description)) {
        await deactivateSubscriber(env.DB, chatId);
      }
      await completeUpdate(env.DB, updateId);
      return;
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
    if (data.startsWith("cq|")) {
      await handleCustomPicksClarify(env, cb, cbId, chatId, msg);
      return;
    }
    const parsed = parseCallback(data);
    if (!parsed) {
      await answerCallback(env, cbId);
      return;
    }
    const from = cb.from as Record<string, unknown> | undefined;
    const admin = !msg?.sender_chat && from?.username !== 'GroupAnonymousBot' && isAdmin(env, from?.id);
    const chatType = String((msg?.chat as Record<string, unknown> | undefined)?.type ?? "private");

    // No view ever renders or sends anything new into a GROUP on a button
    // tap (bug 10: previously only "all" was redirected this way — top,
    // daily, stats and yesterday each posted a brand-new full card into the
    // group on EVERY tap). Every group tap is answered via the callback's
    // own deep link instead, whether it's a brand new tap (shouldn't happen
    // once the keyboard itself only ever offers deep-link buttons in a
    // group, see telegram.ts allButtonModeFor) or a tap on an OLD group
    // message from before this redirect existed. Editing/sending into the
    // group would also disturb every OTHER button sharing that keyboard —
    // the rest of the group menu, on that message and everywhere else, is
    // left untouched either way.
    if (chatType !== "private") {
      if (restrictedView(parsed.view)) {
        await answerCallback(env, cbId);
        if (admin) {
          const rendered = await renderView(env, parsed.view, parsed.page, parsed.day, 'private', true);
          await tgApi(env, 'sendMessage', {chat_id: String(from?.id), text: rendered.text, parse_mode:'HTML', reply_markup:rendered.replyMarkup});
        }
        return;
      }
      const startParam = parsed.view.startsWith("all") ? "all" : parsed.view;
      await answerCallbackWithDeepLink(env, cbId, startParam, VIEW_FALLBACK_COMMAND[startParam] ?? "/all");
      return;
    }

    // Private chats get the real page edit.
    const { text, replyMarkup } = await renderView(env, parsed.view, parsed.page, parsed.day, chatType, admin);
    await answerCallback(env, cbId);
    if (!chatId) return;
    const messageId = (msg as Record<string, unknown> | undefined)?.message_id;
    if (messageId) {
      await editMessageTextIdempotent(env, {
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
  // splitCommand normalizes @BotName suffixes and case, exactly like
  // production's split_command(); the deep-link parameter itself stays an
  // exact match, as in production (`args == [CUSTOM_PICKS_START_PARAMETER]`).
  const userId = String(from.id ?? "");
  const { cmd, args } = splitCommand(text);
  const admin = !msg.sender_chat && from.username !== 'GroupAnonymousBot' && isAdmin(env, from.id);
  // WHY: the group is a game-content channel, not a command/help conversation.
  const requestedView = commandToView(cmd) ?? (cmd === '/start' ? args[0] : null);
  if (chatType !== 'private') {
    if (requestedView && restrictedView(requestedView)) {
      if (admin) {
        const rendered = await renderView(env, requestedView, 0, null, 'private', true);
        await tgApi(env, 'sendMessage', { chat_id: userId, text: rendered.text, parse_mode: 'HTML', reply_markup: rendered.replyMarkup });
      }
      return;
    }
    if (cmd === '/menu' || cmd === '/pinmenu') {
      if (!admin) return;
    } else {
      if (requestedView || cmd === '/start') await sendGroupAllRedirect(env, chatId);
      return;
    }
  }
  if (chatType === 'private' && await handleGifAdmin(env, msg, userId, chatId, cmd, args, admin)) return;
  if (cmd === "/start" && args.length === 1 && args[0] === "custom_picks") {
    if (chatType !== "private" || !userId) {
      // Custom Picks has no group path — redirect to a private chat instead
      // of going silent (production: "Use this command in a private chat
      // with the bot."). A fresh message; nothing existing is edited.
      await sendPrivateChatRedirect(env, chatId, "custom_picks");
      return;
    }
    await enterCustomSession(env.DB, userId, chatId);
    await tgApi(env, "sendMessage", { chat_id: chatId, text: introText(parseEnabledSports(env.ALLOWED_SPORTS)) });
    return;
  }

  // /start <view> (any group deep-link button's target, e.g.
  // https://t.me/<bot>?start=top — generalized, bug 10, from "all" being
  // the only param this ever worked for) — Telegram always sends this as a
  // plain text message in the private chat it just opened, whether or not
  // the user had ever started the bot before (deep links don't need a
  // prior /start). Shows that view immediately, same pagination/formatting
  // as typing the matching command directly. An unrecognized param falls
  // through to the plain /start welcome below.
  if (cmd === "/start" && args.length === 1 && Object.hasOwn(VIEW_FALLBACK_COMMAND, args[0])) {
    const view = args[0];
    if (chatType !== "private") {
      // Only reachable if this was literally typed in a group (a deep link
      // itself always opens a private chat) — "all"'s own redirect copy/
      // button still applies well enough as a generic "open a DM" nudge.
      await sendGroupAllRedirect(env, chatId);
      return;
    }
    if (userId) await leaveCustomSession(env.DB, userId, chatId);
    await upsertSubscriber(env.DB, chatId, {
      username: (from.username as string) ?? null,
      firstName: (from.first_name as string) ?? null,
    });
    const { text: body, replyMarkup } = await renderView(env, view, 0, null, chatType, admin);
    await tgApi(env, "sendMessage", { chat_id: chatId, text: body, parse_mode: "HTML", reply_markup: replyMarkup });
    return;
  }

  // /start subscribes (private) and always shows the menu; group /start
  // shows the menu without subscribing the group. Also leaves any active
  // Custom Picks session (predbot's `_custom_leave` on plain /start).
  if (cmd === "/start") {
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
      reply_markup: menuKeyboard(allButtonModeFor(chatType, env.TELEGRAM_BOT_USERNAME, admin)),
    });
    return;
  }

  // /menu (admin-only): posts the community menu — including the Custom
  // Picks deep-link button — to the GROUP, regardless of which chat /menu
  // was typed in (predbot/bot/commands.py:542-545, _post_community_menu).
  // This is the group's normal (non-empty-Top-card) Custom Picks entry
  // point; the empty-Top-card keyboard (above) is the other one.
  if (cmd === "/menu" && args.length === 0) {
    if (!admin) {
      await tgApi(env, "sendMessage", { chat_id: chatType === "private" ? chatId : userId, text: "Not allowed." });
      return;
    }
    if (!env.TELEGRAM_CHAT_ID) {
      await tgApi(env, "sendMessage", { chat_id: chatType === "private" ? chatId : userId, text: "Community destination is not configured. Set TELEGRAM_CHAT_ID." });
      return;
    }
    if (!env.TELEGRAM_BOT_USERNAME) {
      await tgApi(env, "sendMessage", {
        chat_id: chatType === "private" ? chatId : userId,
        text: "Could not resolve this bot's username. Community menu was not posted. Try /menu again.",
      });
      return;
    }
    await tgApi(env, "sendMessage", {
      chat_id: env.TELEGRAM_CHAT_ID,
      text: COMMUNITY_MENU_TEXT,
      reply_markup: communityMenuKeyboard(env.TELEGRAM_BOT_USERNAME),
    });
    await tgApi(env, "sendMessage", { chat_id: chatType === "private" ? chatId : userId, text: "Community menu posted." });
    return;
  }

  // /pinmenu (admin-only, private chat only): posts (or refreshes in place)
  // the SAME community menu as /menu, then pins it in the group. A
  // separate command from /menu rather than changing /menu's own meaning —
  // /menu already works from any chat and never pins, and the task that
  // asked for this explicitly said to preserve that existing behavior
  // untouched and add a new command instead of overloading it.
  if (cmd === "/pinmenu" && args.length === 0) {
    if (chatType !== "private") return; // admin command, private chat only — silently ignored elsewhere.
    if (!admin) {
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
        text: "Could not resolve this bot's username. Menu was not posted. Try /pinmenu again.",
      });
      return;
    }
    await pinCommunityMenu(env, chatId);
    return;
  }

  // /postpicks (admin-only, private chat only): manual stopgap for
  // postGroupPicks's own Cron sweep (CRON_POST_SWEEP) — e.g. a slow/late
  // GitHub Actions generation run, or a Cron Trigger that hasn't started
  // firing yet after a schedule change (observed in production,
  // 2026-10-16: a newly-registered trigger took well over an hour to start
  // executing). Calls the exact same postGroupPicks used by the sweep, so
  // it is just as idempotent: the claimPost-based receipt means running
  // this command twice, or running it right before/after the sweep itself
  // finally fires, can still send at most once per kind per day — never a
  // duplicate group message.
  if (cmd === "/postpicks" && args.length === 0) {
    if (chatType !== "private") return; // admin command, private chat only — silently ignored elsewhere.
    if (!admin) {
      await tgApi(env, "sendMessage", { chat_id: chatId, text: "Not allowed." });
      return;
    }
    const top = await postGroupPicks(env, "top");
    const daily = await postGroupPicks(env, "daily");
    await tgApi(env, "sendMessage", { chat_id: chatId, text: `Top: ${top}\nDaily: ${daily}` });
    return;
  }

  // /stop leaves any active Custom Picks session and marks the subscriber
  // inactive, with a confirmation. Bug 11: the old text ("no more daily
  // picks") promised an automatic DM delivery that sweepDeliveries never
  // actually sends in production (SUBSCRIBER_DELIVERY_ENABLED=false) — this
  // no longer claims anything was ever being pushed, only that /stop was
  // received. The subscriber row is deactivated in every chat type (group
  // delivery is receipt-based, so deactivating a group chat_id is a
  // harmless no-op there); the session leave is private-chat-only, where
  // sessions exist.
  if (cmd === "/stop" && args.length === 0) {
    if (chatType === "private" && userId) await leaveCustomSession(env.DB, userId, chatId);
    await deactivateSubscriber(env.DB, chatId);
    await tgApi(env, "sendMessage", {
      chat_id: chatId,
      text: "🛑 Stopped. Send /start any time to come back.",
    });
    return;
  }

  // Free-text routing into an active Custom Picks session — private chat
  // only, mirrors commands.py:356-358. Checked before the generic command
  // lookup so a Custom Picks reply (e.g. "3 basketball picks") is never
  // mistaken for an unknown command.
  if (chatType === "private" && userId && text.trim() !== "" && !text.startsWith("/")) {
    if (await isCustomSessionActive(env.DB, userId, chatId)) {
      await processCustomPicksText(env, userId, chatId, text);
      return;
    }
  }

  // Custom Picks alternative-suggestion button tap (~ custom_recovery.py
  // Recovery.select, callback_data "cp|<token>"). Handled here rather than
  // the generic pg|view|... callback router, which is a different codec.

  const viewCmd = normalizeCommand(text);
  if (viewCmd) {
    const view = commandToView(viewCmd) ?? "top";
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
    const { text: body, replyMarkup } = await renderView(env, view, 0, null, chatType, admin);
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
  if (chatType === "private" && (text.trim() !== "" || msg.animation || msg.document || msg.video)) {
    await tgApi(env, "sendMessage", {
      chat_id: chatId,
      text: welcomeMessage(String(from.first_name ?? "there")),
      parse_mode: "HTML",
      reply_markup: menuKeyboard(admin ? "admin" : "callback"),
    });
  }
}

// WHY: collect state is durable across Worker instances, scoped to the actual admin sender.
async function handleGifAdmin(env: Env, msg: Record<string, any>, userId: string, chatId: string,
  cmd: string, args: string[], admin: boolean): Promise<boolean> {
  if (!admin) {
    if (['/setcommands','/addgif','/done','/gifs','/cleargifs'].includes(cmd)) {
      await tgApi(env,'sendMessage',{chat_id:chatId,text:welcomeMessage('there'),reply_markup:menuKeyboard()});
      return true;
    }
    return false;
  }
  const key = `gif_collect:${userId}`;
  if (cmd.startsWith('/')) await env.DB.prepare('DELETE FROM kv WHERE k=?1').bind(key).run();
  const reply = (text: string) => tgApi(env,'sendMessage',{chat_id:chatId,text});
  if (cmd === '/setcommands') {
    const regular = ['start','top','daily','all'].map(command=>({command,description:command}));
    const elevated = [...regular,...['stats','yesterday','gifs','addgif','cleargifs','done','setcommands','menu','pinmenu'].map(command=>({command,description:command}))];
    const scopes = [{type:'default'},...(env.TELEGRAM_ADMIN_IDS??'').split(',').map(s=>s.trim()).filter(Boolean).map(chat_id=>({type:'chat',chat_id}))];
    for (const scope of scopes) {
      try {
        const res=await tgApi(env,'setMyCommands',{scope,commands:scope.type==='default'?regular:elevated});
        const result=await res.json();
        await reply(`${JSON.stringify(scope)}: ${JSON.stringify(result)}`);
      } catch (err) { await reply(`${JSON.stringify(scope)}: ${err instanceof Error ? err.message : 'failed'}`); }
    }
    return true;
  }
  if (cmd === '/done') { await reply('GIF collection ended.'); return true; }
  if (cmd === '/addgif') {
    if (!['won','lost'].includes(args[0]) || args.length !== 1) { await reply('Use /addgif won or /addgif lost.'); return true; }
    await env.DB.prepare('INSERT INTO kv(k,v) VALUES(?1,?2) ON CONFLICT(k) DO UPDATE SET v=excluded.v')
      .bind(key,JSON.stringify({kind:args[0],expires:Date.now()+15*60000})).run();
    await reply(`Send ${args[0]} GIFs for the next 15 minutes, then /done.`);return true;
  }
  if (cmd === '/cleargifs') {
    if (['won','lost'].includes(args[0]) && args.length === 1) await env.DB.prepare('DELETE FROM result_gifs WHERE kind=?1').bind(args[0]).run();
    else {await reply('Use /cleargifs won or /cleargifs lost.');return true;}
  }
  if (cmd === '/gifs' || cmd === '/cleargifs') {
    const {results}=await env.DB.prepare('SELECT kind,COUNT(*) AS n FROM result_gifs GROUP BY kind').all<{kind:string;n:number}>();
    await reply(['won','lost'].map(k=>`${k}: ${results.find(r=>r.kind===k)?.n??0}`).join('\n'));return true;
  }
  const media=msg.animation ?? (['image/gif','video/mp4'].includes(msg.document?.mime_type)?msg.document:null) ?? msg.video;
  if (!media?.file_id || !media.file_unique_id) return false;
  const row=await env.DB.prepare('SELECT v FROM kv WHERE k=?1').bind(key).first<{v:string}>();
  let kind=String(msg.caption??'').trim().toLowerCase();
  if (row) {
    try {const state=JSON.parse(row.v);if(state.expires>Date.now())kind=state.kind;
      else await env.DB.prepare('DELETE FROM kv WHERE k=?1').bind(key).run();} catch { /* ignore invalid state */ }
  }
  if (!['won','lost'].includes(kind)) return false;
  await env.DB.prepare('INSERT OR IGNORE INTO result_gifs(kind,file_id,file_unique_id,added_at) VALUES(?1,?2,?3,?4)')
    .bind(kind,media.file_id,media.file_unique_id,new Date().toISOString()).run();
  const count=await env.DB.prepare('SELECT COUNT(*) AS n FROM result_gifs WHERE kind=?1').bind(kind).first<{n:number}>();
  await reply(`Added (${count?.n??0} ${kind} GIFs)`);return true;
}

/** Builds the {getPool, getCandidates} deps object respond()/loadPool() need,
 * bound to this Env's D1 — every Custom Picks call site wants the same pair. */
function customPicksDeps(env: Env) {
  return {
    getPool: (d: string, z: string) => getCustomPickPool(env.DB, d, z),
    getCandidates: (d: string, z: string) => getCustomPickCandidates(env.DB, d, z),
  };
}

/** Plain D1 read of today's candidate pool, for keyboard-building only (never
 * a provider call) — used when a clarification keyboard needs to know what's
 * actually available (Part C's "never advertise an option that can't be
 * selected"). Fails open to an empty list: a keyboard that can't confirm
 * availability shows no options rather than a possibly-wrong one. */
async function poolCandidates(env: Env, day: string, zone: string, nowMs: number) {
  const loaded = await loadPool(customPicksDeps(env), day, zone, nowMs);
  return loaded ? loaded.candidates : [];
}

const CANCEL_BUTTON = { text: "❌ Cancel", callback_data: "cq|x" };

/**
 * Builds the inline keyboard for whatever respond()/searchWithRequest just
 * answered — reusing the SAME pool read `eligible()` itself uses, so a
 * button is never offered for a sport/market/line nothing today actually
 * has (spec Part C "Dynamic options"). Appended BELOW any proven `cp|`
 * ticket-suggestion buttons (storeAndBuildSuggestionKeyboard) rather than
 * replacing them, so "no suitable predictions" still shows a real proven
 * alternative first, with Change sport/market/Cancel as a fallback under it.
 */
async function buildClarificationKeyboard(
  env: Env,
  reason: string,
  request: ParsedRequest | null,
  day: string,
  zone: string,
  nowMs: number,
  enabled: Set<string>,
  suggestionKeyboard: Record<string, unknown> | undefined,
): Promise<Record<string, unknown> | undefined> {
  const extraRows: { text: string; callback_data: string }[][] = [];

  if (reason === "ambiguous_line" && request?.ambiguousLine != null) {
    const line = request.ambiguousLine;
    extraRows.push([
      { text: `Over ${line}`, callback_data: `cq|r|over ${line}` },
      { text: `Under ${line}`, callback_data: `cq|r|under ${line}` },
    ]);
  } else if (reason === "invalid_request" || reason === "unsupported_sport" || reason === "ask_sport" || reason === "sport_unavailable") {
    const pool = await poolCandidates(env, day, zone, nowMs);
    const sports = availableSports(pool, request ?? freshRequest(), nowMs, enabled);
    for (const s of sports) extraRows.push([{ text: SPORT_LABEL[s] ?? s, callback_data: `cq|r|${s.replace(/_/g, " ")}` }]);
  } else if (reason === "market_unavailable") {
    const pool = await poolCandidates(env, day, zone, nowMs);
    const markets = availableMarkets(pool, request ?? freshRequest(), nowMs);
    for (const m of markets) {
      const reply = MARKET_REPLY[m];
      if (!reply) continue; // result3 has no standalone reply phrase — see MARKET_REPLY's comment
      extraRows.push([{ text: MARKET_LABEL[m] ?? m, callback_data: `cq|r|${reply}` }]);
    }
    extraRows.push([{ text: "🏷 Change sport", callback_data: "cq|k|sport" }]);
  } else if (reason === "line_unavailable" && request) {
    const pool = await poolCandidates(env, day, zone, nowMs);
    const lines = availableTotalLines(pool, request, nowMs);
    for (const l of lines.slice(0, 6)) {
      extraRows.push([{ text: `${l.selection === "over" ? "Over" : "Under"} ${l.line}`, callback_data: `cq|r|${l.selection} ${l.line}` }]);
    }
    if (lines.length) extraRows.push([{ text: "🎯 Any available line", callback_data: "cq|z|line" }]);
    extraRows.push([{ text: "🔁 Change market", callback_data: "cq|k|market" }]);
  } else if (reason === "invalid_count") {
    extraRows.push([1, 2, 3, 4].map((n) => ({ text: String(n), callback_data: `cq|r|${n} picks` })));
    extraRows.push([{ text: "✏️ Custom amount", callback_data: "cq|p|count" }]);
  } else if (
    ["below_quality_floor", "search_failed", "insufficient_count", "target_too_low", "target_unreachable", "all_started", "conflicting_constraints"].includes(reason)
  ) {
    // "No matching predictions": real proven alternatives already sit above
    // (suggestionKeyboard) when any exist — this is the fallback underneath.
    if (reason === "insufficient_count") {
      extraRows.push([1, 2, 3].map((n) => ({ text: String(n), callback_data: `cq|r|${n} picks` })));
    }
    extraRows.push([{ text: "🏷 Change sport", callback_data: "cq|k|sport" }, { text: "🔁 Change market", callback_data: "cq|k|market" }]);
  } else if (!suggestionKeyboard) {
    return undefined; // success / archive_unavailable / anything else: no buttons at all, unchanged from before this feature.
  }

  extraRows.push([CANCEL_BUTTON]);
  const existingRows = (suggestionKeyboard?.inline_keyboard as unknown[][] | undefined) ?? [];
  return { inline_keyboard: [...existingRows, ...extraRows] };
}

/** Persists the resolved request (if any) and delivers respond()'s result —
 * the common tail shared by a typed free-text reply, a replayed clarification
 * button, and a structured button action (clear-line) that skips parseRequest
 * entirely. Always sends exactly one message, with whatever keyboard
 * buildClarificationKeyboard computes for it. */
async function deliverCustomPicksResult(
  env: Env,
  userId: string,
  chatId: string,
  day: string,
  zone: string,
  nowMs: number,
  result: { text: string; reason: string; suggestions: Suggestion[] },
  request: ParsedRequest | null,
): Promise<void> {
  await recordCustomRequest(env.DB, userId, chatId, nowMs);
  if (request) {
    const expiresAtMs = Math.min(nowMs + CUSTOM_PICKS_TTL_MS, lagosDayBounds(day).endUtcMs);
    await setConversationRequest(env.DB, userId, chatId, JSON.stringify(serializeRequest(request)), expiresAtMs);
  }
  const suggestionKeyboard = await storeAndBuildSuggestionKeyboard(env, result.suggestions, userId, chatId, nowMs, day);
  const replyMarkup = await buildClarificationKeyboard(env, result.reason, request, day, zone, nowMs, parseEnabledSports(env.ALLOWED_SPORTS), suggestionKeyboard);
  await tgApi(env, "sendMessage", {
    chat_id: chatId,
    text: result.text,
    parse_mode: "HTML",
    ...(replyMarkup ? { reply_markup: replyMarkup } : {}),
  });
}

/**
 * Shared by a typed free-text reply AND a replayed clarification button (see
 * "cq|r|" below) — a button tap is just this same pipeline fed with the
 * button's own reply phrase as `text`, so it "behave[s] exactly like the
 * equivalent correctly interpreted text response" (spec Part D). Caller has
 * already verified an active session/cooldown is appropriate for the text
 * path; button taps re-check session liveness themselves (see
 * handleCustomPicksClarify) since the session may have expired between the
 * question being asked and the button being tapped.
 */
async function processCustomPicksText(env: Env, userId: string, chatId: string, text: string): Promise<void> {
  if (await isCustomOnCooldown(env.DB, userId, chatId, Date.now(), CUSTOM_PICKS_COOLDOWN_MS)) {
    await tgApi(env, "sendMessage", { chat_id: chatId, text: BUSY });
    return;
  }
  const nowMs = Date.now();
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
    const { result, request } = await customPicksRespond(customPicksDeps(env), text, zone, day, nowMs, previous, userId, parseEnabledSports(env.ALLOWED_SPORTS));
    // The cooldown timestamp is recorded ONLY after the request was
    // actually served: recording it up-front poisoned webhook retries —
    // a throw during respond() left last_request_at set, so the
    // redelivery was rejected with BUSY instead of doing the real work.
    // Cooldown enforcement for sequential requests is unchanged (a
    // completed request still stamps the time before any later message
    // is checked), and simultaneous duplicates stay fenced by the
    // update-claim dedupe above.
    await clearLoadingIndicator(env, chatId, loadingMessageId);
    await deliverCustomPicksResult(env, userId, chatId, day, zone, nowMs, result, request);
  } catch (err) {
    await clearLoadingIndicator(env, chatId, loadingMessageId);
    throw err;
  }
}

/**
 * Every Custom Picks clarification-button tap lands here, dispatched by its
 * "cq|<kind>|..." sub-prefix:
 *   r|<text>   replay <text> through the exact text pipeline (Over/Under,
 *              a sport name, a market phrase, a pick count — anything a
 *              typed reply could also say).
 *   k|<kind>   re-render a DIFFERENT clarification keyboard ("Change sport"/
 *              "Change market") from the currently stored request, without
 *              running a new search.
 *   z|line     a structured request edit no typed phrase can express
 *              ("Any available line": drop the stuck exact line, keep the
 *              market) — see custom_picks.ts clearTotalLine().
 *   p|count    prompt for free-text input ("Custom amount"); no state change.
 * Ownership is implicit: a private chat has exactly one user in it, so no
 * separate user/chat check beyond "is this a private chat" is needed (unlike
 * CALLBACK_PREFIX's "cp|" tickets, which can be tapped from a forwarded
 * message). A tap in a chat with no active session (expired, or simply never
 * started one) gets the same safe "expired" fallback either way — it never
 * resolves a question it has no record of ever having asked.
 */
async function handleCustomPicksClarify(env: Env, cb: Record<string, unknown>, cbId: string, chatId: string, msg: Record<string, unknown> | undefined): Promise<void> {
  const userId = String((cb.from as Record<string, unknown> | undefined)?.id ?? "");
  const chatType = String((msg?.chat as Record<string, unknown> | undefined)?.type ?? "private");
  if (chatType !== "private" || !userId || !chatId) {
    await answerCallback(env, cbId, "Open your own Custom Picks conversation in a private chat with the bot.");
    return;
  }
  const nowMs = Date.now();
  if (!(await isCustomSessionActive(env.DB, userId, chatId))) {
    await answerCallback(env, cbId);
    await tgApi(env, "sendMessage", { chat_id: chatId, text: "⏰ That question has expired\n\nSend /start to begin again." });
    return;
  }
  const data = String((cb.data as string) ?? "");
  const [, kind, ...rest] = data.split("|");
  const arg = rest.join("|");

  if (kind === "x") {
    await answerCallback(env, cbId);
    await leaveCustomSession(env.DB, userId, chatId);
    await tgApi(env, "sendMessage", { chat_id: chatId, text: "❌ Cancelled\n\nSend /start anytime to try again." });
    return;
  }

  if (kind === "p") {
    await answerCallback(env, cbId, "Type the number of picks you'd like (e.g. \"5 picks\").");
    return;
  }

  if (kind === "r") {
    await answerCallback(env, cbId);
    await processCustomPicksText(env, userId, chatId, arg);
    return;
  }

  if (kind === "k") {
    await answerCallback(env, cbId);
    const zone = env.TIMEZONE ?? "Africa/Lagos";
    const day = lagosDateString(nowMs);
    const previousJson = await getConversationRequest(env.DB, userId, chatId, nowMs);
    const previous = previousJson ? deserializeRequest(JSON.parse(previousJson)) : null;
    const base = previous ?? freshRequest();
    const enabled = parseEnabledSports(env.ALLOWED_SPORTS);
    const text = arg === "sport" ? "Which sport would you like?" : "Which market would you prefer?";
    const reason = arg === "sport" ? "sport_unavailable" : "market_unavailable";
    const keyboard = await buildClarificationKeyboard(env, reason, base, day, zone, nowMs, enabled, undefined);
    await tgApi(env, "sendMessage", { chat_id: chatId, text, ...(keyboard ? { reply_markup: keyboard } : {}) });
    return;
  }

  if (kind === "z" && arg === "line") {
    await answerCallback(env, cbId);
    const zone = env.TIMEZONE ?? "Africa/Lagos";
    const day = lagosDateString(nowMs);
    const previousJson = await getConversationRequest(env.DB, userId, chatId, nowMs);
    const previous = previousJson ? deserializeRequest(JSON.parse(previousJson)) : null;
    if (!previous) {
      await tgApi(env, "sendMessage", { chat_id: chatId, text: "⏰ That question has expired\n\nSend /start to begin again." });
      return;
    }
    const request = clearTotalLine(previous);
    const result = await searchWithRequest(customPicksDeps(env), request, "", zone, day, nowMs, userId);
    await deliverCustomPicksResult(env, userId, chatId, day, zone, nowMs, result.result, result.request);
    return;
  }

  await answerCallback(env, cbId);
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

  // Cron entrypoint — live in production (wrangler.toml's STAGE3_CRON_ENABLED
  // = "1" and its [[triggers]] crons). handleScheduled is a no-op only in an
  // environment that omits STAGE3_CRON_ENABLED (e.g. a fresh D1 with no
  // triggers configured yet).
  async scheduled(event: ScheduledEvent, env: Env, ctx: ExecutionContext): Promise<void> {
    const { handleScheduled } = await import("./cron.ts");
    ctx.waitUntil(handleScheduled(env, event.cron, Date.now()));
  },
};
