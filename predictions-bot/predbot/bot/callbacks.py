"""Telegram callback-query handlers, the /start menu and the shared view router.

Routing
    /top /daily /today /stats   typed commands  -> open_view()
    pg|{view}|{page}[|{day}]    inline buttons  -> open_view()

Commands and buttons deliberately share ONE handler: tapping 🏆 /top and typing
/top render the same snapshot slice with the same keyboard, so there is no
second implementation to drift out of sync.

Every read is cache-first (``UnifiedFetcher.get_daily_snapshot``); the only path
that may reach the providers is a cold cache on a brand-new day (the documented
write-once-per-day miss).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

from predbot.bot import pagination
from predbot.bot.pagination import (VIEW_COMMAND, VIEW_DESC, VIEW_ICON,
                                    view_button_label)
from predbot.bot.telegram import Telegram, esc
from predbot.cache.fetcher import UnifiedFetcher
from predbot.cache.models import VIEW_DAILY, VIEW_STATS, VIEW_TOP, VIEW_YESTERDAY
from predbot.config import Config
from predbot.storage.db import Database
from predbot.util.timeutil import resolve_timezone

log = logging.getLogger(__name__)

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
    "/top": VIEW_TOP,        # strongest picks
    "/daily": VIEW_DAILY,    # every prediction for today
    "/today": VIEW_DAILY,    # alias kept for older clients
    "/stats": VIEW_STATS,    # quick summary
    "/yesterday": VIEW_YESTERDAY,   # the audit trail: every pick from yesterday with its result
}

WAVING = "\U0001F44B"        # 👋


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
                     for v in (VIEW_TOP, VIEW_DAILY, VIEW_STATS))


#: The welcome screen's command list, kept exactly as it has always read (/start is unchanged); /help lists all
#: the view commands via commands_text().
WELCOME_COMMANDS = ("<b>🏆 /top</b> – Top picks of the day\n"
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
        [view_button(VIEW_TOP), view_button(VIEW_DAILY)],
        [view_button(VIEW_STATS), view_button(VIEW_YESTERDAY)],
    ]}


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


def navigation_mode(cfg: Config) -> str:
    """``VIEW_NAVIGATION``: new (default) | edit_pages | edit - anything else counts as new."""
    mode = str(getattr(cfg, "view_navigation", "new") or "new")
    return mode if mode in NAVIGATION_MODES else "new"


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
    snapshot = service.get_daily_snapshot()          # cache-first read
    if day and view in (VIEW_TOP, VIEW_DAILY) and day != snapshot.date and hasattr(service, "snapshot_for"):
        snapshot = service.snapshot_for(day)         # an old message keeps turning ITS day's pages
    timezone = resolve_timezone(db.subscriber_timezone(chat_id) if db is not None else "",
                                cfg.timezone)
    text, markup = pagination.render_view(snapshot, view, page, page_size,
                                          timezone, db=db, day=day)
    if message_id and edit:
        tg.edit_message_text(chat_id, int(message_id), text, reply_markup=markup)
    else:
        tg.send(text, chat_id=chat_id, reply_markup=markup)
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

    With the default ``VIEW_NAVIGATION=new`` nothing is ever edited or deleted: every press - a view switch or
    a page turn - sends a new message with its own keyboard, so the chat keeps the record and older messages
    keep working buttons. The press is always answered so the button stops spinning."""
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
        open_view(view, service=service, tg=tg, cfg=cfg, chat_id=chat_id, db=db,
                  page=page, message_id=message_id, page_size=page_size, day=day, edit=edit)
        if mode == "edit":
            toast = _toast(view, page, page_size, service.get_daily_snapshot())
        else:
            toast = _new_toast(view, page, page_turn, service, day, page_size)
        tg.answer_callback_query(cb_id, text=toast)
    except Exception as exc:  # noqa: BLE001
        log.exception("callback %r failed: %s", data, exc)
        if cb_id:
            tg.answer_callback_query(cb_id, text="Something went wrong.", show_alert=True)


def _new_toast(view: str, page: int, page_turn: bool, service, day: Optional[str], size: int) -> str:
    """The toast when a press sent a message: 'Daily picks ↓' for a view, 'Page 2 of 3' for a turn."""
    if page_turn and view in (VIEW_TOP, VIEW_DAILY):
        snapshot = service.snapshot_for(day) if day and hasattr(service, "snapshot_for") else service.get_daily_snapshot()
        count = len(snapshot.view_items(view))
        return f"Page {pagination.clamp_page(page, count, size) + 1} of {pagination.total_pages(count, size)}"
    if page_turn:
        return f"Page {page + 1}"
    return f"{pagination.VIEW_LABEL[view]} ↓"


def _toast(view: str, page: int, size: int, snapshot) -> str:
    """Short confirmation shown at the top of the chat when a button is used."""
    if view in (VIEW_STATS, VIEW_YESTERDAY):
        return pagination.VIEW_LABEL[view]
    count = len(snapshot.view_items(view))
    page = pagination.clamp_page(page, count, size)   # toast matches the rendered page
    return (f"{pagination.VIEW_LABEL[view]} · "
            f"page {page + 1}/{pagination.total_pages(count, size)}")
