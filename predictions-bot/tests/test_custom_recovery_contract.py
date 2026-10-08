"""Recovery-button contract: a button is a promise, and a tap delivers exactly that promise or nothing.

* "N ... Picks" -> the prepared ticket has exactly N unique, upcoming legs; never fewer, at creation or at the tap;
* "X Odds" -> the prepared ticket's combined odds are within the configured tolerance of X;
* "Safe" -> every leg clears the safe probability floor;
* a tap serves the prepared ticket unchanged; if any leg is no longer valid the whole option is withdrawn and the user
  is told - it is never re-searched looser, shortened, or swapped for another ticket.

Reproduces the live report: a failed "safe 50 odds" request offered "🛡️ 3 Safe Picks · All Sports" whose prepared
ticket held one selection @1.22 (the uncounted search returned "up to 3"). Archive-only: no provider is reachable."""
from __future__ import annotations

import re
from datetime import timedelta
from unittest.mock import Mock

import pytest
import requests

from predbot import custom_picks as cp
from predbot.custom_recovery import CALLBACK_PREFIX, COPY, claimed_count, valid_ticket
from predbot.jobs import daily_picks
from predbot.providers.chain import ProviderChain
from predbot.providers.oddsapi import OddsApiProvider
from predbot.providers.sharpapi import SharpApiProvider
from predbot.providers.sportsgameodds import SportsGameOddsProvider
from tests.test_custom_picks import NOW, ZONE, private_bot, rows_for, store  # noqa: F401 (fixture)
from tests.test_custom_recovery import buttons, callback, request

COUNT = re.compile(r"(\d+) [^·]*Picks")
ODDS = re.compile(r"(\d+(?:\.\d+)?) Odds")


@pytest.fixture(autouse=True)
def no_providers(monkeypatch):
    forbidden = Mock(side_effect=AssertionError("Custom Picks reached a provider or the model"))
    for obj, name in ((ProviderChain, "collect"), (daily_picks, "build_picks"), (OddsApiProvider, "fetch_events"),
                      (SharpApiProvider, "fetch_events"), (SportsGameOddsProvider, "fetch_events"),
                      (SportsGameOddsProvider, "_call"), (requests.Session, "request")):
        monkeypatch.setattr(obj, name, forbidden)
    yield forbidden
    forbidden.assert_not_called()


def legs_of(text):
    """Selections in a rendered ticket: the header block, then one block per pick."""
    return len(text.split("\n\n")) - 1 if text.startswith("🔥 ") else 0


def prepared(loop, button):
    return loop._custom_picks.recovery.ready[button["callback_data"][len(CALLBACK_PREFIX):]]


def tap(loop, button):
    loop._dispatch_callback(callback(button["callback_data"]))
    return loop.tg.sends[-1]["text"]


def assert_truthful(loop, sent):
    """Every offered button keeps the promise its label makes."""
    for button in buttons(sent):
        s = prepared(loop, button)
        picks = s.ticket.picks
        assert len({c.fixture_id for c in picks}) == len(picks)                        # one leg per fixture
        assert all(c.start > NOW for c in picks)
        assert all(c.probability >= cp.MIN_PROBABILITY[s.request.preference] for c in picks)
        if "Safe" in button["text"]:
            assert all(c.probability >= cp.MIN_PROBABILITY["safe"] for c in picks)
        n = COUNT.search(button["text"])
        if n:
            assert len(picks) == int(n.group(1)), button["text"]
            assert claimed_count(s.request) == int(n.group(1))
        x = ODDS.search(button["text"])
        if x:
            target = float(x.group(1))
            assert target * (1 - cp.TOLERANCE) <= s.ticket.odds <= target * (1 + cp.TOLERANCE), button["text"]
        assert valid_ticket(s.ticket, s.request, NOW, ZONE)


def test_reported_failure_no_three_safe_picks_button_over_a_one_leg_ticket(private_bot, db):
    # one fixture clears the safe floor, two do not: "3 Safe Picks" cannot be honoured
    store(db, rows_for(1, price=1.22, probability=.82) + rows_for(2, price=1.5, probability=.60)
          + rows_for(3, price=1.7, probability=.58))
    sent = request(private_bot, "safe 50 odds")
    assert not any("3 Safe Picks" in b["text"] for b in buttons(sent))
    assert_truthful(private_bot, sent)
    for button in buttons(sent):
        n = COUNT.search(button["text"])
        assert legs_of(tap(private_bot, button)) == (int(n.group(1)) if n else legs_of(private_bot.tg.sends[-1]["text"]))


def test_insufficient_safe_candidates_never_get_a_three_safe_picks_button(private_bot, db):
    store(db, rows_for(1, price=1.3, probability=.80))                             # one safe fixture only
    sent = request(private_bot, "safe tennis picks")                              # sport missing -> recovery
    assert not any("3 Safe Picks" in b["text"] for b in buttons(sent))
    assert_truthful(private_bot, sent)


def test_three_safe_picks_button_returns_exactly_three_picks(private_bot, db):
    store(db, [r for i in range(1, 6) for r in rows_for(i, price=1.3, probability=.80)])
    sent = request(private_bot, "safe tennis picks")
    assert_truthful(private_bot, sent)
    button = next(b for b in buttons(sent) if "3 Safe Picks" in b["text"])
    s = prepared(private_bot, button)
    text = tap(private_bot, button)
    assert legs_of(text) == 3
    assert text == cp.format_ticket(s.ticket, s.partial, ZONE)                    # the exact prepared ticket


def test_a_leg_that_starts_after_the_button_withdraws_the_whole_option(private_bot, db, monkeypatch):
    early = NOW + timedelta(minutes=30)
    store(db, rows_for(1, price=1.3, probability=.80, start=early)
          + [r for i in range(2, 5) for r in rows_for(i, price=1.3, probability=.80)])
    sent = request(private_bot, "safe 50 odds")
    button = next(b for b in buttons(sent) if "3 Safe Picks" in b["text"])
    assert any(c.start == early for c in prepared(private_bot, button).ticket.picks)
    monkeypatch.setattr(cp, "utcnow", lambda: early)                              # that leg has now started
    text = tap(private_bot, button)
    assert legs_of(text) == 0                                                      # no 2- or 1-pick ticket
    assert text in (COPY["expired"], COPY["expired_empty"])


def test_a_tap_never_serves_fewer_picks_than_the_label_promised(private_bot, db):
    store(db, [r for i in range(1, 6) for r in rows_for(i, price=1.3, probability=.80)])
    sent = request(private_bot, "safe tennis picks")
    button = next(b for b in buttons(sent) if "3 Safe Picks" in b["text"])
    token = button["callback_data"][len(CALLBACK_PREFIX):]
    ready = private_bot._custom_picks.recovery.ready
    s = ready[token]
    shortened = cp.State(s.ticket.picks[:1], s.ticket.picks[0].price)
    from dataclasses import replace
    ready[token] = replace(s, ticket=shortened)                                    # however it got shorter
    text = tap(private_bot, button)
    assert legs_of(text) == 0 and text in (COPY["expired"], COPY["expired_empty"])


def test_target_odds_buttons_meet_the_tolerance_and_serve_that_ticket(private_bot, db):
    store(db, [r for i in range(1, 9) for r in rows_for(i, price=1.45 + i * 0.03, probability=.70)])
    sent = request(private_bot, "100 odds")
    assert_truthful(private_bot, sent)
    odds_buttons = [b for b in buttons(sent) if ODDS.search(b["text"])]
    assert odds_buttons
    for button in odds_buttons:
        s = prepared(private_bot, button)
        assert tap(private_bot, button) == cp.format_ticket(s.ticket, s.partial, ZONE)


def test_every_count_based_option_is_exact(private_bot, db):
    store(db, [r for i in range(1, 4) for r in rows_for(i, sport="football", price=1.3, probability=.80)]
          + [r for i in range(11, 13) for r in rows_for(i, sport="basketball", price=1.4, probability=.80)])
    for text in ("safe 50 odds", "7 picks", "baseball picks", "safe 4 basketball picks"):
        sent = request(private_bot, text)
        assert_truthful(private_bot, sent)
        for button in buttons(sent):
            n = COUNT.search(button["text"])
            served = tap(private_bot, button)
            if n:
                assert legs_of(served) == int(n.group(1)), (text, button["text"])


def test_claimed_count_matches_the_label():
    assert claimed_count(cp.Request(preference="safe")) == cp.DEFAULT_COUNT          # "3 Safe Picks · All Sports"
    assert claimed_count(cp.Request(pick_count=5)) == 5
    assert claimed_count(cp.Request(preference="best")) is None                       # "Best Across All Sports"
    assert claimed_count(cp.Request(preference="safe", sports=frozenset({"football"}))) is None
    assert claimed_count(cp.Request(preference="safe", target_odds=4.0)) is None
