"""Tests for subscriber management, daily broadcasts and the /start menu."""
from __future__ import annotations

from datetime import datetime, timezone

from predbot.bot import broadcast, callbacks
from predbot.bot.callbacks import (handle_callback, handle_start, menu_markup,
                                   welcome_message)
from predbot.bot.commands import CommandLoop
from predbot.bot.pagination import view_button_label
from predbot.cache.fetcher import UnifiedFetcher
from predbot.cache.models import (DailySnapshot, PickRecord, StatsSummary, VIEW_DAILY,
                                  VIEW_STATS, VIEW_TOP)
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
def test_start_registers_subscriber_and_sends_the_menu(db):
    tg = FakeTG()
    handle_start(db, tg, "42", {"username": "zed", "first_name": "Zed"})

    assert len(tg.sends) == 1
    assert tg.sends[0]["chat_id"] == "42"
    assert tg.sends[0]["text"].startswith("👋 Hi, Zed")          # clean one-line greeting
    assert "/top" in tg.sends[0]["text"] and "/daily" in tg.sends[0]["text"]
    rows = db.subscribers_active()
    assert len(rows) == 1 and rows[0]["chat_id"] == "42"
    assert rows[0]["username"] == "zed"
    assert db.subscriber_timezone("42") == ""                    # '' -> cfg.timezone


def test_subscriber_toggle_on_and_off(db):
    db.subscriber_add("42", "zed", "Zed")
    db.subscriber_remove("42")
    assert db.subscribers_active() == []
    db.subscriber_add("42")                     # re-subscribe keeps stored names
    rows = db.subscribers_active()
    assert len(rows) == 1 and rows[0]["username"] == "zed"


def test_menu_buttons_match_the_requested_actions():
    buttons = [b for row in menu_markup()["inline_keyboard"] for b in row]
    assert [b["text"] for b in buttons] == [
        "\U0001f3c6 Top Picks", "\U0001f4cb Daily Picks", "\U0001f4ca Stats",
        "\U0001f515 Unsubscribe from daily picks"]
    assert [b["callback_data"] for b in buttons] == [
        "pg|top|0", "pg|daily|0", "pg|stats|0", "sub|off"]


# ------------------------------------------------------------------- broadcasts
def test_deliver_picks_reaches_all_subscribers_once(db):
    db.subscriber_add("42")
    db.subscriber_add("77")
    tg = FakeTG()
    now = datetime(2030, 6, 1, 9, 30, tzinfo=timezone.utc)

    sent = broadcast.deliver_picks(db, Config(timezone="UTC"), tg, _snapshot, now=now)

    assert sent == 2
    assert [s["chat_id"] for s in tg.sends] == ["42", "77"]
    card = tg.sends[0]["text"]
    assert card.startswith("<b>Today's</b> predictions—— <b>2030-06-01</b>")
    assert card.count("Confidence:") == 4        # 1 top + 3 daily picks
    assert card.count("Football") == 4                 # one sport line per pick, no league
    assert "Combined odds:" in card

    # same day again (e.g. a restart): deliveries are de-duplicated per user
    assert broadcast.deliver_picks(db, Config(timezone="UTC"), tg, _snapshot, now=now) == 0
    assert len(tg.sends) == 2


def test_deliver_picks_skips_empty_snapshot(db):
    db.subscriber_add("42")
    empty = DailySnapshot(date="2030-06-01", origin="db")
    now = datetime(2030, 6, 1, 9, 30, tzinfo=timezone.utc)
    assert broadcast.deliver_picks(db, Config(timezone="UTC"), FakeTG(),
                                   lambda: empty, now=now) == 0


def test_all_subscribers_receive_at_same_nigerian_instant(db):
    db.subscriber_add("42", timezone="Asia/Tokyo")
    db.subscriber_add("77", timezone="America/New_York")
    db.subscriber_add("88", timezone="Africa/Lagos")
    tg = FakeTG()
    cfg = Config(timezone="Africa/Lagos", post_time="09:00")
    before = datetime(2030, 6, 1, 7, 59, tzinfo=timezone.utc)
    due = datetime(2030, 6, 1, 8, 0, tzinfo=timezone.utc)
    assert broadcast.deliver_picks(db, cfg, tg, _snapshot, now=before) == 0
    assert broadcast.deliver_picks(db, cfg, tg, _snapshot, now=due) == 3
    assert "03:00 AM JST" in tg.sends[0]["text"]
    assert "02:00 PM EDT" in tg.sends[1]["text"]
    assert "07:00 PM WAT" in tg.sends[2]["text"]
    assert broadcast.deliver_picks(db, cfg, tg, _snapshot, now=due) == 0


def test_deliver_picks_uses_the_config_timezone_when_unset(db):
    db.subscriber_add("42")                                # never ran /timezone
    db.subscriber_add("77", timezone="Asia/Tokyo")
    tg = FakeTG()
    now = datetime(2030, 6, 1, 9, 30, tzinfo=timezone.utc)

    assert broadcast.deliver_picks(db, Config(timezone="UTC"), tg, _snapshot, now=now) == 2
    assert broadcast.subscriber_tz(db.subscribers_active()[0], "UTC") == "UTC"


def test_deliver_picks_ignores_an_unknown_stored_timezone(db):
    db.subscriber_add("42")
    db.subscriber_set_timezone("42", "Mars/Olympus")       # never trusted
    tg = FakeTG()
    now = datetime(2030, 6, 1, 9, 30, tzinfo=timezone.utc)

    assert broadcast.deliver_picks(db, Config(timezone="UTC"), tg, _snapshot, now=now) == 1
    assert broadcast.subscriber_tz(db.subscribers_active()[0], "UTC") == "UTC"


def test_deliver_recap_dedupes_per_day(db):
    db.subscriber_add("42")
    tg = FakeTG()
    assert broadcast.deliver_recap(db, Config(), tg, "📊 RESULTS · Sun 01 Jun") == 1
    assert broadcast.deliver_recap(db, Config(), tg, "📊 RESULTS · Sun 01 Jun") == 0
    assert len(tg.sends) == 1


# -------------------------------------------------------------------- commands
def _loop(db) -> CommandLoop:
    """A dispatcher with an inert config: text commands only, never polls."""
    return CommandLoop(FakeTG(), db, Config(timezone="UTC"), {}, service=None)


def test_timezone_command_sets_the_delivery_zone(db):
    db.subscriber_add("42")
    loop = _loop(db)

    reply = loop.handle("/timezone", "1", "42", ["Africa/Lagos"])
    assert "Africa/Lagos" in reply
    assert db.subscriber_timezone("42") == "Africa/Lagos"

    assert "Africa/Lagos" in loop.handle("/timezone", "1", "42")   # no args: just report


def test_timezone_command_rejects_an_unknown_zone(db):
    db.subscriber_add("42")

    reply = _loop(db).handle("/timezone", "1", "42", ["Mars/Olympus"])
    assert "Unknown timezone" in reply
    assert db.subscriber_timezone("42") == ""                      # unchanged


def test_stop_command_unsubscribes(db):
    db.subscriber_add("42")
    assert "Unsubscribed" in _loop(db).handle("/stop", "1", "42")
    assert db.subscribers_active() == []


def test_help_lists_every_advertised_command(db):
    text = _loop(db).handle("/help", "1", "42")
    for cmd in ("/top", "/daily", "/stats", "/timezone", "/stop"):
        assert cmd in text


# ---------------------------------------------------------- one view, two doors
def test_button_and_command_render_the_same_card(db):
    """The /top button and typed /top render byte-identical text and keyboard."""
    cache = None
    fetcher = UnifiedFetcher(FakeChain(), db, Config())
    from tests.test_cache_pagination import _seed
    _seed(fetcher, _snapshot())
    tg = FakeTG()

    assert callbacks.handle_command("/top", "42", service=fetcher, tg=tg, db=db,
                                    cfg=Config())
    handle_callback(_cb("pg|top|0"), service=fetcher, tg=tg, db=db, cfg=Config())

    assert len(tg.sends) == 1 and len(tg.edits) == 1
    assert tg.sends[0]["text"] == tg.edits[0]["text"]
    assert tg.sends[0]["reply_markup"] == tg.edits[0]["reply_markup"]


def test_page_callbacks_still_work_after_router_change(db):
    cache = None
    fetcher = UnifiedFetcher(FakeChain(), db, Config())
    from tests.test_cache_pagination import _seed
    _seed(fetcher, _snapshot())
    tg = FakeTG()

    handle_callback(_cb("pg|top|0"), service=fetcher, tg=tg, db=db, cfg=Config())

    assert len(tg.edits) == 1
    assert "<b>Top</b> predictions" in tg.edits[0]["text"]
    assert [b["text"] for b in tg.edits[0]["reply_markup"]["inline_keyboard"][-1]] == \
        ["🏆 /top", "📋 /daily", "📊 /stats"]


# ----------------------------------------------------------------- helpers
def datetime_now_utc() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

