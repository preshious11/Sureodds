"""H12/H15: delivery renders once per zone, sends through a small paced pool, records in batched transactions,
and keeps "once per subscriber per day" and "joined before the target" exactly."""
from __future__ import annotations

import threading
from datetime import datetime, timezone

import pytest

from predbot.bot import broadcast
from predbot.bot.broadcast import deliver_picks
from predbot.cache.models import DailySnapshot, PickRecord
from predbot.config import Config


class Tg:
    token = "t"

    def __init__(self, fail=()):
        self.fail, self.sent, self.threads, self._l = set(fail), [], set(), threading.Lock()

    def send(self, text, chat_id=None, **kw):
        with self._l:
            self.threads.add(threading.current_thread().name)
            if str(chat_id) in self.fail:
                return None
            self.sent.append(str(chat_id))
        return "1"


@pytest.fixture
def snap(monkeypatch):
    snap = DailySnapshot(date="2026-09-19", generated_at="2026-09-19T08:00:00Z", top_picks=[object()], daily_picks=[])
    calls = []
    monkeypatch.setattr(broadcast, "picks_text", lambda s, tz, now=None: calls.append(tz) or f"card for {tz}")
    snap.render_calls = calls
    return snap


def _subs(db, n, joined="2026-09-01T00:00:00Z"):
    zones = ["Africa/Lagos", "Europe/London", "Asia/Tokyo"]
    for i in range(n):
        db.subscriber_add(str(100 + i), f"u{i}", f"U{i}", timezone=zones[i % 3])
    db.conn.execute("UPDATE subscribers SET subscribed_at=?", (joined,))
    db.conn.commit()


NOW = datetime(2026, 9, 19, 8, 31, tzinfo=timezone.utc)      # 09:31 in Lagos


def test_a_large_delivery_renders_once_per_zone_sends_in_a_pool_and_records_in_batches(db, snap, monkeypatch):
    monkeypatch.setattr(broadcast, "SENDS_PER_SECOND", 1e9)
    _subs(db, 60)
    tg = Tg()
    db.stats.update(executes=0, commits=0)
    sent = deliver_picks(db, Config(), tg, snap, now=NOW)
    assert sent == 60 and sorted(tg.sent) == sorted(str(100 + i) for i in range(60))
    assert sorted(snap.render_calls) == ["Africa/Lagos", "Asia/Tokyo", "Europe/London"]      # 3 renders, not 60
    assert len(tg.threads) > 1                                                                # a pool, not one thread
    assert db.stats["commits"] <= 60 // broadcast.RECORD_BATCH + 2                            # batched, not one per send
    assert len(db.deliveries_done("picks", "2026-09-19")) == 60
    assert deliver_picks(db, Config(), Tg(), snap, now=NOW) == 0                              # once per subscriber per day


def test_failed_sends_are_not_recorded_and_are_retried_on_the_next_poll(db, snap, monkeypatch):
    monkeypatch.setattr(broadcast, "SENDS_PER_SECOND", 1e9)
    _subs(db, 20)
    assert deliver_picks(db, Config(), Tg(fail={"105", "110"}), snap, now=NOW) == 18
    assert db.deliveries_done("picks", "2026-09-19") == {str(100 + i) for i in range(20)} - {"105", "110"}
    retry = Tg()
    assert deliver_picks(db, Config(), retry, snap, now=NOW) == 2 and sorted(retry.sent) == ["105", "110"]


def test_subscribers_who_joined_after_the_target_and_early_polls_are_skipped(db, snap, monkeypatch):
    monkeypatch.setattr(broadcast, "SENDS_PER_SECOND", 1e9)
    _subs(db, 12)
    db.conn.execute("UPDATE subscribers SET subscribed_at='2026-09-19T08:45:00Z' WHERE chat_id IN ('100','101')")
    db.conn.commit()
    tg = Tg()
    assert deliver_picks(db, Config(), tg, snap, now=NOW) == 10 and "100" not in tg.sent and "101" not in tg.sent
    assert deliver_picks(db, Config(), Tg(), snap, now=datetime(2026, 9, 19, 8, 0, tzinfo=timezone.utc)) == 0   # 09:00 Lagos: too early


def test_submissions_are_paced(db, snap, monkeypatch):
    import time
    monkeypatch.setattr(broadcast, "SENDS_PER_SECOND", 100.0)
    _subs(db, 30)
    t0 = time.perf_counter()
    deliver_picks(db, Config(), Tg(), snap, now=NOW)
    assert time.perf_counter() - t0 >= 0.25                    # 30 sends at <= 100/s cannot finish faster
