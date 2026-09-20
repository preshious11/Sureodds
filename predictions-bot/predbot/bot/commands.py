"""Optional long-polling command handler and the shared view commands.

Typed /top, /daily (alias /today) and /stats are dispatched to
``callbacks.handle_command`` - the very handler the inline menu buttons use, so a
tap and a type produce identical output.  /start shows the menu and subscribes,
/stop unsubscribes; admins also get /run, /settle, /recap and /lastrun.

Timezones are never asked for: they are detected from the locale Telegram sends
with every update (``callbacks._detect_user_timezone``).
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional, Tuple

from predbot.bot import callbacks, lastrun, pagination
from predbot.bot.callbacks import handle_callback, handle_start
from predbot.bot.telegram import Telegram, PollingError
from predbot.bot.runtime import PollingLease, log_identity
from predbot.cache.fetcher import UnifiedFetcher
from predbot.config import Config
from predbot.jobs import stats as stats_mod
from predbot.storage.db import Database
from predbot.util.timeutil import canonical_tz, resolve_timezone, to_local, utcnow, parse_hhmm

log = logging.getLogger(__name__)

def _retry_get_updates(tg: Telegram, offset: Optional[int], timeout: int, max_retries: int = 3) -> list:
    """Retry get_updates with exponential backoff on transient failures.

    ``tg.get_updates`` wraps every transport failure (including
    ``requests.RequestException``, which is NOT a subclass of the builtin
    ``ConnectionError``/``TimeoutError``) into ``PollingError`` - that's the
    exception actually worth retrying on.
    """
    for attempt in range(max_retries):
        try:
            return tg.get_updates(offset, timeout=timeout)
        except PollingError as e:
            if attempt == max_retries - 1:
                # Still failing after the quick retries: let _poll()'s own
                # 30s backoff handle a sustained outage/conflict instead of
                # spinning here.
                raise
            wait = 2 ** attempt
            log.debug("get_updates attempt %d failed, retrying in %ds: %s", attempt + 1, wait, e)
            time.sleep(wait)
    return []


def _retry_send(tg: Telegram, text: str, chat_id: str, reply_markup: Optional[Dict] = None, max_retries: int = 3) -> bool:
    """Retry send with exponential backoff on transient failures. Returns True if sent.

    ``tg.send`` never raises (transport failures return None like any other
    failure), so retrying here means checking its return value, not catching
    an exception it will never throw.
    """
    for attempt in range(max_retries):
        if tg.send(text, chat_id=chat_id, reply_markup=reply_markup):
            return True
        if attempt == max_retries - 1:
            log.error("send failed after %d retries", max_retries)
            return False
        wait = 2 ** attempt
        log.debug("send attempt %d failed, retrying in %ds", attempt + 1, wait)
        time.sleep(wait)
    return False


def split_command(text: str) -> Tuple[str, List[str]]:
    """``"/top@MyBot extra"`` -> ``("/top", ["extra"])`` (Telegram adds @bot in groups)."""
    parts = text.split()
    if not parts:
        return "", []
    return parts[0].split("@")[0].lower(), parts[1:]


def recap_card(db: Database, local_now: datetime) -> str:
    """The recap card: yesterday's settled results, the per-sport split, the last 7 days, the Top ticket record."""
    from predbot.bot.format import recap_message  # local import keeps module import light

    return recap_message(stats_mod.compute(db, local_now))


#: admin commands that run a whole job (generation / settlement / recap)
LONG_RUNNING = frozenset({"/run", "/settle", "/recap"})


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
        self._stop_event = threading.Event()
        self._admin_threads: List[threading.Thread] = []

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        try:
            with PollingLease(self.tg.token):
                self.instance = log_identity(handle_start)
                self._poll()
        except Exception as exc:
            log.critical("Command loop stopped | error_type=%s | check polling ownership and restart the intended instance",
                         type(exc).__name__)

    def _poll(self) -> None:
        while not self._stop_event.is_set():
            try:
                updates = _retry_get_updates(self.tg, self.offset, timeout=20)
            except PollingError as exc:
                log.error("%s", exc)
                self._stop_event.wait(30)
                continue
            for upd in updates:
                self.offset = upd["update_id"] + 1
                try:
                    self._handle_update(upd)
                except Exception:  # noqa: BLE001 - one bad update must not kill the poll loop
                    log.error("update %s failed", upd.get("update_id"), exc_info=True)
        for worker in self._admin_threads:            # leaving the loop: let an admin action in flight finish
            worker.join(timeout=5)

    def _handle_update(self, upd: dict) -> None:
        cb = upd.get("callback_query")
        if cb:
            self._dispatch_callback(cb)
            return
        msg = upd.get("message") or {}
        text = (msg.get("text") or "").strip()
        if not text.startswith("/"):
            return
        chat_id = str(msg.get("chat", {}).get("id"))
        user_id = str((msg.get("from") or {}).get("id"))
        cmd, args = split_command(text)
        if cmd == "/start":
            try:
                handle_start(self.db, self.tg, chat_id, msg.get("from") or {})
                log.info("start handled | instance=%s | update_id=%s",
                         self.instance, upd["update_id"])
            except Exception as exc:
                log.error("start failed | error_type=%s", type(exc).__name__)
            return
        if self.service is not None and callbacks.is_view_command(cmd):
            self._send_view(cmd, chat_id)
            return
        if cmd in LONG_RUNNING and user_id in self.cfg.telegram_admin_ids and self.actions.get(cmd):
            # /run, /settle and /recap take minutes: on their own thread, so /top, /daily and the buttons keep
            # answering. The reply is the same text, sent when the action finishes.
            worker = threading.Thread(target=self._admin_action, args=(cmd, user_id, chat_id, args, upd.get("update_id")),
                                      daemon=True, name=f"admin{cmd}")
            self._admin_threads = [t for t in self._admin_threads if t.is_alive()] + [worker]
            worker.start()
            return
        self._answer(cmd, user_id, chat_id, args)

    def _admin_action(self, cmd, user_id, chat_id, args, update_id) -> None:
        try:
            self._answer(cmd, user_id, chat_id, args)
        except Exception:  # noqa: BLE001 - same log line the poll loop writes for a failed update
            log.error("update %s failed", update_id, exc_info=True)

    def _answer(self, cmd: str, user_id: str, chat_id: str, args: List[str]) -> None:
        try:
            reply = self.handle(cmd, user_id, chat_id, args)
        except (ValueError, KeyError, AttributeError) as e:
            log.exception("command failed: %s", e)
            reply = "Command failed."
        if reply:
            _retry_send(self.tg, reply, chat_id)

    def _send_view(self, cmd: str, chat_id: str) -> None:
        """Typed /top, /daily, /today or /stats: the same handler as the buttons."""
        try:
            callbacks.handle_command(cmd, chat_id, service=self.service, tg=self.tg,
                                     cfg=self.cfg, db=self.db, page_size=self.page_size)
        except (ValueError, KeyError, AttributeError) as e:
            log.exception("%s failed: %s", cmd, e)
            _retry_send(self.tg, "That view is unavailable right now.", chat_id)

    def _dispatch_callback(self, cb: Dict) -> None:
        if self.service is None and cb.get("data") != "menu":
            self.tg.answer_callback_query(str(cb.get("id") or ""), text="Unavailable.")
            return
        try:
            handle_callback(cb, service=self.service, tg=self.tg, db=self.db,
                            cfg=self.cfg, page_size=self.page_size)
        except (ValueError, KeyError, AttributeError, IndexError) as e:
            log.exception("callback dispatch failed: %s", e)

    def handle(self, cmd: str, user_id: str, chat_id: str = "",
               args: Optional[List[str]] = None) -> Optional[str]:
        """Text-only commands; the three views go through ``_send_view``."""
        local = to_local(utcnow(), self.cfg.timezone)
        if cmd == "/help":
            extra = ("\n\n<b>Admin</b>: /run · /settle · /recap · /lastrun"
                     if user_id in self.cfg.telegram_admin_ids else "")
            extra = "\n/timezone Area/City — kick-off times in your zone" + extra
            zone = resolve_timezone(self.db.subscriber_timezone(chat_id or user_id),
                                    self.cfg.timezone)
            return ("<b>Commands</b>\n\n" + callbacks.commands_text() + "\n\n"
                    f"{self._schedule_text(zone)}\n"
                    "/stop — unsubscribe" + extra)
        if cmd == "/stop":
            self.db.subscriber_remove(chat_id or user_id)
            return ("🛑 Unsubscribed — no more daily picks. "
                    "Send /start whenever you want them back.")
        if cmd == "/timezone":
            return self._timezone(chat_id or user_id, args or [])
        if cmd == "/lastrun":
            if user_id not in self.cfg.telegram_admin_ids:
                return "Not allowed."
            return lastrun.lastrun_text(self.db, self.cfg)
        if cmd in ("/run", "/settle", "/recap"):
            if user_id not in self.cfg.telegram_admin_ids:
                return "Not allowed."
            fn = self.actions.get(cmd)
            return fn() if fn else "Unavailable."
        return None

    def _timezone(self, chat_id: str, args: List[str]) -> str:
        """``/timezone Africa/Lagos`` - the zone kick-off times are DISPLAYED in.
        The delivery instant is the same for everyone (DELIVERY_TIME in TIMEZONE)."""
        current = resolve_timezone(self.db.subscriber_timezone(chat_id), self.cfg.timezone)
        if not args:
            return (f"🌍 Your timezone: <b>{current}</b>\n"
                    "Change it with /timezone Area/City, e.g. /timezone Africa/Lagos")
        zone = canonical_tz(args[0])
        if zone is None:
            return ("Unknown timezone. Use an Area/City name such as Africa/Lagos, "
                    "Europe/London or America/New_York.")
        self.db.subscriber_set_timezone(chat_id, zone)
        return (f"✅ Timezone set to <b>{zone}</b>. Kick-off times on your cards use it. "
                f"Delivery stays at {self.cfg.delivery_time} {self.cfg.timezone} for everyone.")

    def _schedule_text(self, zone: str) -> str:
        now = utcnow()
        hour, minute = parse_hhmm(self.cfg.delivery_time, "09:30")
        scheduled = to_local(now, self.cfg.timezone).replace(hour=hour, minute=minute, second=0, microsecond=0)
        if scheduled <= now:
            scheduled += timedelta(days=1)
        local = to_local(scheduled, zone)
        return (f"Daily broadcast: {scheduled.strftime('%I:%M %p %Z')}; "
                f"next scheduled time in your zone: {local.strftime('%Y-%m-%d %I:%M %p %Z')}.")
