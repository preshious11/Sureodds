"""Database-backed leader lease: one production worker per database.

Two processes pointed at the same database (a stale host that was never stopped, a second container during a deploy,
the bot started on a PC with production's ``DB_PATH``) used to both run the scheduler and poll Telegram: duplicate
posts, duplicate provider spend, a 409 tug-of-war on the token. The local ``PollingLease`` lock only protects one host.
This lease lives in the shared SQLite file, so every process that can reach the data also sees the owner.

Rules
* ONE row per lease name: owner, a random token minted on each acquisition, ``expires_at`` (wall clock, epoch s).
* Acquire is atomic (``BEGIN IMMEDIATE``: SQLite serialises writers across processes): it succeeds only when there is
  no row, the row has expired, or the row is already this owner's.
* Renew is a compare-and-set on (owner, token): after anyone else has taken the lease, the old owner's renew changes
  no row, and the old owner learns it lost.
* Self-fencing: the holder considers itself the leader only while its last SUCCESSFUL renewal (measured on its own
  monotonic clock, from BEFORE the write was attempted) is younger than ``ttl - margin``. A holder that cannot reach
  the database - or is paused - stops acting as leader ``margin`` seconds before anyone else may take over, so two
  workers never act together, even when the takeover happens while the old one is stuck.
* Release deletes the row only if it is still ours (owner and token), so a late release can never evict a successor.
* Crash: nothing to release; the row expires after ``ttl`` and the next process takes over.

``margin`` must cover one renewal interval (how late the holder can notice) plus any wall-clock skew between hosts
sharing the file. Identifiers in the owner string are host name, pid and a random suffix - no secret.
"""
from __future__ import annotations

import logging
import os
import secrets
import socket
import sqlite3
import threading
import time
from typing import Callable, Dict, Optional

log = logging.getLogger(__name__)

WORKER_LEASE = "worker"
#: a crashed holder blocks a successor for at most this long
DEFAULT_TTL = 120.0
#: renew this often (four tries inside one TTL)
DEFAULT_RENEW_EVERY = 30.0
#: stop acting as leader this long before the lease can be taken: one renewal interval plus 10 s of clock skew
DEFAULT_MARGIN = 40.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS leader_lease (
    name TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    token TEXT NOT NULL,
    acquired_at REAL NOT NULL,
    renewed_at REAL NOT NULL,
    expires_at REAL NOT NULL
)
"""


def default_owner() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{secrets.token_hex(4)}"


class LeaderLease:
    def __init__(self, db, name: str = WORKER_LEASE, ttl: float = DEFAULT_TTL,
                 renew_every: float = DEFAULT_RENEW_EVERY, margin: float = DEFAULT_MARGIN,
                 owner: Optional[str] = None, clock: Callable[[], float] = time.time,
                 monotonic: Callable[[], float] = time.monotonic):
        if not 0 < margin < ttl or not 0 < renew_every < ttl - margin:
            raise ValueError("lease timing must satisfy 0 < renew_every < ttl - margin and 0 < margin < ttl")
        self.db, self.name, self.ttl, self.renew_every, self.margin = db, name, ttl, renew_every, margin
        self.owner = owner or default_owner()
        self._clock, self._mono = clock, monotonic
        self._token: Optional[str] = None
        self._confirmed_at: Optional[float] = None     # monotonic time the last successful write STARTED
        self._lost = False
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        with db._lock:
            db.conn.execute(SCHEMA)
            db.conn.commit()

    # ------------------------------------------------------------------ state
    def held(self) -> bool:
        """Whether this process may act as the leader right now (fails closed)."""
        return (not self._lost and self._token is not None and self._confirmed_at is not None
                and self._mono() - self._confirmed_at < self.ttl - self.margin)

    @property
    def lost(self) -> bool:
        return self._lost

    def holder(self) -> Optional[Dict]:
        """The current row: owner and seconds until it expires (negative = expired). None when nobody holds it."""
        rows = self.db.query("SELECT owner, expires_at FROM leader_lease WHERE name=?", (self.name,))
        if not rows:
            return None
        return {"owner": rows[0]["owner"], "expires_in": float(rows[0]["expires_at"]) - self._clock()}

    # ------------------------------------------------------------------ transitions
    def acquire(self) -> bool:
        """Take the lease if it is free, expired or already ours. Atomic across processes."""
        if self._lost:
            return False
        started = self._mono()
        now = self._clock()
        try:
            with self.db.transaction():
                rows = self.db.query("SELECT owner, token, expires_at FROM leader_lease WHERE name=?", (self.name,))
                row = rows[0] if rows else None
                ours = row is not None and row["owner"] == self.owner and row["token"] == self._token
                if row is not None and not ours and float(row["expires_at"]) > now:
                    return False
                token = self._token if ours else secrets.token_hex(16)
                self.db.execute("INSERT INTO leader_lease(name, owner, token, acquired_at, renewed_at, expires_at) "
                                "VALUES(?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET owner=excluded.owner, "
                                "token=excluded.token, acquired_at=excluded.acquired_at, "
                                "renewed_at=excluded.renewed_at, expires_at=excluded.expires_at",
                                (self.name, self.owner, token, now, now, now + self.ttl))
        except sqlite3.Error as exc:
            log.warning("leader lease: acquire failed (%s)", type(exc).__name__)
            return False
        if row is not None and not ours:
            log.warning("leader lease: took over an expired lease from %s", row["owner"])
        self._token, self._confirmed_at = token, started
        return True

    def renew(self) -> bool:
        """Extend our lease. False when it could not be extended; ``lost`` is set when another owner has it (or our
        own fence already ran out) - that is permanent for this object."""
        if self._lost or self._token is None:
            return False
        if not self.held():
            self._mark_lost("not renewed within the fence window")
            return False
        started = self._mono()
        now = self._clock()
        try:
            cur = self.db.execute("UPDATE leader_lease SET renewed_at=?, expires_at=? "
                                  "WHERE name=? AND owner=? AND token=?",
                                  (now, now + self.ttl, self.name, self.owner, self._token))
        except sqlite3.Error as exc:
            log.warning("leader lease: renew failed (%s); still leader for %.0fs unless it succeeds",
                        type(exc).__name__, max(0.0, self.ttl - self.margin - (self._mono() - self._confirmed_at)))
            return False
        if cur.rowcount != 1:
            self._mark_lost("another owner holds the lease")
            return False
        self._confirmed_at = started
        return True

    def release(self) -> None:
        """Give the lease up now (clean shutdown) so a successor need not wait for it to expire."""
        self.stop_heartbeat()
        token, self._token = self._token, None
        if token is None:
            return
        try:
            self.db.execute("DELETE FROM leader_lease WHERE name=? AND owner=? AND token=?",
                            (self.name, self.owner, token))
        except sqlite3.Error as exc:
            log.warning("leader lease: release failed (%s); it expires in %.0fs", type(exc).__name__, self.ttl)

    def _mark_lost(self, why: str) -> None:
        if not self._lost:
            self._lost = True
            log.critical("leader lease LOST (%s): this process must stop scheduled work and Telegram polling", why)

    # ------------------------------------------------------------------ heartbeat
    def start_heartbeat(self, on_lost: Callable[[], None], tick: float = 5.0) -> None:
        """Renew every ``renew_every`` seconds on a daemon thread, checking the fence every ``tick`` seconds; call
        ``on_lost`` once if leadership ends."""
        tick = max(0.01, min(tick, self.renew_every))

        def beat():
            next_renew = self._mono() + self.renew_every
            while not self._stop.wait(tick):
                if self._mono() >= next_renew:
                    self.renew()
                    next_renew = self._mono() + self.renew_every
                if not self.held():
                    self._mark_lost("fence window elapsed or lease taken")
                    on_lost()
                    return

        self._stop.clear()
        self._thread = threading.Thread(target=beat, name=f"lease-{self.name}", daemon=True)
        self._thread.start()

    def stop_heartbeat(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)


def wait_for_leadership(lease: LeaderLease, stop: threading.Event, log_every: float = 600.0) -> bool:
    """Standby: keep trying until the lease is ours (True) or ``stop`` is set (False). A standby process runs no
    scheduler and no Telegram polling - it only waits for the holder to release or expire."""
    last_log = None
    while not stop.is_set():
        if lease.acquire():
            return True
        now = time.monotonic()
        if last_log is None or now - last_log >= log_every:
            current = lease.holder() or {}
            log.warning("STANDBY: another instance holds the worker lease (owner=%s, expires in %.0fs). This process "
                        "runs no scheduler and no Telegram polling; it takes over only when that instance stops or "
                        "stops renewing. If this is unexpected, stop the duplicate deployment.",
                        current.get("owner", "?"), max(0.0, current.get("expires_in", 0.0)))
            last_log = now
        stop.wait(lease.renew_every)
    return False
