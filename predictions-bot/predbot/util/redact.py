"""Never let a credential or an account identifier reach a log line, a report or a terminal.

``redact`` masks, in any text: API keys in query strings / headers (``apiKey=…``, ``x-api-key: …``,
``Authorization: Bearer …``), Telegram bot tokens (inside ``…/bot<id>:<secret>/…`` URLs and bare), e-mail addresses,
and long hexadecimal identifiers (SGO's ``keyID`` is a 64-character hex string). ``known_secrets`` adds exact values
(the configured keys) so a key that appears in an unexpected shape is still removed.

``RedactingFilter`` applies it to every log record, so a ``requests`` exception that embeds the full request URL
(``…?apiKey=SECRET``) is masked before it is written anywhere."""
from __future__ import annotations

import logging
import re
from typing import Iterable, List

MASK = "***"

_PATTERNS = [
    # key=value in query strings / dict-ish text
    (re.compile(r"(?i)\b(api[_-]?key|apikey|token|secret|password|access[_-]?token|key)(=|\":\s*\"|':\s*')([^&\s\"',)]+)"),
     lambda m: f"{m.group(1)}{m.group(2)}{MASK}"),
    # HTTP headers
    (re.compile(r"(?i)\b(x-api-key|x-apikey|authorization|api-key)(\s*[:=]\s*)(bearer\s+)?([^\s,;\"']+)"),
     lambda m: f"{m.group(1)}{m.group(2)}{(m.group(3) or '')}{MASK}"),
    # Telegram: https://api.telegram.org/bot123456:ABC-DEF/...  and a bare 123456:AA… token
    (re.compile(r"(/bot)\d{5,}:[A-Za-z0-9_-]{20,}"), lambda m: f"{m.group(1)}{MASK}"),
    (re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b"), lambda m: MASK),
    # e-mail addresses
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), lambda m: MASK),
    # long hex identifiers (SGO keyID is 64 hex chars; customer ids look like cus_XXXXXXXX)
    (re.compile(r"\b[0-9a-fA-F]{32,}\b"), lambda m: MASK),
    (re.compile(r"\bcus_[A-Za-z0-9]{8,}\b"), lambda m: MASK),
]


def redact(text, known_secrets: Iterable[str] = ()) -> str:
    out = str(text)
    for secret in known_secrets:
        if secret and len(str(secret)) >= 6:
            out = out.replace(str(secret), MASK)
    for pattern, repl in _PATTERNS:
        out = pattern.sub(repl, out)
    return out


class RedactingFilter(logging.Filter):
    """Masks credentials and account identifiers in every record (message, args and exception text)."""

    def __init__(self, known_secrets: Iterable[str] = ()):
        super().__init__()
        self.known: List[str] = [s for s in known_secrets if s]

    def add_secret(self, value: str) -> None:
        if value and value not in self.known:
            self.known.append(value)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:                                  # a malformed format string must never break logging
            message = str(record.msg)
        record.msg, record.args = redact(message, self.known), None
        if record.exc_info and record.exc_info[1] is not None:
            record.exc_text = redact("".join(__import__("traceback").format_exception(*record.exc_info)), self.known)
            record.exc_info = None
        return True


def install(known_secrets: Iterable[str] = ()) -> RedactingFilter:
    """Attach (once) a RedactingFilter to every root handler; later calls just register more secrets."""
    root = logging.getLogger()
    for handler in root.handlers:
        for existing in handler.filters:
            if isinstance(existing, RedactingFilter):
                for s in known_secrets:
                    existing.add_secret(s)
                break
        else:
            handler.addFilter(RedactingFilter(known_secrets))
    return next((f for h in root.handlers for f in h.filters if isinstance(f, RedactingFilter)), RedactingFilter())
