"""Unit tests for the daily cache, unified fetcher, pagination and callbacks.

All external calls are faked (FakeChain / FakeTG): the suite proves the
write-once-per-day guarantees - cache hits, single API batch under concurrency,
zero-API DB rebuilds and zero-API page navigation - without network access.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from predbot.bot import pagination
from predbot.bot.callbacks import handle_callback
from predbot.cache.fetcher import UnifiedFetcher
from predbot.cache.manager import CacheManager, ttl_until_end_of_utc_day
from predbot.cache.models import (DailySnapshot, PickRecord, StatsSummary,
                                  VIEW_DAILY, VIEW_TOP)
from predbot.config import Config


class FakeChain:
    """Stands in for ProviderChain; counts how often the pipeline would run."""

    def __init__(self, delay: float = 0.0):
        self.calls = 0
        self.delay = delay
        self.order = ["fake"]

    def collect(self, start, end):
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        return []


class FakeTG:
    """Records Telegram calls instead of hitting the Bot API."""

    def __init__(self):
        self.edits = []
        self.answers = []

    def edit_message_text(self, chat_id, message_id, text, reply_markup=None,
                          parse_mode="HTML"):
        self.edits.append({"chat_id": chat_id, "message_id": message_id,
                           "text": text, "reply_markup": reply_markup})
        return True

    def answer_callback_query(self, callback_query_id, text=None, show_alert=False):
        self.answers.append({"id": callback_query_id, "text": text})
        return True


def make_pick(i: int, tier: str = "daily") -> PickRecord:
    return PickRecord(pick_id=i, tier=tier, rank=i, sport="football", league="EPL",
                      home=f"Home{i}", away=f"Away{i}",
                      kickoff_utc="2030-06-01T18:00:00Z", label=f"Over {i}.5 goals",
                      market="total", selection="over", line=i + 0.5, price=1.9 + i / 20,
                      prob=0.6, ev=0.05, p_market=0.55, bookmaker="bet", n_books=2,
                      sources=["market", "score_model"], status="pending",
                      pick_date="2030-06-01")


def make_snapshot(origin: str = "db") -> DailySnapshot:
    return DailySnapshot(date="2030-06-01", generated_at="2030-06-01T09:00:00Z",
                         origin=origin, top_picks=[make_pick(1, "top")],
                         daily_picks=[make_pick(i) for i in range(2, 25)],
                         stats=StatsSummary.from_raw({"last7": {"won": 3, "lost": 1, "roi": 12.5},
                                                      "streak": "W2", "brier": 0.2}))


# ------------------------------------------------------------------ cache manager
def test_daily_key_is_utc_deterministic():
    cache = CacheManager()
    day = datetime(2030, 6, 1, 23, 59, tzinfo=timezone.utc).date()
    assert cache.daily_key(day) == "bot:predictions:daily:2030-06-01"
    assert cache.lock_key(day) == "bot:predictions:lock:2030-06-01"


def test_ttl_ends_at_utc_midnight():
    now = datetime(2030, 6, 1, 23, 0, 0, tzinfo=timezone.utc)
    assert ttl_until_end_of_utc_day(now) == 3600


def test_set_get_roundtrip_and_expiry():
    cache = CacheManager()
    cache.set("k", {"a": 1}, ttl_seconds=60)
    assert cache.get("k") == {"a": 1}
    cache._backend.set("k2", '{"b": 2}', ttl_seconds=1)
    time.sleep(1.1)
    assert cache.get("k2") is None            # expired at its TTL


def test_lock_is_mutually_exclusive_and_releasable():
    cache = CacheManager()
    lock_key = cache.lock_key()
    with cache.lock(lock_key, wait_seconds=0.2) as first:
        assert first is True
        with cache.lock(lock_key, wait_seconds=0.1) as second:
            assert second is False            # held by the first holder
    with cache.lock(lock_key, wait_seconds=0.2) as third:
        assert third is True                  # released again


# --------------------------------------------------------------------- snapshot
def test_snapshot_json_roundtrip():
    snap = make_snapshot()
    restored = DailySnapshot.from_dict(snap.to_dict())
    assert restored.date == snap.date
    assert restored.origin == snap.origin
    assert restored.total_picks() == snap.total_picks() == 24
    assert restored.stats.last7.won == 3
    assert restored.stats.last7.roi == pytest.approx(12.5)
    assert restored.daily_picks[0].label == "Over 2.5 goals"
    assert restored.view_items(VIEW_TOP)[0].tier == "top"


def test_snapshot_rejects_unknown_schema_version():
    bad = make_snapshot().to_dict()
    bad["schema_version"] = 99
    with pytest.raises(ValueError):
        DailySnapshot.from_dict(bad)


# ----------------------------------------------------------------- fetcher flow
def _fetcher(db, chain=None, cache=None) -> UnifiedFetcher:
    return UnifiedFetcher(chain or FakeChain(), db, Config(),
                          cache or CacheManager())


def _insert_picks(db, date_str: str, count: int = 3):
    for i in range(1, count + 1):
        db.insert_pick({
            "pick_date": date_str, "tier": "top" if i == 1 else "daily", "rank": i,
            "provider": "fake", "provider_event_id": f"ev{i}", "sport": "football",
            "league": "EPL", "home": f"Home{i}", "away": f"Away{i}",
            "start_utc": "2030-06-01T18:00:00Z", "market": "total", "period": "full",
            "stat": "points", "entity": "all", "line": 2.5, "selection": "over",
            "label": f"Over 2.5 goals #{i}", "price": 1.9, "prob": 0.6, "ev": 0.05,
            "p_market": 0.55, "sources": {"market": 0.55}, "provider_odd_id": "o1",
        })


def test_db_rebuild_is_zero_api_and_populates_cache(db):
    chain = FakeChain()
    cache = CacheManager()
    fetcher = _fetcher(db, chain, cache)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    _insert_picks(db, today)

    snap = fetcher.get_daily_snapshot()

    assert snap.origin == "db"
    assert chain.calls == 0                       # no external call at all
    assert snap.total_picks() == 3
    assert snap.top_picks[0].pick_id is not None
    assert cache.get(cache.daily_key()) is not None  # written with daily TTL

    again = fetcher.get_daily_snapshot()
    # origin is provenance ("db": built from the picks table); the second read
    # is served from the cache entry - proven by zero further chain calls.
    assert again.origin == "db" and again.total_picks() == 3
    assert chain.calls == 0


def test_double_check_returns_cached_snapshot(db):
    chain = FakeChain()
    cache = CacheManager()
    fetcher = _fetcher(db, chain, cache)
    cache.set(cache.daily_key(), make_snapshot(origin="cache").to_dict(), ttl_seconds=60)

    snap = fetcher.get_daily_snapshot()

    assert snap.origin == "cache"
    assert chain.calls == 0


def test_force_refresh_bypasses_cache(db):
    chain = FakeChain()
    cache = CacheManager()
    fetcher = _fetcher(db, chain, cache)
    cache.set(cache.daily_key(), make_snapshot().to_dict(), ttl_seconds=60)

    snap = fetcher.get_daily_snapshot(force_refresh=True)

    assert snap.origin == "api"                   # rebuilt through the pipeline
    assert chain.calls == 1


def test_concurrent_readers_dispatch_single_api_batch(db):
    chain = FakeChain(delay=0.4)
    cache = CacheManager()
    fetcher = _fetcher(db, chain, cache)
    results = []

    def worker():
        results.append(fetcher.get_daily_snapshot())

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for t in threads:
        t.start()
        time.sleep(0.05)
    for t in threads:
        t.join(timeout=30)

    assert chain.calls == 1                       # one API batch, three readers
    assert len(results) == 3
    assert all(r.date == results[0].date for r in results)


# ------------------------------------------------------------------- pagination
def test_pagination_slicing_and_clamping():
    items = [make_pick(i) for i in range(1, 24)]  # 23 items
    assert pagination.total_pages(len(items), 5) == 5
    assert len(pagination.slice_page(items, 0, 5)) == 5
    assert len(pagination.slice_page(items, 4, 5)) == 3
    assert pagination.slice_page(items, 99, 5)[0] == items[20]  # clamped to last page
    assert pagination.clamp_page(-3, len(items), 5) == 0


def test_callback_payload_codec():
    assert pagination.encode_callback("top", 2) == "pg|top|2"
    assert pagination.decode_callback("pg|daily|0") == (VIEW_DAILY, 0)
    assert pagination.decode_callback("pg|nope|0") is None
    assert pagination.decode_callback("pg|top|x") is None
    assert pagination.decode_callback(None) is None
    assert pagination.decode_callback("pg|top|2|extra") is None


def test_render_page_structure_and_keyboard():
    snap = make_snapshot()
    text, markup = pagination.render_page(snap, VIEW_DAILY, 0, 10, "UTC")
    assert "Daily picks for today" in text and "Page 1/3" in text
    nav = markup["inline_keyboard"][0]
    assert nav[0]["text"] == "1/3"                # no ◀️ button on the first page
    assert nav[1]["callback_data"] == "pg|daily|1"
    assert "◀️" not in [b["text"] for b in nav]
    assert len(markup["inline_keyboard"][-1]) == 3  # switch row: top/daily/stats

    text_last, markup_last = pagination.render_page(snap, VIEW_DAILY, 2, 10, "UTC")
    assert "Page 3/3" in text_last
    buttons = [b["text"] for b in markup_last["inline_keyboard"][0]]
    assert "◀️" in buttons and "▶️" not in buttons


def test_render_stats_shows_the_sureodds_yesterday_card():
    text, markup = pagination.render_stats(make_snapshot())
    assert "SUREODDS — YESTERDAY" in text and "LAST 7 DAYS" in text
    assert markup["inline_keyboard"]


# ------------------------------------------------------------- callback handler
def _cb(data: str, page_message_id: int = 77) -> dict:
    return {"id": "cb1", "data": data,
            "message": {"message_id": page_message_id, "chat": {"id": -100123}}}


def test_callback_navigation_is_zero_api(db):
    chain = FakeChain()
    cache = CacheManager()
    fetcher = _fetcher(db, chain, cache)
    cache.set(cache.daily_key(), make_snapshot(origin="cache").to_dict(), ttl_seconds=60)
    tg = FakeTG()

    handle_callback(_cb("pg|daily|1"), service=fetcher, tg=tg,
                    cfg=Config(), page_size=10)

    assert chain.calls == 0                                  # zero external calls
    assert len(tg.edits) == 1
    edit = tg.edits[0]
    assert edit["chat_id"] == "-100123" and edit["message_id"] == 77
    assert "Daily picks for today" in edit["text"] and "Page 2/3" in edit["text"]
    assert edit["reply_markup"]["inline_keyboard"]
    assert tg.answers and tg.answers[0]["id"] == "cb1"


def test_callback_unknown_payload_is_acked_silently(db):
    fetcher = _fetcher(db, FakeChain())
    tg = FakeTG()
    handle_callback(_cb("nonsense"), service=fetcher, tg=tg, cfg=Config())
    assert tg.edits == []
    assert tg.answers == [{"id": "cb1", "text": None}]



