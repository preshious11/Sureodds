"""CacheManager: deterministic UTC keys, TTL values, per-day mutex.

Keys (all UTC-based, per the write-once-per-day contract)
    bot:predictions:daily:{YYYY-MM-DD}   -> unified snapshot for that UTC day
    bot:predictions:lock:{YYYY-MM-DD}    -> build mutex (one API batch per day)

TTL
    Values expire at the *end* of the UTC day they belong to, so every new UTC
    day begins with a guaranteed cache miss and exactly one build.

Backends (priority: Redis → File → Memory)
    * RedisBackend (optional)   - enabled when ``redis_url`` is set and the
      ``redis`` package is installed (``pip install redis``).  Shares cache and
      lock across processes/replicas (for Railway multi-instance).
    * FileBackend (optional)    - enabled when ``cache_dir`` is set. Stores
      JSON snapshots locally with TTL metadata. Good for dev/offline use.
    * InMemoryBackend (default) - thread-safe TTL dictionary guarded by an
      RLock. Correct for the single-process worker (scheduler thread and the
      command-loop thread share one CacheManager instance).

The mutex is a token lock: in-memory it is a threading.Lock per key; on Redis
it is ``SET NX PX`` acquired with a unique token and released via a
compare-and-delete Lua script, so a stale holder can never delete someone
else's lock.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterator, Optional, Tuple

log = logging.getLogger(__name__)

DAILY_KEY_PREFIX = "bot:predictions:daily:"
LOCK_KEY_PREFIX = "bot:predictions:lock:"


def utc_day(now: Optional[datetime] = None) -> date:
    return (now or datetime.now(timezone.utc)).date()


def ttl_until_end_of_utc_day(now: Optional[datetime] = None, minimum: int = 60) -> int:
    """Seconds from *now* until the end of the current UTC day (>= minimum)."""
    now = now or datetime.now(timezone.utc)
    midnight = datetime.combine(now.date() + timedelta(days=1),
                                datetime.min.time(), tzinfo=timezone.utc)
    return max(minimum, int((midnight - now).total_seconds()))


class _Backend:
    """Shared backend interface (JSON strings in/out, token mutex)."""

    def get(self, key: str) -> Optional[str]:
        raise NotImplementedError

    def set(self, key: str, payload: str, ttl_seconds: float) -> None:
        raise NotImplementedError

    def delete(self, key: str) -> None:
        raise NotImplementedError

    def try_acquire(self, key: str, token: str, ttl_seconds: float) -> bool:
        raise NotImplementedError

    def release(self, key: str, token: str) -> None:
        raise NotImplementedError


class InMemoryBackend(_Backend):
    """TTL dictionary + per-key mutex. Single process, zero dependencies."""

    def __init__(self) -> None:
        self._data: Dict[str, Tuple[float, str]] = {}
        self._meta = threading.RLock()
        self._mutexes: Dict[str, threading.Lock] = {}

    def get(self, key: str) -> Optional[str]:
        with self._meta:
            item = self._data.get(key)
        if item is None:
            return None
        expires_at, payload = item
        if expires_at and time.time() >= expires_at:
            with self._meta:
                self._data.pop(key, None)
            return None
        return payload

    def set(self, key: str, payload: str, ttl_seconds: float) -> None:
        expires_at = time.time() + max(1.0, float(ttl_seconds))
        with self._meta:
            self._data[key] = (expires_at, payload)

    def delete(self, key: str) -> None:
        with self._meta:
            self._data.pop(key, None)

    def try_acquire(self, key: str, token: str, ttl_seconds: float) -> bool:
        del token, ttl_seconds  # process-local lock: ownership is implicit
        with self._meta:
            lock = self._mutexes.setdefault(key, threading.Lock())
        return lock.acquire(blocking=False)

    def release(self, key: str, token: str) -> None:
        del token
        with self._meta:
            lock = self._mutexes.get(key)
        if lock is not None:
            try:
                lock.release()
            except RuntimeError:
                pass  # not held by this thread any more


class FileBackend(_Backend):
    """Local filesystem cache: JSON files with TTL metadata. Good for dev/offline."""

    def __init__(self, cache_dir: str = "./cache") -> None:
        self._cache_dir = cache_dir
        os.makedirs(cache_dir, exist_ok=True)
        self._mutexes: Dict[str, threading.Lock] = {}

    def _path(self, key: str) -> str:
        safe_key = key.replace(":", "_").replace("/", "_")
        return os.path.join(self._cache_dir, f"{safe_key}.json")

    def _meta_path(self, key: str) -> str:
        return self._path(key).replace(".json", ".meta")

    def get(self, key: str) -> Optional[str]:
        path = self._path(key)
        meta_path = self._meta_path(key)
        if not os.path.exists(path):
            return None
        try:
            if os.path.exists(meta_path):
                with open(meta_path) as f:
                    meta = json.load(f)
                    if time.time() >= meta.get("expires_at", 0):
                        os.remove(path)
                        os.remove(meta_path)
                        return None
            with open(path) as f:
                return f.read()
        except Exception:
            return None

    def set(self, key: str, payload: str, ttl_seconds: float) -> None:
        path = self._path(key)
        meta_path = self._meta_path(key)
        expires_at = time.time() + max(1.0, float(ttl_seconds))
        try:
            with open(path, "w") as f:
                f.write(payload)
            with open(meta_path, "w") as f:
                json.dump({"expires_at": expires_at}, f)
        except Exception:
            pass

    def delete(self, key: str) -> None:
        path = self._path(key)
        meta_path = self._meta_path(key)
        try:
            os.remove(path)
            os.remove(meta_path)
        except FileNotFoundError:
            pass

    def try_acquire(self, key: str, token: str, ttl_seconds: float) -> bool:
        with threading.RLock():
            lock = self._mutexes.setdefault(key, threading.Lock())
        return lock.acquire(blocking=False)

    def release(self, key: str, token: str) -> None:
        with threading.RLock():
            lock = self._mutexes.get(key)
        if lock is not None:
            try:
                lock.release()
            except RuntimeError:
                pass


class RedisBackend(_Backend):
    """Cross-process backend enabled by REDIS_URL."""

    _RELEASE_LUA = (
        "if redis.call('get', KEYS[1]) == ARGV[1] then "
        "return redis.call('del', KEYS[1]) else return 0 end"
    )

    def __init__(self, url: str) -> None:
        import redis

        self._client = redis.Redis.from_url(url, decode_responses=True,
                                            socket_connect_timeout=3, socket_timeout=5)
        self._client.ping()  # fail fast -> the manager falls back to memory
        self._release_script = self._client.register_script(self._RELEASE_LUA)

    def get(self, key: str) -> Optional[str]:
        return self._client.get(key)

    def set(self, key: str, payload: str, ttl_seconds: float) -> None:
        self._client.set(key, payload, ex=max(1, int(ttl_seconds)))

    def delete(self, key: str) -> None:
        self._client.delete(key)

    def try_acquire(self, key: str, token: str, ttl_seconds: float) -> bool:
        return bool(self._client.set(key, token, nx=True,
                                     px=max(1000, int(ttl_seconds * 1000))))

    def release(self, key: str, token: str) -> None:
        self._release_script(keys=[key], args=[token])


class CacheManager:
    """Public cache API: JSON values, UTC daily keys, token mutex.

    Cache priority: Redis (multi-instance) → File (local persistent) → Memory.
    """

    def __init__(self, redis_url: str = "", cache_dir: str = "") -> None:
        self._backend: _Backend = InMemoryBackend()
        self._backend_name = "memory"

        if redis_url:
            try:
                self._backend = RedisBackend(redis_url)
                self._backend_name = "redis"
                log.info("Cache backend: redis (connection verified)")
                return
            except Exception as exc:  # noqa: BLE001
                log.warning("REDIS_URL is set but Redis initialization failed (%s); "
                            "Redis caching is inactive, trying file cache. "
                            "Check the redis dependency, credentials and network reachability.",
                            type(exc).__name__)

        if cache_dir:
            try:
                self._backend = FileBackend(cache_dir)
                self._backend_name = "file"
                log.info("Using local file cache: %s", cache_dir)
                return
            except Exception as exc:  # noqa: BLE001
                log.warning("File cache unavailable (%s) - using in-process cache", exc)

    @property
    def backend_name(self) -> str:
        return self._backend_name

    # ------------------------------------------------------------ keys
    @staticmethod
    def daily_key(day: Optional[date] = None) -> str:
        return f"{DAILY_KEY_PREFIX}{(day or utc_day()).isoformat()}"

    @staticmethod
    def lock_key(day: Optional[date] = None) -> str:
        return f"{LOCK_KEY_PREFIX}{(day or utc_day()).isoformat()}"

    @staticmethod
    def seconds_until_midnight_utc(now: Optional[datetime] = None) -> int:
        return ttl_until_end_of_utc_day(now)

    # ------------------------------------------------------------ values
    def get(self, key: str) -> Optional[Any]:
        payload = self._backend.get(key)
        if payload is None:
            return None
        try:
            return json.loads(payload)
        except ValueError:
            log.warning("corrupt cache payload for %s - dropping entry", key)
            self._backend.delete(key)
            return None

    def set(self, key: str, value: Any, ttl_seconds: float) -> None:
        self._backend.set(key, json.dumps(value, separators=(",", ":"), default=str),
                          ttl_seconds)

    def delete(self, key: str) -> None:
        self._backend.delete(key)

    # ------------------------------------------------------------ mutex
    @contextmanager
    def lock(self, key: str, token: Optional[str] = None, ttl_seconds: float = 30.0,
             wait_seconds: float = 45.0, poll_seconds: float = 0.25) -> Iterator[bool]:
        """Try to acquire the named mutex, spinning until *wait_seconds* elapse.

        Yields True when the lock is held (the caller must build), False when
        the wait timed out (the caller should look for the writer's cache
        entry instead of building).
        """
        token = token or f"{time.time_ns()}-{threading.get_ident()}"
        deadline = time.monotonic() + wait_seconds
        acquired = self._backend.try_acquire(key, token, ttl_seconds)
        try:
            while not acquired and time.monotonic() < deadline:
                time.sleep(poll_seconds)
                acquired = self._backend.try_acquire(key, token, ttl_seconds)
            yield acquired
        finally:
            if acquired:
                try:
                    self._backend.release(key, token)
                except Exception:  # noqa: BLE001
                    log.exception("failed to release lock %s", key)

