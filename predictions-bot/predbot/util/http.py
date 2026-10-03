"""Shared HTTP session with retries and a simple per-provider rate limiter."""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import requests

log = logging.getLogger(__name__)

_REQUESTS: Dict[str, int] = {}
_REQUESTS_LOCK = threading.Lock()


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


class ProviderError(Exception):
    """A provider call failed. ``status`` is the HTTP status when there was one (429 = rate limited / quota)."""

    def __init__(self, message: str = "", status: Optional[int] = None):
        super().__init__(message)
        self.status = status


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
                 rate: Optional[RateLimiter] = None, retries: int = 2, sleep_on_429: bool = True):
        self.base_url = base_url.rstrip("/")
        self._session: Optional[requests.Session] = None
        self._headers = {"User-Agent": "predbot/2.0", "Accept": "application/json"}
        if headers:
            self._headers.update(headers)
        self.timeout = timeout
        self.rate = rate
        self.retries = retries
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

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        url = path if path.startswith("http") else f"{self.base_url}/{path.lstrip('/')}"
        from predbot.util.redact import redact
        last_err: Optional[Exception] = None
        self.last_attempts = 0
        for attempt in range(self.retries + 1):
            if self.rate:
                self.rate.wait()
            self.last_attempts += 1
            _count_request(url)
            try:
                r = self.session.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as e:
                last_err = e
                # (a requests exception embeds the full URL - query-string credentials included; the logging filter
                #  and this explicit redaction both keep them out of the log)
                from predbot.util.redact import redact
                log.warning("HTTP error %s (attempt %d): %s", url, attempt + 1, redact(e))
                time.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code == 429:
                if not self.sleep_on_429:
                    raise ProviderError(f"429 from {url}: rate limited", status=429)
                retry_after = r.headers.get("Retry-After")
                wait = float(retry_after) if retry_after and retry_after.isdigit() else 6.0 * (attempt + 1)
                log.warning("429 from %s, sleeping %.0fs", url, min(wait, 60))
                time.sleep(min(wait, 60))
                last_err = ProviderError("rate limited", status=429)
                continue
            if r.status_code >= 500:
                last_err = ProviderError(f"{r.status_code} from {url}")
                time.sleep(2.0 * (attempt + 1))
                continue
            if r.status_code >= 400:
                # Capture headers even on a quota error: the Odds API still
                # reports x-requests-remaining on 401 OUT_OF_USAGE_CREDITS.
                self.last_headers = {k.lower(): v for k, v in r.headers.items()}
                from predbot.util.redact import redact
                raise ProviderError(f"{r.status_code} from {url}: {redact(r.text[:300])}", status=r.status_code)
            try:
                payload = r.json()
            except ValueError as e:
                raise ProviderError(f"non-JSON response from {url}") from e
            self.last_headers = {k.lower(): v for k, v in r.headers.items()}
            return payload
        raise ProviderError(f"request failed: {redact(last_err)}", status=getattr(last_err, "status", None))
