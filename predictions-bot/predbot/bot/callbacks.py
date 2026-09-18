"""Telegram callback-query handlers, the /start menu and the shared view router.

Routing
    /top /daily /today /stats   typed commands  -> open_view()
    pg|{view}|{page}            inline buttons  -> open_view()

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
from predbot.cache.models import VIEW_DAILY, VIEW_STATS, VIEW_TOP
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

def _detect_user_timezone(user: Dict[str, Any]) -> Optional[str]:
    """Detect a subscriber's timezone from the locale Telegram already sends."""
    if not user:
        return None
    locale = user.get("language_code") or user.get("language")
    if locale:
        return LOCALE_TO_TIMEZONE.get(locale)
    return None


#: The public commands and the view each one opens.  Buttons open these same
#: views (``pagination.VIEW_COMMAND``), so a tap and a type are the same action
#: and cannot drift apart.
COMMANDS: Dict[str, str] = {
    "/top": VIEW_TOP,        # strongest picks
    "/daily": VIEW_DAILY,    # every prediction for today
    "/today": VIEW_DAILY,    # alias kept for older clients
    "/stats": VIEW_STATS,    # quick summary
}

WAVING = "\U0001F44B"        # 👋


def command_view(cmd: str) -> Optional[str]:
    """Map a typed command to its view (``"/top"`` and ``"top"`` both work)."""
    key = str(cmd or "").strip().lower()
    if key and not key.startswith("/"):
        key = "/" + key
    return COMMANDS.get(key)


def is_view_command(cmd: str) -> bool:
    """True for /top, /daily, /today and /stats (with or without the slash)."""
    return command_view(cmd) is not None


def view_button(view: str) -> Dict[str, str]:
    """The inline button for ``view``: its label is the command that opens it."""
    return {"text": view_button_label(view),
            "callback_data": pagination.encode_callback(view, 0)}


def commands_text() -> str:
    """The three view commands - one definition for the welcome screen and /help."""
    return "\n".join(f"<b>{VIEW_ICON[v]} {VIEW_COMMAND[v]}</b> – {VIEW_DESC[v]}"
                     for v in (VIEW_TOP, VIEW_DAILY, VIEW_STATS))


def welcome_message(first_name: str = "there") -> str:
    """The /start screen: one greeting line, then the three commands."""
    return (f"{WAVING} Hi, {esc(first_name)}\n\n"
            "Use the menu below or these commands:\n\n"
            f"{commands_text()}")


def menu_markup() -> Dict[str, Any]:
    """Inline keyboard mirroring the commands - one button per view.

    There is deliberately no subscribe/unsubscribe toggle: /start subscribes,
    and picks only ever arrive on the schedule.
    """
    return {"inline_keyboard": [
        [view_button(VIEW_TOP), view_button(VIEW_DAILY)],
        [view_button(VIEW_STATS)],
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
def open_view(view: str, *, service: UnifiedFetcher, tg: Telegram, cfg: Config,
              chat_id: str, db: Optional[Database] = None, page: int = 0,
              message_id: Optional[int] = None,
              page_size: int = pagination.PAGE_SIZE_DEFAULT) -> Tuple[str, Dict]:
    """Render ``view`` and deliver it - the single handler for taps and types.

    A button press edits the message it came from, a typed command posts a new
    one; both receive exactly the same text and keyboard.
    """
    snapshot = service.get_daily_snapshot()          # cache-first read
    timezone = resolve_timezone(db.subscriber_timezone(chat_id) if db is not None else "",
                                cfg.timezone)
    text, markup = pagination.render_view(snapshot, view, page, page_size,
                                          timezone, db=db)
    if message_id:
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
    """Process one Telegram callback_query update (an inline keyboard press)."""
    cb_id = str(cb.get("id") or "")
    data = cb.get("data") or ""
    message = cb.get("message") or {}
    chat_id = str((message.get("chat") or {}).get("id"))
    message_id = message.get("message_id")
    try:
        if data == "menu":
            user = cb.get("from") or {}
            if message_id:
                tg.edit_message_text(chat_id, int(message_id),
                                     welcome_message(str(user.get("first_name") or "there")),
                                     reply_markup=menu_markup())
            tg.answer_callback_query(cb_id, text="Menu")
            return
        parsed = pagination.decode_callback(data)
        if parsed is None:
            tg.answer_callback_query(cb_id)      # e.g. the page indicator button
            return
        view, page = parsed
        open_view(view, service=service, tg=tg, cfg=cfg, chat_id=chat_id, db=db,
                  page=page, message_id=message_id, page_size=page_size)
        tg.answer_callback_query(cb_id, text=_toast(view, page, page_size,
                                                    service.get_daily_snapshot()))
    except Exception as exc:  # noqa: BLE001
        log.exception("callback %r failed: %s", data, exc)
        if cb_id:
            tg.answer_callback_query(cb_id, text="Something went wrong.", show_alert=True)


def _toast(view: str, page: int, size: int, snapshot) -> str:
    """Short confirmation shown at the top of the chat when a button is used."""
    if view == VIEW_STATS:
        return "Stats"
    count = len(snapshot.view_items(view))
    page = pagination.clamp_page(page, count, size)   # toast matches the rendered page
    return (f"{pagination.VIEW_TITLES[view]} · "
            f"page {page + 1}/{pagination.total_pages(count, size)}")
