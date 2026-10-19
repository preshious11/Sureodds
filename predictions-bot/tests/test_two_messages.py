"""The 09:30 broadcast is TWO messages - the Top card, then the Daily card - each with the four-button keyboard and
each recorded on its own, for subscribers and for the channel post."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from predbot.bot import broadcast
from predbot.bot.format import no_selection_message
from predbot.cache.models import DailySnapshot
from predbot.config import Config
from predbot.jobs import daily_picks
from tests.test_subscribers_broadcast import FakeTG, _snapshot

NOW = datetime(2030, 6, 1, 9, 30, tzinfo=timezone.utc)
CFG = Config(timezone="UTC")
LABELS = ["📚 All Predictions", "🔥 Bet of the Day", "📋 Daily Picks", "📊 Stats", "🗓 Yesterday"]


@pytest.fixture(autouse=True)
def _joined_early(monkeypatch):
    import predbot.storage.db as db_mod
    monkeypatch.setattr(db_mod, "_now", lambda: "2030-01-01T00:00:00Z")


class FailSecond(FakeTG):
    """Fails the Daily message (the second send to a chat) for the chats in ``fail``."""

    def __init__(self, fail=()):
        super().__init__()
        self.fail, self.count = set(fail), {}

    def send(self, text, chat_id=None, reply_markup=None, parse_mode="HTML"):
        n = self.count[chat_id] = self.count.get(chat_id, 0) + 1
        if chat_id in self.fail and n == 2:
            return None
        return super().send(text, chat_id=chat_id, reply_markup=reply_markup)


def test_two_messages_per_subscriber_in_order_each_with_the_four_button_keyboard(db):
    db.subscriber_add("42")
    tg = FakeTG()
    assert broadcast.deliver_picks(db, CFG, tg, _snapshot(), now=NOW) == 1
    assert len(tg.sends) == 2
    top, daily = tg.sends
    assert top["text"].startswith("🔥 BET OF THE DAY") and "DAILY PICKS" not in top["text"]
    assert daily["text"].startswith("📋 DAILY PICKS") and "BET OF THE DAY" not in daily["text"]
    for send in tg.sends:
        assert [b["text"] for row in send["reply_markup"]["inline_keyboard"] for b in row] == LABELS
    assert db.deliveries_done("picks_top", "2030-06-01") == db.deliveries_done("picks_daily", "2030-06-01") == {"42"}


def test_a_failed_daily_is_retried_alone(db):
    db.subscriber_add("42")
    db.subscriber_add("77")
    tg = FailSecond(fail={"42"})
    assert broadcast.deliver_picks(db, CFG, tg, _snapshot(), now=NOW) == 2       # both chats got at least one message
    assert db.deliveries_done("picks_top", "2030-06-01") == {"42", "77"}
    assert db.deliveries_done("picks_daily", "2030-06-01") == {"77"}
    retry = FakeTG()
    assert broadcast.deliver_picks(db, CFG, retry, _snapshot(), now=NOW) == 1
    assert [(s["chat_id"], s["text"][:1]) for s in retry.sends] == [("42", "📋")]   # the Daily only, to the chat that missed it
    assert broadcast.deliver_picks(db, CFG, FakeTG(), _snapshot(), now=NOW) == 0


def test_a_failed_top_holds_back_the_daily(db):
    db.subscriber_add("42")

    class FailFirst(FakeTG):
        def send(self, *a, **k):
            return None

    tg = FailFirst()
    assert broadcast.deliver_picks(db, CFG, tg, _snapshot(), now=NOW) == 0
    assert tg.sends == [] and db.deliveries_done("picks_daily", "2030-06-01") == set()


def test_an_empty_top_says_so_and_the_daily_still_goes_out(db):
    db.subscriber_add("42")
    snap = _snapshot()
    snap.top_picks = []
    tg = FakeTG()
    broadcast.deliver_picks(db, CFG, tg, snap, now=NOW)
    assert len(tg.sends) == 2
    assert tg.sends[0]["text"] == "🔥 BET OF THE DAY · Sat 1 Jun\nNo Bet of the Day for today.\n\nSee other picks:\n\nBet only what you can afford to lose."
    assert tg.sends[1]["text"].startswith("📋 DAILY PICKS") and "3 picks" in tg.sends[1]["text"]


def test_an_empty_daily_says_so_and_the_top_still_goes_out(db):
    db.subscriber_add("42")
    snap = _snapshot()
    snap.daily_picks = []
    tg = FakeTG()
    broadcast.deliver_picks(db, CFG, tg, snap, now=NOW)
    assert tg.sends[0]["text"].startswith("🔥 BET OF THE DAY") and "1 pick" in tg.sends[0]["text"]
    assert tg.sends[1]["text"] == f"📋 DAILY PICKS · Sat 1 Jun\n{no_selection_message()}\n\nBet only what you can afford to lose."


def test_nothing_at_all_sends_nothing(db):
    db.subscriber_add("42")
    tg = FakeTG()
    assert broadcast.deliver_picks(db, CFG, tg, DailySnapshot(date="2030-06-01"), now=NOW) == 0
    assert tg.sends == []


def test_the_pace_cap_counts_both_messages(db, monkeypatch):
    import time
    monkeypatch.setattr(broadcast, "SENDS_PER_SECOND", 50.0)
    for i in range(12):
        db.subscriber_add(str(100 + i))
    t0 = time.perf_counter()
    tg = FakeTG()
    broadcast.deliver_picks(db, CFG, tg, _snapshot(), now=NOW)
    assert len(tg.sends) == 24 and time.perf_counter() - t0 >= 0.3             # 24 messages at <= 50/s


# ------------------------------------------------------------------------------------------------ channel post
def _channel(db, monkeypatch, tg, force=False):
    class Service:
        def get_daily_snapshot(self, **kw):
            return _snapshot()
    daily_picks.run(None, db, CFG, tg, force=force, service=Service())


def _channel_tg(fail_daily=False):
    tg = Mock()
    tg.chat_id = "-1001234567890"
    tg.enabled.return_value = True
    tg.send.side_effect = lambda text, **kw: None if (fail_daily and text.startswith("📋")) else "mid"
    return tg


def test_the_channel_post_is_two_static_messages_recorded_separately(db, monkeypatch):
    tg = _channel_tg()
    _channel(db, monkeypatch, tg)
    texts = [c.args[0][:1] for c in tg.send.call_args_list]
    assert texts == ["🔥", "📋"]
    assert all(not c.kwargs.get("reply_markup") for c in tg.send.call_args_list)
    assert db.post_done("picks_top", "2030-06-01") and db.post_done("picks_daily", "2030-06-01")
    assert db.delivery_done("community_picks_top", "2030-06-01", tg.chat_id)
    assert db.delivery_done("community_picks_daily", "2030-06-01", tg.chat_id)
    _channel(db, monkeypatch, tg)
    assert tg.send.call_count == 2                                           # already posted: nothing more


def test_a_channel_retry_posts_only_the_missing_message(db, monkeypatch):
    _channel(db, monkeypatch, _channel_tg(fail_daily=True))
    assert db.post_done("picks_top", "2030-06-01") and not db.post_done("picks_daily", "2030-06-01")
    assert db.delivery_done("community_picks_top", "2030-06-01", "-1001234567890")
    assert not db.delivery_done("community_picks_daily", "2030-06-01", "-1001234567890")
    retry = _channel_tg()
    _channel(db, monkeypatch, retry)
    assert [c.args[0][:1] for c in retry.send.call_args_list] == ["📋"]


# ------------------------------------------------------------------------------------------------ migration
def test_old_picks_rows_count_as_both_messages_so_nothing_is_resent(tmp_path):
    from predbot.storage.db import Database
    path = str(tmp_path / "old.db")
    old = Database(path)
    old.execute("INSERT INTO deliveries(kind,post_date,chat_id,created_at) VALUES('picks','2030-06-01','42','x')")
    old.execute("INSERT INTO posts(kind,post_date,message_id,created_at) VALUES('picks','2030-06-01','9','x')")
    old.close()
    db = Database(path)                                   # the next start migrates
    assert db.deliveries_done("picks_top", "2030-06-01") == db.deliveries_done("picks_daily", "2030-06-01") == {"42"}
    assert db.post_done("picks_top", "2030-06-01") and db.post_done("picks_daily", "2030-06-01")
    assert db.deliveries_exist("picks", "2030-06-01")
    db.subscriber_add("42")
    tg = FakeTG()
    assert broadcast.deliver_picks(db, CFG, tg, _snapshot(), now=NOW) == 0 and tg.sends == []
    Database(path).close()                                # migrating again changes nothing
    assert len(db.query("SELECT * FROM deliveries")) == 3


def test_deliveries_exist_sees_either_new_kind(db):
    db.mark_delivery("picks_daily", "2030-06-01", "42")
    assert db.deliveries_exist("picks", "2030-06-01") and not db.deliveries_exist("picks", "2030-06-02")
