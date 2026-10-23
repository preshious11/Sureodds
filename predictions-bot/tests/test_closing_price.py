"""Part 3 (2026-10-23): closing-price capture for published Top/Daily picks."""
from __future__ import annotations

import json
from datetime import timedelta
from unittest.mock import Mock

import pytest

from predbot.config import Config
from predbot.jobs import closing_price
from predbot.providers.budget import Budget
from predbot.providers.oddsapi import OddsApiProvider
from predbot.storage.db import Database
from predbot.util.timeutil import iso, parse_iso
from tests.conftest import dt
from tests.test_live_results import pick as _pick

START = parse_iso("2030-06-01T19:00:00Z")


def pick(db, **changes):
    row = dict(canonical_competition_id="epl", canonical_event_id="event:epl-1",
              start_utc=iso(START), market="result3", selection="home", price=1.9, prob=0.55, ev=0.02)
    row.update(changes)
    return _pick(db, **row)


def oddsapi_row(home="Chelsea", away="Arsenal", start=START, event_id="oa-1",
                bookmakers=None):
    bookmakers = bookmakers if bookmakers is not None else [
        {"key": "pinnacle", "markets": [{"key": "h2h", "outcomes": [
            {"name": home, "price": 1.80}, {"name": away, "price": 4.20}, {"name": "Draw", "price": 3.60}]}]},
        {"key": "bet365", "markets": [{"key": "h2h", "outcomes": [
            {"name": home, "price": 1.95}, {"name": away, "price": 3.90}, {"name": "Draw", "price": 3.50}]}]},
    ]
    return {"id": event_id, "commence_time": iso(start), "home_team": home, "away_team": away,
           "sport_title": "EPL", "bookmakers": bookmakers}


@pytest.fixture
def db():
    d = Database(":memory:")
    yield d
    d.close()


@pytest.fixture
def oddsapi():
    budget = Budget(Database(":memory:"))
    return OddsApiProvider("test-key", budget, regions=["eu"])


def cfg(**kw):
    kw.setdefault("closing_price_enabled", True)
    return Config(sharp_books=["pinnacle"], **kw)


def test_closing_price_capture_defaults_to_off(monkeypatch):
    monkeypatch.delenv("CLOSING_PRICE_ENABLED", raising=False)
    assert Config().closing_price_enabled is False


def run_job(db, c, oddsapi_provider, fetch_return, now=None):
    oddsapi_provider._get = Mock(return_value=fetch_return)
    return closing_price.run(db, c, now=now or (START - timedelta(minutes=5)),
                             providers={"oddsapi": oddsapi_provider})


# ------------------------------------------------------------- the core path
def test_captures_closing_price_and_clv_and_sharp_fair_within_window(db, oddsapi):
    pid = pick(db)
    counts = run_job(db, cfg(), oddsapi, [oddsapi_row()])
    assert counts["captured"] == 1 and counts["requests"] == 1
    row = db.query("SELECT * FROM picks WHERE id=?", (pid,))[0]
    assert row["closing_price"] is not None and row["clv"] is not None
    assert row["closing_fair_prob"] is not None     # pinnacle quoted every selection -> real sharp fair
    assert parse_iso(row["closing_captured_at"]) == START - timedelta(minutes=5)


def test_a_capture_leaves_sources_byte_identical(db, oddsapi):
    pid = pick(db)
    before = db.query("SELECT sources FROM picks WHERE id=?", (pid,))[0]["sources"]
    assert run_job(db, cfg(), oddsapi, [oddsapi_row()])["captured"] == 1
    assert db.query("SELECT sources FROM picks WHERE id=?", (pid,))[0]["sources"] == before


def test_closing_price_is_derived_the_same_way_as_the_published_price(db, oddsapi):
    """Sharp (pinnacle) + soft (bet365) both quote this line: the PUBLISHED price convention
    (predbot/model/ensemble.py's _soft_prices) re-derives the available price from the soft book
    alone - here, with exactly one soft quote, that is bet365's own price, not a blend with pinnacle."""
    pid = pick(db)
    counts = run_job(db, cfg(), oddsapi, [oddsapi_row()])
    assert counts["captured"] == 1
    row = db.query("SELECT closing_price FROM picks WHERE id=?", (pid,))[0]
    assert row["closing_price"] == pytest.approx(1.95)        # bet365's own home price, never pinnacle's


def test_clv_uses_the_existing_stats_clv_function(db, oddsapi):
    from predbot.jobs import stats as stats_mod
    pid = pick(db, price=1.9)
    run_job(db, cfg(), oddsapi, [oddsapi_row()])
    row = db.query("SELECT price, closing_price, clv FROM picks WHERE id=?", (pid,))[0]
    assert row["clv"] == pytest.approx(stats_mod.clv(row["price"], row["closing_price"]))


def test_only_top_daily_captured_never_all(db, oddsapi):
    all_only = pick(db, tier="all", is_representative=1)
    counts = run_job(db, cfg(), oddsapi, [oddsapi_row()])
    assert counts["captured"] == 0           # nothing due: the 'all'-only row is never published
    row = db.query("SELECT closing_price FROM picks WHERE id=?", (all_only,))[0]
    assert row["closing_price"] is None


def test_grouping_one_request_covers_every_pick_sharing_a_league(db, oddsapi):
    p1 = pick(db, provider_event_id="e1", home="Chelsea", away="Arsenal", market="result3", selection="home")
    p2 = pick(db, provider_event_id="e2", canonical_event_id="event:epl-2", home="Other Home",
             away="Other Away", market="total", selection="over", line=2.5,
             start_utc=iso(START + timedelta(minutes=2)))
    rows = [oddsapi_row(), oddsapi_row(home="Other Home", away="Other Away", event_id="oa-2",
                        start=START + timedelta(minutes=2), bookmakers=[
                            {"key": "pinnacle", "markets": [{"key": "totals", "outcomes": [
                                {"name": "Over", "point": 2.5, "price": 1.85},
                                {"name": "Under", "point": 2.5, "price": 2.00}]}]},
                            {"key": "bet365", "markets": [{"key": "totals", "outcomes": [
                                {"name": "Over", "point": 2.5, "price": 1.90},
                                {"name": "Under", "point": 2.5, "price": 1.95}]}]},
                        ])]
    counts = run_job(db, cfg(), oddsapi, rows)
    assert counts["requests"] == 1           # ONE fetch covered both picks (same league)
    assert counts["captured"] == 2
    for pid in (p1, p2):
        assert db.query("SELECT closing_price FROM picks WHERE id=?", (pid,))[0]["closing_price"] is not None


# ------------------------------------------------------------- idempotency / never overwrites
def test_idempotent_never_recaptured_once_set(db, oddsapi):
    pid = pick(db)
    run_job(db, cfg(), oddsapi, [oddsapi_row()])
    first = dict(db.query("SELECT * FROM picks WHERE id=?", (pid,))[0])
    oddsapi._get = Mock(return_value=[oddsapi_row(bookmakers=[
        {"key": "bet365", "markets": [{"key": "h2h", "outcomes": [
            {"name": "Chelsea", "price": 9.99}, {"name": "Arsenal", "price": 1.01},
            {"name": "Draw", "price": 1.01}]}]}])])
    counts = closing_price.run(db, cfg(), now=START - timedelta(minutes=4),
                               providers={"oddsapi": oddsapi})
    assert counts["captured"] == 0            # nothing was "due" - closing_price already set
    second = dict(db.query("SELECT * FROM picks WHERE id=?", (pid,))[0])
    assert second == first                    # byte-identical: no re-snapshot, no drift


def test_never_touches_price_or_other_published_fields(db, oddsapi):
    pid = pick(db, price=1.9, prob=0.55, ev=0.02)
    before = dict(db.query("SELECT * FROM picks WHERE id=?", (pid,))[0])
    run_job(db, cfg(), oddsapi, [oddsapi_row()])
    after = dict(db.query("SELECT * FROM picks WHERE id=?", (pid,))[0])
    changed = {k for k in before if before[k] != after[k]}
    assert changed <= {"closing_price", "clv", "closing_fair_prob", "closing_captured_at"}
    assert before["price"] == after["price"] == 1.9
    assert before["prob"] == after["prob"] and before["ev"] == after["ev"]
    assert before["status"] == after["status"]


# ------------------------------------------------------------- edge cases
def test_pick_already_started_is_never_captured(db, oddsapi):
    pid = pick(db, start_utc=iso(START))
    counts = run_job(db, cfg(), oddsapi, [oddsapi_row()], now=START + timedelta(minutes=1))
    assert counts["captured"] == 0
    assert oddsapi._get.call_count == 0        # already started: due_for_closing_price excludes it entirely
    row = db.query("SELECT closing_price FROM picks WHERE id=?", (pid,))[0]
    assert row["closing_price"] is None


def test_kickoff_moved_since_publication_is_not_captured(db, oddsapi):
    pid = pick(db, start_utc=iso(START))
    moved = START + timedelta(minutes=45)
    counts = run_job(db, cfg(), oddsapi, [oddsapi_row(start=moved)])
    assert counts["kickoff_moved"] == 1 and counts["captured"] == 0
    row = db.query("SELECT closing_price FROM picks WHERE id=?", (pid,))[0]
    assert row["closing_price"] is None


def test_odds_no_longer_quoted_leaves_pick_uncaptured(db, oddsapi):
    pid = pick(db, market="handicap", selection="home", line=-1.5)
    # the fetched event has NO spreads market at all - the line this pick needs was pulled
    counts = run_job(db, cfg(), oddsapi, [oddsapi_row(bookmakers=[
        {"key": "pinnacle", "markets": [{"key": "h2h", "outcomes": [
            {"name": "Chelsea", "price": 1.8}, {"name": "Arsenal", "price": 4.2},
            {"name": "Draw", "price": 3.6}]}]}])])
    assert counts["no_quote"] == 1 and counts["captured"] == 0
    row = db.query("SELECT closing_price FROM picks WHERE id=?", (pid,))[0]
    assert row["closing_price"] is None


def test_no_sharp_book_quote_stores_clv_against_price_only(db, oddsapi):
    """No configured sharp book quotes this match at all: closing_price/clv are still captured (priced
    off the ordinary book consensus), but sharp_fair in the notes stays None - never fabricated."""
    pid = pick(db)
    rows = [oddsapi_row(bookmakers=[
        {"key": "bet365", "markets": [{"key": "h2h", "outcomes": [
            {"name": "Chelsea", "price": 1.9}, {"name": "Arsenal", "price": 4.0},
            {"name": "Draw", "price": 3.5}]}]},
        {"key": "williamhill", "markets": [{"key": "h2h", "outcomes": [
            {"name": "Chelsea", "price": 1.85}, {"name": "Arsenal", "price": 4.1},
            {"name": "Draw", "price": 3.55}]}]},
    ])]
    counts = run_job(db, cfg(), oddsapi, rows)
    assert counts["captured"] == 1
    row = db.query("SELECT closing_price, clv, closing_fair_prob FROM picks WHERE id=?", (pid,))[0]
    assert row["closing_price"] is not None and row["clv"] is not None
    assert row["closing_fair_prob"] is None


def test_unsupported_sport_never_calls_the_provider(db, oddsapi):
    pid = pick(db, sport="basketball", canonical_competition_id="nba", market="result2")
    oddsapi._get = Mock(return_value=[])
    counts = closing_price.run(db, cfg(), now=START - timedelta(minutes=5), providers={"oddsapi": oddsapi})
    assert counts["unsupported_sport"] == 1
    oddsapi._get.assert_not_called()
    row = db.query("SELECT closing_price FROM picks WHERE id=?", (pid,))[0]
    assert row["closing_price"] is None


def test_disabled_by_config_is_a_total_noop(db, oddsapi):
    pick(db)
    oddsapi._get = Mock(return_value=[oddsapi_row()])
    counts = closing_price.run(db, cfg(closing_price_enabled=False), now=START - timedelta(minutes=5),
                               providers={"oddsapi": oddsapi})
    assert counts.get("skipped") == 1
    oddsapi._get.assert_not_called()


def test_daily_request_cap_stops_further_groups(db, oddsapi):
    pick(db, provider_event_id="e1", home="Chelsea", away="Arsenal", canonical_competition_id="epl")
    pick(db, provider_event_id="e2", canonical_event_id="event:laliga-1", home="La Liga Home",
        away="La Liga Away", canonical_competition_id="la_liga", start_utc=iso(START + timedelta(minutes=1)))
    c = cfg(closing_price_max_requests_per_day=1)
    oddsapi._get = Mock(side_effect=lambda path, params: (
        [oddsapi_row()] if "soccer_epl" in path else
        [oddsapi_row(home="La Liga Home", away="La Liga Away", event_id="oa-la")]))
    counts = closing_price.run(db, c, now=START - timedelta(minutes=5), providers={"oddsapi": oddsapi})
    assert counts["requests"] == 1
    assert counts["skipped_cap"] == 1


def test_no_provider_configured_is_safe_and_captures_nothing(db):
    pick(db)
    counts = closing_price.run(db, cfg(), now=START - timedelta(minutes=5), providers={})
    assert counts["captured"] == 0
