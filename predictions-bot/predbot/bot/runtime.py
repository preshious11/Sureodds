"""Polling ownership on this host and safe, immutable startup identity."""
from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
import socket
import subprocess
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)


class CommandLoopSupervisor:
    """Keep exactly one Telegram poller alive without taking the scheduler down.

    A dead command thread used to raise out of the scheduler's health check,
    which killed the whole worker - no picks, no settlement, no delivery.  A
    poller that dies (transient Telegram outage, lost lease) must cost commands
    only: the supervisor restarts it, backs off while restarts keep failing
    (e.g. another process owns the polling lease), and the scheduler keeps
    generating and delivering predictions either way.
    """

    def __init__(self, start: Callable[[], Any], cooldown: float = 60.0,
                 backoff_max: float = 900.0):
        self._start = start
        self._cooldown = max(1.0, cooldown)
        self._backoff_max = max(self._cooldown, backoff_max)
        self._backoff = self._cooldown
        self._next_attempt = 0.0
        self._loop: Any = None

    @property
    def loop(self) -> Any:
        return self._loop

    def start(self, now: Optional[float] = None) -> Any:
        """Start the first consumer; the returned object needs ``is_alive``."""
        self._loop = self._start()
        self._next_attempt = (time.monotonic() if now is None else now) + self._cooldown
        return self._loop

    def ensure(self, now: Optional[float] = None) -> None:
        """Restart the consumer when it died; never raises, never restarts early."""
        if self._loop is not None and self._loop.is_alive():
            return
        moment = time.monotonic() if now is None else now
        if moment < self._next_attempt:
            return
        try:
            loop = self._start()
        except Exception as exc:  # noqa: BLE001
            self._backoff = min(self._backoff_max, max(self._cooldown, self._backoff * 2))
            self._next_attempt = moment + self._backoff
            log.error("command consumer restart failed (%s); retrying in %.0fs",
                      type(exc).__name__, self._backoff)
            return
        if self._loop is not None:
            stop = getattr(self._loop, "stop", None)
            if callable(stop):
                try:
                    stop()
                except Exception:  # noqa: BLE001
                    pass
        self._loop = loop
        self._backoff = self._cooldown
        self._next_attempt = moment + self._cooldown
        log.error("command consumer died; restarted it (%s), scheduler unaffected",
                  getattr(loop, "name", type(loop).__name__))

    def stop(self, join_timeout: float = 35.0) -> None:
        if self._loop is None:
            return
        stop = getattr(self._loop, "stop", None)
        if callable(stop):
            stop()
        join = getattr(self._loop, "join", None)
        if callable(join):
            join(timeout=join_timeout)


class PollingLease:
    """OS-held lock shared by local processes, released even after a crash.

    This is deliberately NOT a cross-container/distributed lock. Production
    must still have exactly one replica/service consuming this bot's updates.
    """

    def __init__(self, token: str):
        import tempfile
        self.path = Path(tempfile.gettempdir()) / (
            "predbot-poll-" + hashlib.sha256(token.encode()).hexdigest() + ".lock")
        self.file = None

    def __enter__(self):
        self.file = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self.file.seek(0, 2)
                if self.file.tell() == 0:
                    self.file.write(b"0")
                    self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            self.file = None
            raise RuntimeError("Another local process already owns Telegram polling") from None
        return self

    def __exit__(self, *args):
        if self.file is not None:
            self.file.close()
            self.file = None


def log_identity(handler) -> str:
    source = Path(handler.__code__.co_filename).resolve()
    fingerprint = hashlib.sha256(source.read_bytes()).hexdigest()[:16]
    commit = os.environ.get("RAILWAY_GIT_COMMIT_SHA", "")
    if not commit:
        try:
            commit = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=source.parent,
                stderr=subprocess.DEVNULL, text=True, timeout=3).strip()
        except (OSError, subprocess.SubprocessError):
            commit = "unknown"
    identity = f"{socket.gethostname()}:{os.getpid()}:{fingerprint}"
    log.info("Bot instance started | utc=%s | instance=%s | commit=%s | mode=polling | "
             "service=%s | deployment=%s | start_handlers=1 | callback_routers=1 | "
             "start_handler=%s.%s | source=%s",
             datetime.now(timezone.utc).isoformat(), identity, commit,
             os.environ.get("RAILWAY_SERVICE_NAME", "local"),
             os.environ.get("RAILWAY_DEPLOYMENT_ID", "local"),
             handler.__module__, handler.__name__, source)
    return identity
