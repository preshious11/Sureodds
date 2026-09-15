"""Optional long-polling command handler: /stats /today /yesterday, and for
admins /run (force picks), /settle, /recap.

When a UnifiedFetcher is wired in, /today renders the paginated inline-keyboard
view (zero API reads) and inline-button presses are routed to the callback
handler.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import timedelta
from typing import Callable, Dict, Optional

from predbot.bot import pagination
from predbot.bot.callbacks import handle_callback, handle_start
from predbot.bot.format import RESULT_ICON, stats_message
from predbot.bot.telegram import Telegram, esc
from predbot.cache.fetcher import UnifiedFetcher
from predbot.config import Config
from predbot.jobs import stats as stats_mod
from predbot.storage.db import Database
from predbot.util.timeutil import parse_iso, to_local, utcnow

log = logging.getLogger(__name__)


def _retry_get_updates(tg: Telegram, offset: Optional[int], timeout: int, max_retries: int = 3) -> list:
    """Retry get_updates with exponential backoff on transient failures."""
    for attempt in range(max_retries):
        try:
            return tg.get_updates(offset, timeout=timeout)
        except (ConnectionError, TimeoutError) as e:
            if attempt == max_retries - 1:
                log.warning("get_updates failed after %d retries: %s", max_retries, e)
                return []
            wait = 2 ** attempt
            log.debug("get_updates attempt %d failed, retrying in %ds: %s", attempt + 1, wait, e)
            time.sleep(wait)
    return []


def _retry_send(tg: Telegram, text: str, chat_id: str, reply_markup: Optional[Dict] = None, max_retries: int = 3) -> bool:
    """Retry send with exponential backoff on transient failures. Returns True if sent."""
    for attempt in range(max_retries):
        try:
            tg.send(text, chat_id=chat_id, reply_markup=reply_markup)
            return True
        except (ConnectionError, TimeoutError) as e:
            if attempt == max_retries - 1:
                log.error("send failed after %d retries: %s", max_retries, e)
                return False
            wait = 2 ** attempt
            log.debug("send attempt %d failed, retrying in %ds: %s", attempt + 1, wait, e)
            time.sleep(wait)
    return False


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
            for upd in _retry_get_updates(self.tg, self.offset, timeout=20):
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
                except (ValueError, KeyError, AttributeError) as e:
                    log.exception("command failed: %s", e)
                    reply = "Command failed."
                if reply:
                    _retry_send(self.tg, reply, chat_id)

    def _send_today(self, chat_id: str) -> None:
        """Paginated /today: page 0 of the top picks with inline navigation."""
        try:
            snapshot = self.service.get_daily_snapshot()
            text, markup = pagination.render_view(snapshot, pagination.VIEW_TOP, 0,
                                                  self.page_size, self.cfg.timezone)
            _retry_send(self.tg, text, chat_id, reply_markup=markup)
        except (ValueError, KeyError, AttributeError) as e:
            log.exception("today failed: %s", e)
            _retry_send(self.tg, "Today's picks are unavailable.", chat_id)

    def _dispatch_callback(self, cb: Dict) -> None:
        if self.service is None:
            self.tg.answer_callback_query(str(cb.get("id") or ""), text="Unavailable.")
            return
        try:
            handle_callback(cb, service=self.service, tg=self.tg, db=self.db,
                            cfg=self.cfg, page_size=self.page_size)
        except (ValueError, KeyError, AttributeError, IndexError) as e:
            log.exception("callback dispatch failed: %s", e)

    def _day_list(self, date_str: str, title: str) -> str:
        rows = self.db.picks_for_date(date_str)
        if not rows:
            return f"No picks for {title}."
        lines = [f"<b>{title}</b>", ""]
        for r in rows:
            sport = r["sport"].replace("_", " ").title()
            league = r["league"] or sport
            fixture = f"{esc(r['home'])} vs {esc(r['away'])}"
            outcome = esc(r["label"])
            confidence = f"<b>{round(float(r['prob']) * 100)}%</b>" if r.get("prob") else "–"
            if r.get("start_utc"):
                local = to_local(parse_iso(r["start_utc"]), self.cfg.timezone)
                tz_abbr = local.tzname() or self.cfg.timezone.split("/")[-1]
                kickoff = f"{local.strftime('%H:%M')} {tz_abbr}"
            else:
                kickoff = "--:--"
            odd = f"{float(r['price']):.2f}"

            lines.append(sport)
            lines.append(league)
            lines.append(fixture)
            lines.append(outcome)
            lines.append(f"Confidence level: {confidence} | {kickoff}")
            lines.append(odd)
            lines.append("")
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
