"""Provider parsers: raw API payloads -> canonical Event / MarketLine / MatchResult."""
from __future__ import annotations

from datetime import datetime, timezone

from predbot.providers.sharpapi import SharpApiProvider
from predbot.providers.sportsgameodds import SportsGameOddsProvider

from tests.conftest import load

WIN_START = datetime(2029, 1, 1, tzinfo=timezone.utc)
WIN_END = datetime(2031, 12, 31, tzinfo=timezone.utc)


# ----------------------------------------------------------------- SportsGameOdds
def test_sgo_parse_event(budget):
    p = SportsGameOddsProvider("k", budget, ["SOCCER"], [])
    e = p._parse_event(load("sgo_event.json"))
    assert e is not None
    assert e.sport == "football"
    assert e.home == "Angers" and e.away == "Nantes"
    assert e.markets, "expected at least one parsed market"
    # the 3-way home line should be present as result3/home
    r3 = [m for m in e.markets if m.market == "result3" and m.selection == "home"]
    assert r3 and r3[0].price > 1.0


def test_sgo_football_ml_is_dnb(budget):
    p = SportsGameOddsProvider("k", budget, ["SOCCER"], [])
    e = p._parse_event(load("sgo_event.json"))
    # 2-way moneyline in football is Draw No Bet
    for m in e.markets:
        if m.market == "result2":
            assert False, "football 2-way ml should map to dnb, not result2"


def test_sgo_result_final(budget):
    p = SportsGameOddsProvider("k", budget, ["SOCCER"], [])
    r = p._parse_result(load("sgo_event_final.json"))
    assert r.status == "final"
    # reg mapped to full for football
    assert r.score("full") == (2.0, 1.0)
    assert r.score("1h") == (1.0, 1.0)
    assert r.stat("corners", "full") == (6.0, 3.0)
    # per-odd settled score captured
    assert r.odd_scores.get("points-all-reg-ou-over") == 3.0


# ----------------------------------------------------------------------- SharpAPI
def test_sharp_rows_to_events(budget):
    p = SharpApiProvider("k", budget, ["nba"])
    rows = load("sharp_odds.json")["data"]
    events = p._rows_to_events(rows, WIN_START, WIN_END)
    assert events, "expected at least one parsed event"
    nba = next(e for e in events if e.sport == "basketball")
    assert nba.home == "PHI 76ers" and nba.away == "PHO Suns"
    # basketball moneyline is 2-way
    assert [m for m in nba.markets if m.market == "result2"], "expected a 2-way moneyline"


def test_sharp_soccer_moneyline_is_3way(budget):
    p = SharpApiProvider("k", budget, ["epl"])
    rows = load("sharp_odds.json")["data"]
    events = p._rows_to_events(rows, WIN_START, WIN_END)
    soccer = [e for e in events if e.sport == "football"]
    if soccer:                       # soccer moneyline should be 3-way (draw loses)
        for e in soccer:
            assert not any(m.market == "result2" for m in e.markets)


def test_sharp_skips_player_props(budget):
    p = SharpApiProvider("k", budget, ["nba"])
    rows = load("sharp_odds.json")["data"]
    events = p._rows_to_events(rows, WIN_START, WIN_END)
    for e in events:
        for m in e.markets:
            assert not m.is_player_prop


def test_sharp_best_price_across_books(budget):
    p = SharpApiProvider("k", budget, ["nba"], price_mode="best")
    rows = load("sharp_odds.json")["data"]
    events = p._rows_to_events(rows, WIN_START, WIN_END)
    e = events[0]
    for m in e.markets:
        # book_prices holds every seen price; PRICE_MODE=best keeps the max of them
        assert m.price == max(m.book_prices)


