"""Small in-process sliding-window limits for what users can make the bot do.

Used by the command loop: how often one user may interact, how often the same public view may be posted into a
group, and how often one user may start a Custom Picks search. Memory is bounded (least recently used keys are
dropped), state is per process (a new leader starts with fresh windows, which is harmless).
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict, deque
from typing import Callable, Deque, Hashable


class SlidingWindow:
    """At most ``limit`` events per ``window`` seconds per key."""

    def __init__(self, limit: int, window: float, clock: Callable[[], float] = time.monotonic,
                 max_keys: int = 10_000):
        self.limit, self.window, self._clock, self._max_keys = limit, window, clock, max_keys
        self._events: "OrderedDict[Hashable, Deque[float]]" = OrderedDict()
        self._lock = threading.Lock()

    def _trim(self, key: Hashable, now: float) -> Deque[float]:
        events = self._events.get(key)
        if events is None:
            events = self._events[key] = deque()
            while len(self._events) > self._max_keys:
                self._events.popitem(last=False)
        else:
            self._events.move_to_end(key)
        while events and now - events[0] >= self.window:
            events.popleft()
        return events

    def allow(self, key: Hashable) -> bool:
        """Record one event for ``key`` if it fits in the window; False (and nothing recorded) otherwise."""
        with self._lock:
            now = self._clock()
            events = self._trim(key, now)
            if len(events) >= self.limit:
                return False
            events.append(now)
            return True

    def retry_in(self, key: Hashable) -> float:
        """Seconds until ``key`` may act again (0 when it may now)."""
        with self._lock:
            now = self._clock()
            events = self._trim(key, now)
            return 0.0 if len(events) < self.limit else max(0.0, events[0] + self.window - now)


class Cooldown:
    """One event per ``seconds`` per key; ``touch`` starts the cool-down, ``ready`` checks it."""

    def __init__(self, seconds: float, clock: Callable[[], float] = time.monotonic, max_keys: int = 10_000):
        self._window = SlidingWindow(1, seconds, clock, max_keys)

    def ready(self, key: Hashable) -> bool:
        return self._window.retry_in(key) <= 0

    def touch(self, key: Hashable) -> bool:
        """Start the cool-down if it is not running; False when it already was."""
        return self._window.allow(key)


class WorkWindow:
    """At most ``limit`` units of work (e.g. search expansions) per ``window`` seconds per key."""

    def __init__(self, limit: float, window: float, clock: Callable[[], float] = time.monotonic,
                 max_keys: int = 10_000):
        self.limit, self.window, self._clock, self._max_keys = limit, window, clock, max_keys
        self._spent: "OrderedDict[Hashable, Deque[tuple]]" = OrderedDict()
        self._lock = threading.Lock()

    def _total(self, key: Hashable, now: float) -> float:
        entries = self._spent.get(key)
        if entries is None:
            return 0.0
        while entries and now - entries[0][0] >= self.window:
            entries.popleft()
        return sum(amount for _, amount in entries)

    def exhausted(self, key: Hashable) -> bool:
        with self._lock:
            return self._total(key, self._clock()) >= self.limit

    def charge(self, key: Hashable, amount: float) -> None:
        if amount <= 0:
            return
        with self._lock:
            now = self._clock()
            entries = self._spent.setdefault(key, deque())
            self._spent.move_to_end(key)
            entries.append((now, amount))
            while len(self._spent) > self._max_keys:
                self._spent.popitem(last=False)
