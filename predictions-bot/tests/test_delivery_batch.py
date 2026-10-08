"""H12/H15: delivery renders once per zone, sends through a small paced pool, records each accepted send at once,
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
    monkeypatch.setattr(broadcast, "picks_messages", lambda s, tz, now=None: calls.append(tz) or [
        ("picks_top", f"top for {tz}", None), ("picks_daily", f"daily for {tz}", None)])
    snap.render_calls = calls
    return snap


def _subs(db, n, joined="2026-09-01T00:00:00Z"):
    zones = ["Africa/Lagos", "Europe/London", "Asia/Tokyo"]
    for i in range(n):
        db.subscriber_add(str(100 + i), f"u{i}", f"U{i}", timezone=zones[i % 3])
    db.conn.execute("UPDATE subscribers SET subscribed_at=?", (joined,))
    db.conn.commit()


NOW = datetime(2026, 9, 19, 8, 31, tzinfo=timezone.utc)      # 09:31 in Lagos


def test_a_large_delivery_renders_once_per_zone_sends_in_a_pool_and_records_each_send(db, snap, monkeypatch):
    monkeypatch.setattr(broadcast, "SENDS_PER_SECOND", 1e9)
    _subs(db, 60)
    tg = Tg()
    db.stats.update(executes=0, commits=0)
    sent = deliver_picks(db, Config(), tg, snap, now=NOW)
    assert sent == 60 and sorted(set(tg.sent)) == sorted(str(100 + i) for i in range(60)) and len(tg.sent) == 120   # two messages each
    assert sorted(snap.render_calls) == ["Africa/Lagos", "Asia/Tokyo", "Europe/London"]      # 3 renders, not 60
    assert len(tg.threads) > 1                                                                # a pool, not one thread
    # H5: each receipt is committed as its message is accepted (a crash mid-run can no longer resend a whole batch)
    assert db.stats["commits"] == 120
    assert len(db.deliveries_done("picks_top", "2026-09-19")) == len(db.deliveries_done("picks_daily", "2026-09-19")) == 60
    assert deliver_picks(db, Config(), Tg(), snap, now=NOW) == 0                              # once per subscriber per day


def test_failed_sends_are_not_recorded_and_are_retried_on_the_next_poll(db, snap, monkeypatch):
    monkeypatch.setattr(broadcast, "SENDS_PER_SECOND", 1e9)
    _subs(db, 20)
    assert deliver_picks(db, Config(), Tg(fail={"105", "110"}), snap, now=NOW) == 18
    assert db.deliveries_done("picks_top", "2026-09-19") == {str(100 + i) for i in range(20)} - {"105", "110"}
    retry = Tg()
    assert deliver_picks(db, Config(), retry, snap, now=NOW) == 2 and sorted(retry.sent) == ["105", "105", "110", "110"]


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
    assert time.perf_counter() - t0 >= 0.5                     # 30 chats x 2 messages at <= 100/s cannot finish faster


# ------------------------------------------------------------------ H14: admin actions do not block the poll loop
def test_a_long_admin_action_does_not_block_other_commands(db):
    import time
    from predbot.bot.commands import CommandLoop
    sent = []

    class TG:
        token = "t"

        def send(self, text, chat_id=None, **kw):
            sent.append((time.perf_counter(), text))
            return True

    def slow():
        time.sleep(0.5)
        return "generation done"

    loop = CommandLoop(TG(), db, Config(telegram_admin_ids=["111"]), {"/run": slow})
    mk = lambda uid, text: {"update_id": uid, "message": {"text": text, "chat": {"id": 1}, "from": {"id": 111}}}
    t0 = time.perf_counter()
    loop._handle_update(mk(1, "/run"))
    loop._handle_update(mk(2, "/help"))
    assert time.perf_counter() - t0 < 0.3                       # neither call waited for the generation
    assert any("Commands" in text for _, text in sent)          # /help was answered while /run is still going
    assert not any(text == "generation done" for _, text in sent)
    for w in loop._admin_threads:
        w.join(2)
    assert sent[-1][1] == "generation done"                      # the same reply, sent when the action finished
