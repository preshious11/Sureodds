"""Tests for subscriber management, daily broadcasts and the /start menu."""
from __future__ import annotations

from predbot.bot import broadcast
from predbot.bot.callbacks import handle_callback, handle_start, menu_markup
from predbot.cache.fetcher import UnifiedFetcher
from predbot.cache.manager import CacheManager
from predbot.cache.models import DailySnapshot, PickRecord, StatsSummary
from predbot.config import Config


class FakeTG:
    token = "test-token"

    def __init__(self):
        self.sends = []
        self.edits = []
        self.answers = []

    def send(self, text, chat_id=None, reply_markup=None, parse_mode="HTML"):
        self.sends.append({"chat_id": chat_id, "text": text,
                           "reply_markup": reply_markup})
        return "123"

    def edit_message_text(self, chat_id, message_id, text, reply_markup=None,
                          parse_mode="HTML"):
        self.edits.append({"chat_id": chat_id, "message_id": message_id,
                           "text": text, "reply_markup": reply_markup})
        return True

    def answer_callback_query(self, callback_query_id, text=None, show_alert=False):
        self.answers.append({"id": callback_query_id, "text": text})
        return True


class FakeChain:
    def __init__(self):
        self.calls = 0
        self.order = ["fake"]

    def collect(self, start, end):
        self.calls += 1
        return []


def _snapshot() -> DailySnapshot:
    top = [PickRecord(pick_id=1, tier="top", rank=1, sport="football", home="A",
                      away="B", kickoff_utc="2030-06-01T18:00:00Z", label="Over 2.5",
                      price=1.9, prob=0.7, ev=0.1)]
    daily = [PickRecord(pick_id=i, tier="daily", rank=i, sport="football", home=f"H{i}",
                        away=f"A{i}", kickoff_utc="2030-06-01T20:00:00Z",
                        label=f"Under {i}.5", price=1.8, prob=0.6, ev=0.05)
             for i in range(1, 4)]
    return DailySnapshot(date="2030-06-01", generated_at="2030-06-01T09:00:00Z",
                         origin="db", top_picks=top, daily_picks=daily,
                         stats=StatsSummary.from_raw({"last7": {"won": 2, "lost": 1, "roi": 8.0},
                                                      "streak": "W1"}))


def _cb(data: str, message_id: int = 55, chat: str = "42") -> dict:
    return {"id": "cbx", "data": data,
            "message": {"message_id": message_id, "chat": {"id": chat}}}


# ----------------------------------------------------------------- subscribers
def test_start_registers_active_subscriber_and_sends_menu(db):
    tg = FakeTG()
    handle_start(db, tg, "42", {"username": "zed", "first_name": "Zed"})
    assert len(tg.sends) == 1
    assert tg.sends[0]["chat_id"] == "42"
    assert "Welcome" in tg.sends[0]["text"]
    rows = db.subscribers_active()
    assert len(rows) == 1 and rows[0]["chat_id"] == "42"
    assert rows[0]["username"] == "zed"


def test_subscriber_toggle_on_and_off(db):
    db.subscriber_add("42", "zed", "Zed")
    db.subscriber_remove("42")
    assert db.subscribers_active() == []
    db.subscriber_add("42")                     # re-subscribe keeps stored names
    rows = db.subscribers_active()
    assert len(rows) == 1 and rows[0]["username"] == "zed"


def test_menu_markup_reflects_subscription_state():
    on = menu_markup(subscribed=True)
    texts = [b["text"] for row in on["inline_keyboard"] for b in row]
    assert "🏆 Top Picks" in texts and "🔕 Unsubscribe from daily posts" in texts
    off = menu_markup(subscribed=False)
    texts_off = [b["text"] for row in off["inline_keyboard"] for b in row]
    assert "🔔 Subscribe to daily posts" in texts_off


# ------------------------------------------------------------------- broadcasts
def test_deliver_picks_reaches_all_subscribers_once(db):
    db.subscriber_add("42")
    db.subscriber_add("77")
    tg = FakeTG()

    sent = broadcast.deliver_picks(db, Config(), tg, _snapshot())

    assert sent == 2
    assert [s["chat_id"] for s in tg.sends] == ["42", "77"]
    card = tg.sends[0]["text"]
    assert card.startswith("📋 Top Picks for Today")
    assert card.count("Confidence:") == 4                # 1 top + 3 daily picks
    assert "Combined Odds:" in card

    # same day again (e.g. a restart): deliveries are de-duplicated per user
    assert broadcast.deliver_picks(db, Config(), tg, _snapshot()) == 0
    assert len(tg.sends) == 2


def test_deliver_picks_skips_empty_snapshot(db):
    db.subscriber_add("42")
    empty = DailySnapshot(date="2030-06-01", origin="db")
    assert broadcast.deliver_picks(db, Config(), FakeTG(), empty) == 0


def test_deliver_recap_dedupes_per_day(db):
    db.subscriber_add("42")
    tg = FakeTG()
    assert broadcast.deliver_recap(db, Config(), tg, "📊 RESULTS · Sun 01 Jun") == 1
    assert broadcast.deliver_recap(db, Config(), tg, "📊 RESULTS · Sun 01 Jun") == 0
    assert len(tg.sends) == 1


# ---------------------------------------------------------- /start menu routing
def test_callback_sub_toggle_updates_db_and_menu(db):
    cache = CacheManager()
    fetcher = UnifiedFetcher(FakeChain(), db, Config(), cache)
    db.subscriber_add("42", "zed", "Zed")
    tg = FakeTG()

    handle_callback(_cb("sub|off"), service=fetcher, tg=tg, db=db, cfg=Config())
    assert db.subscribers_active() == []
    assert "Unsubscribe" not in tg.edits[-1]["text"]       # menu re-rendered
    assert any(b["callback_data"] == "sub|on"
               for row in tg.edits[-1]["reply_markup"]["inline_keyboard"] for b in row)

    handle_callback(_cb("sub|on"), service=fetcher, tg=tg, db=db, cfg=Config())
    assert len(db.subscribers_active()) == 1
    assert tg.answers[-1]["text"] == "Subscribed ✅"


def test_new_subscriber_gets_todays_picks_via_sub_on(db):
    today = datetime_now_utc()
    db.insert_pick({
        "pick_date": today, "tier": "top", "rank": 1, "provider": "fake",
        "provider_event_id": "e1", "sport": "football", "league": "EPL",
        "home": "A", "away": "B", "start_utc": "2030-06-01T18:00:00Z",
        "market": "total", "period": "full", "stat": "points", "entity": "all",
        "line": 2.5, "selection": "over", "label": "Over 2.5 goals", "price": 1.9,
        "prob": 0.7, "ev": 0.1, "p_market": 0.55, "sources": {"market": 0.55},
    })
    fetcher = UnifiedFetcher(FakeChain(), db, Config(), CacheManager())
    tg = FakeTG()

    handle_callback(_cb("sub|on"), service=fetcher, tg=tg, db=db, cfg=Config())

    picks_sends = [s for s in tg.sends if "Top Picks for Today" in s["text"]]
    assert len(picks_sends) == 1            # zero-API catch-up from the DB


def test_page_callbacks_still_work_after_router_change(db):
    cache = CacheManager()
    cache.set(cache.daily_key(), _snapshot().to_dict(), ttl_seconds=60)
    fetcher = UnifiedFetcher(FakeChain(), db, Config(), cache)
    tg = FakeTG()

    handle_callback(_cb("pg|top|0"), service=fetcher, tg=tg, db=db, cfg=Config())

    assert len(tg.edits) == 1 and "TOP PICKS" in tg.edits[0]["text"]


# ----------------------------------------------------------------- helpers
def datetime_now_utc() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

