"""Process-wide shutdown signal.

``main`` requests a shutdown on SIGTERM / SIGINT or when the leader lease is lost. Long-running work (subscriber
delivery, recap delivery) checks ``shutting_down()`` between sends and stops cleanly: every completed send already
has its receipt, so the next leader resumes with exactly what is missing instead of a half-finished batch being
killed mid-way.
"""
from __future__ import annotations

import threading

_SHUTDOWN = threading.Event()


def request_shutdown() -> None:
    _SHUTDOWN.set()


def shutting_down() -> bool:
    return _SHUTDOWN.is_set()


def reset() -> None:
    """A fresh worker (and every test) starts running."""
    _SHUTDOWN.clear()
