"""A card is built once but read at every subscriber's own 9:30 (New York and
Honolulu are hours after Lagos) and at every /top and /daily tap. Legs stay in the
ticket, but a leg that has already started must say so (🔴 after its time) and one that has
finished says 'Finished'. The card no longer carries a 'Generated' line or 'odds as of'."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from predbot.bot import broadcast, pagination
from predbot.bot.format import FINISHED_MARK, STARTED_MARK, leg_state, pick_line_from_record
from predbot.config import Config
from tests.test_subscribers_broadcast import FakeTG, _snapshot

KICK = datetime(2030, 6, 1, 18, 0, tzinfo=timezone.utc)      # the Top leg (football, 2.2h)
DAILY_KICK = datetime(2030, 6, 1, 20, 0, tzinfo=timezone.utc)
STARTED, FINISHED = STARTED_MARK, FINISHED_MARK


# ----------------------------------------------------------------- the rule
def test_leg_state_before_at_and_after_kickoff_and_the_typical_length():
    assert leg_state(KICK, "football", KICK - timedelta(seconds=1)) == ""
    assert leg_state(KICK, "football", KICK) == "started"
    assert leg_state(KICK, "football", KICK + timedelta(hours=2, minutes=11)) == "started"
    assert leg_state(KICK, "football", KICK + timedelta(hours=2, minutes=12)) == "finished"
    assert leg_state(None, "football", KICK) == ""


def test_the_finish_estimate_follows_the_sport():
    assert leg_state(KICK, "football", KICK + timedelta(hours=2, minutes=30)) == "finished"
    assert leg_state(KICK, "baseball", KICK + timedelta(hours=2, minutes=30)) == "started"     # 3.6h


def test_cards_carry_no_generated_line_and_no_odds_as_of():
    card = broadcast.picks_text(_snapshot(), "Africa/Lagos", now=KICK - timedelta(hours=1))
    text, _ = pagination.render_page(_snapshot(), "daily", 0, 10, "Africa/Lagos")
    for shown in (card, text):
        assert "Generated" not in shown and "odds as of" not in shown


# ------------------------------------------------------------------ the card
def _card(tz, now):
    return broadcast.picks_text(_snapshot(), tz, now=now)


def test_nothing_is_marked_before_any_kickoff():
    card = _card("America/New_York", KICK - timedelta(hours=1))

    assert STARTED not in card and FINISHED not in card
    assert "times in EDT" in card


def test_a_started_leg_is_marked_and_a_later_one_is_not():
    card = _card("Pacific/Honolulu", KICK + timedelta(hours=1))         # Top started; Daily (20:00Z) not yet

    assert card.count(STARTED) == 1 and FINISHED not in card
    top_block = card.split("DAILY PICKS")[0]
    assert STARTED in top_block                                          # the marker sits on the Top leg


def test_a_finished_leg_is_marked_finished():
    card = _card("Pacific/Honolulu", DAILY_KICK + timedelta(hours=1))   # Top finished (20:12Z passed? no: 21:00Z yes)

    assert FINISHED in card.split("DAILY PICKS")[0]
    assert card.count(STARTED) == 3                                      # the three Daily legs are in play


def test_legs_stay_in_the_ticket_when_marked():
    early = _card("America/New_York", KICK - timedelta(hours=1))
    late = _card("Pacific/Honolulu", DAILY_KICK + timedelta(hours=1))

    for card in (early, late):
        assert card.count("1 pick ·") == 1 and card.count("3 picks ·") == 1
        assert card.count("Combined odds 1.90") == 1 and card.count("Combined odds 5.83") == 1
    assert early.count("🎯") == late.count("🎯") == 4


def test_the_views_show_the_same_markers_and_generation_time():
    text, _ = pagination.render_page(_snapshot(), "daily", 0, 10, "Africa/Lagos",
                                     "Pacific/Honolulu", now=KICK + timedelta(hours=1))

    assert text.count(STARTED) == 0 and "times in HST" in text and "3 picks" in text       # the Top leg is not in the Daily view


# ------------------------------------------------- the subscribers in the question
def _deliver(monkeypatch, chat, tz, now):
    from predbot.storage import db as db_mod
    import os, tempfile
    from predbot.storage.db import Database
    monkeypatch.setattr(db_mod, "_now", lambda: "2030-01-01T00:00:00Z")       # subscribed long ago
    db = Database(os.path.join(tempfile.mkdtemp(), "t.db"))
    db.subscriber_add(chat, timezone=tz)
    tg = FakeTG()
    sent = broadcast.deliver_picks(db, Config(timezone="Africa/Lagos"), tg, _snapshot(), now=now)
    return sent, "\n\n".join(s["text"] for s in tg.sends)


def test_new_york_at_its_930_sees_upcoming_legs_and_when_the_card_was_made(monkeypatch):
    sent, text = _deliver(monkeypatch, "ny", "America/New_York", datetime(2030, 6, 1, 13, 30, tzinfo=timezone.utc))

    assert sent == 1
    assert STARTED not in text and FINISHED not in text
    assert "times in EDT" in text


def test_honolulu_at_its_930_sees_the_top_leg_already_started(monkeypatch):
    sent, text = _deliver(monkeypatch, "hnl", "Pacific/Honolulu", datetime(2030, 6, 1, 19, 30, tzinfo=timezone.utc))

    assert sent == 1
    assert text.count(STARTED) == 1 and text.count("🎯") == 4
    assert "times in HST" in text


def test_lagos_at_930_is_unmarked(monkeypatch):
    sent, text = _deliver(monkeypatch, "lag", "Africa/Lagos", datetime(2030, 6, 1, 8, 30, tzinfo=timezone.utc))

    assert sent == 1 and STARTED not in text and "times in WAT" in text


def test_pick_line_carries_the_state():
    snap = _snapshot()
    line = pick_line_from_record(snap.top_picks[0], "UTC", None, KICK + timedelta(minutes=5))
    assert line.state == "started"
