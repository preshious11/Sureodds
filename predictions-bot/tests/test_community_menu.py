"""Community UX regressions through the real command, callback and cache paths."""
from datetime import datetime, timezone
import logging
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from predbot.bot import callbacks, pagination
from predbot.bot.commands import CommandLoop
from predbot.bot.telegram import Telegram
from predbot.cache.fetcher import UnifiedFetcher
from predbot.config import Config
from predbot.jobs import daily_picks
from tests.test_cache_pagination import _insert_picks, make_snapshot
from tests.test_subscribers_broadcast import FakeTG


COMMUNITY = "-1001234567890"
BOT_USERNAME = "PredictionsTestBot"
NOW = datetime(2030, 6, 1, 12, tzinfo=timezone.utc)
PUBLIC_VIEWS = ("top", "daily", "all", "stats", "yesterday")


class MenuTelegram(FakeTG):
    def __init__(self, username=BOT_USERNAME, fail_menu=False):
        super().__init__()
        self.username = username
        self.username_reads = 0
        self.fail_menu = fail_menu

    def get_bot_username(self):
        self.username_reads += 1
        return self.username

    def send(self, text, chat_id=None, reply_markup=None, parse_mode="HTML"):
        if self.fail_menu and text == callbacks.COMMUNITY_MENU_TEXT:
            return None
        return super().send(text, chat_id, reply_markup, parse_mode)


def message_update(text, chat_id="42", chat_type="private", user_id="9"):
    return {"update_id": 77, "message": {
        "text": text, "chat": {"id": int(chat_id), "type": chat_type},
        "from": {"id": int(user_id), "first_name": "Ada", "language_code": "en"},
    }}


def button_update(data, chat_id=COMMUNITY, text=callbacks.COMMUNITY_MENU_TEXT):
    # Ordinary member: no admin ID and no send-message permission is required by this route.
    return {"update_id": 78, "callback_query": {
        "id": "button-78", "data": data, "from": {"id": 42},
        "message": {"message_id": 123, "text": text,
                    "chat": {"id": int(chat_id), "type": "supergroup" if chat_id.startswith("-") else "private"}},
    }}


@pytest.fixture
def menu_loop(db):
    cfg = Config(timezone="UTC", telegram_chat_id=COMMUNITY, telegram_admin_ids=["9"])
    tg = MenuTelegram()
    loop = CommandLoop(tg, db, cfg, {})
    loop.instance = "test-menu"
    return loop


@pytest.fixture
def cached_loop(menu_loop, db, monkeypatch):
    monkeypatch.setattr("predbot.cache.fetcher.utcnow", lambda: NOW)
    monkeypatch.setattr("predbot.bot.pagination.utcnow", lambda: NOW)
    chain = Mock()
    chain.collect.side_effect = AssertionError("unexpected provider call")
    service = UnifiedFetcher(chain, db, menu_loop.cfg)
    snapshot = make_snapshot()
    snapshot.all_predictions = snapshot.top_picks + snapshot.daily_picks
    service._store(snapshot, NOW)
    _insert_picks(db, "2030-05-31")
    for row, outcome in zip(db.picks_for_date("2030-05-31"), ("won", "lost", "void")):
        db.settle_pick(row["id"], outcome)
    menu_loop.service = service
    return menu_loop


def test_exact_community_menu_layout_and_private_deep_link(menu_loop):
    menu_loop._handle_update(message_update("/menu"))
    menu, confirmation = menu_loop.tg.sends
    assert menu["chat_id"] == COMMUNITY
    assert menu["text"] == "⚽ Predictions\n\nToday's picks, results and more."
    rows = menu["reply_markup"]["inline_keyboard"]
    assert [[b["text"] for b in row] for row in rows] == [
        ["🔥 Top Picks", "📋 Daily Picks"], ["📚 All Predictions"],
        ["📊 Stats", "🗓 Yesterday"], ["✨ Custom Picks"],
    ]
    assert [b["callback_data"] for row in rows[:3] for b in row] == [
        pagination.encode_callback(view, 0) for view in PUBLIC_VIEWS
    ]
    assert rows[3] == [{"text": "✨ Custom Picks",
                       "url": f"https://t.me/{BOT_USERNAME}?start=custom_picks"}]
    assert confirmation["chat_id"] == "42" and "Message ID: 123" in confirmation["text"]


@pytest.mark.parametrize("view", PUBLIC_VIEWS)
@pytest.mark.parametrize("mode", callbacks.NAVIGATION_MODES)
def test_ordinary_member_buttons_use_existing_views_in_supergroup(cached_loop, db, monkeypatch, view, mode):
    loop = cached_loop
    monkeypatch.setattr(callbacks, "navigation_mode", lambda cfg: mode)
    open_view = Mock(wraps=callbacks.open_view)
    monkeypatch.setattr(callbacks, "open_view", open_view)
    generate = Mock(side_effect=AssertionError("unexpected generation"))
    monkeypatch.setattr(daily_picks, "build_picks", generate)
    button = next(b for row in callbacks.community_menu_markup(BOT_USERNAME)["inline_keyboard"]
                  for b in row if b.get("callback_data") == f"pg|{view}|0")
    loop._handle_update(button_update(button["callback_data"]))
    open_view.assert_called_once()
    assert open_view.call_args.args == (view,)
    assert open_view.call_args.kwargs["chat_id"] == COMMUNITY
    assert open_view.call_args.kwargs["edit"] is False
    assert len(loop.tg.sends) == 1 and loop.tg.edits == []  # pinnable menu survives every mode
    sent = loop.tg.sends[0]
    assert sent["chat_id"] == COMMUNITY and sent["reply_markup"]["inline_keyboard"]
    assert "Something went wrong" not in sent["text"]
    expected_title = {"top": "TOP PICKS", "daily": "DAILY PICKS", "all": "ALL PREDICTIONS",
                      "stats": "STATS", "yesterday": "YESTERDAY"}[view]
    assert expected_title in sent["text"]
    if view in ("stats", "yesterday"):
        assert "✅" in sent["text"] and "❌" in sent["text"]
    assert loop.tg.answers[0]["id"] == "button-78"
    assert loop.tg.answers[0]["text"] != "Something went wrong."
    assert db.subscribers_active() == []
    loop.service.chain.collect.assert_not_called()
    generate.assert_not_called()
    assert loop.service.api_batches == 0


@pytest.mark.parametrize("chat_id,chat_type", [("42", "private"), (COMMUNITY, "supergroup"),
                                               ("-1009999999999", "supergroup")])
def test_menu_with_cold_cache_never_calls_service_providers_jobs_or_changes_database(
        menu_loop, db, monkeypatch, caplog, chat_id, chat_type):
    loop = menu_loop
    db.subscriber_add("67", "existing", "Existing")
    _insert_picks(db, "2030-05-31")
    before = list(db.conn.iterdump())
    subscribers = Mock(wraps=db.subscriber_add)
    monkeypatch.setattr(db, "subscriber_add", subscribers)
    generate = Mock(side_effect=AssertionError("generation forbidden"))
    monkeypatch.setattr(daily_picks, "build_picks", generate)
    chain = Mock()
    loop.service = UnifiedFetcher(chain, db, loop.cfg)
    snapshot = Mock(side_effect=AssertionError("menu must not read snapshots"))
    monkeypatch.setattr(loop.service, "get_daily_snapshot", snapshot)
    loop.actions = {cmd: Mock(side_effect=AssertionError("job forbidden"))
                    for cmd in ("/run", "/settle", "/recap")}
    with caplog.at_level(logging.INFO):
        loop._handle_update(message_update("/menu@PredictionsTestBot", chat_id, chat_type))
    assert loop.tg.sends[0]["chat_id"] == COMMUNITY
    assert "Message ID: 123" in loop.tg.sends[1]["text"]
    assert "chat_id=" + COMMUNITY + " | message_id=123" in caplog.text
    assert list(db.conn.iterdump()) == before  # includes all picks, subscribers, posts, caches and receipts
    subscribers.assert_not_called()
    snapshot.assert_not_called()
    generate.assert_not_called()
    chain.collect.assert_not_called()
    for action in loop.actions.values():
        action.assert_not_called()


@pytest.mark.parametrize("chat_type,chat_id", [("private", "42"), ("supergroup", COMMUNITY)])
def test_menu_is_admin_only(menu_loop, db, chat_type, chat_id):
    menu_loop._handle_update(message_update("/menu", chat_id, chat_type, user_id="42"))
    assert menu_loop.tg.sends == [{"chat_id": chat_id, "text": "Not allowed.", "reply_markup": None}]
    assert menu_loop.tg.username_reads == 0
    assert db.subscribers_active() == []


@pytest.mark.parametrize("failure", ["destination", "username", "send"])
def test_menu_failures_report_no_success_or_database_changes(menu_loop, db, failure):
    if failure == "destination":
        menu_loop.cfg.telegram_chat_id = ""
    elif failure == "username":
        menu_loop.tg.username = None
    else:
        menu_loop.tg.fail_menu = True
    before = list(db.conn.iterdump())
    menu_loop._handle_update(message_update("/menu"))
    assert len(menu_loop.tg.sends) == 1 and menu_loop.tg.sends[0]["chat_id"] == "42"
    assert "Message ID:" not in menu_loop.tg.sends[0]["text"]
    assert list(db.conn.iterdump()) == before


def test_custom_picks_private_entry_is_distinct_and_does_not_subscribe(menu_loop, db, monkeypatch):
    entry = Mock(wraps=callbacks.handle_custom_picks_start)
    monkeypatch.setattr(callbacks, "handle_custom_picks_start", entry)
    menu_loop.service = Mock()
    before = list(db.conn.iterdump())
    menu_loop._handle_update(message_update("/start custom_picks", user_id="42"))
    entry.assert_called_once_with(menu_loop.tg, "42")
    from predbot.custom_picks import INTRO
    assert ('42', '42') in menu_loop._custom_users
    assert menu_loop.tg.sends == [{"chat_id": "42", "text": INTRO,
                                 "reply_markup": None}]
    assert list(db.conn.iterdump()) == before
    assert menu_loop.service.mock_calls == []


@pytest.mark.parametrize("command", ["/start", "/start other_payload"])
def test_normal_private_start_keeps_existing_welcome_and_subscription(menu_loop, db, monkeypatch, command):
    entry = Mock()
    monkeypatch.setattr(callbacks, "handle_custom_picks_start", entry)
    menu_loop._handle_update(message_update(command, user_id="42"))
    entry.assert_not_called()
    assert menu_loop.tg.sends == [{"chat_id": "42", "text": callbacks.welcome_message("Ada"),
                                 "reply_markup": callbacks.menu_markup()}]
    assert [row["chat_id"] for row in db.subscribers_active()] == ["42"]


def test_custom_picks_start_is_rejected_in_supergroup(menu_loop, db, monkeypatch):
    entry = Mock()
    monkeypatch.setattr(callbacks, "handle_custom_picks_start", entry)
    menu_loop._handle_update(message_update("/start custom_picks", COMMUNITY, "supergroup", user_id="42"))
    entry.assert_not_called()
    assert menu_loop.tg.sends[0]["text"] == "Use this command in a private chat with the bot."
    assert db.subscribers_active() == []


@pytest.mark.parametrize("view", PUBLIC_VIEWS)
def test_private_view_commands_and_callbacks_keep_existing_navigation(cached_loop, view):
    loop = cached_loop
    loop._handle_update(message_update("/" + view, user_id="42"))
    assert len(loop.tg.sends) == 1 and loop.tg.sends[0]["chat_id"] == "42"
    assert loop.tg.sends[0]["text"] != "Something went wrong."
    # The test suite's classic navigation mode still edits private messages.
    loop._handle_update(button_update(f"pg|{view}|0", "42", callbacks.welcome_message("Ada")))
    assert len(loop.tg.edits) == 1 and loop.tg.edits[0]["chat_id"] == "42"
    assert loop.tg.edits[0]["text"] == loop.tg.sends[0]["text"]
    loop.service.chain.collect.assert_not_called()


def test_getme_resolves_same_bot_username_and_caches_success():
    tg = Telegram("test-token", COMMUNITY)
    tg.session = Mock()
    tg.session.get.return_value = SimpleNamespace(
        status_code=200, json=lambda: {"ok": True, "result": {"username": BOT_USERNAME}})
    assert tg.get_bot_username() == BOT_USERNAME
    assert tg.get_bot_username() == BOT_USERNAME
    tg.session.get.assert_called_once_with("https://api.telegram.org/bottest-token/getMe", timeout=10)
    button = callbacks.community_menu_markup(tg.get_bot_username())["inline_keyboard"][-1][0]
    assert button["url"] == f"https://t.me/{BOT_USERNAME}?start=custom_picks"


@pytest.mark.parametrize("response", [
    {"ok": False}, {"ok": True, "result": {}},
    {"ok": True, "result": {"username": "not/a/username"}},
])
def test_getme_rejects_invalid_identity_and_can_retry(response):
    tg = Telegram("test-token", COMMUNITY)
    tg.session = Mock()
    tg.session.get.return_value = SimpleNamespace(status_code=200, json=lambda: response)
    assert tg.get_bot_username() is None
    tg.session.get.return_value = SimpleNamespace(
        status_code=200, json=lambda: {"ok": True, "result": {"username": BOT_USERNAME}})
    assert tg.get_bot_username() == BOT_USERNAME


def test_getme_network_failure_does_not_log_token(caplog):
    tg = Telegram("secret-test-token", COMMUNITY)
    tg.session = Mock()
    tg.session.get.side_effect = requests.ConnectionError(tg.base)
    with caplog.at_level(logging.WARNING):
        assert tg.get_bot_username() is None
    assert "secret-test-token" not in caplog.text
