"""Optional long-polling command handler: /stats /today /yesterday, and for
admins /run (force picks), /settle, /recap.

When a UnifiedFetcher is wired in, /today renders the paginated inline-keyboard
view (zero API reads) and inline-button presses are routed to the callback
handler.
"""
from __future__ import annotations

import logging
import threading
from datetime import timedelta
from typing import Callable, Dict, Optional

from predbot.bot import broadcast, pagination
from predbot.bot.callbacks import handle_callback, handle_start
from predbot.bot.format import stats_message
from predbot.bot.telegram import Telegram, esc
from predbot.cache.fetcher import UnifiedFetcher
from predbot.config import Config
from predbot.jobs import stats as stats_mod
from predbot.storage.db import Database
from predbot.util.timeutil import to_local, utcnow

log = logging.getLogger(__name__)


def recap_card(db: Database, local_now: datetime) -> str:
    """The /yesterday summary card (wins/losses/void, hit rate, per-sport, 7-day, best pick)."""
    from predbot.bot.format import recap_message  # local import keeps module import light

    st = stats_mod.compute(db, local_now)
    yesterday = (local_now - timedelta(days=1)).strftime("%Y-%m-%d")
    rows = stats_mod.rows_for_recap(db, yesterday)
    return recap_message("YESTERDAY", stats_mod.by_sport(rows), st)


class CommandLoop(threading.Thread):
    def __init__(self, tg: Telegram, db: Database, cfg: Config,
                 actions: Dict[str, Callable[[], str]],
                 service: Optional[UnifiedFetcher] = None,
                 page_size: int = pagination.PAGE_SIZE_DEFAULT):
        super().__init__(daemon=True, name="commands")
        self.tg, self.db, self.cfg, self.actions = tg, db, cfg, actions
        self.service = service
        self.page_size = page_size
        self.offset: Optional[int] = None
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        log.info("command loop started")
        while not self._stop.is_set():
            for upd in self.tg.get_updates(self.offset, timeout=20):
                self.offset = upd["update_id"] + 1
                cb = upd.get("callback_query")
                if cb:
                    self._dispatch_callback(cb)
                    continue
                msg = upd.get("message") or {}
                text = (msg.get("text") or "").strip()
                if not text.startswith("/"):
                    continue
                chat_id = str(msg.get("chat", {}).get("id"))
                user_id = str((msg.get("from") or {}).get("id"))
                cmd = text.split("@")[0].lower()
                if cmd == "/start":
                    handle_start(self.db, self.tg, chat_id, msg.get("from") or {})
                    continue
                if cmd == "/today" and self.service is not None:
                    self._send_today(chat_id)
                    continue
                try:
                    reply = self.handle(cmd, user_id)
                except Exception as e:  # noqa: BLE001
                    log.exception("command failed: %s", e)
                    reply = "Command failed."
                if reply:
                    self.tg.send(reply, chat_id=chat_id)

    def _send_today(self, chat_id: str) -> None:
        """Paginated /today: page 0 of the top picks with inline navigation."""
        try:
            snapshot = self.service.get_daily_snapshot()
            text, markup = pagination.render_view(snapshot, pagination.VIEW_TOP, 0,
                                                  self.page_size, self.cfg.timezone)
            self.tg.send(text, chat_id=chat_id, reply_markup=markup)
        except Exception as e:  # noqa: BLE001
            log.exception("today failed: %s", e)
            self.tg.send("Today's picks are unavailable.", chat_id=chat_id)

    def _dispatch_callback(self, cb: Dict) -> None:
        if self.service is None:
            self.tg.answer_callback_query(str(cb.get("id") or ""), text="Unavailable.")
            return
        try:
            handle_callback(cb, service=self.service, tg=self.tg, db=self.db,
                            cfg=self.cfg, page_size=self.page_size)
        except Exception as e:  # noqa: BLE001
            log.exception("callback dispatch failed: %s", e)

    def _day_list(self, date_str: str, title: str) -> str:
        rows = self.db.picks_for_date(date_str)
        if not rows:
            return f"No picks for {title}."
        icons = {"won": "✅", "lost": "❌", "push": "↩️", "half_won": "✅½", "half_lost": "❌½", "void": "⚪", "pending": "⏳"}
        lines = [f"<b>{title}</b>"]
        for r in rows:
            tag = "🏆 " if r["tier"] == "top" else ""
            lines.append(f"{icons.get(r['status'], '⏳')} {tag}{esc(r['home'])} vs {esc(r['away'])} · {esc(r['label'])} @ {float(r['price']):.2f}")
        return "\n".join(lines)

    def handle(self, cmd: str, user_id: str) -> Optional[str]:
        local = to_local(utcnow(), self.cfg.timezone)
        if cmd in ("/stats", "/help"):
            if cmd != "/stats":
                return ("Commands: /start (menu) · /today · /yesterday · /stats" +
                        (" · /run /settle /recap" if user_id in self.cfg.telegram_admin_ids else ""))
            return stats_message(stats_mod.compute(self.db, local))
        if cmd == "/today":
            return self._day_list(local.strftime("%Y-%m-%d"), f"Picks · {local.strftime('%a %d %b')}")
        if cmd == "/yesterday":
            y = local - timedelta(days=1)
            return recap_card(self.db, local)
        if cmd in ("/run", "/settle", "/recap"):
            if user_id not in self.cfg.telegram_admin_ids:
                return "Not allowed."
            fn = self.actions.get(cmd)
            return fn() if fn else "Unavailable."
        return None
