"""Replay harness: one recorded day of provider data -> the whole pipeline, offline and deterministic.

    python tools/replay.py                   # run + print a summary (timings, counts, latencies)
    python tools/replay.py --record [slate|render]   # (re)write tests/fixtures/replay/golden_{slate,render}.json
    python tools/replay.py --profile         # cProfile top-30 for score + select
    python tools/replay.py --json out.json   # write the full result

``tests/fixtures/replay/replay.db`` holds the ``provider_cache`` rows of ONE real generation (SharpAPI +
SportsGameOdds, recorded by a single live ``cli.py picks --dry-run``) and the clock instant it ran at. The
harness copies it to a temp file, pins every ``utcnow`` to that instant, makes every HTTP call raise, and runs
``collect_persisted -> merge -> score -> select -> store -> render`` exactly as the scheduler does. It also
drives /top, /daily, the pagination callbacks and ``deliver_picks`` (300 fake subscribers) against a fake
Telegram client. It writes two goldens: the *slate* (stored picks, their order, prices, labels, delivery counts and
the selection diagnostics with every timing field stripped - byte-identical before and after any optimisation or
presentation change) and the *render* (every text a reader sees: cards, pages, deliveries, the stats and recap
cards over a seeded settled history, /yesterday, every empty state).
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
GOLDEN_SLATE = ROOT / "tests" / "fixtures" / "replay" / "golden_slate.json"
GOLDEN_RENDER = ROOT / "tests" / "fixtures" / "replay" / "golden_render.json"
TZ = "Africa/Lagos"

#: keys that hold timings or wall-clock stamps - never part of the golden
_VOLATILE = {"timings", "generated_at", "retry_after", "updated_at", "at", "collect_s", "select_timings",
             "today"}                                # budget.used_today reads the real calendar day, not the pinned clock


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
            # This frozen recording predates collection signatures. Stamp only
            # the temporary copy so replay continues testing its recorded odds,
            # without attempting to fetch today's expanded league configuration.
            # Signature invalidation itself is covered by provider-cache tests.
            from predbot.cache.providers import collection_signature
            for row in db.query('SELECT cache_key, metadata_json FROM provider_cache'):
                metadata = json.loads(row['metadata_json'])
                name = row['cache_key'].rsplit(':', 1)[-1]
                metadata['collection_signature'] = collection_signature(name, cfg)
                db.execute('UPDATE provider_cache SET metadata_json=? WHERE cache_key=?',
                           (json.dumps(metadata, sort_keys=True), row['cache_key']))
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
                r["collections"] = sorted(r.get("collections") or ())      # a set: its repr order varies with the hash seed
            render = {"card": card}
            slate = {"status": snapshot.status, "date": snapshot.date,
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
            day_key = snapshot.date.replace("-", "")
            for view in ("top", "daily"):
                for page, data in ((0, f"pg|{view}|0"), (1, f"pg|{view}|1|{day_key}")):   # a view switch, then a page turn
                    tg.sent.clear()
                    callbacks.handle_callback({"id": f"cb-{view}-{page}", "data": data,
                                               "message": {"chat": {"id": 100}, "message_id": 7}, "from": {}},
                                              service=service, tg=tg, cfg=cfg, db=db)
                    pages[f"{view}:{page}"] = [s[1:] for s in tg.sent]
            render["views"], render["pages"] = views, pages
            render["all"] = _all_renders(snapshot, now)
            sizes: dict = {}

            # ---- delivery: N subscribers, real per-user rendering, a fake network with a per-send delay
            tzs = ["Africa/Lagos", "Europe/London", "America/New_York", "Asia/Tokyo"]
            for i in range(subscribers):
                db.subscriber_add(str(1000 + i), f"s{i}", f"Sub{i}", timezone=tzs[i % len(tzs)])
            db.conn.execute("UPDATE subscribers SET subscribed_at='2026-09-01T00:00:00Z'")
            db.conn.commit()
            deliver_now = now.replace(hour=8, minute=31)          # 09:31 in Lagos on the card's day
            dtg = FakeTelegram(delay=send_delay)
            import predbot.bot.broadcast as broadcast
            if not send_delay:
                broadcast.SENDS_PER_SECOND = 1e9                  # tests: no pacing (the golden does not depend on it)
            db.stats.update(executes=0, commits=0)
            t0 = time.perf_counter()
            sent = deliver_picks(db, cfg, dtg, service.published_snapshot, now=deliver_now)
            deliver_wall = time.perf_counter() - t0
            again = deliver_picks(db, cfg, FakeTelegram(), service.published_snapshot, now=deliver_now)
            broadcast.SENDS_PER_SECOND = 20.0
            result["delivery"] = {"subscribers": subscribers, "sent": sent, "second_pass_sent": again,
                                  "wall": round(deliver_wall, 3), "sqlite": dict(db.stats)}
            by_chat: dict = {}
            for c, text, _ in dtg.sent:                            # a chat gets two messages: the Top card, then the Daily
                by_chat.setdefault(c, []).append(text)
            slate["delivery"] = {"sent": sent, "second_pass": again, "chats": sorted(by_chat)[:5],
                                 "n_deliveries": db.query("SELECT COUNT(DISTINCT chat_id) AS n FROM deliveries")[0]["n"]}
            from predbot.bot.telegram import _split
            cards = {tz: next(by_chat[str(1000 + i)] for i in range(subscribers) if tzs[i % len(tzs)] == tz) for tz in tzs}    # tz -> [top, daily]
            render["delivery"] = {"cards_by_tz": cards, "message_lengths": {tz: [[len(x) for x in _split(m)] for m in c] for tz, c in cards.items()}}
            render.update(_results_renders(db, snapshot, service, now, cfg, tmp, callbacks))
            sizes = _sizes(render)
            result["slate"], result["render"], result["sizes"] = slate, render, sizes
            result["peak_rss_mb"] = peak_rss_mb()
    finally:
        try:
            src.close()
        except Exception:  # noqa: BLE001
            pass
        if not workdir:
            shutil.rmtree(tmp, ignore_errors=True)
    return result


# ---------------------------------------------------------------------------------------------- results cards
_OUTCOMES = ["won", "won", "lost", "won", "void", "lost", "won", "won", "lost", "won"]
_COLS = ("tier", "rank", "provider", "provider_event_id", "sport", "league", "home", "away", "start_utc", "market", "period",
         "stat", "entity", "line", "selection", "label", "price", "prob", "ev", "p_market", "sources", "provider_odd_id")


def _seed_history(db, rows, today: str, days=(1, 2, 3, 4)) -> None:
    """The recorded data has no results of its own: clone the day's slate onto the previous days, with a fixed
    pattern of outcomes, so the stats, recap and /yesterday cards render real labels and prices."""
    from datetime import date, timedelta
    from predbot.util.timeutil import iso, parse_iso
    base = date.fromisoformat(today)
    for back in days:
        for i, r in enumerate(rows):
            row = {k: r[k] for k in _COLS}
            row["pick_date"] = (base - timedelta(days=back)).isoformat()
            row["start_utc"] = iso(parse_iso(r["start_utc"]) - timedelta(days=back))
            db.settle_pick(db.insert_pick(row), _OUTCOMES[(i + back) % len(_OUTCOMES)])


def _all_renders(snapshot, now) -> dict:
    """/all as a reader sees it: the sport menu, then every page of every sport (fixtures grouped, never split)."""
    from predbot.bot import pagination
    from predbot.cache.models import SPORT_VIEW_PREFIX, sport_bucket
    menu_text, menu_markup = pagination.render_sport_menu(snapshot)
    out: dict = {"menu": menu_text, "menu_buttons": [b["text"] for row in menu_markup["inline_keyboard"] for b in row][:6], "sports": {}}
    for sport in sorted({sport_bucket(p.sport) for p in snapshot.all_predictions}):
        view, pages, page = SPORT_VIEW_PREFIX + sport, [], 0
        while True:
            body, markup = pagination.render_page(snapshot, view, page, pagination.PAGE_SIZE_DEFAULT, TZ, now=now)
            pages.append(body)
            if not any(b["text"] == "▶️" for row in markup["inline_keyboard"] for b in row):
                break
            page += 1
        out["sports"][sport] = pages
    return out


def _results_renders(db, snapshot, service, now, cfg, tmp, callbacks) -> dict:
    from predbot.bot import pagination
    from predbot.bot.format import recap_message, stats_message
    from predbot.cache.models import DailySnapshot
    from predbot.jobs import stats as stats_mod
    from predbot.storage.db import Database
    from predbot.util.timeutil import to_local

    rows = [dict(r) for r in db.picks_for_date(snapshot.date)]
    from datetime import date, timedelta
    yesterday = (date.fromisoformat(snapshot.date) - timedelta(days=1)).isoformat()
    local_now = to_local(now, TZ)
    out: dict = {}

    def cards(label):
        stats = stats_mod.compute(db, local_now)
        text, _ = pagination.render_stats(snapshot, db=db, tz=TZ, now=now)
        pages, page = [], 0
        while True:
            body, markup = pagination.render_yesterday(db, TZ, page, pagination.PAGE_SIZE_DEFAULT, day=yesterday, now=now)
            pages.append(body)
            if not any(b["text"] == "▶️" for row in markup["inline_keyboard"] for b in row):
                break
            page += 1
        out[f"stats_{label}"], out[f"recap_{label}"] = text, recap_message(stats)
        out[f"yesterday_{label}"] = pages

    _seed_history(db, rows, snapshot.date)
    cards("final")                                             # every pick of yesterday settled
    db.execute("UPDATE picks SET status='pending', settled_at=NULL WHERE pick_date=? AND (sport='baseball' OR id % 4 = 0)", (yesterday,))
    cards("partial")                                           # some settled, one sport with nothing settled yet

    # button routing for the new view: a switch, then a page turn on yesterday's audit trail
    tg = FakeTelegram()
    db.subscriber_add("100", "u", "User", timezone=TZ)
    for data in ("pg|stats|0", "pg|yesterday|0", f"pg|yesterday|1|{yesterday.replace('-', '')}"):
        tg.sent.clear()
        callbacks.handle_callback({"id": "cb", "data": data, "message": {"chat": {"id": 100}, "message_id": 7}, "from": {}},
                                  service=service, tg=tg, cfg=cfg, db=db)
        out[f"callback:{data.split('|')[1]}:{data.split('|')[2]}"] = [x[1:] for x in tg.sent]

    # empty states
    def fresh(name):
        return Database(os.path.join(tmp, name))

    empty = fresh("empty.db")
    out["stats_no_results_yet"] = pagination.render_stats(snapshot, db=empty, tz=TZ, now=now)[0]
    out["yesterday_no_picks"] = pagination.render_yesterday(empty, TZ, 0, 5, day=yesterday, now=now)[0]
    only_pending = fresh("pending.db")
    _seed_history(only_pending, rows, snapshot.date, days=(1, 3))
    only_pending.execute("UPDATE picks SET status='pending', settled_at=NULL WHERE pick_date=?", (yesterday,))
    out["stats_nothing_settled_yesterday"] = pagination.render_stats(snapshot, db=only_pending, tz=TZ, now=now)[0]
    history_only = fresh("history.db")
    _seed_history(history_only, rows, snapshot.date, days=(2, 3))
    out["stats_no_picks_yesterday"] = pagination.render_stats(snapshot, db=history_only, tz=TZ, now=now)[0]
    fewer = fresh("fewer.db")
    _seed_history(fewer, rows, snapshot.date, days=(1, 2))
    out["stats_fewer_than_7_days"] = pagination.render_stats(snapshot, db=fewer, tz=TZ, now=now)[0]

    def picks_view(view, snap):
        return pagination.render_page(snap, view, 0, 5, TZ, now=now)[0]

    out["empty_states"] = {
        "before_post_time": picks_view("top", DailySnapshot(date=snapshot.date, generation={"reason": "before_post_time", "ready_at": "09:00 WAT"})),
        "generating": picks_view("daily", DailySnapshot(date=snapshot.date, generation={"reason": "generating"})),
        "assembling": picks_view("daily", DailySnapshot(date=snapshot.date, status="PARTIAL")),
        "top_empty": picks_view("top", DailySnapshot(date=snapshot.date, status="COMPLETE", daily_picks=snapshot.daily_picks)),
        "daily_empty": picks_view("daily", DailySnapshot(date=snapshot.date, status="COMPLETE", top_picks=snapshot.top_picks)),
    }
    return out


def _sizes(render: dict):
    """(length, where) of every rendered text, longest first - never part of the golden."""
    found = []

    def walk(obj, path):
        if isinstance(obj, str):
            found.append((len(obj), path))
        elif isinstance(obj, dict):
            for k, v in obj.items():
                walk(v, f"{path}/{k}")
        elif isinstance(obj, (list, tuple)):
            for i, v in enumerate(obj):
                walk(v, f"{path}[{i}]")

    walk(render, "")
    return sorted(found, reverse=True)[:8]


def golden_text(result: dict, part: str = "slate") -> str:
    """``slate`` = which picks, their order, prices, labels, diagnostics; ``render`` = every text a reader sees."""
    return json.dumps(result[part], indent=1, sort_keys=True, ensure_ascii=False, default=str)


def main(argv):
    result = run(profile="--profile" in argv)
    if "--record" in argv:
        nxt = argv[argv.index("--record") + 1:][:1]
        which = nxt[0] if nxt and nxt[0] in ("slate", "render") else "both"
        for part, path in (("slate", GOLDEN_SLATE), ("render", GOLDEN_RENDER)):
            if which in (part, "both"):
                path.write_text(golden_text(result, part) + "\n", encoding="utf-8")
                print("golden written:", path, path.stat().st_size, "bytes")
    if "--json" in argv:
        Path(argv[argv.index("--json") + 1]).write_text(json.dumps(result, indent=1, default=str), encoding="utf-8")
    summary = {k: v for k, v in result.items() if k not in ("slate", "render", "profile")}
    print(json.dumps(summary, indent=1, default=str))
    if result.get("profile"):
        print(result["profile"])


if __name__ == "__main__":
    main(sys.argv[1:])
