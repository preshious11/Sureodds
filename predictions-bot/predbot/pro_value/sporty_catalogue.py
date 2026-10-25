"""SportyBet public football catalogue for the Pro value shadow: one fixed read-only GET, nothing else.

Anonymous, bounded, no cookies or session, no redirects. The only route allowed is the upcoming-events catalogue, so
this module cannot place, share or book anything."""
from __future__ import annotations

import json
import math
import time

import requests

BASE = "https://www.sportybet.com/api/ng"
CATALOGUE_PATH = "/factsCenter/pcUpcomingEvents"
MAX_BYTES = 2_000_000


class CatalogueError(Exception):
    """Fixed status only: never upstream text, headers or cookies."""

    def __init__(self, status):
        self.status = status
        super().__init__(status)


class AnonymousAuth(requests.auth.AuthBase):
    """No-op auth hook: suppress implicit netrc credentials."""

    def __call__(self, request):
        return request


class CatalogueTransport:
    """Bounded anonymous GETs of the upcoming-events catalogue."""

    def __init__(self, limit=4, total_timeout=30):
        if (isinstance(total_timeout, bool) or not isinstance(total_timeout, (int, float))
                or not math.isfinite(total_timeout) or not 0 < total_timeout <= 120):
            raise CatalogueError("invalid_timeout")
        self.limit, self.used, self.last_call = limit, 0, 0.0
        self.deadline = time.monotonic() + total_timeout

    def request(self, method, path, *, params=None):
        if method != "GET" or path != CATALOGUE_PATH:
            raise CatalogueError("forbidden_route")
        if self.used >= self.limit:
            raise CatalogueError("request_budget_exhausted")
        if time.monotonic() >= self.deadline:
            raise CatalogueError("operation_timeout")
        wait = 0.5 - (time.monotonic() - self.last_call)
        if wait > 0:
            time.sleep(wait)
        self.used += 1
        self.last_call = time.monotonic()
        started = time.monotonic()
        remaining = self.deadline - started
        try:
            with requests.Session() as session:  # fresh session per call: no jar, no credentials
                session.auth = AnonymousAuth()
                with session.get(BASE + path, params=params, timeout=(min(4, remaining), min(8, remaining)),
                                 headers={"Accept": "application/json", "Current-Country": "NG",
                                          "User-Agent": "Pro-Value-Shadow/1.0"},
                                 stream=True, allow_redirects=False) as response:
                    if response.status_code in (401, 403):
                        raise CatalogueError("access_blocked")
                    if response.status_code == 429:
                        raise CatalogueError("rate_limited")
                    if response.status_code != 200:
                        raise CatalogueError("http_error")
                    if "json" not in response.headers.get("Content-Type", "").lower():
                        raise CatalogueError("non_json_response")
                    body = bytearray()
                    for part in response.iter_content(8192):
                        body.extend(part)
                        if len(body) > MAX_BYTES:
                            raise CatalogueError("response_too_large")
                        if time.monotonic() - started > 15:
                            raise CatalogueError("timeout")
                    return json.loads(body)
        except CatalogueError:
            raise
        except requests.Timeout:
            raise CatalogueError("timeout") from None
        except (requests.RequestException, OSError):
            raise CatalogueError("network_unavailable") from None
        except (ValueError, TypeError, RecursionError):
            raise CatalogueError("malformed_response") from None


def envelope(data):
    """The catalogue's success envelope; anything else is refused."""
    if not isinstance(data, dict) or data.get("bizCode") != 10000 or not isinstance(data.get("data"), dict):
        raise CatalogueError("provider_rejected")
    return data["data"]


__all__ = ["CatalogueTransport", "CatalogueError", "envelope", "CATALOGUE_PATH"]
