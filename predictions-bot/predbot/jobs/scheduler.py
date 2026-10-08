"""Tiny in-process scheduler (no external deps): daily recap, daily picks and
smart settlement (runs after all predictions end), all in the configured timezone.

Overlap: a job never runs twice at once in this process. While a job's previous run is still going, it is not
dispatched again: an interval job waits for its next interval, a daily or deadline job is retried on the next tick
(so it still runs once the previous run ends). Different jobs never wait for each other. (The database leader lease
keeps a second PROCESS from running them at all.)

Shutdown: ``loop`` returns promptly when stopped; ``drain`` then gives the runs in progress a bounded time to
finish (they also see ``lifecycle.shutting_down()`` and stop between sends) instead of being killed mid-write."""
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


class _Runner:
    """The thread of a job's current run; refuses a second concurrent run of the same job."""

    thread: Optional[threading.Thread] = None

    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def _start(self) -> None:
        self.thread = threading.Thread(target=_run_fn, args=(self.name, self.fn),
                                       name=f"job-{self.name}", daemon=True)
        self.thread.start()

    def _busy(self) -> bool:
        if self.running():
            log.warning("job %s: previous run still in progress - not starting another", self.name)
            return True
        return False


class DailyJob(_Runner):
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
        if self._busy():
            return                         # not marked as run: retried on the next tick
        self.last_run_date = to_local(now, self.tz).strftime("%Y-%m-%d")
        self._start()


class IntervalJob(_Runner):
    def __init__(self, name: str, minutes: int, fn: Callable[[], None]):
        self.name, self.every, self.fn = name, timedelta(minutes=minutes), fn
        self.next = utcnow() + timedelta(minutes=2)

    def due(self, now: datetime) -> bool:
        return now >= self.next

    def run(self, now: datetime) -> None:
        self.next = now + self.every
        if self._busy():
            return
        self._start()


class DynamicJob(_Runner):
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
        if self._busy():
            return                         # the deadline is kept: retried on the next tick
        self.last_run_deadline = self.next
        self.next = None
        self._start()


def drain(jobs, timeout: float) -> list:
    """Wait up to ``timeout`` seconds in total for the jobs' runs in progress; returns the names still running."""
    deadline = time.monotonic() + max(0.0, timeout)
    for j in jobs:
        thread = getattr(j, "thread", None)
        if thread is not None and thread.is_alive():
            thread.join(max(0.0, deadline - time.monotonic()))
    left = [j.name for j in jobs if getattr(j, "thread", None) is not None and j.thread.is_alive()]
    if left:
        log.warning("shutdown: %s still running after %.0fs - exiting anyway (receipts make it resumable)",
                    ", ".join(left), timeout)
    return left


def loop(jobs, poll_seconds: int = 30, health_check=None, leader_check: Optional[Callable[[], bool]] = None,
         stop_event: Optional[threading.Event] = None) -> None:
    """Dispatch due jobs every ``poll_seconds`` until SIGTERM/SIGINT or ``stop_event``.

    ``leader_check``: asked before every dispatch; False (this process is no longer the database's leader) stops the
    loop at once, so a worker that lost its lease never starts another job."""
    log.info("scheduler running with %d jobs", len(jobs))
    shutdown = False

    def handle_signal(signum, frame):
        nonlocal shutdown
        log.info("received signal %d, shutting down gracefully", signum)
        shutdown = True
        if stop_event is not None:
            stop_event.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    def leading() -> bool:
        if leader_check is None or leader_check():
            return True
        log.critical("scheduler: this process is no longer the leader - stopping before dispatching anything")
        return False

    while not shutdown and not (stop_event is not None and stop_event.is_set()):
        if not leading():
            break
        if health_check is not None:
            health_check()
        now = utcnow()
        for j in jobs:
            try:
                if j.due(now):
                    if not leading():
                        shutdown = True
                        break
                    log.info("running job %s", j.name)
                    j.run(now)
            except Exception:  # noqa: BLE001 - one broken job must not stop the scheduler
                log.exception("scheduler: job %s failed to check/dispatch", getattr(j, "name", "?"))
        if shutdown:
            break
        if stop_event is not None:
            stop_event.wait(poll_seconds)
        else:
            time.sleep(poll_seconds)

    log.info("scheduler stopped")
