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

log = logging.getLogger(__name__)

#: The public commands and the view each one opens.  Buttons are labelled with
#: these same strings (``pagination.VIEW_COMMAND``), so a tap and a type are the
#: same action and cannot drift apart.
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
    return "\n".join(f"<b>{VIEW_ICON[v]} {VIEW_COMMAND[v]}</b> — {VIEW_DESC[v]}"
                     for v in (VIEW_TOP, VIEW_DAILY, VIEW_STATS))


def welcome_message(first_name: str = "there") -> str:
    """The /start screen: one greeting line, then the three commands."""
    return (f"{WAVING} Hi, {esc(first_name)}\n\n"
            "Tap the menu below or use a command:\n\n"
            "/top — Strongest picks today\n"
            "/daily — All predictions today\n"
            "/stats — Prediction summary")


def menu_markup(subscribed: bool = True) -> Dict[str, Any]:
    """Inline keyboard mirroring the commands: every button is its command."""
    return {"inline_keyboard": [
        [{"text": "🏆 Top Picks", "callback_data": "pg|top|0"},
         {"text": "📋 Daily Picks", "callback_data": "pg|daily|0"}],
        [{"text": "📊 Stats", "callback_data": "pg|stats|0"}],
        [{"text": "🔕 Unsubscribe from daily picks" if subscribed else "🔔 Subscribe to daily picks",
          "callback_data": "sub|off" if subscribed else "sub|on"}],
    ]}


def handle_start(db: Database, tg: Telegram, chat_id: str, user: Optional[Dict[str, Any]]) -> None:
    """/start: register (or re-activate) the subscriber, then show the menu."""
    user = user or {}
    first_name = str(user.get("first_name") or "there")
    db.subscriber_add(chat_id, str(user.get("username") or ""), first_name)
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
    text, markup = pagination.render_view(snapshot, view, page, page_size,
                                          cfg.timezone, db=db)
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
    open_view(view, service=service, tg=tg, cfg=cfg, chat_id=chat_id, db=db,
              page_size=page_size)
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
        if data in ("menu", "sub|on", "sub|off"):
            if db is None or chat_id == "None":
                tg.answer_callback_query(cb_id, text="Unavailable.")
                return
            user = cb.get("from") or {}
            if data == "sub|off":
                db.subscriber_remove(chat_id)
            elif data == "sub|on":
                db.subscriber_add(chat_id, str(user.get("username") or ""),
                                  str(user.get("first_name") or ""))
            active = bool(db.query("SELECT 1 FROM subscribers WHERE chat_id=? AND active=1", (chat_id,)))
            if message_id:
                tg.edit_message_text(chat_id, int(message_id),
                                     welcome_message(str(user.get("first_name") or "there")),
                                     reply_markup=menu_markup(active))
            tg.answer_callback_query(cb_id, text={"sub|off": "Unsubscribed", "sub|on": "Subscribed"}.get(data, "Menu"))
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
