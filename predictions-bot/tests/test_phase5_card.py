"""Phase 5: PRICE_MODE (median / best), readable stats and league names, 'odds as of'."""
from __future__ import annotations

import logging
import os

import pytest

from predbot.bot import broadcast, pagination
from predbot.bot.format import odds_as_of_label, odds_note, pick_line_from_record, pick_lines
from predbot.cache.models import DailySnapshot, PickRecord
from predbot.config import Config
from predbot.markets.catalog import pretty_league, readable_stat, selection_label, short_selection
from predbot.providers.base import Event, MarketLine, apply_price_mode
from predbot.providers.chain import merge_events
from predbot.providers.oddsapi import OddsApiProvider
from predbot.providers.sharpapi import SharpApiProvider
from tests.conftest import dt, load
from tests.test_subscribers_broadcast import _snapshot


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    monkeypatch.setattr(os, "environ", os.environ.copy())
    monkeypatch.delenv("PRICE_MODE", raising=False)


# ============================================================== 5a price mode
def _line(prices, bookmaker="bet365"):
    return MarketLine("result3", "home", max(prices), bookmaker=bookmaker, n_books=len(prices), book_prices=list(prices))


def test_the_median_is_the_default_and_best_keeps_the_highest_price():
    assert Config().price_mode == "median"
    m = _line([2.0, 2.1, 2.2, 2.3, 3.5])
    apply_price_mode(m, "median")
    assert m.price == 2.2 and m.bookmaker == "median" and m.n_books == 5
    b = _line([2.0, 2.1, 2.2, 2.3, 3.5])
    apply_price_mode(b, "best")
    assert b.price == 3.5 and b.bookmaker == "bet365"


def test_an_even_number_of_books_averages_the_middle_two():
    m = _line([2.0, 2.4])
    apply_price_mode(m, "median")
    assert m.price == 2.2


def test_a_single_book_keeps_its_price_and_its_name():
    m = _line([2.5], "unibet")
    apply_price_mode(m, "median")
    assert m.price == 2.5 and m.bookmaker == "unibet"


def test_an_unknown_price_mode_falls_back_to_median(monkeypatch, caplog):
    monkeypatch.setenv("PRICE_MODE", "cheapest")
    with caplog.at_level(logging.WARNING):
        assert Config().price_mode == "median"
    assert any("PRICE_MODE" in r.message for r in caplog.records)


def test_sharpapi_prices_a_line_at_the_median_or_the_best_of_its_books(budget):
    rows = load("sharp_odds.json")["data"]
    for mode in ("median", "best"):
        p = SharpApiProvider("k", budget, ["nba"], price_mode=mode)
        events = p._rows_to_events(rows, dt(hh=0).replace(year=2029), dt(hh=0).replace(year=2031))
        multi = [m for e in events for m in e.markets if len(m.book_prices) > 1]
        assert multi
        for m in multi:
            if mode == "median":
                assert min(m.book_prices) <= m.price <= max(m.book_prices) and m.bookmaker == "median"
            else:
                assert m.price == max(m.book_prices) and m.bookmaker != "median"


def test_oddsapi_prices_a_line_at_the_median_of_its_books(db):
    from predbot.providers.budget import Budget
    row = {"id": "e1", "sport_key": "soccer_epl", "sport_title": "EPL", "commence_time": "2030-06-01T19:00:00Z",
           "home_team": "Arsenal", "away_team": "Chelsea",
           "bookmakers": [{"key": k, "title": k, "markets": [{"key": "h2h", "outcomes": [
               {"name": "Arsenal", "price": price}, {"name": "Chelsea", "price": 4.0}, {"name": "Draw", "price": 3.5}]}]}
               for k, price in (("a", 1.8), ("b", 1.9), ("c", 2.0), ("d", 2.1), ("e", 3.0))]}
    for mode, expected in (("median", 2.0), ("best", 3.0)):
        p = OddsApiProvider("k", Budget(db), price_mode=mode)
        (e,) = p._rows_to_events([("football", row)], dt(hh=12), dt(hh=23))
        home = next(m for m in e.markets if m.market == "result3" and m.selection == "home")
        assert home.price == expected and home.n_books == 5


def _event(provider, prices, odd_id):
    ml = MarketLine("result3", "home", max(prices), bookmaker=provider, n_books=len(prices),
                    provider_odd_id=odd_id, book_prices=list(prices))
    return Event(provider=provider, provider_event_id=provider[0], sport="football", league="L", home="Arsenal",
                 away="Chelsea", start=dt(hh=20), markets=[ml], sources=[provider])


def test_merging_providers_reprices_from_all_books_and_keeps_the_best_books_odd_id():
    a = _event("oddsapi", [2.0, 2.1, 2.2], "oa-id")
    b = _event("sharpapi", [2.0, 2.6], "sh-id")

    (merged,) = merge_events([a], [b])
    m = merged.markets[0]

    assert m.n_books == 5 and m.price == 2.1 and m.bookmaker == "median"
    assert m.provider_odd_id == "sh-id"                     # the best price (2.6) came from sharpapi

    a2, b2 = _event("oddsapi", [2.0, 2.1, 2.2], "oa-id"), _event("sharpapi", [2.0, 2.6], "sh-id")
    (best,) = merge_events([a2], [b2], "best")
    assert best.markets[0].price == 2.6 and best.markets[0].bookmaker == "sharpapi"


def _record(**kw):
    base = dict(sport="football", league="England • Premier League", home="Arsenal", away="Chelsea",
                kickoff_utc="2030-06-01T18:00:00Z", label="Arsenal to Win", market="result3", selection="home",
                price=2.10, prob=0.6)
    base.update(kw)
    return PickRecord(**base)


def test_the_card_says_where_the_price_comes_from():
    median = pick_lines(pick_line_from_record(_record(bookmaker="median", n_books=6), "Africa/Lagos"))
    assert "Odds: 2.10 (median of 6 books)" in median
    best = pick_lines(pick_line_from_record(_record(price=2.35, bookmaker="bet365", n_books=6), "Africa/Lagos"))
    assert "Odds: 2.35 (Bet365)" in best
    plain = pick_lines(pick_line_from_record(_record(bookmaker="", n_books=1), "Africa/Lagos"))
    assert "Odds: 2.10" in plain


def test_odds_note_edge_cases():
    assert odds_note("median", 1) == "" and odds_note("", 3) == ""
    assert odds_note("consensus", 4) == " (consensus of 4 books)"
    assert odds_note("william_hill", 2) == " (William hill)"
    assert odds_note("Bet365", 2) == " (Bet365)"


# ================================================================= 5b readable words
def test_stat_words_never_contain_underscores_or_camel_case():
    assert readable_stat("pitching_strikeouts") == "Pitching Strikeouts"
    assert readable_stat("totalBases") == "Total Bases"
    for stat in ("pitching_strikeouts", "batting_hits", "totalBases"):
        ml = MarketLine("total", "over", 1.9, stat=stat, line=2.5)
        for text in (selection_label(ml, "H", "A", "baseball"), short_selection(ml, "H", "A", "baseball")):
            assert "_" not in text and text, (stat, text)


@pytest.mark.parametrize("slug,expected", [
    ("england_-_premier_league", "England • Premier League"),
    ("spain_-_la_liga", "Spain • La Liga"),
    ("italy_-_serie_a", "Italy • Serie A"),
    ("france_-_ligue_1", "France • Ligue 1"),
    ("uefa_-_champions_league", "UEFA • Champions League"),
    ("nigeria_-_premier_league", "Nigeria • Premier League"),
    ("nba", "NBA"), ("atp", "ATP"), ("euroleague", "Euroleague"), ("", ""),
])
def test_pretty_league(slug, expected):
    assert pretty_league(slug) == expected


def test_pretty_league_prefers_the_providers_country():
    assert pretty_league("england_-_premier_league", "Wales") == "Wales • Premier League"
    assert pretty_league("nba", "USA") == "USA • NBA"


def test_sharpapi_events_carry_a_pretty_league_not_a_shouted_slug(budget):
    p = SharpApiProvider("k", budget, ["england_-_premier_league"])
    rows = load("sharp_odds.json")["data"]
    events = p._rows_to_events(rows, dt().replace(year=2029), dt().replace(year=2031))
    assert events and all("_" not in e.league and e.league != e.league.upper() or len(e.league) <= 4 for e in events)
    assert all(e.league_id for e in events)                         # the slug is still kept for lookups


# ================================================================== 5c odds as of
def test_odds_as_of_sits_next_to_the_combined_odds_and_generated_stays():
    card = broadcast.picks_text(_snapshot(), "Africa/Lagos", now=dt(hh=0))
    combined = [ln for ln in card.splitlines() if ln.startswith("Combined Odds:")]
    assert len(combined) == 2 and all("odds as of" in ln for ln in combined)
    assert "<i>Generated " in card


def test_the_views_show_odds_as_of_too():
    text, _ = pagination.render_page(_snapshot(), "daily", 0, 5, "Africa/Lagos")
    assert "odds as of" in [ln for ln in text.splitlines() if ln.startswith("Combined Odds:")][0]
    assert "<i>Generated " in text


def test_odds_as_of_label():
    assert odds_as_of_label("2030-06-01T09:04:00Z", "Africa/Lagos") == "odds as of 10:04 AM WAT"
    assert odds_as_of_label("", "Africa/Lagos") == "" and odds_as_of_label(None, "UTC") == ""
