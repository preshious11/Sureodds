"""Minimal Telegram Bot API client (sendMessage + getUpdates)."""
from __future__ import annotations

import logging
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
            r = self.session.post(f"{self.base}/sendMessage", json=body, timeout=20)
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
        r = self.session.post(f"{self.base}/editMessageText", json={
            "chat_id": chat_id, "message_id": int(message_id), "text": text,
            "parse_mode": parse_mode, "disable_web_page_preview": True,
            **({"reply_markup": reply_markup} if reply_markup else {})}, timeout=20)
        if r.status_code == 200 or "message is not modified" in r.text:
            return True
        log.error("editMessageText failed %s: %s", r.status_code, r.text[:200])
        return False

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


def _split(text: str) -> List[str]:
    if len(text) <= MAX_LEN:
        return [text]
    parts, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > MAX_LEN:
            parts.append(cur)
            cur = ""
        cur += line + "\n"
    if cur:
        parts.append(cur)
    return parts


def esc(s: str) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
