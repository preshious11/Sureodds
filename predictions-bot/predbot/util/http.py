"""Shared HTTP session with retries and a simple per-provider rate limiter."""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, Optional

import requests

log = logging.getLogger(__name__)


class ProviderError(Exception):
    pass


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
                 rate: Optional[RateLimiter] = None, retries: int = 2):
        self.base_url = base_url.rstrip("/")
        self._session: Optional[requests.Session] = None
        self._headers = {"User-Agent": "predbot/2.0", "Accept": "application/json"}
        if headers:
            self._headers.update(headers)
        self.timeout = timeout
        self.rate = rate
        self.retries = retries

    @property
    def session(self) -> requests.Session:
        """Lazy-initialize requests.Session on first use to speed up startup."""
        if self._session is None:
            self._session = requests.Session()
            self._session.headers.update(self._headers)
        return self._session

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        url = path if path.startswith("http") else f"{self.base_url}/{path.lstrip('/')}"
        last_err: Optional[Exception] = None
        for attempt in range(self.retries + 1):
            if self.rate:
                self.rate.wait()
            try:
                r = self.session.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as e:
                last_err = e
                log.warning("HTTP error %s (attempt %d): %s", url, attempt + 1, e)
                time.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code == 429:
                retry_after = r.headers.get("Retry-After")
                wait = float(retry_after) if retry_after and retry_after.isdigit() else 6.0 * (attempt + 1)
                log.warning("429 from %s, sleeping %.0fs", url, min(wait, 60))
                time.sleep(min(wait, 60))
                last_err = ProviderError("rate limited")
                continue
            if r.status_code >= 500:
                last_err = ProviderError(f"{r.status_code} from {url}")
                time.sleep(2.0 * (attempt + 1))
                continue
            if r.status_code >= 400:
                raise ProviderError(f"{r.status_code} from {url}: {r.text[:300]}")
            try:
                return r.json()
            except ValueError as e:
                raise ProviderError(f"non-JSON response from {url}") from e
        raise ProviderError(f"request failed: {last_err}")
