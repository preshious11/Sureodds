"""Telegram callback-query handlers, the /start menu and the shared view router.

Routing
    /top /daily /today /stats   typed commands  -> open_view()
    pg|{view}|{page}[|{day}]    inline buttons  -> open_view()

Commands and buttons deliberately share ONE handler: tapping 🏆 /top and typing
/top render the same snapshot slice with the same keyboard, so there is no
second implementation to drift out of sync.

Every read is a cache/database read (``UnifiedFetcher.get_daily_snapshot(allow_api_call=False)``):
a user view NEVER generates and never reaches a provider. Before today's scheduled
(or admin) generation has run, a view says the picks are not ready yet.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

from predbot.bot import pagination
from predbot import ticket_audit_switch
from predbot.bot.pagination import (VIEW_COMMAND, VIEW_DESC, VIEW_ICON,
                                    view_button_label)
from predbot.bot.telegram import Telegram, esc
from predbot.cache.fetcher import UnifiedFetcher
from predbot.cache.models import SPORT_VIEW_PREFIX, VIEW_ALL, VIEW_DAILY, VIEW_STATS, VIEW_TOP, VIEW_YESTERDAY
from predbot.config import Config
from predbot.storage.db import Database
from predbot.util.timeutil import resolve_timezone

log = logging.getLogger(__name__)


class TelegramBusy(RuntimeError):
    """The view could not be delivered because Telegram rate-limited this chat (answered as 'busy')."""

#: Telegram hands us the client's ``language_code`` on every update, so the
#: subscriber's zone is inferred from it - nobody is asked to pick one.
LOCALE_TO_TIMEZONE = {
    "en_US": "America/New_York",
    "en_GB": "Europe/London",
    "de_DE": "Europe/Berlin",
    "fr_FR": "Europe/Paris",
    "it_IT": "Europe/Rome",
    "es_ES": "Europe/Madrid",
    "pt_PT": "Europe/Lisbon",
    "pt_BR": "America/Sao_Paulo",
    "ru_RU": "Europe/Moscow",
    "ja_JP": "Asia/Tokyo",
    "zh_CN": "Asia/Shanghai",
    "zh_TW": "Asia/Taipei",
    "ko_KR": "Asia/Seoul",
    "en_IN": "Asia/Kolkata",
    "ar_SA": "Asia/Riyadh",
    "ar_EG": "Africa/Cairo",
    "en_NG": "Africa/Lagos",
    "ar_AE": "Asia/Dubai",
    "en_SG": "Asia/Singapore",
    "en_AU": "Australia/Sydney",
    "en_NZ": "Pacific/Auckland",
}

_LOCALES = {k.lower(): v for k, v in LOCALE_TO_TIMEZONE.items()}
#: A bare English locale (Telegram often sends just "en") defaults to the bot's home market.
BARE_ENGLISH_TIMEZONE = "Africa/Lagos"


def _detect_user_timezone(user: Dict[str, Any]) -> Optional[str]:
    """Detect a subscriber's timezone from the locale Telegram already sends.

    Telegram uses BCP-47 tags (``en-US``, ``pt-br``, bare ``en``) while the table is
    keyed ``en_US``: hyphens are normalised and the match ignores case. A bare
    ``en`` maps to Africa/Lagos; anything else unknown returns None (the bot's own
    zone applies)."""
    if not user:
        return None
    locale = str(user.get("language_code") or user.get("language") or "").strip()
    if not locale:
        return None
    key = locale.replace("-", "_").lower()
    if key in _LOCALES:
        return _LOCALES[key]
    if key == "en":
        return BARE_ENGLISH_TIMEZONE
    return None


#: The public commands and the view each one opens.  Buttons open these same
#: views (``pagination.VIEW_COMMAND``), so a tap and a type are the same action
#: and cannot drift apart.
COMMANDS: Dict[str, str] = {
    "/all": VIEW_ALL,
    "/top": VIEW_TOP,        # strongest picks
    "/daily": VIEW_DAILY,    # every prediction for today
    "/today": VIEW_DAILY,    # alias kept for older clients
    "/stats": VIEW_STATS,    # quick summary
    "/yesterday": VIEW_YESTERDAY,   # the audit trail: every pick from yesterday with its result
}

WAVING = "\U0001F44B"        # 👋

#: What Telegram lists when a reader types "/" (registered at startup with setMyCommands).
BOT_COMMANDS = [
    {"command": "all", "description": "All model predictions for today"},
    {"command": "top", "description": "Bet of the Day"},
    {"command": "daily", "description": "Daily picks"},
    {"command": "stats", "description": "Results so far: yesterday and the last 7 days"},
    {"command": "yesterday", "description": "Every pick from yesterday, with its result"},
    {"command": "timezone", "description": "Show kick-off times in your time zone"},
    {"command": "help", "description": "What the bot can do"},
]


def command_view(cmd: str) -> Optional[str]:
    """Map a typed command to its view (``"/top"`` and ``"top"`` both work)."""
    key = str(cmd or "").strip().lower()
    if key and not key.startswith("/"):
        key = "/" + key
    return COMMANDS.get(key)


def is_view_command(cmd: str) -> bool:
    """True for /top, /daily, /today, /stats and /yesterday (with or without the slash)."""
    return command_view(cmd) is not None


def view_button(view: str) -> Dict[str, str]:
    """The inline button for ``view``: its label is the command that opens it."""
    return {"text": view_button_label(view),
            "callback_data": pagination.encode_callback(view, 0)}


def commands_text() -> str:
    """The three view commands - one definition for the welcome screen and /help."""
    return "\n".join(f"<b>{VIEW_ICON[v]} {VIEW_COMMAND[v]}</b> – {VIEW_DESC[v]}"
                     for v in (VIEW_ALL, VIEW_TOP, VIEW_DAILY, VIEW_STATS))


#: The welcome screen's command list, kept exactly as it has always read (/start is unchanged); /help lists all
#: the view commands via commands_text().
WELCOME_COMMANDS = ("<b>🏆 /top</b> – Bet of the Day\n"
                    "<b>📚 /all</b> – All predictions\n"
                    "<b>📋 /daily</b> – Daily picks · Accumulator slip\n"
                    "<b>📊 /stats</b> – See prediction results")


def welcome_message(first_name: str = "there") -> str:
    """The /start screen: one greeting line, then the commands."""
    return (f"{WAVING} Hi, {esc(first_name)}\n\n"
            "Use the menu below or these commands:\n\n"
            f"{WELCOME_COMMANDS}")


def menu_markup() -> Dict[str, Any]:
    """Inline keyboard mirroring the commands - one button per view.

    There is deliberately no subscribe/unsubscribe toggle: /start subscribes,
    and picks only ever arrive on the schedule.
    """
    return {"inline_keyboard": [
        [view_button(VIEW_ALL)],
        [view_button(VIEW_TOP), view_button(VIEW_DAILY)],
        [view_button(VIEW_STATS), view_button(VIEW_YESTERDAY)],
    ]}


COMMUNITY_MENU_TEXT = "⚽ Predictions\n\nToday's picks, results and more."
CUSTOM_PICKS_START_PARAMETER = "custom_picks"
TICKET_AUDIT_START_PARAMETER = "ticket_audit"


def custom_picks_button(bot_username: str) -> Dict[str, str]:
    """The same private entry point for the community menu and empty Top card."""
    return {"text": "✨ Custom Picks",
            "url": f"https://t.me/{bot_username}?start={CUSTOM_PICKS_START_PARAMETER}"}


def top_empty_markup(bot_username: str) -> Dict[str, Any]:
    return {"inline_keyboard": [
        [view_button(VIEW_DAILY), view_button(VIEW_ALL)],
        [custom_picks_button(bot_username)],
    ]}


def community_menu_markup(bot_username: str) -> Dict[str, Any]:
    """Community entry points reuse the existing view callback payloads."""
    rows = [
        [view_button(VIEW_TOP), view_button(VIEW_DAILY)],
        [view_button(VIEW_ALL)],
        [view_button(VIEW_STATS), view_button(VIEW_YESTERDAY)],
        [custom_picks_button(bot_username)],
    ]
    if ticket_audit_switch.ENABLED:
        rows.append([{"text": "🔍 Ticket Audit",
                      "url": f"https://t.me/{bot_username}?start={TICKET_AUDIT_START_PARAMETER}"}])
    return {"inline_keyboard": rows}


def handle_custom_picks_start(tg: Telegram, chat_id: str, allowed_sports=None) -> None:
    """Introduction after CommandLoop activates this private user's mode."""
    from predbot.custom_picks import INTRO, intro_text
    text = INTRO if allowed_sports is None else intro_text(allowed_sports)
    tg.send(text, chat_id=chat_id)


def handle_start(db: Database, tg: Telegram, chat_id: str, user: Optional[Dict[str, Any]]) -> None:
    """/start: subscribe, auto-detect the timezone, then show the menu.

    Predictions are never pushed here - the menu is the only thing sent.  Picks
    arrive on the schedule, and only then.
    """
    user = user or {}
    first_name = str(user.get("first_name") or "there")
    db.subscriber_add(chat_id, str(user.get("username") or ""), first_name,
                      timezone=_detect_user_timezone(user) or "")
    tg.send(welcome_message(first_name), chat_id=chat_id, reply_markup=menu_markup())


# --------------------------------------------------------------------- routing
NAVIGATION_MODES = ("new", "edit_pages", "edit")
DEFAULT_NAVIGATION = "edit_pages"


def navigation_mode(cfg: Config) -> str:
    """``VIEW_NAVIGATION``: edit_pages (default) | new | edit - anything else counts as edit_pages."""
    mode = str(getattr(cfg, "view_navigation", DEFAULT_NAVIGATION) or DEFAULT_NAVIGATION)
    return mode if mode in NAVIGATION_MODES else DEFAULT_NAVIGATION


def _edits(mode: str, page_turn: bool) -> bool:
    """Does a button press edit the pressed message (True) or send a new one (False)?"""
    return mode == "edit" or (mode == "edit_pages" and page_turn)


def open_view(view: str, *, service: UnifiedFetcher, tg: Telegram, cfg: Config,
              chat_id: str, db: Optional[Database] = None, page: int = 0,
              message_id: Optional[int] = None,
              page_size: int = pagination.PAGE_SIZE_DEFAULT,
              day: Optional[str] = None, edit: bool = True) -> Tuple[str, Dict]:
    """Render ``view`` and deliver it - the single handler for taps and types.

    A typed command posts a new message. A button press posts a new message too, unless ``edit`` and the
    message it came from are given (VIEW_NAVIGATION=edit / edit_pages); both receive the same text and
    keyboard. ``day`` (page-turn buttons carry it) renders that day's picks instead of today's.
    """
    # Every view is storage-only: a tap or a typed command never spends provider quota (M4).
    snapshot = service.get_daily_snapshot(allow_api_call=False)
    if day and pagination.is_prediction_view(view) and day != snapshot.date and hasattr(service, "snapshot_for"):
        snapshot = service.snapshot_for(day)         # an old message keeps turning ITS day's pages
    timezone = resolve_timezone(db.subscriber_timezone(chat_id) if db is not None else "",
                                cfg.timezone)
    text, markup = pagination.render_view(snapshot, view, page, page_size,
                                          timezone, db=db, day=day)
    if (view == VIEW_TOP and pagination.predictions_evaluated(snapshot)
            and text == pagination.empty_view_message(snapshot, VIEW_TOP, timezone)):
        username = tg.get_bot_username()
        if not username:
            raise RuntimeError('bot_username_unavailable')
        markup = top_empty_markup(username)
    if message_id and edit:
        delivered = tg.edit_message_text(chat_id, int(message_id), text, reply_markup=markup)
    else:
        delivered = tg.send(text, chat_id=chat_id, reply_markup=markup)
    limited = getattr(tg, "rate_limited", None)
    if not delivered and callable(limited) and limited(chat_id):
        raise TelegramBusy("chat rate limited")
    return text, markup


def handle_command(cmd: str, chat_id: str, *, service: UnifiedFetcher, tg: Telegram,
                   cfg: Config, db: Optional[Database] = None,
                   page_size: int = pagination.PAGE_SIZE_DEFAULT) -> bool:
    """Typed /top | /daily | /today | /stats; returns False when unknown.

    Delegates to :func:`open_view`, i.e. the very handler the menu buttons use.
    """
    view = command_view(cmd)
    if view is None:
        return False
    try:
        open_view(view, service=service, tg=tg, cfg=cfg, chat_id=chat_id, db=db,
                  page_size=page_size)
    except TelegramBusy:
        log.warning("command %r not answered: the chat is rate limited", cmd)   # a reply would be refused too
    except Exception as exc:  # noqa: BLE001
        # Match the button path (handle_callback): one bad pick/price/network
        # blip must not crash the whole command loop for a typed command.
        log.exception("command %r failed: %s", cmd, exc)
        tg.send("Something went wrong.", chat_id=chat_id)
    return True


def handle_callback(cb: Dict[str, Any], *, service: UnifiedFetcher, tg: Telegram,
                    cfg: Config, db: Optional[Database] = None,
                    page_size: int = pagination.PAGE_SIZE_DEFAULT) -> None:
    """Process one Telegram callback_query update (an inline keyboard press).

    With the default ``VIEW_NAVIGATION=edit_pages`` a page turn edits the message it was pressed on and a view
    switch sends a new message (the chat keeps every view it opened; older messages keep working buttons);
    ``new`` sends a new message for every press, ``edit`` edits for every press. The press is always answered so the button stops spinning."""
    cb_id = str(cb.get("id") or "")
    data = cb.get("data") or ""
    message = cb.get("message") or {}
    chat_id = str((message.get("chat") or {}).get("id"))
    message_id = message.get("message_id")
    mode = navigation_mode(cfg)
    try:
        if data == "menu":
            user = cb.get("from") or {}
            welcome = welcome_message(str(user.get("first_name") or "there"))
            if message_id and _edits(mode, page_turn=False):
                tg.edit_message_text(chat_id, int(message_id), welcome, reply_markup=menu_markup())
            else:
                tg.send(welcome, chat_id=chat_id, reply_markup=menu_markup())
            tg.answer_callback_query(cb_id, text="Menu")
            return
        parsed = pagination.parse_callback(data)
        if parsed is None:
            tg.answer_callback_query(cb_id)      # e.g. the page indicator button
            return
        view, page, day = parsed
        page_turn = day is not None              # only page-turn buttons carry the day of their card
        edit = _edits(mode, page_turn)
        # Keep the community's pinnable entry menu intact, including in classic edit mode.
        if (chat_id == str(cfg.telegram_chat_id)
                and (message.get("chat") or {}).get("type") == "supergroup"
                and message.get("text") == COMMUNITY_MENU_TEXT):
            edit = False
        open_view(view, service=service, tg=tg, cfg=cfg, chat_id=chat_id, db=db,
                  page=page, message_id=message_id, page_size=page_size, day=day, edit=edit)
        if mode == "edit":
            # the view above already generated if it was allowed to; the toast only reads what is stored
            toast = _toast(view, page, page_size, service.get_daily_snapshot(allow_api_call=False))
        else:
            toast = _new_toast(view, page, page_turn, service, day, page_size)
        tg.answer_callback_query(cb_id, text=toast)
    except TelegramBusy:
        if cb_id:
            tg.answer_callback_query(cb_id, text="Busy right now — try again in a few seconds.")
    except Exception as exc:  # noqa: BLE001
        log.exception("callback %r failed: %s", data, exc)
        if cb_id:
            tg.answer_callback_query(cb_id, text="Something went wrong.", show_alert=True)


def _new_toast(view: str, page: int, page_turn: bool, service, day: Optional[str], size: int) -> str:
    """The toast when a press sent a message: 'Daily picks ↓' for a view, 'Page 2 of 3' for a turn."""
    if page_turn and pagination.is_prediction_view(view) and view != VIEW_ALL:
        snapshot = service.snapshot_for(day) if day and hasattr(service, "snapshot_for") else service.get_daily_snapshot(allow_api_call=False)
        count = len(snapshot.view_items(view))
        return f"Page {pagination.clamp_page(page, count, size) + 1} of {pagination.total_pages(count, size)}"
    if page_turn:
        return f"Page {page + 1}"
    return f"{pagination.VIEW_LABEL[view]} ↓"


def _toast(view: str, page: int, size: int, snapshot) -> str:
    """Short confirmation shown at the top of the chat when a button is used."""
    if view in (VIEW_STATS, VIEW_YESTERDAY, VIEW_ALL):
        return pagination.VIEW_LABEL[view]
    count = len(snapshot.view_items(view))
    page = pagination.clamp_page(page, count, size)   # toast matches the rendered page
    return (f"{pagination.VIEW_LABEL[view]} · "
            f"page {page + 1}/{pagination.total_pages(count, size)}")
