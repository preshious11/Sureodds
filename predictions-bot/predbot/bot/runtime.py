"""Polling ownership on this host and safe, immutable startup identity."""
from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
import socket
import subprocess
from datetime import datetime, timezone

log = logging.getLogger(__name__)


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
