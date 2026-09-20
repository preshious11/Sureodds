"""VIEW_NAVIGATION: what a button press does. ``new`` (default) never edits or deletes - every press sends a new
message; ``edit_pages`` edits only page turns; ``edit`` is the classic edit-in-place behaviour."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from predbot.bot import callbacks, pagination
from predbot.bot.callbacks import handle_callback, handle_command, navigation_mode
from predbot.cache.models import DailySnapshot, PickRecord, StatsSummary
from predbot.config import Config
from tests.test_cache_pagination import make_pick, make_snapshot
from tests.test_subscribers_broadcast import FakeTG


def _service(*snaps):
    today, older = snaps[0], {s.date: s for s in snaps}
    return SimpleNamespace(get_daily_snapshot=lambda: today, snapshot_for=lambda day: older[day])


def _press(data, service, mode, message_id=55, tg=None):
    tg = tg or FakeTG()
    cfg = Config(view_navigation=mode)
    handle_callback({"id": "cb1", "data": data, "message": {"message_id": message_id, "chat": {"id": "42"}}},
                    service=service, tg=tg, cfg=cfg, db=None, page_size=5)
    return tg


def _old_day_snapshot():
    picks = [PickRecord(pick_id=i, tier="daily", rank=i, sport="football", league="EPL", home=f"OldHome{i}",
                        away=f"OldAway{i}", kickoff_utc="2030-05-31T18:00:00Z", label="Over 2.5 goals",
                        market="total", selection="over", line=2.5, price=1.8, prob=0.6, pick_date="2030-05-31")
             for i in range(1, 8)]
    return DailySnapshot(date="2030-05-31", origin="db", daily_picks=picks)


def test_shipped_default_is_edit_pages(monkeypatch):
    monkeypatch.delenv("VIEW_NAVIGATION", raising=False)
    assert Config().view_navigation == "edit_pages"
    assert navigation_mode(Config()) == "edit_pages"
    assert navigation_mode(SimpleNamespace()) == "edit_pages"          # a config without the setting


def test_unknown_setting_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv("VIEW_NAVIGATION", "sideways")
    assert Config().view_navigation == "edit_pages"


def test_the_other_two_modes_stay_available(monkeypatch):
    for mode in ("new", "edit"):
        monkeypatch.setenv("VIEW_NAVIGATION", mode)
        assert Config().view_navigation == mode


def test_new_a_view_switch_sends_and_never_edits():
    tg = _press("pg|daily|0", _service(make_snapshot()), "new")
    assert len(tg.sends) == 1 and tg.edits == []
    assert "DAILY PICKS" in tg.sends[0]["text"] and tg.sends[0]["chat_id"] == "42"
    assert tg.sends[0]["reply_markup"]["inline_keyboard"]          # its own keyboard
    assert tg.answers == [{"id": "cb1", "text": "Daily Picks ↓"}]


def test_new_a_page_turn_sends_and_never_edits():
    tg = _press("pg|daily|1|20300601", _service(make_snapshot()), "new")
    assert len(tg.sends) == 1 and tg.edits == []
    assert "Page 2 of " in tg.sends[0]["text"]
    assert tg.answers[0]["text"].startswith("Page 2 of ")


@pytest.mark.parametrize("data", ["pg|top|0", "pg|daily|0", "pg|stats|0", "pg|daily|2|20300601"])
def test_new_every_press_is_answered(data):
    tg = _press(data, _service(make_snapshot()), "new")
    assert len(tg.answers) == 1 and tg.edits == []


def test_new_menu_button_sends_a_new_welcome():
    cb = {"id": "m", "data": "menu", "from": {"first_name": "Ada"},
          "message": {"message_id": 9, "chat": {"id": "42"}}}
    tg = FakeTG()
    handle_callback(cb, service=None, tg=tg, cfg=Config(view_navigation="new"), db=None)
    assert tg.edits == [] and tg.sends[0]["text"].startswith("👋 Hi, Ada")


def test_edit_pages_page_turn_edits_the_same_message_and_a_switch_sends():
    service = _service(make_snapshot())
    turn = _press("pg|daily|1|20300601", service, "edit_pages", message_id=77)
    assert turn.sends == [] and [e["message_id"] for e in turn.edits] == [77]
    switch = _press("pg|top|0", service, "edit_pages", message_id=77)
    assert switch.edits == [] and len(switch.sends) == 1


def test_edit_reproduces_the_classic_call_sequence():
    service = _service(make_snapshot())
    for data in ("pg|top|0", "pg|daily|1|20300601"):
        tg = _press(data, service, "edit", message_id=77)
        assert tg.sends == [] and [e["message_id"] for e in tg.edits] == [77]
        assert len(tg.answers) == 1
    assert tg.answers[0]["text"] == "Daily Picks · page 2/5"        # the old toast, unchanged


def test_a_page_turn_on_an_old_message_renders_that_days_snapshot():
    today, old = make_snapshot(), _old_day_snapshot()
    tg = _press("pg|daily|1|20300531", _service(today, old), "new")
    text = tg.sends[0]["text"]
    assert "OldHome6" in text and "Home2" not in text                 # page 2 of the OLD day's 7 picks
    assert "Page 2 of 2" in text
    keyboard = tg.sends[0]["reply_markup"]["inline_keyboard"][0]
    assert [b["callback_data"] for b in keyboard if b["callback_data"] != "noop"] == ["pg|daily|0|20300531"]


def test_typed_commands_post_new_messages_in_every_mode():
    for mode in ("new", "edit_pages", "edit"):
        tg = FakeTG()
        handle_command("/top", "42", service=_service(make_snapshot()), tg=tg, cfg=Config(view_navigation=mode), db=None)
        assert len(tg.sends) == 1 and tg.edits == []


def test_page_turn_buttons_carry_the_day_and_switch_buttons_do_not():
    _, markup = pagination.render_page(make_snapshot(), "daily", 1, 5, "UTC")
    arrows = [b for b in markup["inline_keyboard"][0] if b["text"] in ("◀️", "▶️")]
    assert arrows and all(b["callback_data"].endswith("|20300601") for b in arrows)
    assert all(pagination.parse_callback(b["callback_data"])[2] is None for b in markup["inline_keyboard"][-1])
    assert pagination.parse_callback("pg|daily|2|20300601") == ("daily", 2, "2030-06-01")
    assert pagination.parse_callback("pg|daily|2|2030") is None


def _default_press(data, service, message_id=90):
    """A press under the shipped default (no explicit mode)."""
    tg = FakeTG()
    handle_callback({"id": "cb1", "data": data, "message": {"message_id": message_id, "chat": {"id": "42"}}},
                    service=service, tg=tg, cfg=Config(), db=None, page_size=5)
    return tg


def test_by_default_a_page_turn_on_a_delivered_daily_message_edits_that_message():
    tg = _default_press("pg|daily|1|20300601", _service(make_snapshot()), message_id=90)
    assert tg.sends == [] and [e["message_id"] for e in tg.edits] == [90]
    assert "Page 2 of" in tg.edits[0]["text"] and tg.answers[0]["text"].startswith("Page 2 of ")


def test_by_default_a_page_turn_on_an_earlier_days_message_edits_it_and_renders_that_day():
    tg = _default_press("pg|daily|1|20300531", _service(make_snapshot(), _old_day_snapshot()), message_id=91)
    assert tg.sends == [] and [e["message_id"] for e in tg.edits] == [91]
    assert "OldHome6" in tg.edits[0]["text"] and "Fri 31 May" in tg.edits[0]["text"]


@pytest.mark.parametrize("data", ["pg|top|0", "pg|daily|0", "pg|stats|0", "pg|yesterday|0"])
def test_by_default_a_view_switch_sends_a_new_message(data):
    tg = _default_press(data, _service(make_snapshot()))
    assert tg.edits == [] and len(tg.sends) == 1 and len(tg.answers) == 1


def test_by_default_the_menu_button_sends_a_new_welcome():
    tg = FakeTG()
    handle_callback({"id": "m", "data": "menu", "from": {"first_name": "Ada"},
                     "message": {"message_id": 9, "chat": {"id": "42"}}}, service=None, tg=tg, cfg=Config(), db=None)
    assert tg.edits == [] and tg.sends[0]["text"].startswith("👋 Hi, Ada")
