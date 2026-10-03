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
    # POST_TIME=00:00: these tests are about caching, not the pre-POST_TIME gate, so they must
    # behave the same whatever hour of the day the suite happens to run.
    return UnifiedFetcher(chain or FakeChain(), db, Config(post_time="00:00"))


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


def test_double_check_returns_cached_snapshot(db):
    chain = FakeChain()
    cache = None
    fetcher = _fetcher(db, chain, cache)
    _seed(fetcher, make_snapshot(origin="cache"))

    snap = fetcher.get_daily_snapshot()

    assert snap.origin == "cache"
    assert chain.calls == 0


def test_force_refresh_bypasses_cache(db):
    chain = FakeChain()
    cache = None
    fetcher = _fetcher(db, chain, cache)
    _seed(fetcher, make_snapshot())

    snap = fetcher.get_daily_snapshot(force_refresh=True)

    assert snap.origin == "sqlite"                   # rebuilt through the pipeline
    assert chain.calls == 1


def test_concurrent_readers_dispatch_single_api_batch(db):
    chain = FakeChain(delay=0.4)
    cache = None
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
    assert text.startswith("📋 DAILY PICKS · Sat 1 Jun") and "Page 1 of 3" in text and "23 picks" in text
    nav = markup["inline_keyboard"][0]
    assert nav[0]["text"] == "1/3"                # no ◀️ button on the first page
    assert nav[1]["callback_data"] == "pg|daily|1|20300601"      # a page turn carries the day of its card
    assert "◀️" not in [b["text"] for b in nav]
    assert [b["text"] for row in markup["inline_keyboard"][-2:] for b in row] == [
        "🔥 Top Picks", "📋 Daily Picks", "📊 Stats", "🗓 Yesterday"]      # two buttons a row

    text_last, markup_last = pagination.render_page(snap, VIEW_DAILY, 2, 10, "UTC")
    assert "Page 3 of 3" in text_last and "23 picks" in text_last       # the count is the whole ticket on every page
    buttons = [b["text"] for b in markup_last["inline_keyboard"][0]]
    assert "◀️" in buttons and "▶️" not in buttons


def test_render_stats_matches_the_stats_command(db):
    """📊 The /stats button and the /stats command must send the exact same card."""
    from predbot.bot.format import stats_message
    from predbot.jobs import stats as stats_mod
    from predbot.util.timeutil import to_local

    now = datetime(2030, 6, 1, 9, 30, tzinfo=timezone.utc)
    _insert_picks(db, "2030-05-31", 2)
    for row in db.picks_for_date("2030-05-31"):
        db.settle_pick(row["id"], "won")
    text, markup = pagination.render_stats(make_snapshot(), db=db, tz="UTC", now=now)

    # the Stats button/command now sends the collection performance card (headline over All Predictions)
    assert text == pagination.render_view(make_snapshot(), pagination.VIEW_STATS, 0, 10, "UTC", db=db, now=now)[0]
    assert text.startswith("<b>Performance</b>")
    assert "All Time\n2/2 · 100%" in text and "Last 7 Days\n2/2 · 100%" in text
    assert "Today\n0/0" in text
    assert [b["callback_data"] for row in markup["inline_keyboard"] for b in row] == \
        ["pg|stats_top|0", "pg|stats_daily|0", "pg|stats_all|0"]


def test_render_stats_without_db_falls_back_to_the_snapshot():
    text, markup = pagination.render_stats(make_snapshot())
    assert text.startswith("📊 STATS") and "📈 Last 7 days" in text
    assert "✅ 3  ❌ 1  ➖ 0" in text and "Yesterday" not in text                 # an older snapshot knows nothing about yesterday
    assert markup["inline_keyboard"]


def test_render_stats_reports_an_empty_snapshot_plainly():
    text, _ = pagination.render_stats(DailySnapshot(date="2030-06-01"))
    assert text == "📊 No results yet — the first recap arrives the morning after the first slate."


# ------------------------------------------------------------- callback handler
def _cb(data: str, page_message_id: int = 77) -> dict:
    return {"id": "cb1", "data": data,
            "message": {"message_id": page_message_id, "chat": {"id": -100123}}}


def test_callback_navigation_is_zero_api(db):
    chain = FakeChain()
    cache = None
    fetcher = _fetcher(db, chain, cache)
    _seed(fetcher, make_snapshot(origin="cache"))
    tg = FakeTG()

    handle_callback(_cb("pg|daily|1"), service=fetcher, tg=tg,
                    cfg=Config(), page_size=10)

    assert chain.calls == 0                                  # zero external calls
    assert len(tg.edits) == 1
    edit = tg.edits[0]
    assert edit["chat_id"] == "-100123" and edit["message_id"] == 77
    assert edit["text"].startswith("📋 DAILY PICKS · ") and "Page 2 of 3" in edit["text"]
    assert edit["reply_markup"]["inline_keyboard"]
    assert tg.answers and tg.answers[0]["id"] == "cb1"


def test_callback_unknown_payload_is_acked_silently(db):
    fetcher = _fetcher(db, FakeChain())
    tg = FakeTG()
    handle_callback(_cb("nonsense"), service=fetcher, tg=tg, cfg=Config())
    assert tg.edits == []
    assert tg.answers == [{"id": "cb1", "text": None}]





def _seed(service, snapshot):
    from predbot.util.timeutil import utcnow, to_local
    now = utcnow()
    snapshot.date = to_local(now, service.cfg.timezone).date().isoformat()
    service._store(snapshot, now)
