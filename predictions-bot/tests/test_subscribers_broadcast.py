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
    assert db.subscriber_timezone("42") == "Africa/Lagos"


def test_subscriber_toggle_on_and_off(db):
    db.subscriber_add("42", "zed", "Zed")
    db.subscriber_remove("42")
    assert db.subscribers_active() == []
    db.subscriber_add("42")                     # re-subscribe keeps stored names
    rows = db.subscribers_active()
    assert len(rows) == 1 and rows[0]["username"] == "zed"


def test_menu_has_no_subscription_button():
    """The subscribe/unsubscribe toggle was removed - it must not come back."""
    buttons = [b for row in menu_markup()["inline_keyboard"] for b in row]
    assert [b["text"] for b in buttons] == [
        "\U0001f3c6 Top Picks", "\U0001f4cb Daily Picks", "\U0001f4ca Stats"]
    assert [b["callback_data"] for b in buttons] == ["pg|top|0", "pg|daily|0", "pg|stats|0"]
    assert not any(b["callback_data"].startswith("sub|") for b in buttons)
    assert [view_button_label(v) for v in ("top", "daily", "stats")] == \
        [b["text"] for b in buttons]          # menu and switch row share one definition


# ------------------------------------------------------------------- broadcasts
def _freeze(monkeypatch, iso: str) -> None:
    """Pin broadcast.utcnow() so the per-subscriber time gate is deterministic."""
    frozen = datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)
    monkeypatch.setattr(broadcast, "utcnow", lambda: frozen)


def test_deliver_picks_reaches_all_subscribers_once(db, monkeypatch):
    db.subscriber_add("42")
    db.subscriber_add("77")
    tg = FakeTG()
    now = datetime(2030, 6, 1, 9, 30, tzinfo=timezone.utc)   # 9:30 for UTC subscribers

    sent = broadcast.deliver_picks(db, Config(timezone="UTC"), tg, _snapshot, now=now)

    assert sent == 2
    assert [s["chat_id"] for s in tg.sends] == ["42", "77"]
    card = tg.sends[0]["text"]
    assert card.startswith("📋 Daily Picks for Today 2030-06-01")
    assert "predictions——" not in card and "Today's" not in card   # old copy stays gone
    assert card.count("Confidence level:") == 4       # 1 top + 3 daily picks, one block each
    assert "Combined Odds:" in card

    # same day again (e.g. a restart): deliveries are de-duplicated per user
    assert broadcast.deliver_picks(db, Config(timezone="UTC"), tg, _snapshot, now=now) == 0
    assert len(tg.sends) == 2


def test_deliver_picks_waits_until_930_in_subscriber_timezone(db):
    db.subscriber_add("42")
    tg = FakeTG()
    cfg = Config(timezone="UTC")

    early = datetime(2030, 6, 1, 6, 0, tzinfo=timezone.utc)     # 6am - too early
    assert broadcast.deliver_picks(db, cfg, tg, _snapshot(), now=early) == 0
    assert tg.sends == []

    due = datetime(2030, 6, 1, 9, 30, tzinfo=timezone.utc)      # 9:30am - fires
    assert broadcast.deliver_picks(db, cfg, tg, _snapshot(), now=due) == 1


def test_deliver_picks_uses_each_subscribers_own_timezone(db):
    """The whole point: 9:30 means 9:30 where the subscriber is, not one instant."""
    db.subscriber_add("lagos", timezone="Africa/Lagos")   # UTC+1 -> their 9:30 == 08:30 UTC
    db.subscriber_add("utc")
    tg = FakeTG()
    cfg = Config(timezone="UTC")

    lagos_930 = datetime(2030, 6, 1, 8, 30, tzinfo=timezone.utc)
    assert broadcast.deliver_picks(db, cfg, tg, _snapshot(), now=lagos_930) == 1
    assert tg.sends[0]["chat_id"] == "lagos"             # the UTC subscriber waits
    assert "WAT" in tg.sends[0]["text"]                  # kickoffs in their zone

    utc_930 = datetime(2030, 6, 1, 9, 30, tzinfo=timezone.utc)
    assert broadcast.deliver_picks(db, cfg, tg, _snapshot(), now=utc_930) == 1
    assert tg.sends[1]["chat_id"] == "utc"
    assert broadcast.deliver_picks(db, cfg, tg, _snapshot(), now=utc_930) == 0   # once each


def test_deliver_picks_survives_a_missed_poll_tick(db):
    """A 5-minute poll must not skip anyone whose exact 9:30 was never sampled."""
    db.subscriber_add("42")
    tg = FakeTG()
    late = datetime(2030, 6, 1, 9, 34, tzinfo=timezone.utc)      # first tick after 9:30
    assert broadcast.deliver_picks(db, Config(timezone="UTC"), tg, _snapshot(), now=late) == 1


def test_deliver_picks_skips_empty_snapshot(db):
    db.subscriber_add("42")
    empty = DailySnapshot(date="2030-06-01", origin="db")
    now = datetime(2030, 6, 1, 9, 30, tzinfo=timezone.utc)
    assert broadcast.deliver_picks(db, Config(timezone="UTC"), FakeTG(),
                                   lambda: empty, now=now) == 0


def test_each_zone_is_served_at_its_own_930(db):
    """Tokyo, New York and Lagos each get the card at their own 9:30, not a shared one."""
    db.subscriber_add("tokyo", timezone="Asia/Tokyo")          # UTC+9  -> 00:30 UTC
    db.subscriber_add("lagos", timezone="Africa/Lagos")        # UTC+1  -> 08:30 UTC
    db.subscriber_add("newyork", timezone="America/New_York")  # UTC-4  -> 13:30 UTC
    tg = FakeTG()
    cfg = Config(timezone="UTC")

    def at(hour, minute):
        return broadcast.deliver_picks(
            db, cfg, tg, _snapshot(),
            now=datetime(2030, 6, 1, hour, minute, tzinfo=timezone.utc))

    assert at(0, 0) == 0                       # nobody's 9:30 yet
    assert at(0, 30) == 1 and tg.sends[-1]["chat_id"] == "tokyo"
    assert at(8, 30) == 1 and tg.sends[-1]["chat_id"] == "lagos"
    assert at(13, 30) == 1 and tg.sends[-1]["chat_id"] == "newyork"
    assert at(13, 30) == 0                     # and only once each

    # each card carries kickoffs rendered in that subscriber's own zone
    assert "3:00 AM JST" in tg.sends[0]["text"]
    assert "7:00 PM WAT" in tg.sends[1]["text"]
    assert "2:00 PM EDT" in tg.sends[2]["text"]


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


# ---------------------------------------------------------- /start menu routing
def test_pagination_keyboard_has_no_subscription_button(db):
    """No rendered view may bring the subscribe toggle back."""
    import predbot.bot.pagination as pg
    for view in ("top", "daily", "stats"):
        data = [b["callback_data"]
                for row in pg.build_keyboard(view, 0, 3)["inline_keyboard"] for b in row]
        assert not any(d.startswith("sub|") for d in data)


def _seed_yesterday_fixture(db, pick_date: str, start_utc: str) -> None:
    db.insert_pick({
        "pick_date": pick_date, "tier": "daily", "rank": 1, "provider": "fake",
        "provider_event_id": "ev1", "sport": "football", "league": "EPL",
        "home": "H", "away": "A", "start_utc": start_utc, "market": "total",
        "period": "full", "stat": "points", "entity": "all", "line": 2.5,
        "selection": "over", "label": "Over 2.5 goals", "price": 1.9, "prob": 0.6,
        "ev": 0.05, "p_market": 0.55, "sources": {"market": 0.55}, "provider_odd_id": "o1",
    })


def test_deliver_recap_waits_for_the_last_fixture_then_dedupes(db, monkeypatch):
    """Recap lands only after the day's last fixture has finished in their zone."""
    db.subscriber_add("42")
    _seed_yesterday_fixture(db, "2030-05-31", "2030-05-31T23:00:00Z")   # +4h buffer -> 03:00
    tg = FakeTG()
    cfg = Config(timezone="UTC")

    _freeze(monkeypatch, "2030-06-01T02:00:00")          # fixtures still running
    assert broadcast.deliver_recap(db, cfg, tg, "📊 RESULTS · Sat 31 May") == 0
    assert tg.sends == []

    _freeze(monkeypatch, "2030-06-01T04:00:00")          # last fixture done
    assert broadcast.deliver_recap(db, cfg, tg, "📊 RESULTS · Sat 31 May") == 1
    assert broadcast.deliver_recap(db, cfg, tg, "📊 RESULTS · Sat 31 May") == 0
    assert len(tg.sends) == 1


# -------------------------------------------------------------------- commands
def _loop(db) -> CommandLoop:
    """A dispatcher with an inert config: text commands only, never polls."""
    return CommandLoop(FakeTG(), db, Config(timezone="UTC"), {}, service=None)


def test_there_is_no_manual_timezone_command(db):
    """Zones are detected, never asked for - /timezone must not come back."""
    assert _loop(db).handle("/timezone", "1", "42", ["Africa/Lagos"]) is None
    assert not hasattr(CommandLoop, "_timezone")
    assert "/timezone" not in (_loop(db).handle("/help", "1", "42") or "")


def test_stop_command_unsubscribes(db):
    db.subscriber_add("42")
    assert "Unsubscribed" in _loop(db).handle("/stop", "1", "42")
    assert db.subscribers_active() == []


def test_help_lists_every_advertised_command(db):
    text = _loop(db).handle("/help", "1", "42")
    for cmd in ("/top", "/daily", "/stats", "/stop"):
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
    assert tg.edits[0]["text"].startswith("🏆 Top Picks for Today")
    assert "predictions——" not in tg.edits[0]["text"]          # old copy stays gone
    assert [b["text"] for b in tg.edits[0]["reply_markup"]["inline_keyboard"][-1]] == \
        ["🏆 Top Picks", "📋 Daily Picks", "📊 Stats"]


# ----------------------------------------------------------------- helpers
def datetime_now_utc() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

