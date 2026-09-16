"""Tests for subscriber management, daily broadcasts and the /start menu."""
from __future__ import annotations

from datetime import datetime, timezone

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
    text = tg.sends[0]["text"]
    assert "Hi, Zed" in text
    assert "/daily" in text and "/top" in text
    rows = db.subscribers_active()
    assert len(rows) == 1 and rows[0]["chat_id"] == "42"
    assert rows[0]["username"] == "zed"


def test_start_sends_menu_only_never_predictions(db):
    """/start must never fire predictions at the user - menu only."""
    tg = FakeTG()
    handle_start(db, tg, "42", {"username": "zed", "first_name": "Zed"})
    assert len(tg.sends) == 1                       # exactly one message: the menu
    text = tg.sends[0]["text"]
    assert "Confidence level:" not in text          # no pick lines
    assert "Total odds" not in text and "Combined Odds" not in text


def test_start_autodetects_timezone_from_locale(db):
    tg = FakeTG()
    handle_start(db, tg, "42", {"first_name": "Zed", "language_code": "en_NG"})
    assert db.get_user_timezone("42") == "Africa/Lagos"


def test_subscriber_toggle_on_and_off(db):
    db.subscriber_add("42", "zed", "Zed")
    db.subscriber_remove("42")
    assert db.subscribers_active() == []
    db.subscriber_add("42")                     # re-subscribe keeps stored names
    rows = db.subscribers_active()
    assert len(rows) == 1 and rows[0]["username"] == "zed"


def test_menu_has_no_subscription_button():
    """The subscribe/unsubscribe toggle was removed - it must not come back."""
    texts = [b["text"] for row in menu_markup()["inline_keyboard"] for b in row]
    assert texts == ["🏆 Top Picks", "📋 Daily Picks", "📊 Stats"]
    data = [b["callback_data"] for row in menu_markup()["inline_keyboard"] for b in row]
    assert not any(d.startswith("sub|") for d in data)


# ------------------------------------------------------------------- broadcasts
def _freeze(monkeypatch, iso: str) -> None:
    """Pin broadcast.utcnow() so the per-subscriber time gate is deterministic."""
    frozen = datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)
    monkeypatch.setattr(broadcast, "utcnow", lambda: frozen)


def test_deliver_picks_reaches_all_subscribers_once(db, monkeypatch):
    db.subscriber_add("42")
    db.subscriber_add("77")
    tg = FakeTG()
    _freeze(monkeypatch, "2030-06-01T09:30:00")      # 9:30am for UTC subscribers

    sent = broadcast.deliver_picks(db, Config(), tg, _snapshot())

    assert sent == 2
    assert [s["chat_id"] for s in tg.sends] == ["42", "77"]
    card = tg.sends[0]["text"]
    assert card.startswith("📋 Daily Picks for Today")
    assert "Top Picks for Today" not in card
    assert card.count("Confidence level:") == 4       # 1 top + 3 daily picks, one block each
    assert "Combined Odds:" in card

    # same day again (e.g. a restart): deliveries are de-duplicated per user
    assert broadcast.deliver_picks(db, Config(), tg, _snapshot()) == 0
    assert len(tg.sends) == 2


def test_deliver_picks_waits_until_930_in_subscriber_timezone(db, monkeypatch):
    db.subscriber_add("42")
    tg = FakeTG()

    _freeze(monkeypatch, "2030-06-01T06:00:00")      # 6am - too early
    assert broadcast.deliver_picks(db, Config(), tg, _snapshot()) == 0
    assert tg.sends == []

    _freeze(monkeypatch, "2030-06-01T09:30:00")      # 9:30am - fires
    assert broadcast.deliver_picks(db, Config(), tg, _snapshot()) == 1


def test_deliver_picks_uses_each_subscribers_own_timezone(db, monkeypatch):
    db.subscriber_add("lagos")
    db.set_user_timezone("lagos", "Africa/Lagos")     # UTC+1 -> 9:30am WAT == 08:30 UTC
    db.subscriber_add("utc")
    tg = FakeTG()

    _freeze(monkeypatch, "2030-06-01T08:30:00")
    assert broadcast.deliver_picks(db, Config(), tg, _snapshot()) == 1
    assert tg.sends[0]["chat_id"] == "lagos"
    assert "WAT" in tg.sends[0]["text"]               # kickoffs rendered in their zone

    _freeze(monkeypatch, "2030-06-01T09:30:00")
    assert broadcast.deliver_picks(db, Config(), tg, _snapshot()) == 1
    assert tg.sends[1]["chat_id"] == "utc"


def test_deliver_picks_skips_empty_snapshot(db):
    db.subscriber_add("42")
    empty = DailySnapshot(date="2030-06-01", origin="db")
    assert broadcast.deliver_picks(db, Config(), FakeTG(), empty) == 0


# ---------------------------------------------------------- /start menu routing
def test_pagination_keyboard_has_no_subscription_button(db):
    """No rendered view may bring the subscribe toggle back."""
    import predbot.bot.pagination as pg
    for view in ("top", "daily", "stats"):
        data = [b["callback_data"]
                for row in pg.build_keyboard(view, 0, 3)["inline_keyboard"] for b in row]
        assert not any(d.startswith("sub|") for d in data)


def test_page_callbacks_still_work_after_router_change(db):
    cache = CacheManager()
    cache.set(cache.daily_key(), _snapshot().to_dict(), ttl_seconds=60)
    fetcher = UnifiedFetcher(FakeChain(), db, Config(), cache)
    tg = FakeTG()

    handle_callback(_cb("pg|top|0"), service=fetcher, tg=tg, db=db, cfg=Config())

    assert len(tg.edits) == 1 and "Top picks for today" in tg.edits[0]["text"]


# ----------------------------------------------------------------- helpers
def datetime_now_utc() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

