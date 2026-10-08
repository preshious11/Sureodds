"""Shared HTTP session with retries and a simple per-provider rate limiter.

Safety rules every provider inherits (none of them changes what a provider asks for):

* connect and read time out separately (``CONNECT_TIMEOUT`` / the provider's ``timeout``), so an unreachable host
  fails in seconds instead of holding a generation for the whole read timeout;
* a request that carries a credential (an auth header, or a key/token in the query) never follows a redirect to a
  different scheme, host or port - the key would be sent to whoever the redirect names. Same-origin redirects are
  followed, without the original query;
* response bodies are read with a byte cap (``max_bytes``; a short cap for error bodies), so a misbehaving endpoint
  cannot exhaust memory;
* nothing sleeps after the final attempt, and ``Retry-After`` is honoured in both its forms (seconds or HTTP-date).
"""
from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urljoin, urlparse

import requests

log = logging.getLogger(__name__)

_REQUESTS: Dict[str, int] = {}
_REQUESTS_LOCK = threading.Lock()

#: seconds to establish a connection (the read timeout is each provider's own ``timeout``)
CONNECT_TIMEOUT = 5.0
#: default cap on a successful response body; callers with bigger pages pass their own ``max_bytes``
MAX_BODY_BYTES = 50 * 1024 * 1024
#: an error body is only ever quoted (300 characters), so little of it is read
MAX_ERROR_BYTES = 64 * 1024
#: longest a single back-off may sleep, whatever Retry-After asks for
MAX_RETRY_WAIT = 60.0
MAX_REDIRECTS = 3

_CREDENTIAL_HEADER = re.compile(r"(?i)(auth|key|token|secret|identity|cookie|session|password)")
_CREDENTIAL_PARAM = re.compile(r"(?i)(key|token|secret|password|signature|auth)")
_PUBLIC_HEADERS = frozenset({"user-agent", "accept"})


def _count_request(url: str) -> None:
    host = urlparse(url).netloc or "unknown"
    with _REQUESTS_LOCK:
        _REQUESTS[host] = _REQUESTS.get(host, 0) + 1


def request_counts() -> Dict[str, int]:
    """HTTP attempts made by this process, per host, retries included (billed or not). Diff two snapshots to
    attribute a generation cycle's consumption."""
    with _REQUESTS_LOCK:
        return dict(_REQUESTS)


def request_delta(before: Dict[str, int]) -> Dict[str, int]:
    now = request_counts()
    return {h: n - before.get(h, 0) for h, n in sorted(now.items()) if n - before.get(h, 0)}


def _kind_of(status: Optional[int]) -> str:
    if status is None:
        return ""
    if status == 429:
        return "rate_limited"
    return "server" if status >= 500 else "client" if status >= 400 else ""


class ProviderError(Exception):
    """A provider call failed. ``status`` is the HTTP status when there was one (429 = rate limited / quota).

    ``kind`` says what failed - ``transport`` (connection refused / reset / DNS), ``timeout``, ``rate_limited``,
    ``client`` (another 4xx), ``server`` (5xx), ``invalid`` (unreadable body), ``redirect`` (refused) or
    ``too_large`` - and ``body`` holds the (redacted) start of an error response, so a caller can decide on what the
    PROVIDER said instead of on text that merely contains the request URL."""

    def __init__(self, message: str = "", status: Optional[int] = None, kind: str = "", body: str = "",
                 retry_after: Optional[float] = None):
        super().__init__(message)
        self.status = status
        self.kind = kind or _kind_of(status)
        self.body = body
        #: seconds the provider asked us to wait (its Retry-After), when it said
        self.retry_after = retry_after


def retry_after_seconds(value: Optional[str], now: Optional[datetime] = None) -> Optional[float]:
    """``Retry-After`` as seconds to wait: delay-seconds (``"120"``) or an HTTP-date. None when absent/unreadable."""
    text = str(value).strip() if value is not None else ""
    if not text:
        return None
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - (now or datetime.now(timezone.utc))).total_seconds())


def _origin(url: str) -> Tuple[str, str, Optional[int]]:
    p = urlparse(url)
    return p.scheme.lower(), (p.hostname or "").lower(), p.port or {"https": 443, "http": 80}.get(p.scheme.lower())


class RateLimiter:
    def __init__(self, min_interval: float):
        self.min_interval = min_interval
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            delta = now - self._last
            if delta < self.min_interval:
                time.sleep(self.min_interval - delta)
            self._last = time.monotonic()


class Http:
    def __init__(self, base_url: str, headers: Optional[Dict[str, str]] = None, timeout: int = 25,
                 rate: Optional[RateLimiter] = None, retries: int = 2, sleep_on_429: bool = True,
                 max_bytes: int = MAX_BODY_BYTES):
        self.base_url = base_url.rstrip("/")
        self._session: Optional[requests.Session] = None
        self._headers = {"User-Agent": "predbot/2.0", "Accept": "application/json"}
        if headers:
            self._headers.update(headers)
        #: (connect, read): ``timeout`` stays the read timeout; connecting gets at most CONNECT_TIMEOUT of it
        self.timeout = (min(CONNECT_TIMEOUT, float(timeout)), float(timeout))
        self.rate = rate
        self.retries = retries
        self.max_bytes = max_bytes
        #: False = a 429 is reported immediately instead of sleeping and retrying (the caller owns the cool-down)
        self.sleep_on_429 = sleep_on_429
        #: HTTP attempts made by the most recent ``get`` (retries included) - request metering reads this
        self.last_attempts = 0
        # Response headers from the MOST RECENT successful call. Providers that
        # meter usage in response headers (the Odds API's x-requests-remaining /
        # x-requests-used) read them here instead of re-implementing requests.
        self.last_headers: Dict[str, str] = {}

    @property
    def session(self) -> requests.Session:
        """Lazy-initialize requests.Session on first use to speed up startup."""
        if self._session is None:
            self._session = requests.Session()
            self._session.headers.update(self._headers)
        return self._session

    def carries_credentials(self, params: Optional[Dict[str, Any]] = None) -> bool:
        """An auth header (``X-Api-Key``, ``X-Auth-Token``, ``Authorization`` ...) or a key/token query parameter."""
        if any(name.lower() not in _PUBLIC_HEADERS and _CREDENTIAL_HEADER.search(name) for name in self._headers):
            return True
        return any(_CREDENTIAL_PARAM.search(str(name)) for name in (params or {}))

    def _send(self, url: str, params: Optional[Dict[str, Any]], keyed: bool):
        """One logical request. Redirects are followed by hand so a credential never leaves the origin it was
        configured for; an unkeyed request follows them to any host, as before."""
        r = self.session.get(url, params=params, timeout=self.timeout, allow_redirects=False, stream=True)
        for _hop in range(MAX_REDIRECTS + 1):
            status = getattr(r, "status_code", 0)
            location = (getattr(r, "headers", None) or {}).get("Location") if 300 <= status < 400 else None
            if not location:
                return r
            _close(r)
            if _hop == MAX_REDIRECTS:
                break
            target = urljoin(url, location)
            if keyed and _origin(target) != _origin(url):
                raise ProviderError(f"{status} redirect to another host refused (credentials not forwarded)",
                                    status=status, kind="redirect")
            url, params = target, None
            _count_request(url)
            r = self.session.get(url, params=None, timeout=self.timeout, allow_redirects=False, stream=True)
        raise ProviderError("too many redirects", status=getattr(r, "status_code", None), kind="redirect")

    def _backoff(self, attempt: int, seconds: float) -> bool:
        """Sleep before the next attempt; False (and no sleep) when this was the last one."""
        if attempt >= self.retries:
            return False
        time.sleep(min(MAX_RETRY_WAIT, max(0.0, seconds)))
        return True

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        url = path if path.startswith("http") else f"{self.base_url}/{path.lstrip('/')}"
        from predbot.util.redact import redact
        keyed = self.carries_credentials(params)
        last_err: Optional[ProviderError] = None
        self.last_attempts = 0
        for attempt in range(self.retries + 1):
            if self.rate:
                self.rate.wait()
            self.last_attempts += 1
            _count_request(url)
            try:
                r = self._send(url, params, keyed)
            except requests.RequestException as e:
                # (a requests exception embeds the full URL - query-string credentials included; the logging filter
                #  and this explicit redaction both keep them out of the log)
                last_err = ProviderError(redact(e), kind="timeout" if isinstance(e, requests.Timeout) else "transport")
                log.warning("HTTP error %s (attempt %d): %s", url, attempt + 1, redact(e))
                self._backoff(attempt, 1.5 * (attempt + 1))
                continue
            if r.status_code == 429:
                wait = retry_after_seconds((r.headers or {}).get("Retry-After"))
                _close(r)
                last_err = ProviderError(f"429 from {url}: rate limited", status=429, retry_after=wait)
                if not self.sleep_on_429:
                    raise last_err
                wait = wait if wait is not None else 6.0 * (attempt + 1)
                if attempt < self.retries:
                    log.warning("429 from %s, sleeping %.0fs", url, min(wait, MAX_RETRY_WAIT))
                self._backoff(attempt, wait)
                continue
            if r.status_code >= 500:
                _close(r)
                last_err = ProviderError(f"{r.status_code} from {url}", status=r.status_code)
                self._backoff(attempt, 2.0 * (attempt + 1))
                continue
            if r.status_code >= 400:
                # Capture headers even on a quota error: the Odds API still
                # reports x-requests-remaining on 401 OUT_OF_USAGE_CREDITS.
                self.last_headers = {k.lower(): v for k, v in (r.headers or {}).items()}
                body = redact(_text(r, MAX_ERROR_BYTES)[:300])
                raise ProviderError(f"{r.status_code} from {url}: {body}", status=r.status_code, body=body)
            if not _read(r, self.max_bytes):
                raise ProviderError(f"response from {url} exceeds {self.max_bytes} bytes", status=r.status_code,
                                    kind="too_large")
            try:
                payload = r.json()
            except ValueError as e:
                raise ProviderError(f"non-JSON response from {url}", status=r.status_code, kind="invalid") from e
            self.last_headers = {k.lower(): v for k, v in (r.headers or {}).items()}
            return payload
        err = last_err or ProviderError("no attempt made")
        raise ProviderError(f"request failed: {redact(err)}", status=err.status, kind=err.kind, body=err.body,
                            retry_after=err.retry_after)


def _close(r) -> None:
    close = getattr(r, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # noqa: BLE001 - releasing a connection must never mask the real outcome
            pass


def _read(r, limit: int) -> bool:
    """Load a streamed body into the response, refusing more than ``limit`` bytes. A test double that is not a
    real ``requests.Response`` already holds its body and passes through."""
    if not isinstance(r, requests.Response) or r._content_consumed:
        return True
    declared = str(r.headers.get("Content-Length", ""))
    if declared.isdigit() and int(declared) > limit:
        _close(r)
        return False
    chunks, size = [], 0
    for chunk in r.iter_content(64 * 1024):
        size += len(chunk)
        if size > limit:
            _close(r)
            return False
        chunks.append(chunk)
    r._content = b"".join(chunks)
    r._content_consumed = True
    return True


def _text(r, limit: int) -> str:
    """At most ``limit`` bytes of an error body, as text."""
    if isinstance(r, requests.Response) and not r._content_consumed:
        chunks, size = [], 0
        for chunk in r.iter_content(8 * 1024):
            chunks.append(chunk)
            size += len(chunk)
            if size >= limit:
                break
        _close(r)
        r._content = b"".join(chunks)[:limit]
        r._content_consumed = True
    try:
        return r.text or ""
    except Exception:  # noqa: BLE001
        return ""
