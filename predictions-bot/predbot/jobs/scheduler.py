"""Tiny in-process scheduler (no external deps): daily recap, daily picks and
smart settlement (runs after all predictions end), all in the configured timezone."""
from __future__ import annotations

import logging
import signal
import time
from datetime import datetime, timedelta
from typing import Callable, Optional

from predbot.util.timeutil import to_local, utcnow

log = logging.getLogger(__name__)


def _parse_hhmm(s: str):
    h, m = s.split(":")
    return int(h), int(m)


class DailyJob:
    def __init__(self, name: str, at: str, tz: str, fn: Callable[[], None]):
        self.name, self.tz, self.fn = name, tz, fn
        self.h, self.m = _parse_hhmm(at)
        self.last_run_date: Optional[str] = None

    def due(self, now: datetime) -> bool:
        local = to_local(now, self.tz)
        today = local.strftime("%Y-%m-%d")
        if self.last_run_date == today:
            return False
        return (local.hour, local.minute) >= (self.h, self.m)

    def run(self, now: datetime) -> None:
        self.last_run_date = to_local(now, self.tz).strftime("%Y-%m-%d")
        try:
            self.fn()
        except Exception as e:  # noqa: BLE001
            log.exception("job %s failed: %s", self.name, e)


class IntervalJob:
    def __init__(self, name: str, minutes: int, fn: Callable[[], None]):
        self.name, self.every, self.fn = name, timedelta(minutes=minutes), fn
        self.next = utcnow() + timedelta(minutes=2)

    def due(self, now: datetime) -> bool:
        return now >= self.next

    def run(self, now: datetime) -> None:
        self.next = now + self.every
        try:
            self.fn()
        except Exception as e:  # noqa: BLE001
            log.exception("job %s failed: %s", self.name, e)


class DynamicJob:
    """Runs once when a computed deadline is reached (e.g., after all daily matches end)."""
    def __init__(self, name: str, tz: str, compute_next_fn: Callable[[datetime], Optional[datetime]], fn: Callable[[], None]):
        self.name, self.tz, self.fn = name, tz, fn
        self.compute_next_fn = compute_next_fn
        self.next: Optional[datetime] = None
        self.last_run_date: Optional[str] = None

    def due(self, now: datetime) -> bool:
        if self.next is None:
            self.next = self.compute_next_fn(now)
        if self.next is None:
            return False
        if self.last_run_date == to_local(now, self.tz).strftime("%Y-%m-%d"):
            return False
        return now >= self.next

    def run(self, now: datetime) -> None:
        self.last_run_date = to_local(now, self.tz).strftime("%Y-%m-%d")
        self.next = None
        try:
            self.fn()
        except Exception as e:  # noqa: BLE001
            log.exception("job %s failed: %s", self.name, e)


def loop(jobs, poll_seconds: int = 30, health_check=None) -> None:
    log.info("scheduler running with %d jobs", len(jobs))
    shutdown = False

    def handle_signal(signum, frame):
        nonlocal shutdown
        log.info("received signal %d, shutting down gracefully", signum)
        shutdown = True

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    while not shutdown:
        if health_check is not None:
            health_check()
        now = utcnow()
        for j in jobs:
            if j.due(now):
                log.info("running job %s", j.name)
                j.run(now)
        time.sleep(poll_seconds)

    log.info("scheduler stopped")
