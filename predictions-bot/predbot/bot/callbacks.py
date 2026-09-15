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
from predbot.bot.telegram import Telegram
from predbot.cache.fetcher import UnifiedFetcher
from predbot.cache.models import DailySnapshot, VIEW_STATS
from predbot.config import Config
from predbot.storage.db import Database

log = logging.getLogger(__name__)

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
    """Detect timezone from Telegram user locale."""
    if not user:
        return None
    locale = user.get("language_code") or user.get("language")
    if locale:
        return LOCALE_TO_TIMEZONE.get(locale)
    return None

def welcome_message(first_name: str = "there") -> str:
    """Generate personalized welcome message."""
    return (
        f"👋 Hi, {first_name}\n\n"
        "Use the menu below or these commands:\n\n"
        "<b>🏆 /top</b> – Top picks of the day\n"
        "<b>📋 /daily</b> – Daily picks · Accumulator slip\n"
        "<b>📊 /stats</b> – See prediction results"
    )


def menu_markup() -> Dict[str, Any]:
    return {"inline_keyboard": [
        [{"text": "🏆 Top Picks", "callback_data": "pg|top|0"},
         {"text": "📋 Daily Picks", "callback_data": "pg|daily|0"}],
        [{"text": "📊 Stats", "callback_data": "pg|stats|0"}],
    ]}


def handle_start(db: Database, tg: Telegram, chat_id: str, user: Optional[Dict[str, Any]]) -> None:
    """/start: show the menu and detect user timezone."""
    user = user or {}
    first_name = str(user.get("first_name") or "there")

    user_tz = _detect_user_timezone(user)
    db.subscriber_add(chat_id, str(user.get("username") or ""), first_name)
    if user_tz:
        db.set_user_timezone(chat_id, user_tz)

    tg.send(welcome_message(first_name), chat_id=chat_id, reply_markup=menu_markup())


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
            _handle_menu_action(cb_id, data, chat_id, message_id, db, tg, page_size)
            return
        parsed = pagination.decode_callback(data)
        if parsed is None:
            tg.answer_callback_query(cb_id)      # e.g. the page indicator button
            return
        view, page = parsed
        user_tz = db.get_user_timezone(chat_id) if db else None
        snapshot = service.get_daily_snapshot()  # cache-first read
        text, markup = pagination.render_view(snapshot, view, page, page_size,
                                              cfg.timezone, db=db, user_tz=user_tz)
        if chat_id and message_id:
            tg.edit_message_text(chat_id, int(message_id), text, reply_markup=markup)
        tg.answer_callback_query(cb_id, text=_toast(view, snapshot, page, page_size))
    except Exception as exc:  # noqa: BLE001
        log.exception("callback %r failed: %s", data, exc)
        if cb_id:
            tg.answer_callback_query(cb_id, text="Something went wrong.", show_alert=True)


def _handle_menu_action(cb_id: str, data: str, chat_id: str, message_id,
                        db: Optional[Database], tg: Telegram, page_size: int) -> None:
    """Re-render the menu."""
    if not chat_id:
        tg.answer_callback_query(cb_id, text="Unavailable.")
        return
    if message_id:
        tg.edit_message_text(chat_id, int(message_id), welcome_message(),
                             reply_markup=menu_markup())
    tg.answer_callback_query(cb_id, text="Menu")


def _toast(view: str, snapshot: DailySnapshot, page: int, size: int) -> str:
    """Short confirmation shown at the top of the chat when a button is used."""
    if view == VIEW_STATS:
        return "Stats"
    count = len(snapshot.view_items(view))
    page = pagination.clamp_page(page, count, size)   # toast matches the rendered page
    return (f"{pagination.VIEW_TITLES[view]} · "
            f"page {page + 1}/{pagination.total_pages(count, size)}")
