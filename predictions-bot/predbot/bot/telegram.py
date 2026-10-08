"""Minimal Telegram Bot API client (sendMessage + getUpdates).

Rate limits (Telegram Bots FAQ: about 1 message/s per chat, 20/min per group, about 30/s overall) are handled
here, with two policies chosen per THREAD:

* interactive (the polling thread answering a tap or a command, inside ``with tg.interactive():``) never sleeps
  for a limit: a chat that is cooling down after a 429, or a group whose 20-a-minute window is full, fails fast
  (``last_failure(chat) == "rate_limited"``) so one busy chat cannot freeze every other user;
* scheduled (delivery, recap, live notices, community posts - job threads) waits a bounded time
  (``SCHEDULED_MAX_WAIT``) and retries a 429 once, as before; longer waits fail the send, which the caller's
  receipts leave retryable on its next run, so nothing scheduled is silently dropped.

Every send is also paced globally (``GLOBAL_INTERVAL``). A definitive "cannot reach this chat" answer (403 blocked /
kicked / deactivated, 400 chat not found) is reported through ``on_permanent_failure(chat_id, reason)``; a timeout
or 5xx never is. Message length is measured in UTF-16 code units, as Telegram counts it.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from collections import deque
from contextlib import contextmanager
from typing import Any, Callable, Deque, Dict, List, Optional

import requests

log = logging.getLogger(__name__)
#: per-message limit in UTF-16 code units (Telegram allows 4,096; the margin covers entities)
MAX_LEN = 4000
#: longest a scheduled send waits for a chat's cool-down / group window, or sleeps on a 429
SCHEDULED_MAX_WAIT = 60.0
#: Telegram: about 20 messages a minute into one group
GROUP_LIMIT, GROUP_WINDOW = 20, 60.0
#: Telegram: about 30 messages a second overall; stay under it
GLOBAL_INTERVAL = 1.0 / 25
#: waits that must not show up as ``time.sleep`` (tests patch that to observe 429 back-off)
_PAUSE = threading.Event()
#: definitive delivery failures: the chat is gone or the bot may not write to it
_PERMANENT_400 = ("chat not found", "user is deactivated", "peer_id_invalid", "bot was kicked",
                  "group chat was deactivated", "chat_write_forbidden")


class PollingError(RuntimeError):
    """Sanitized polling failure; never includes a credential-bearing URL. ``retry_in``: seconds to wait first."""

    def __init__(self, message: str = "", retry_in: float = 30.0):
        super().__init__(message)
        self.retry_in = retry_in


def utf16_len(text: str) -> int:
    """Length as Telegram counts it (UTF-16 code units: an emoji outside the BMP counts twice)."""
    return len(text.encode("utf-16-le")) // 2


def _prefix_len(text: str, limit: int) -> int:
    """The largest character index ``i`` with ``utf16_len(text[:i]) <= limit``."""
    if utf16_len(text) <= limit:
        return len(text)
    units = 0
    for i, ch in enumerate(text):
        units += 2 if ord(ch) > 0xFFFF else 1
        if units > limit:
            return i
    return len(text)


def _description(r) -> str:
    try:
        payload = r.json()
        return str(payload.get("description") or "") if isinstance(payload, dict) else ""
    except (ValueError, TypeError, AttributeError):
        return str(getattr(r, "text", "") or "")


def _parameters(r) -> Dict:
    try:
        payload = r.json()
        params = payload.get("parameters") if isinstance(payload, dict) else None
        return params if isinstance(params, dict) else {}
    except (ValueError, TypeError, AttributeError):
        return {}


def _retry_after(r) -> Optional[int]:
    value = _parameters(r).get("retry_after")
    return value if type(value) is int and value > 0 else None


def is_permanent_failure(status: int, description: str) -> bool:
    """Telegram said this chat cannot receive messages from the bot (not a transient condition)."""
    text = (description or "").lower()
    return status == 403 or (status == 400 and any(marker in text for marker in _PERMANENT_400))


class TelegramImageError(RuntimeError):
    """Fixed image intake failure; no file path, user identity or token."""


class Telegram:
    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id
        self.base = f"https://api.telegram.org/bot{token}"
        self.session = requests.Session()
        self._bot_username: Optional[str] = None
        self._state = threading.Lock()
        self._cooldown: Dict[str, float] = {}          # chat -> monotonic time its 429 cool-down ends
        self._group_sends: Dict[str, Deque[float]] = {}
        self._next_global = 0.0
        self._failures: Dict[str, str] = {}            # chat -> last failure kind (bounded)
        self._local = threading.local()
        #: called with (chat_id, reason) when Telegram definitively refuses a chat (blocked, deleted, kicked)
        self.on_permanent_failure: Optional[Callable[[str, str], None]] = None

    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    # ------------------------------------------------------------------ rate-limit policy
    @contextmanager
    def interactive(self):
        """Mark this thread's sends as answers to a user: rate limits fail fast instead of sleeping."""
        previous = getattr(self._local, "interactive", False)
        self._local.interactive = True
        try:
            yield self
        finally:
            self._local.interactive = previous

    def is_interactive(self) -> bool:
        return bool(getattr(self._local, "interactive", False))

    def last_failure(self, chat_id) -> Optional[str]:
        """Why the last send/edit to this chat failed: rate_limited | permanent | transport | http | None."""
        with self._state:
            return self._failures.get(str(chat_id))

    def rate_limited(self, chat_id) -> bool:
        return self.last_failure(chat_id) == "rate_limited"

    def _fail(self, chat: str, kind: str) -> None:
        with self._state:
            self._failures.pop(chat, None)
            self._failures[chat] = kind
            while len(self._failures) > 4096:
                del self._failures[next(iter(self._failures))]

    def _ok(self, chat: str) -> None:
        with self._state:
            self._failures.pop(chat, None)

    def _cool(self, chat: str, seconds: float) -> None:
        with self._state:
            self._cooldown[chat] = max(self._cooldown.get(chat, 0.0), time.monotonic() + seconds)
            if len(self._cooldown) > 4096:
                now = time.monotonic()
                self._cooldown = {c: t for c, t in self._cooldown.items() if t > now}

    def _uncool(self, chat: str) -> None:
        with self._state:
            self._cooldown.pop(chat, None)

    def _admit(self, chat: str) -> bool:
        """Wait for (scheduled) or refuse (interactive) this chat's cool-down and group window, then take a global
        pacing slot. False = not sent; ``last_failure`` says rate_limited."""
        interactive = self.is_interactive()
        for _ in range(3):
            with self._state:
                now = time.monotonic()
                wait = max(0.0, self._cooldown.get(chat, 0.0) - now)
                if chat.startswith("-"):
                    sent = self._group_sends.setdefault(chat, deque())
                    while sent and now - sent[0] >= GROUP_WINDOW:
                        sent.popleft()
                    if len(sent) >= GROUP_LIMIT:
                        wait = max(wait, sent[0] + GROUP_WINDOW - now)
                if wait <= 0:
                    if chat.startswith("-"):
                        self._group_sends[chat].append(now)
                    slot = max(now, self._next_global)
                    self._next_global = slot + GLOBAL_INTERVAL
                    pace = slot - now
                    break
            if interactive or wait > SCHEDULED_MAX_WAIT:
                self._fail(chat, "rate_limited")
                log.warning("telegram: chat is rate limited for %.0fs - %s", wait,
                            "not waiting on an interactive thread" if interactive else "deferred to the next run")
                return False
            _PAUSE.wait(wait)
        else:
            self._fail(chat, "rate_limited")
            return False
        if pace > 0:
            _PAUSE.wait(pace)
        return True

    def _refused(self, chat: str, r) -> None:
        """A non-200 answer: classify it, and report a chat that can never be reached."""
        description = _description(r)
        if is_permanent_failure(r.status_code, description):
            self._fail(chat, "permanent")
            hook = self.on_permanent_failure
            if hook is not None:
                try:
                    hook(chat, f"{r.status_code}: {description[:80]}")
                except Exception as exc:  # noqa: BLE001 - bookkeeping must never break a send
                    log.warning("telegram: permanent-failure hook failed | error_type=%s", type(exc).__name__)
        else:
            self._fail(chat, "rate_limited" if r.status_code == 429 else "http")

    def _migrated(self, chat: str, r) -> Optional[str]:
        """A group that became a supergroup answers 400 with its new id: follow it once."""
        if r.status_code != 400:
            return None
        new = _parameters(r).get("migrate_to_chat_id")
        if type(new) is not int:
            return None
        new_chat = str(new)
        if self.chat_id and str(self.chat_id) == chat:
            self.chat_id = new_chat
            log.warning("telegram: the community group migrated to a supergroup; using the new chat id for this "
                        "process - update TELEGRAM_CHAT_ID in the deployment settings")
        return new_chat

    def download_audit_image(self, file_id: str) -> bytes:
        """Download PNG/JPEG to bounded memory only, from this bot's Telegram files."""
        limit = 5_000_000
        try:
            if not isinstance(file_id, str) or not 1 <= len(file_id) <= 512:
                raise TelegramImageError('invalid_image')
            with self.session.get(f'{self.base}/getFile', params={'file_id': file_id},
                                  timeout=(5, 10), allow_redirects=False) as metadata:
                if metadata.status_code != 200:
                    raise TelegramImageError('image_unavailable')
                payload = metadata.json()
                result = payload.get('result') or {}
                path = result.get('file_path')
                size = result.get('file_size')
                if (not payload.get('ok') or not isinstance(path, str) or len(path) > 512
                        or not re.fullmatch(r'[A-Za-z0-9_./-]+', path)
                        or path.startswith('/') or '..' in path.split('/')
                        or (type(size) is int and size > limit)):
                    raise TelegramImageError('invalid_image')
            started = time.monotonic()
            with self.session.get(f'https://api.telegram.org/file/bot{self.token}/{path}',
                                  stream=True, timeout=(5, 10), allow_redirects=False) as response:
                if response.status_code != 200:
                    raise TelegramImageError('image_unavailable')
                declared = response.headers.get('Content-Length')
                if declared is not None and int(declared) > limit:
                    raise TelegramImageError('image_too_large')
                body = bytearray()
                for chunk in response.iter_content(chunk_size=1):
                    if time.monotonic() - started > 20:
                        raise TelegramImageError('image_timeout')
                    if chunk:
                        body.extend(chunk)
                    if len(body) > limit:
                        raise TelegramImageError('image_too_large')
                data = bytes(body)
                if not (data.startswith(b'\x89PNG\r\n\x1a\n') or data.startswith(b'\xff\xd8\xff')):
                    raise TelegramImageError('invalid_image')
                return data
        except TelegramImageError:
            raise
        except (requests.RequestException, ValueError, TypeError, AttributeError, KeyError):
            raise TelegramImageError('image_unavailable') from None

    def get_bot_username(self) -> Optional[str]:
        """Resolve this token's bot username for private deep links; cache success."""
        if self._bot_username:
            return self._bot_username
        if not self.token:
            return None
        try:
            response = self.session.get(f"{self.base}/getMe", timeout=10)
            if response.status_code != 200:
                log.warning("getMe failed | HTTP %s", response.status_code)
                return None
            payload = response.json()
            username = (payload.get("result") or {}).get("username")
            if payload.get("ok") and isinstance(username, str) and re.fullmatch(r"[A-Za-z0-9_]+", username):
                self._bot_username = username
                return username
            log.warning("getMe returned no valid bot username")
        except (requests.RequestException, ValueError, TypeError, AttributeError) as exc:
            log.warning("getMe failed | error_type=%s", type(exc).__name__)
        return None

    def send(self, text: str, chat_id: Optional[str] = None, parse_mode: str = "HTML",
             reply_markup: Optional[Dict] = None) -> Optional[str]:
        """Return the last message ID only if every chunk succeeds.

        A rate-limited chunk is retried once after Telegram's retry_after on a scheduled thread (when that is at most
        ``SCHEDULED_MAX_WAIT``); on an interactive thread it fails at once and the chat cools down. An HTTP or
        transport failure returns None even when earlier chunks were accepted.
        """
        chat = str(chat_id or self.chat_id)
        last_id = None
        for chunk in _split(text):
            body: Dict[str, Any] = {
                "chat_id": chat, "text": chunk, "parse_mode": parse_mode,
                "disable_web_page_preview": True}
            if reply_markup:
                body["reply_markup"] = reply_markup
            if not self._admit(chat):
                return None
            for attempt in range(2):
                try:
                    r = self.session.post(f"{self.base}/sendMessage", json=body, timeout=20)
                except requests.RequestException as e:
                    log.error("telegram send transport failure: %s", type(e).__name__)
                    self._fail(chat, "transport")
                    return None
                if r.status_code == 429 and attempt == 0:
                    retry_after = _retry_after(r)
                    if retry_after is not None:
                        self._cool(chat, retry_after)
                        if not self.is_interactive() and retry_after <= SCHEDULED_MAX_WAIT:
                            time.sleep(retry_after)
                            self._uncool(chat)
                            continue
                if attempt == 0:
                    migrated = self._migrated(chat, r)
                    if migrated:
                        chat = migrated
                        body = {**body, "chat_id": migrated}
                        continue
                break
            if r.status_code != 200:
                log.error("telegram send failed %s: %s", r.status_code, str(getattr(r, "text", ""))[:200])
                self._refused(chat, r)
                return None
            try:
                message_id = (r.json().get("result") or {}).get("message_id")
            except (ValueError, TypeError, AttributeError):
                message_id = None
            if message_id is None:
                log.error("telegram send returned no message ID")
                self._fail(chat, "http")
                return None
            last_id = str(message_id)
        self._ok(chat)
        return last_id

    def edit_message_text(self, chat_id: str, message_id: int, text: str,
                          reply_markup: Optional[Dict] = None,
                          parse_mode: str = "HTML") -> bool:
        """Edit an existing message (inline pagination).

        Returns True on success; Telegram's 'message is not modified' error
        (same text + same keyboard) is treated as success. A text too long for one
        message is sent as new (split) messages instead. A 429 fails fast on an
        interactive thread and is retried once on a scheduled one."""
        chat = str(chat_id)
        if utf16_len(text) > MAX_LEN:
            log.warning("editMessageText: text exceeds one message; sending it as new messages")
            return self.send(text, chat_id=chat, parse_mode=parse_mode, reply_markup=reply_markup) is not None
        if not self._admit(chat):
            return False
        body = {"chat_id": chat, "message_id": int(message_id), "text": text,
                "parse_mode": parse_mode, "disable_web_page_preview": True,
                **({"reply_markup": reply_markup} if reply_markup else {})}
        for attempt in range(2):
            try:
                r = self.session.post(f"{self.base}/editMessageText", json=body, timeout=20)
            except requests.RequestException as e:
                log.error("editMessageText transport failure: %s", type(e).__name__)
                self._fail(chat, "transport")
                return False
            if r.status_code == 429 and attempt == 0:
                retry_after = _retry_after(r)
                if retry_after is not None:
                    self._cool(chat, retry_after)
                    if not self.is_interactive() and retry_after <= SCHEDULED_MAX_WAIT:
                        time.sleep(retry_after)
                        self._uncool(chat)
                        continue
            break
        if r.status_code == 200 or "message is not modified" in str(getattr(r, "text", "")):
            self._ok(chat)
            return True
        log.error("editMessageText failed %s: %s", r.status_code, str(getattr(r, "text", ""))[:200])
        self._refused(chat, r)
        return False

    def set_my_commands(self, commands: List[Dict[str, str]]) -> bool:
        """Register the commands Telegram lists in the '/' menu (setMyCommands). Idempotent: the same list sent
        again just replaces itself. Never raises - the menu is a convenience, not a dependency."""
        try:
            r = self.session.post(f"{self.base}/setMyCommands", json={"commands": commands}, timeout=10)
        except requests.RequestException as e:
            log.warning("setMyCommands transport failure: %s", type(e).__name__)
            return False
        if r.status_code != 200:
            log.warning("setMyCommands failed %s: %s", r.status_code, r.text[:200])
        return r.status_code == 200

    def answer_callback_query(self, callback_query_id: str, text: Optional[str] = None,
                              show_alert: bool = False) -> bool:
        """Acknowledge a button press (stops the client's loading spinner). Never sleeps, never raises."""
        if not callback_query_id:
            return False
        body: Dict[str, Any] = {"callback_query_id": callback_query_id,
                                "show_alert": show_alert}
        if text:
            body["text"] = text[:200]
        try:
            r = self.session.post(f"{self.base}/answerCallbackQuery", json=body, timeout=10)
        except requests.RequestException as e:
            log.warning("answerCallbackQuery transport failure: %s", type(e).__name__)
            return False
        if r.status_code != 200:
            log.warning("answerCallbackQuery failed %s: %s", r.status_code, str(getattr(r, "text", ""))[:200])
        return r.status_code == 200

    def get_updates(self, offset: Optional[int], timeout: int = 20) -> List[Dict]:
        try:
            r = self.session.get(f"{self.base}/getUpdates", params={"offset": offset, "timeout": timeout,
                                                                    "allowed_updates": '["message","callback_query"]'},
                                 timeout=timeout + 10)
        except requests.RequestException as e:
            raise PollingError(f"Telegram getUpdates transport failure ({type(e).__name__})", retry_in=5) from None
        if r.status_code != 200:
            if r.status_code == 409:
                raise PollingError("Telegram polling conflict (409): another poller or webhook owns this token; stop duplicate consumers")
            if r.status_code == 429:
                raise PollingError("Telegram getUpdates rate limited (429)",
                                   retry_in=min(SCHEDULED_MAX_WAIT, _retry_after(r) or 5))
            raise PollingError(f"Telegram getUpdates failed (HTTP {r.status_code})",
                               retry_in=5 if r.status_code >= 500 else 30)
        # An unreadable answer (a proxy's HTML error page...) is a short pause, never a dead poller.
        try:
            payload = r.json()
        except ValueError:
            raise PollingError("Telegram getUpdates returned a non-JSON response", retry_in=5) from None
        result = payload.get("result", []) if isinstance(payload, dict) else None
        if not isinstance(result, list):
            raise PollingError("Telegram getUpdates returned an unexpected payload", retry_in=5)
        return [u for u in result if isinstance(u, dict) and isinstance(u.get("update_id"), int)]


_STICKY_HEADER = re.compile(r"^(?:🔥 TOP PICKS|📋 DAILY PICKS) · ")
_TAG = re.compile(r"<[^<>]*>")


def _cut_outside_tag(text: str, limit: int) -> int:
    """The last index <= limit at which ``text`` can be cut without landing inside an HTML tag or entity."""
    cut = limit
    for m in _TAG.finditer(text[:limit + 1]):
        if m.start() < cut < m.end():
            cut = m.start()
    amp = text.rfind("&", 0, cut)
    if amp != -1 and ";" not in text[amp:cut] and cut - amp < 10:
        cut = amp
    return cut or limit


def _split_long(paragraph: str, limit: int) -> List[str]:
    """A block longer than one message (should not happen with a pick block): by lines, then by words, never
    inside a tag. ``limit`` is in UTF-16 code units."""
    out, cur = [], ""
    for line in paragraph.split("\n"):
        while utf16_len(line) > limit:
            head = line[:_cut_outside_tag(line, _prefix_len(line, limit))]
            space = head.rfind(" ")
            if space > len(head) // 2:
                head = head[:space]
            if cur:
                out.append(cur)
                cur = ""
            out.append(head)
            line = line[len(head):].lstrip(" ")
        if utf16_len(cur) + utf16_len(line) + 1 > limit and cur:
            out.append(cur)
            cur = ""
        cur = f"{cur}\n{line}" if cur else line
    if cur:
        out.append(cur)
    return out


def _split(text: str, limit: int = MAX_LEN) -> List[str]:
    """Split ``text`` into messages of at most ``limit`` UTF-16 code units, only BETWEEN blocks (a blank line), so
    a three-line pick block is never cut in half, and never inside an HTML tag. A ticket header is kept with the
    block that follows it."""
    if utf16_len(text) <= limit:
        return [text]
    chunks: List[List[str]] = []
    cur: List[str] = []
    size = 0
    for para in text.split("\n\n"):
        for piece in ([para] if utf16_len(para) <= limit else _split_long(para, limit)):
            add = utf16_len(piece) + (2 if cur else 0)
            if cur and size + add > limit:
                carry = [cur.pop()] if _STICKY_HEADER.match(cur[-1]) else []
                if cur:
                    chunks.append(cur)
                cur, size = carry, sum(utf16_len(c) for c in carry)
                add = utf16_len(piece) + (2 if cur else 0)
            cur.append(piece)
            size += add
    if cur:
        chunks.append(cur)
    return ["\n\n".join(c) for c in chunks]


def esc(s: str) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
