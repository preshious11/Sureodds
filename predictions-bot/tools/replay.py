"""Replay harness: one recorded day of provider data -> the whole pipeline, offline and deterministic.

    python tools/replay.py                   # run + print a summary (timings, counts, latencies)
    python tools/replay.py --record          # (re)write tests/fixtures/replay/golden.json
    python tools/replay.py --profile         # cProfile top-30 for score + select
    python tools/replay.py --json out.json   # write the full result

``tests/fixtures/replay/replay.db`` holds the ``provider_cache`` rows of ONE real generation (SharpAPI +
SportsGameOdds, recorded by a single live ``cli.py picks --dry-run``) and the clock instant it ran at. The
harness copies it to a temp file, pins every ``utcnow`` to that instant, makes every HTTP call raise, and runs
``collect_persisted -> merge -> score -> select -> store -> render`` exactly as the scheduler does. It also
drives /top, /daily, the pagination callbacks and ``deliver_picks`` (300 fake subscribers) against a fake
Telegram client. What it writes is the *slate*: card text, views, deliveries, stored picks and the selection
diagnostics with every timing field stripped - byte-identical before and after any optimisation.
"""
from __future__ import annotations

import json
import os
import shutil
import statistics
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

REPLAY_DB = ROOT / "tests" / "fixtures" / "replay" / "replay.db"
GOLDEN = ROOT / "tests" / "fixtures" / "replay" / "golden.json"
TZ = "Africa/Lagos"

#: keys that hold timings or wall-clock stamps - never part of the golden
_VOLATILE = {"timings", "generated_at", "retry_after", "updated_at", "at", "collect_s", "select_timings"}


def strip(obj):
    if isinstance(obj, dict):
        return {k: strip(v) for k, v in obj.items() if k not in _VOLATILE}
    if isinstance(obj, list):
        return [strip(v) for v in obj]
    return obj


# ---------------------------------------------------------------------------------------------- environment
def peak_rss_mb() -> float:
    try:
        import resource
        v = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return round(v / (1024 * 1024 if sys.platform == "darwin" else 1024), 1)
    except ImportError:
        pass
    try:                                                     # Windows
        import ctypes
        from ctypes import wintypes

        class PMC(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD), ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t), ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t), ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t)]
        pmc = PMC()
        pmc.cb = ctypes.sizeof(PMC)
        kernel32, psapi = ctypes.WinDLL("kernel32"), ctypes.WinDLL("psapi")
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]
        psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb)
        return round(pmc.PeakWorkingSetSize / (1024 * 1024), 1)
    except Exception:  # noqa: BLE001
        return 0.0


@contextmanager
def replay_env(now_iso: str):
    """Pinned clock, blocked network, hermetic configuration."""
    import requests
    import predbot.util.timeutil as tu
    from predbot.util.timeutil import parse_iso

    now = parse_iso(now_iso)
    real = tu.utcnow
    fake = lambda: now                                        # noqa: E731
    patched = []
    for name, mod in list(sys.modules.items()):
        if name.startswith("predbot") and getattr(mod, "utcnow", None) is real:
            patched.append(mod)
            mod.utcnow = fake

    def no_network(*a, **k):
        raise RuntimeError("replay: network access is disabled")

    keep = {k: v for k, v in os.environ.items() if k.upper() in ("PATH", "SYSTEMROOT", "TEMP", "TMP", "USERPROFILE", "HOME", "COMSPEC", "PATHEXT")}
    env = dict(keep, TIMEZONE=TZ, SHARPAPI_KEY="replay", SPORTSGAMEODDS_KEY="replay", ODDS_API_KEY="", FOOTBALL_DATA_KEY="",
               TELEGRAM_BOT_TOKEN="", TELEGRAM_CHAT_ID="")
    with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(requests.Session, "request", no_network), \
            mock.patch.object(requests, "request", no_network):
        try:
            yield now
        finally:
            for mod in patched:
                mod.utcnow = real


class FakeTelegram:
    """Records every message; ``send`` sleeps ``delay`` seconds like a real network round trip."""

    def __init__(self, delay: float = 0.0):
        self.token, self.chat_id, self.delay = "fake-token", "0", delay
        self.sent = []
        self._lock = threading.Lock()

    def enabled(self):
        return True

    def send(self, text, chat_id=None, parse_mode="HTML", reply_markup=None, **kw):
        if self.delay:
            time.sleep(self.delay)
        with self._lock:
            self.sent.append((str(chat_id), text, json.dumps(reply_markup, sort_keys=True) if reply_markup else ""))
        return len(self.sent)

    def edit_message_text(self, chat_id, message_id, text, reply_markup=None, **kw):
        with self._lock:
            self.sent.append((f"edit:{chat_id}:{message_id}", text, json.dumps(reply_markup, sort_keys=True) if reply_markup else ""))
        return True

    def answer_callback_query(self, callback_id, text=None, **kw):
        with self._lock:
            self.sent.append((f"answer:{callback_id}", text or "", ""))
        return True


# ---------------------------------------------------------------------------------------------- the run
def run(profile: bool = False, subscribers: int = 300, send_delay: float = 0.03, workdir: str = "") -> dict:
    from predbot.bot import callbacks
    from predbot.bot.broadcast import deliver_picks, picks_text
    from predbot.cache.fetcher import UnifiedFetcher
    from predbot.config import Config
    from predbot.markets.settlement import log_coverage  # noqa: F401
    from predbot.providers.chain import ProviderChain, build_providers
    from predbot.storage.db import Database
    from predbot.util.timeutil import iso

    tmp = workdir or tempfile.mkdtemp(prefix="predbot-replay-")
    path = os.path.join(tmp, "replay.db")
    shutil.copyfile(REPLAY_DB, path)
    src = Database(path)
    now_iso = src.get_kv("replay:now")
    result: dict = {"recorded_at": now_iso}
    try:
        with replay_env(now_iso) as now:
            cfg = Config()
            db = src
            providers = build_providers(cfg, db)
            chain = ProviderChain(providers, cfg.provider_order)
            service = UnifiedFetcher(chain, db, cfg)
            db.stats.update(executes=0, commits=0)

            # ---- generation: collect -> merge -> score -> select -> store -> render
            import cProfile
            import pstats
            import io
            prof = cProfile.Profile() if profile else None
            wall, cpu = time.perf_counter(), time.process_time()
            if prof:
                prof.enable()
            snapshot = service.get_daily_snapshot(force_refresh=False, persist=True, scheduled=True)   # nothing cached yet: a MISS
            if prof:
                prof.disable()
            gen_wall, gen_cpu = time.perf_counter() - wall, time.process_time() - cpu
            t = time.perf_counter()
            card = picks_text(snapshot, TZ)
            render_s = time.perf_counter() - t
            result["generation"] = {"wall": round(gen_wall, 4), "cpu": round(gen_cpu, 4), "render_card": round(render_s, 5),
                                    "sqlite": dict(db.stats), "timings": snapshot.generation.get("timings", {}),
                                    "peak_rss_mb": peak_rss_mb()}
            if prof:
                buf = io.StringIO()
                pstats.Stats(prof, stream=buf).sort_stats("cumulative").print_stats(30)
                result["profile"] = buf.getvalue()

            # ---- slate (the golden)
            rows = [dict(r) for r in db.picks_for_date(snapshot.date)]
            for r in rows:
                for volatile in ("id", "created_at"):
                    r.pop(volatile, None)
            slate = {"status": snapshot.status, "date": snapshot.date, "card": card,
                     "top": [p.to_dict() if hasattr(p, "to_dict") else str(p) for p in snapshot.top_picks],
                     "daily": [p.to_dict() if hasattr(p, "to_dict") else str(p) for p in snapshot.daily_picks],
                     "stored_picks": rows, "diagnostics": strip(snapshot.generation)}

            # ---- commands: /top /daily, pagination callbacks, warm and while a generation holds the lock
            tg = FakeTelegram()
            db.subscriber_add("100", "u", "User", timezone=TZ)

            def latency(n=40):
                samples = []
                for i in range(n):
                    tg.sent.clear()
                    t0 = time.perf_counter()
                    callbacks.handle_command("/top" if i % 2 == 0 else "/daily", "100", service=service, tg=tg, cfg=cfg, db=db)
                    samples.append(time.perf_counter() - t0)
                return samples

            warm = latency()
            holder_ready, release = threading.Event(), threading.Event()

            def hold():
                with db.prediction_lock:
                    holder_ready.set()
                    release.wait(10)

            th = threading.Thread(target=hold, daemon=True)
            th.start()
            holder_ready.wait(5)
            locked = latency()
            release.set()
            th.join()

            def pct(xs, q):
                xs = sorted(xs)
                return round(xs[min(len(xs) - 1, int(q * len(xs)))] * 1000, 2)

            result["commands"] = {"warm_ms": {"p50": pct(warm, .5), "p95": pct(warm, .95)},
                                  "during_generation_ms": {"p50": pct(locked, .5), "p95": pct(locked, .95)}}
            views = {}
            for cmd in ("/top", "/daily"):
                tg.sent.clear()
                callbacks.handle_command(cmd, "100", service=service, tg=tg, cfg=cfg, db=db)
                views[cmd] = tg.sent[-1][1:]
                markup = json.loads(tg.sent[-1][2] or "{}")
            pages = {}
            for view in ("top", "daily"):
                for page in (0, 1):
                    tg.sent.clear()
                    callbacks.handle_callback({"id": f"cb-{view}-{page}", "data": f"{view}:{page}",
                                               "message": {"chat": {"id": 100}, "message_id": 7}, "from": {}},
                                              service=service, tg=tg, cfg=cfg, db=db)
                    pages[f"{view}:{page}"] = [s[1:] for s in tg.sent]
            slate["views"], slate["pages"] = views, pages

            # ---- delivery: N subscribers, real per-user rendering, a fake network with a per-send delay
            tzs = ["Africa/Lagos", "Europe/London", "America/New_York", "Asia/Tokyo"]
            for i in range(subscribers):
                db.subscriber_add(str(1000 + i), f"s{i}", f"Sub{i}", timezone=tzs[i % len(tzs)])
            db.conn.execute("UPDATE subscribers SET subscribed_at='2026-09-01T00:00:00Z'")
            db.conn.commit()
            deliver_now = now.replace(hour=8, minute=31)          # 09:31 in Lagos on the card's day
            dtg = FakeTelegram(delay=send_delay)
            db.stats.update(executes=0, commits=0)
            t0 = time.perf_counter()
            sent = deliver_picks(db, cfg, dtg, service.published_snapshot, now=deliver_now)
            deliver_wall = time.perf_counter() - t0
            again = deliver_picks(db, cfg, FakeTelegram(), service.published_snapshot, now=deliver_now)
            result["delivery"] = {"subscribers": subscribers, "sent": sent, "second_pass_sent": again,
                                  "wall": round(deliver_wall, 3), "sqlite": dict(db.stats)}
            by_chat = {c: (text) for c, text, _ in dtg.sent}
            slate["delivery"] = {"sent": sent, "second_pass": again,
                                 "cards_by_tz": {tz: next(by_chat[str(1000 + i)] for i in range(subscribers) if tzs[i % len(tzs)] == tz)
                                                 for tz in tzs}, "chats": sorted(by_chat)[:5], "n_deliveries":
                                     db.query("SELECT COUNT(*) AS n FROM deliveries")[0]["n"]}
            result["slate"] = slate
            result["peak_rss_mb"] = peak_rss_mb()
    finally:
        try:
            src.close()
        except Exception:  # noqa: BLE001
            pass
        if not workdir:
            shutil.rmtree(tmp, ignore_errors=True)
    return result


def golden_text(result: dict) -> str:
    return json.dumps(result["slate"], indent=1, sort_keys=True, ensure_ascii=False, default=str)


def main(argv):
    result = run(profile="--profile" in argv)
    if "--record" in argv:
        GOLDEN.write_text(golden_text(result) + "\n", encoding="utf-8")
        print("golden written:", GOLDEN, GOLDEN.stat().st_size, "bytes")
    if "--json" in argv:
        Path(argv[argv.index("--json") + 1]).write_text(json.dumps(result, indent=1, default=str), encoding="utf-8")
    summary = {k: v for k, v in result.items() if k not in ("slate", "profile")}
    print(json.dumps(summary, indent=1, default=str))
    if result.get("profile"):
        print(result["profile"])


if __name__ == "__main__":
    main(sys.argv[1:])
