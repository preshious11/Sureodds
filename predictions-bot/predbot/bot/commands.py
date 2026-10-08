"""Optional long-polling command handler and the shared view commands.

Typed /top, /daily (alias /today) and /stats are dispatched to
``callbacks.handle_command`` - the very handler the inline menu buttons use, so a
tap and a type produce identical output.  /start shows the menu and subscribes,
/stop unsubscribes; admins also get /run, /settle, /recap, /lastrun and /menu.

Timezones are never asked for: they are detected from the locale Telegram sends
with every update (``callbacks._detect_user_timezone``).

Responsiveness: every update is handled inside ``tg.interactive()``, so a Telegram rate limit fails that one answer
fast instead of sleeping on the only polling thread. One user cannot flood the bot (``USER_LIMIT`` updates per
``USER_WINDOW``), one public view cannot be re-posted into a group again and again (``GROUP_REPEAT_SECONDS``), and a
group gets at most ``GROUP_POSTS_PER_MINUTE`` view posts from taps. Custom Picks searches run on one worker thread
(when the loop runs as the polling thread) with a per-user in-flight guard, a request rate limit and a work budget.

Update progress: the next update offset is stored in the database as each update finishes, so a new leader (or a
restarted process) resumes after the last COMPLETED update instead of replaying the last batch. An update that was
fetched but not finished when the process died is delivered again (at-least-once; handlers are idempotent or
harmless to repeat). Work handed to another thread - a Custom Picks search, an admin /run, /settle, /recap - counts
as completed when it is handed over: it is not replayed after a crash (a replayed /run would spend provider quota).
"""
from __future__ import annotations

import contextlib
import hashlib
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional, Tuple

from predbot.bot import callbacks, lastrun, pagination
from predbot.bot.callbacks import handle_callback, handle_start
from predbot.bot.ratelimit import Cooldown, SlidingWindow, WorkWindow
from predbot.bot.telegram import Telegram, PollingError
from predbot.bot.runtime import PollingLease, log_identity
from predbot import ticket_audit_switch
from predbot.cache.fetcher import UnifiedFetcher
from predbot.config import Config
from predbot.custom_picks import CustomPicks, parse_request, RequestError
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
    an exception it will never throw. A rate limit or a chat that refuses the
    bot is NOT retried: waiting on the polling thread is what froze the bot.
    """
    for attempt in range(max_retries):
        if tg.send(text, chat_id=chat_id, reply_markup=reply_markup):
            return True
        last_failure = getattr(tg, "last_failure", None)
        if callable(last_failure) and last_failure(chat_id) in ("rate_limited", "permanent"):
            return False
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
AUDIT_CALLBACK_PREFIX = 'ta|'

#: one user (admins exempt): at most this many updates per window; beyond it they get one short notice per window
USER_LIMIT, USER_WINDOW = 20, 30.0
#: the same public view is posted into a group at most once per this many seconds (later taps: "posted above")
GROUP_REPEAT_SECONDS = 60.0
#: view posts a group gets from taps per minute (Telegram allows ~20 a minute per group, scheduled posts included)
GROUP_POSTS_PER_MINUTE = 10
#: Custom Picks: search work (beam expansions) one user may cause per window - about three worst-case failing
#: requests a minute; ordinary requests use a small fraction - plus a loose count backstop and a bounded queue
CUSTOM_WORK_LIMIT, CUSTOM_WINDOW = 1_200_000, 60.0
CUSTOM_LIMIT = 30
CUSTOM_QUEUE_LIMIT = 16
SLOW_DOWN = "😅 Easy there — give me a few seconds and try again."
TAP_SLOW_DOWN = "You're tapping fast — give it a few seconds."
GROUP_REPEAT = "Just posted above ↑"
GROUP_BUSY = "The group is busy — try again in a minute, or open the bot privately."
VIEW_BUSY = "Busy right now — try again in a few seconds."
CUSTOM_BUSY = "⏳ Still working on your last request — one moment."
CUSTOM_FULL = "😅 I'm a little busy right now — try again in a minute."


def _interactive(tg):
    """``tg.interactive()`` when the client supports it (test doubles may not)."""
    ctx = getattr(tg, "interactive", None)
    return ctx() if callable(ctx) else contextlib.nullcontext()


def offset_key(token: str) -> str:
    """kv key of the stored update offset, per bot token (never the token itself)."""
    return "telegram:update_offset:" + hashlib.sha256(str(token or "").encode()).hexdigest()[:16]


class CommandLoop(threading.Thread):
    #: set by the worker to its database leader lease: False = stop polling and leave pending updates to the new leader
    leader_check: Optional[Callable[[], bool]] = None

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
        self._custom_users = set()
        self._custom_picks = CustomPicks(db, cfg)
        self._user_window = SlidingWindow(USER_LIMIT, USER_WINDOW)
        self._limit_notice = Cooldown(USER_WINDOW)
        self._group_repeat = Cooldown(GROUP_REPEAT_SECONDS)
        self._group_posts = SlidingWindow(GROUP_POSTS_PER_MINUTE, 60.0)
        self._custom_window = SlidingWindow(CUSTOM_LIMIT, CUSTOM_WINDOW)
        self._custom_work = WorkWindow(CUSTOM_WORK_LIMIT, CUSTOM_WINDOW)
        self._custom_inflight: set = set()
        self._custom_pending = 0
        self._custom_guard = threading.Lock()
        #: None = Custom Picks work runs inline (direct calls, tests); run() gives the polling thread a worker
        self._custom_executor: Optional[ThreadPoolExecutor] = None
        self._ticket_audit = None
        if ticket_audit_switch.ENABLED:
            from predbot.bot.ticket_audit import TicketAuditController
            self._ticket_audit = TicketAuditController(tg, cfg)

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        try:
            with PollingLease(self.tg.token):
                self.instance = log_identity(handle_start)
                self._register_commands()
                self.offset = self._stored_offset()
                self._custom_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="custom-picks")
                try:
                    self._poll()
                finally:
                    self._custom_executor.shutdown(wait=False, cancel_futures=True)
        except Exception as exc:
            log.critical("Command loop stopped | error_type=%s | check polling ownership and restart the intended instance",
                         type(exc).__name__)

    # ------------------------------------------------------------------ update progress
    def _stored_offset(self) -> Optional[int]:
        try:
            raw = self.db.get_kv(offset_key(self.tg.token))
            return int(raw) if raw else None
        except (ValueError, TypeError, AttributeError):
            return None
        except Exception as exc:  # noqa: BLE001 - a missing offset only means Telegram's own confirmation applies
            log.warning("update offset not read | error_type=%s", type(exc).__name__)
            return None

    def _store_offset(self, offset: int) -> None:
        try:
            self.db.set_kv(offset_key(self.tg.token), str(offset))
        except Exception as exc:  # noqa: BLE001
            log.warning("update offset not stored | error_type=%s", type(exc).__name__)

    def _register_commands(self) -> None:
        """Put the commands in Telegram's '/' menu. Idempotent; a failure is logged and never stops the bot."""
        register = getattr(self.tg, "set_my_commands", None)
        if register is None:
            return
        try:
            ok = register(callbacks.BOT_COMMANDS)
        except Exception as exc:  # noqa: BLE001 - the menu is a convenience
            log.warning("command menu not registered | error_type=%s", type(exc).__name__)
            return
        log.info("command menu %s | commands=%d", "registered" if ok else "not registered", len(callbacks.BOT_COMMANDS))

    def _leading(self) -> bool:
        if self.leader_check is None or self.leader_check():
            return True
        log.critical("Telegram polling stopped: this process no longer holds the database leader lease")
        self._stop_event.set()
        return False

    def _poll(self) -> None:
        while not self._stop_event.is_set() and self._leading():
            try:
                updates = _retry_get_updates(self.tg, self.offset, timeout=20)
            except PollingError as exc:
                log.error("%s", exc)
                self._stop_event.wait(getattr(exc, "retry_in", 30))
                continue
            for upd in updates:
                if not self._leading():
                    break                       # unacknowledged: the next leader receives it
                self.offset = upd["update_id"] + 1
                try:
                    with _interactive(self.tg):
                        self._handle_update(upd)
                except Exception:  # noqa: BLE001 - one bad update must not kill the poll loop
                    log.error("update %s failed", upd.get("update_id"), exc_info=True)
                self._store_offset(self.offset)     # completed: a successor must not replay it
        for worker in self._admin_threads:            # leaving the loop: let an admin action in flight finish
            worker.join(timeout=5)

    # ------------------------------------------------------------------ flood protection
    def _over_user_limit(self, upd: dict) -> bool:
        """True (and the update is dropped) when this user exceeded USER_LIMIT updates per USER_WINDOW. Admins are
        exempt. The user hears about it once per window: a toast for a tap, a short message in a private chat."""
        cb = upd.get("callback_query")
        source = cb or upd.get("message") or {}
        user = str((source.get("from") or {}).get("id") or "")
        if not user or user in self.cfg.telegram_admin_ids or self._user_window.allow(user):
            return False
        notify = self._limit_notice.touch(user)
        if cb:
            self.tg.answer_callback_query(str(cb.get("id") or ""), text=TAP_SLOW_DOWN if notify else None)
        else:
            chat = (source.get("chat") or {})
            if notify and chat.get("type") == "private":
                self.tg.send(SLOW_DOWN, chat_id=str(chat.get("id")))
        log.info("interaction limit reached | update dropped")
        return True

    def _group_view_refused(self, cb: Dict) -> bool:
        """A tap that would post a public view into a group: refuse a repeat of the same view within
        GROUP_REPEAT_SECONDS, and more than GROUP_POSTS_PER_MINUTE posts, with a toast instead of a message."""
        chat = ((cb.get("message") or {}).get("chat") or {})
        if chat.get("type") not in ("group", "supergroup"):
            return False
        data = str(cb.get("data") or "")
        parsed = pagination.parse_callback(data)
        if parsed is None or parsed[2] is not None:
            return False                 # not a view, or a page turn of an existing message
        chat_id = str(chat.get("id"))
        if not self._group_repeat.ready((chat_id, data)):
            self.tg.answer_callback_query(str(cb.get("id") or ""), text=GROUP_REPEAT)
            return True
        if not self._group_posts.allow(chat_id):
            self.tg.answer_callback_query(str(cb.get("id") or ""), text=GROUP_BUSY)
            return True
        self._group_repeat.touch((chat_id, data))
        return False

    # ------------------------------------------------------------------ Custom Picks off the polling thread
    def _custom_run(self, user_id: str, chat_id: str, work: Callable[[], None], count: bool = True) -> None:
        """Run one Custom Picks request: at most one in flight per user, CUSTOM_LIMIT per window, a bounded queue.
        On the polling thread the work goes to the single Custom Picks worker; called directly it runs inline."""
        with self._custom_guard:
            if user_id in self._custom_inflight:
                refusal = CUSTOM_BUSY
            elif self._custom_work.exhausted(user_id) or (count and not self._custom_window.allow(user_id)):
                refusal = SLOW_DOWN
            elif self._custom_executor is not None and self._custom_pending >= CUSTOM_QUEUE_LIMIT:
                refusal = CUSTOM_FULL
            else:
                refusal = None
                self._custom_inflight.add(user_id)
                self._custom_pending += 1
        if refusal:
            self.tg.send(refusal, chat_id=chat_id)
            return

        def task():
            try:
                with _interactive(self.tg):
                    work()
            finally:
                self._custom_work.charge(user_id, getattr(self._custom_picks.recovery, "last_work", 0))
                with self._custom_guard:
                    self._custom_inflight.discard(user_id)
                    self._custom_pending -= 1

        if self._custom_executor is None:
            task()
            return
        try:
            self._custom_executor.submit(task)
        except RuntimeError:                       # shutting down
            with self._custom_guard:
                self._custom_inflight.discard(user_id)
                self._custom_pending -= 1

    def _custom_leave(self, user_id: str, chat_id: str) -> None:
        """Leave Custom Picks mode; serialised with the worker so its UI state has a single writer."""
        if self._custom_executor is None:
            self._custom_picks.leave(user_id, chat_id)
            return
        try:
            self._custom_executor.submit(self._custom_picks.leave, user_id, chat_id)
        except RuntimeError:
            pass

    def _handle_update(self, upd: dict) -> None:
        if self._over_user_limit(upd):
            return
        cb = upd.get("callback_query")
        if cb:
            self._dispatch_callback(cb)
            return
        msg = upd.get("message") or upd.get("channel_post") or {}
        text = (msg.get("text") or "").strip()
        chat_id = str(msg.get("chat", {}).get("id"))
        user_id = str((msg.get("from") or {}).get("id"))
        chat_type = (msg.get("chat") or {}).get("type")
        if not text.startswith("/"):
            if ticket_audit_switch.ENABLED and self._ticket_audit is not None and self._ticket_audit.message(msg):
                return
            if text and chat_type == 'private' and chat_id.isdigit() and user_id.isdigit() and \
                    (user_id, chat_id) in self._custom_users:
                self._custom_run(user_id, chat_id, lambda: self._custom_request(text, user_id, chat_id))
            return
        cmd, args = split_command(text)
        if cmd == '/start' and args == [callbacks.TICKET_AUDIT_START_PARAMETER]:
            if not ticket_audit_switch.ENABLED or self._ticket_audit is None:
                _retry_send(self.tg, 'This feature is temporarily unavailable.', chat_id)
                return
            if self._ticket_audit.key(msg) is not None:
                self._custom_users.discard((user_id, chat_id))
                self._custom_leave(user_id, chat_id)
                self._ticket_audit.start(msg)
            else:
                username = self.tg.get_bot_username()
                markup = {'inline_keyboard': [[{'text': '🔍 Ticket Audit',
                    'url': f'https://t.me/{username}?start=ticket_audit'}]]} if username else None
                _retry_send(self.tg, 'Open Ticket Audit in your private chat with the bot.', chat_id, markup)
            return
        if cmd in ("/start", "/timezone") and (chat_type not in (None, "private") or chat_id.startswith("-")):
            _retry_send(self.tg, "Use this command in a private chat with the bot.", chat_id)
            return
        if cmd == "/start":
            try:
                if args == [callbacks.CUSTOM_PICKS_START_PARAMETER]:
                    if chat_type != 'private' or not chat_id.isdigit() or not user_id.isdigit():
                        _retry_send(self.tg, "Use this command in a private chat with the bot.", chat_id)
                        return
                    self._custom_leave(user_id, chat_id)
                    self._custom_users.add((user_id, chat_id))
                    if ticket_audit_switch.ENABLED and self._ticket_audit is not None:
                        self._ticket_audit.leave((user_id, chat_id))
                    callbacks.handle_custom_picks_start(self.tg, chat_id)
                    log.info("custom_picks private entry | update_id=%s", upd.get("update_id"))
                    return
                self._custom_users.discard((user_id, chat_id))
                self._custom_leave(user_id, chat_id)
                if ticket_audit_switch.ENABLED and self._ticket_audit is not None:
                    self._ticket_audit.leave((user_id, chat_id))
                handle_start(self.db, self.tg, chat_id, msg.get("from") or {})
                log.info("start handled | instance=%s | update_id=%s",
                         self.instance, upd["update_id"])
            except Exception as exc:
                log.error("start failed | error_type=%s", type(exc).__name__)
            return
        if cmd == '/stop':
            self._custom_users.discard((user_id, chat_id))
            self._custom_leave(user_id, chat_id)
            if ticket_audit_switch.ENABLED and self._ticket_audit is not None:
                self._ticket_audit.leave((user_id, chat_id))
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

    def _custom_request(self, text: str, user_id: str, chat_id: str) -> None:
        loading_id = None
        try:
            parse_request(text, set(self.cfg.allowed_sports))
            loading_id = self.tg.send('🔎 Finding your strongest picks...', chat_id=chat_id)
        except RequestError:
            pass  # Clarifications do not pretend a search is underway.
        except Exception as exc:
            log.warning('Custom Picks loading failed | error_type=%s', type(exc).__name__)
        try:
            reply = self._custom_picks.respond(text, user_id, chat_id)
            self._deliver_custom(reply.text, chat_id, reply.reply_markup, loading_id)
        except Exception as exc:
            log.warning('Custom Picks private response failed | error_type=%s', type(exc).__name__)
            from predbot.custom_recovery import COPY
            self._deliver_custom(COPY['search_failed'], chat_id, None, loading_id)

    def _deliver_custom(self, text, chat_id, markup, loading_id):
        if loading_id is not None:
            try:
                if self.tg.edit_message_text(chat_id, int(loading_id), text, reply_markup=markup):
                    return
            except Exception as exc:
                log.warning('Custom Picks edit failed | error_type=%s', type(exc).__name__)
        _retry_send(self.tg, text, chat_id, markup)

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
        if str(cb.get('data') or '').startswith(AUDIT_CALLBACK_PREFIX):
            if not ticket_audit_switch.ENABLED or self._ticket_audit is None:
                self.tg.answer_callback_query(str(cb.get('id') or ''), text='This feature is temporarily unavailable.')
                return
            self._ticket_audit.callback(cb)
            return
        from predbot.custom_recovery import CALLBACK_PREFIX, COPY
        if str(cb.get('data') or '').startswith(CALLBACK_PREFIX):
            chat = ((cb.get('message') or {}).get('chat') or {})
            key = (str((cb.get('from') or {}).get('id')), str(chat.get('id')))
            if chat.get('type') == 'private' and key not in self._custom_users:
                self.tg.answer_callback_query(str(cb.get('id') or ''),
                    text='Open your own Custom Picks chat again to use these options.')
                return
            if ticket_audit_switch.ENABLED and self._ticket_audit is not None:
                key = self._ticket_audit.key({**(cb.get('message') or {}), 'from': cb.get('from')})
                if self._ticket_audit.active(key):
                    self._ticket_audit._ack(cb, 'Open Custom Picks again to use its buttons.')
                    return
            def tap_work():
                try:
                    tap = self._custom_picks.select(cb)
                    # Acknowledge before sending; Telegram failures must not hide the private result.
                    try:
                        self.tg.answer_callback_query(str(cb.get('id') or ''), text=tap.toast)
                    except Exception as exc:
                        log.warning('Custom Picks callback acknowledgement failed | error_type=%s', type(exc).__name__)
                    if tap.response:
                        chat_id = str(cb['message']['chat']['id'])
                        _retry_send(self.tg, tap.response.text, chat_id, tap.response.reply_markup)
                except Exception as exc:
                    log.warning('Custom Picks callback failed | error_type=%s', type(exc).__name__)
                    chat = ((cb.get('message') or {}).get('chat') or {})
                    if chat.get('type') == 'private' and str(chat.get('id')).isdigit():
                        _retry_send(self.tg, COPY['search_failed'], str(chat['id']))

            chat = ((cb.get('message') or {}).get('chat') or {})
            # a tap on a prepared option needs no search; its work (if it had to rebuild) is metered all the same
            self._custom_run(str((cb.get('from') or {}).get('id')), str(chat.get('id')), tap_work, count=False)
            return
        if self.service is None and cb.get("data") != "menu":
            self.tg.answer_callback_query(str(cb.get("id") or ""), text="Unavailable.")
            return
        if self._group_view_refused(cb):
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
            extra = ("\n\n<b>Admin</b>: /run · /settle · /recap · /lastrun · /menu"
                     if user_id in self.cfg.telegram_admin_ids else "")
            extra = "\n/timezone Area/City — kick-off times in your zone" + extra
            if ticket_audit_switch.ENABLED and chat_id.isdigit():
                extra += '\n/start ticket_audit — check a pasted ticket or screenshot privately'
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
        if cmd == "/menu":
            if user_id not in self.cfg.telegram_admin_ids:
                return "Not allowed."
            return self._post_community_menu()
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

    def _post_community_menu(self) -> str:
        """Post only the menu to the configured community; no service or database access."""
        destination = self.cfg.telegram_chat_id
        if not destination:
            return "Community destination is not configured. Set TELEGRAM_CHAT_ID."
        username = self.tg.get_bot_username()
        if not username:
            return "Could not resolve this bot's username. Community menu was not posted. Try /menu again."
        message_id = self.tg.send(callbacks.COMMUNITY_MENU_TEXT, chat_id=destination,
                                  reply_markup=callbacks.community_menu_markup(username))
        if not message_id:
            log.warning("community menu failed | chat_id=%s", destination)
            return "Community menu could not be posted. Check the bot's access and try /menu again."
        log.info("community menu posted | chat_id=%s | message_id=%s", destination, message_id)
        return f"Community menu posted. Message ID: {message_id}. You can pin it manually in Predictions."

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
