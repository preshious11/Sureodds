"""Minimal Telegram Bot API client (sendMessage + getUpdates)."""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

import requests

log = logging.getLogger(__name__)
MAX_LEN = 4000


class PollingError(RuntimeError):
    """Sanitized polling failure; never includes a credential-bearing URL."""


class Telegram:
    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id
        self.base = f"https://api.telegram.org/bot{token}"
        self.session = requests.Session()

    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def send(self, text: str, chat_id: Optional[str] = None, parse_mode: str = "HTML",
             reply_markup: Optional[Dict] = None) -> Optional[str]:
        chat = chat_id or self.chat_id
        last_id = None
        for chunk in _split(text):
            body: Dict[str, Any] = {
                "chat_id": chat, "text": chunk, "parse_mode": parse_mode,
                "disable_web_page_preview": True}
            if reply_markup:
                body["reply_markup"] = reply_markup
            try:
                r = self.session.post(f"{self.base}/sendMessage", json=body, timeout=20)
            except requests.RequestException as e:
                # A transport failure here must fail like a bad HTTP status
                # (return, don't raise) - callers throughout the bot treat
                # send() as a plain success/failure signal, never an exception.
                log.error("telegram send transport failure: %s", type(e).__name__)
                return last_id
            if r.status_code != 200:
                log.error("telegram send failed %s: %s", r.status_code, r.text[:200])
                return last_id
            last_id = str((r.json().get("result") or {}).get("message_id"))
        return last_id

    def edit_message_text(self, chat_id: str, message_id: int, text: str,
                          reply_markup: Optional[Dict] = None,
                          parse_mode: str = "HTML") -> bool:
        """Edit an existing message (inline pagination).

        Returns True on success; Telegram's 'message is not modified' error
        (same text + same keyboard) is treated as success."""
        try:
            r = self.session.post(f"{self.base}/editMessageText", json={
                "chat_id": chat_id, "message_id": int(message_id), "text": text,
                "parse_mode": parse_mode, "disable_web_page_preview": True,
                **({"reply_markup": reply_markup} if reply_markup else {})}, timeout=20)
        except requests.RequestException as e:
            log.error("editMessageText transport failure: %s", type(e).__name__)
            return False
        if r.status_code == 200 or "message is not modified" in r.text:
            return True
        log.error("editMessageText failed %s: %s", r.status_code, r.text[:200])
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
        """Acknowledge a button press (stops the client's loading spinner)."""
        if not callback_query_id:
            return False
        body: Dict[str, Any] = {"callback_query_id": callback_query_id,
                                "show_alert": show_alert}
        if text:
            body["text"] = text
        r = self.session.post(f"{self.base}/answerCallbackQuery", json=body, timeout=10)
        if r.status_code != 200:
            log.warning("answerCallbackQuery failed %s: %s", r.status_code, r.text[:200])
        return r.status_code == 200

    def get_updates(self, offset: Optional[int], timeout: int = 20) -> List[Dict]:
        try:
            r = self.session.get(f"{self.base}/getUpdates", params={"offset": offset, "timeout": timeout,
                                                                    "allowed_updates": '["message","callback_query"]'},
                                 timeout=timeout + 10)
            if r.status_code != 200:
                if r.status_code == 409:
                    raise PollingError("Telegram polling conflict (409): another poller or webhook owns this token; stop duplicate consumers")
                raise PollingError(f"Telegram getUpdates failed (HTTP {r.status_code})")
            return r.json().get("result", [])
        except requests.RequestException as e:
            raise PollingError(f"Telegram getUpdates transport failure ({type(e).__name__})") from None


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
    inside a tag."""
    out, cur = [], ""
    for line in paragraph.split("\n"):
        while len(line) > limit:
            head = line[:_cut_outside_tag(line, limit)]
            space = head.rfind(" ")
            if space > limit // 2:
                head = head[:space]
            if cur:
                out.append(cur)
                cur = ""
            out.append(head)
            line = line[len(head):].lstrip(" ")
        if len(cur) + len(line) + 1 > limit and cur:
            out.append(cur)
            cur = ""
        cur = f"{cur}\n{line}" if cur else line
    if cur:
        out.append(cur)
    return out


def _split(text: str, limit: int = MAX_LEN) -> List[str]:
    """Split ``text`` into messages of at most ``limit`` characters, only BETWEEN blocks (a blank line), so a
    three-line pick block is never cut in half, and never inside an HTML tag. A ticket header is kept with the
    block that follows it."""
    if len(text) <= limit:
        return [text]
    chunks: List[List[str]] = []
    cur: List[str] = []
    size = 0
    for para in text.split("\n\n"):
        for piece in ([para] if len(para) <= limit else _split_long(para, limit)):
            add = len(piece) + (2 if cur else 0)
            if cur and size + add > limit:
                carry = [cur.pop()] if _STICKY_HEADER.match(cur[-1]) else []
                if cur:
                    chunks.append(cur)
                cur, size = carry, sum(len(c) for c in carry)
                add = len(piece) + (2 if cur else 0)
            cur.append(piece)
            size += add
    if cur:
        chunks.append(cur)
    return ["\n\n".join(c) for c in chunks]


def esc(s: str) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
