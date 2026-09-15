"""Telegram callback-query handlers and the /start menu.

Routes
    pg|{view}|{page}   zero-API pagination over the daily cache
    sub|on / sub|off   subscribe / unsubscribe from the daily broadcasts
    menu               re-render the /start menu

Page flips and menu actions edit the already-sent message; the only path that
may reach the providers is a cold cache on a brand-new day (the documented
write-once-per-day miss).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from predbot.bot import pagination
from predbot.bot.broadcast import picks_text
from predbot.bot.telegram import Telegram
from predbot.cache.fetcher import UnifiedFetcher
from predbot.cache.models import DailySnapshot, VIEW_STATS
from predbot.config import Config
from predbot.storage.db import Database

log = logging.getLogger(__name__)

def welcome_message(first_name: str = "there") -> str:
    """Generate personalized welcome message."""
    return (
        f"👋 Hi, {first_name}\n\n"
        "Use the menu below or these commands:\n\n"
        "/top – Top picks of the day\n"
        "/dailypicks – Accumulator slip\n"
        "/stats – See prediction results."
    )


def menu_markup(subscribed: bool = True) -> Dict[str, Any]:
    toggle = ({"text": "🔕 Unsubscribe from daily posts", "callback_data": "sub|off"}
              if subscribed else
              {"text": "🔔 Subscribe to daily posts", "callback_data": "sub|on"})
    return {"inline_keyboard": [
        [{"text": "🏆 Top Picks", "callback_data": "pg|top|0"},
         {"text": "📋 Daily Picks", "callback_data": "pg|daily|0"}],
        [{"text": "📊 Stats", "callback_data": "pg|stats|0"}, toggle],
    ]}


def handle_start(db: Database, tg: Telegram, chat_id: str, user: Optional[Dict[str, Any]]) -> None:
    """/start: register (or re-activate) the subscriber and show the menu."""
    user = user or {}
    first_name = str(user.get("first_name") or "there")
    db.subscriber_add(chat_id, str(user.get("username") or ""), first_name)
    tg.send(welcome_message(first_name), chat_id=chat_id, reply_markup=menu_markup(subscribed=True))


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
        if data == "menu" or data.startswith("sub|"):
            _handle_menu_action(cb_id, data, chat_id, message_id, db, tg, cfg,
                                service, page_size)
            return
        parsed = pagination.decode_callback(data)
        if parsed is None:
            tg.answer_callback_query(cb_id)      # e.g. the page indicator button
            return
        view, page = parsed
        snapshot = service.get_daily_snapshot()  # cache-first read
        text, markup = pagination.render_view(snapshot, view, page, page_size,
                                              cfg.timezone, db=db)
        if chat_id and message_id:
            tg.edit_message_text(chat_id, int(message_id), text, reply_markup=markup)
        tg.answer_callback_query(cb_id, text=_toast(view, snapshot, page, page_size))
    except Exception as exc:  # noqa: BLE001
        log.exception("callback %r failed: %s", data, exc)
        if cb_id:
            tg.answer_callback_query(cb_id, text="Something went wrong.", show_alert=True)


def _handle_menu_action(cb_id: str, data: str, chat_id: str, message_id,
                        db: Optional[Database], tg: Telegram, cfg: Config,
                        service: Optional[UnifiedFetcher], page_size: int) -> None:
    """Subscribe / unsubscribe / re-render the menu."""
    if db is None or not chat_id:
        tg.answer_callback_query(cb_id, text="Unavailable.")
        return
    subscribed, toast = True, "Menu"
    if data == "sub|on":
        db.subscriber_add(chat_id)
        toast = "Subscribed ✅"
        _send_todays_picks_if_any(db, tg, cfg, service, chat_id)
    elif data == "sub|off":
        db.subscriber_remove(chat_id)
        subscribed, toast = False, "Unsubscribed"
    if message_id:
        tg.edit_message_text(chat_id, int(message_id), welcome_message(),
                             reply_markup=menu_markup(subscribed))
    tg.answer_callback_query(cb_id, text=toast)


def _send_todays_picks_if_any(db: Database, tg: Telegram, cfg: Config,
                              service: Optional[UnifiedFetcher], chat_id: str) -> None:
    """New/re-subscribed user: catch them up with today's picks (zero API)."""
    if service is None:
        return
    try:
        snapshot = service.get_daily_snapshot()
        if snapshot.top_picks or snapshot.daily_picks:
            tg.send(picks_text(snapshot, cfg.timezone), chat_id=chat_id)
    except Exception:  # noqa: BLE001
        log.exception("failed to deliver today's picks to subscriber %s", chat_id)


def _toast(view: str, snapshot: DailySnapshot, page: int, size: int) -> str:
    """Short confirmation shown at the top of the chat when a button is used."""
    if view == VIEW_STATS:
        return "Stats"
    count = len(snapshot.view_items(view))
    page = pagination.clamp_page(page, count, size)   # toast matches the rendered page
    return (f"{pagination.VIEW_TITLES[view]} · "
            f"page {page + 1}/{pagination.total_pages(count, size)}")
