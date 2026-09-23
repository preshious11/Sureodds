"""Tiny in-process scheduler (no external deps): daily recap, daily picks and
smart settlement (runs after all predictions end), all in the configured timezone."""
from __future__ import annotations

import logging
import signal
import threading
import time
from datetime import datetime, timedelta
from typing import Callable, Optional

from predbot.util.timeutil import to_local, utcnow

log = logging.getLogger(__name__)


def _parse_hhmm(s: str):
    h, m = s.split(":")
    return int(h), int(m)


def _run_fn(name: str, fn: Callable[[], None]) -> None:
    try:
        fn()
    except Exception as e:  # noqa: BLE001
        log.exception("job %s failed: %s", name, e)


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
        # Marking "ran today" happens synchronously (so the scheduler's next
        # tick never double-dispatches this job); the job body itself - which
        # can mean provider API calls, scoring, or a settlement scan - runs on
        # its own thread so a catch-up run due right at process start (e.g.
        # after a redeploy that lands past post_time) can never compete with
        # or delay the Telegram polling thread.
        self.last_run_date = to_local(now, self.tz).strftime("%Y-%m-%d")
        threading.Thread(target=_run_fn, args=(self.name, self.fn),
                         name=f"job-{self.name}", daemon=True).start()


class IntervalJob:
    def __init__(self, name: str, minutes: int, fn: Callable[[], None]):
        self.name, self.every, self.fn = name, timedelta(minutes=minutes), fn
        self.next = utcnow() + timedelta(minutes=2)

    def due(self, now: datetime) -> bool:
        return now >= self.next

    def run(self, now: datetime) -> None:
        self.next = now + self.every
        threading.Thread(target=_run_fn, args=(self.name, self.fn),
                         name=f"job-{self.name}", daemon=True).start()


class DynamicJob:
    """Runs when a computed deadline is reached (e.g., after all daily matches end).

    The once-only guard is keyed on the deadline INSTANT, not the calendar day: a
    run at 01:30 for yesterday's late match must not stop the 23:00 deadline of
    today's matches from running."""
    def __init__(self, name: str, tz: str, compute_next_fn: Callable[[datetime], Optional[datetime]], fn: Callable[[], None]):
        self.name, self.tz, self.fn = name, tz, fn
        self.compute_next_fn = compute_next_fn
        self.next: Optional[datetime] = None
        self.last_run_deadline: Optional[datetime] = None

    def due(self, now: datetime) -> bool:
        if self.next is None or self.next == self.last_run_deadline:
            self.next = self.compute_next_fn(now)      # re-derive: the last deadline already ran
        if self.next is None or self.next == self.last_run_deadline:
            return False
        return now >= self.next

    def run(self, now: datetime) -> None:
        self.last_run_deadline = self.next
        self.next = None
        threading.Thread(target=_run_fn, args=(self.name, self.fn),
                         name=f"job-{self.name}", daemon=True).start()


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
            try:
                if j.due(now):
                    log.info("running job %s", j.name)
                    j.run(now)
            except Exception:  # noqa: BLE001 - one broken job must not stop the scheduler
                log.exception("scheduler: job %s failed to check/dispatch", getattr(j, "name", "?"))
        time.sleep(poll_seconds)

    log.info("scheduler stopped")
