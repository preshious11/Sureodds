"""Prediction-quality rework: evidence-based ranking, sharp reference, book coverage, market quality, the 10-12 odds
Daily, honest confidence and accuracy tracking.

These tests verify the IMPLEMENTATION. They say nothing about whether live hit rate improves - that needs settled,
out-of-sample picks (see ``cli.py accuracy``)."""
from __future__ import annotations

import math
import random
import statistics

import pytest

from predbot.config import Config
from predbot.jobs import daily_picks
from predbot.jobs import stats as stats_mod
from predbot.model.calibration import Calibrator
from predbot.model.devig import devig, shin_devig
from predbot.model.elo import Elo
from predbot.model.ensemble import EventModel, Scored
from predbot.providers.base import Event, MarketLine, norm_book
from predbot.providers.chain import ProviderChain, merge_events
from predbot.providers.oddsapi import OddsApiProvider
from predbot.selection import evidence as evmod
from predbot.selection.picker import PickSet, select
from predbot.util.http import ProviderError
from tests.conftest import dt
from tests.test_picker import _fx, ev, sc


def books(n, price=1.5):
    """n DIFFERENT bookmakers quoting about the same price (what 'well covered' looks like)."""
    return {f"book{i}": round(price + 0.004 * i, 4) for i in range(n)}


def line(market="result3", selection="home", price=1.5, *, quotes=None, n=3, **kw):
    ml = MarketLine(market, selection, price, n_books=n, **kw)
    if quotes is not None:
        ml.book_quotes = dict(quotes)
        ml.book_prices = list(quotes.values())
        ml.n_books = len(quotes)
    return ml


def cand(event, market="result3", selection="home", p=0.70, price=1.5, *, quotes=None, books_n=5,
         p_fair=None, edge_ev=None, edge=0.0, sources=("market", "score_model"), independent=(), **kw):
    ml = line(market, selection, price, quotes=quotes if quotes is not None else books(books_n, price), **kw)
    return Scored(event=event, line=ml, p_market=p - 0.01, p_model=p, sources={k: p for k in sources},
                  agreement=1.0, ev=p * price - 1, edge=edge, label=f"{market}:{selection}", liquidity=1.0,
                  p_fair=p_fair, edge_ev=edge_ev, independent=list(independent))


# =========================================================================== config: one authoritative threshold
def test_daily_target_is_10_to_12_odds():
    c = Config()
    assert (c.daily_odds_min, c.daily_odds_max) == (10.0, 12.0)


def test_top_target_is_unchanged():
    c = Config()
    assert (c.top_odds_target, c.top_odds_max, c.top_legs_min, c.top_legs_max) == (2.00, 2.50, 1, 3)


def test_the_top_is_not_weakened_to_reach_its_target_by_default():
    assert Config().top_relax_prob == 0.0           # the per-leg probability floor never drops by default


def test_daily_probability_floor_is_one_consistent_value():
    c = Config()
    assert c.daily_min_prob >= 0.5                  # no 30% candidate can be a Daily leg
    # the picker's own rejection note names the configured value - there is no second, hard-coded number
    pool = [cand(_fx(0), p=c.daily_min_prob - 0.01, price=1.8)]
    trace = {}
    ps = select(pool, c, dt(), trace=trace)
    assert ps.daily == []
    assert f"DAILY_MIN_PROB ({c.daily_min_prob:.3f} < {c.daily_min_prob})" in trace[id(pool[0])] or \
        "DAILY_MIN_PROB" in trace[id(pool[0])]


def test_a_sub_floor_leg_is_never_admitted_to_fill_a_ticket():
    """No second, laxer pass: 30 legs at 0.45-0.54 plus 3 strong ones -> only the strong ones are posted."""
    c = Config()
    weak = [cand(_fx(i), p=0.45 + 0.003 * i, price=1.9) for i in range(30)]
    strong = [cand(_fx(100 + i), p=0.72, price=1.35, selection="home" if i % 2 else "away") for i in range(3)]
    ps = select(weak + strong, c, dt())
    assert all(s.p_model >= c.daily_min_prob for s in ps.all())
    assert math.prod(s.price for s in ps.daily) < c.daily_odds_min          # short of 10, and posted short


def test_misconfigured_daily_floor_is_flagged(caplog):
    import logging
    with caplog.at_level(logging.WARNING):
        Config(daily_min_prob=0.30)
    assert any("DAILY_MIN_PROB" in r.message for r in caplog.records)


# =========================================================================== Daily sizing (10-12, never forced)
def _daily_pool(price, n=40, p=0.70):
    return [cand(_fx(i), selection="home" if i % 2 == 0 else "away", p=p, price=price) for i in range(n)]


@pytest.mark.parametrize("price", [1.25, 1.30, 1.35, 1.40, 1.45, 1.50, 1.60, 1.80])
def test_daily_never_exceeds_12(price):
    ps = select(_daily_pool(price), Config(), dt())
    assert math.prod(s.price for s in ps.daily) <= 12.0 + 1e-9


def test_daily_lands_inside_the_band_when_the_slate_allows():
    ps = select(_daily_pool(1.35), Config(), dt())
    ticket = math.prod(s.price for s in ps.daily)
    assert 10.0 <= ticket <= 12.0
    assert ps.diagnostics["daily_target_reached"] is True


def test_daily_stops_as_soon_as_the_target_is_reached():
    ps = select(_daily_pool(1.35), Config(), dt())
    assert len(ps.daily) == 8 and math.prod(s.price for s in ps.daily) * 1.35 > 12.0


def test_daily_returns_below_10_when_no_further_qualified_pick_exists():
    pool = [cand(_fx(i), selection="home" if i % 2 else "away", p=0.70, price=1.7) for i in range(4)]
    ps = select(pool, Config(), dt())
    ticket = math.prod(s.price for s in ps.daily)
    assert 0 < len(ps.daily) and ticket < 10.0
    assert ps.diagnostics["daily_target_reached"] is False


def test_no_fixed_leg_count_is_required():
    short = select([cand(_fx(i), selection="home" if i % 2 else "away", price=2.4, p=0.62) for i in range(8)], Config(), dt())
    longer = select(_daily_pool(1.35), Config(), dt())
    assert len(short.daily) != len(longer.daily)            # sized by odds, not by a count


def test_ticket_maths_and_diagnostics_agree():
    ps = select(_daily_pool(1.35), Config(), dt())
    assert ps.diagnostics["daily_own_odds"] == pytest.approx(math.prod(s.price for s in ps.daily), abs=0.01)
    assert ps.diagnostics["daily_legs"] == len(ps.daily)


def test_one_selection_per_match_is_still_enforced():
    e = _fx(0)
    pool = [cand(e, "result3", "home", 0.72, 1.4), cand(e, "total", "over", 0.70, 1.5, line=2.5),
            cand(e, "dc", "home_draw", 0.80, 1.25), cand(e, "btts", "yes", 0.66, 1.7)]
    ps = select(pool, Config(), dt())
    assert sum(1 for s in ps.all() if s.event is e) <= 1


# =========================================================================== book coverage
def test_single_book_candidate_never_enters_the_daily():
    lone = cand(_fx(0), p=0.80, price=1.3, quotes={"onlybook": 1.3})
    trace = {}
    ps = select([lone], Config(), dt(), trace=trace)
    assert ps.daily == [] and "backed by 1 book" in trace[id(lone)]
    assert ps.diagnostics["daily_pool_rejections"]["INSUFFICIENT_BOOKS"] == 1


def test_two_books_are_enough_for_a_main_market_daily_leg():
    ok = cand(_fx(0), p=0.80, price=1.3, quotes={"a": 1.30, "b": 1.31})
    assert select([ok], Config(), dt()).daily == [ok]


def test_secondary_and_restricted_markets_need_more_books():
    e1, e2 = _fx(0), _fx(1)
    btts = cand(e1, "btts", "yes", 0.70, 1.5, quotes={"a": 1.5, "b": 1.5})                       # secondary: needs 3
    corners = cand(e2, "total", "over", 0.70, 1.5, quotes=books(3), line=9.5, stat="corners")   # restricted: needs 4
    ps = select([btts, corners], Config(), dt())
    assert ps.daily == []
    btts3 = cand(e1, "btts", "yes", 0.70, 1.5, quotes=books(3))
    corners4 = cand(e2, "total", "over", 0.70, 1.5, quotes=books(4), line=9.5, stat="corners")
    assert {s.line.market for s in select([btts3, corners4], Config(), dt()).daily} == {"btts", "total"}


def test_top_needs_three_distinct_books():
    pool2 = [cand(_fx(i), p=0.72, price=1.45, quotes=books(2, 1.45)) for i in range(6)]
    assert select(pool2, Config(), dt()).top == []
    pool3 = [cand(_fx(i), p=0.72, price=1.45, quotes=books(3, 1.45)) for i in range(6)]
    assert len(select(pool3, Config(), dt()).top) == 2


def test_books_disagreeing_widely_are_rejected_as_stale():
    stale = cand(_fx(0), p=0.80, price=1.4, quotes={"a": 1.20, "b": 1.60, "c": 1.40})        # implied 0.83 vs 0.625
    assert evmod.assess(stale, Config()).dispersion > Config().max_book_dispersion
    trace = {}
    ps = select([stale], Config(), dt(), trace=trace)
    assert ps.daily == [] and ps.diagnostics["daily_pool_rejections"]["OUTLIER_BOOKS"] == 1


def test_coverage_affects_ranking_through_the_uncertainty():
    c = Config()
    thin = cand(_fx(0), p=0.70, quotes={"a": 1.50, "b": 1.60})
    wide = cand(_fx(1), p=0.70, quotes={"a": 1.50, "b": 1.501, "c": 1.502, "d": 1.503})
    assert evmod.assess(wide, c).se < evmod.assess(thin, c).se
    assert evmod.assess(wide, c).rank > evmod.assess(thin, c).rank
    lone = cand(_fx(2), p=0.70, quotes={"a": 1.5})
    assert evmod.assess(lone, c).se > evmod.assess(thin, c).se


def test_book_count_provider_count_and_dispersion_are_tracked():
    ml = line(quotes={"a": 1.50, "b": 1.60})
    ml.providers = ["sharpapi", "oddsapi"]
    assert (ml.distinct_books(), ml.provider_count()) == (2, 2)
    assert ml.price_dispersion() == pytest.approx(1 / 1.5 - 1 / 1.6)
    assert line(quotes={"a": 1.5}).price_dispersion() is None
    legacy = MarketLine("result3", "home", 1.5, n_books=4)                 # a cached row without quotes
    assert legacy.distinct_books() == 4 and legacy.provider_count() == 1


# =========================================================================== market quality
def _q(market, selection="home", **kw):
    e = _fx(0)
    s = cand(e, market, selection, **kw)
    return evmod.classify(s)


@pytest.mark.parametrize("market,sel,kw,expected", [
    ("result3", "home", {}, evmod.MAIN), ("result2", "away", {}, evmod.MAIN), ("dnb", "home", {}, evmod.MAIN),
    ("dc", "home_draw", {}, evmod.MAIN),
    ("btts", "yes", {}, evmod.SECONDARY),
    ("total", "over", {"line": 2.5, "entity": "home"}, evmod.SECONDARY),                    # team total
    ("total", "over", {"line": 9.5, "stat": "corners"}, evmod.RESTRICTED),
    ("total", "under", {"line": 4.5, "stat": "cards"}, evmod.RESTRICTED),
    ("total", "over", {"line": 1.5, "period": "1h"}, evmod.RESTRICTED),
    ("result3", "home", {"period": "1h"}, evmod.RESTRICTED),
    ("correct_score", "2-1", {}, evmod.RESTRICTED), ("odd_even", "odd", {}, evmod.RESTRICTED),
    ("prop", "over", {"line": 1.5}, evmod.RESTRICTED),
])
def test_market_quality_tiers(market, sel, kw, expected):
    assert _q(market, sel, **kw) == expected


def test_totals_are_graded_by_distance_from_the_main_line():
    e = _fx(0)

    def pair(l, p_over):
        return [MarketLine("total", "over", 1 / (p_over * 1.03), line=l, n_books=3),
                MarketLine("total", "under", 1 / ((1 - p_over) * 1.03), line=l, n_books=3)]
    e.markets = pair(2.5, 0.52) + pair(3.5, 0.30) + pair(5.5, 0.04)
    tier = {l: evmod.classify(cand(e, "total", "over", line=l)) for l in (2.5, 3.5, 5.5)}
    assert tier == {2.5: evmod.MAIN, 3.5: evmod.SECONDARY, 5.5: evmod.RESTRICTED}


def test_a_restricted_market_cannot_outrank_a_main_one_merely_on_probability():
    """First-half / corners legs are offered last: a 0.90 corners leg does not jump a 0.70 win."""
    c = Config(daily_odds_min=1.4, daily_odds_max=1.6)                    # one leg completes the ticket
    corners = cand(_fx(0), "total", "over", 0.90, 1.45, quotes=books(6, 1.45), line=9.5, stat="corners")
    win = cand(_fx(1), "result3", "home", 0.70, 1.45, quotes=books(6, 1.45))
    ps = select([corners, win], c, dt())
    assert [s.line.market for s in ps.daily] == ["result3"]


def test_the_top_is_main_markets_only():
    pool = [cand(_fx(i), "btts", "yes", 0.75, 1.45, quotes=books(6, 1.45)) for i in range(6)]
    assert select(pool, Config(), dt()).top == []


# =========================================================================== ranking: evidence, not raw probability
def test_negative_edge_vs_sharp_cannot_beat_a_stronger_positive_edge_on_raw_probability():
    c = Config()
    high_p = cand(_fx(0), p=0.72, price=1.40, p_fair=0.72, edge_ev=-0.005)
    value = cand(_fx(1), p=0.67, price=1.62, p_fair=0.67, edge_ev=0.085)
    a, b = evmod.assess(high_p, c), evmod.assess(value, c)
    assert high_p.p_model > value.p_model                 # the raw-probability order is the opposite ...
    assert b.rank > a.rank                                # ... of the evidence order


def test_a_clearly_negative_edge_vs_sharp_is_rejected_outright():
    assert Config().min_edge_vs_sharp == -0.04
    bad = cand(_fx(0), p=0.80, price=1.30, p_fair=0.78, edge_ev=-0.07)
    trace = {}
    ps = select([bad], Config(), dt(), trace=trace)
    assert ps.daily == [] and ps.diagnostics["daily_pool_rejections"]["NEGATIVE_EDGE_VS_SHARP"] == 1


def test_ranking_orders_the_daily_by_evidence_not_probability():
    c = Config(daily_odds_min=1.3, daily_odds_max=1.5, daily_ranking="raw")      # one leg completes the ticket
    # two books each: eligible for the Daily (2) but not the Top (3), so only the Daily ordering is exercised
    high_p = cand(_fx(0), p=0.74, price=1.35, p_fair=0.74, edge_ev=-0.005, quotes=books(2, 1.35))
    value = cand(_fx(1), p=0.69, price=1.35, p_fair=0.69, edge_ev=0.10, quotes=books(2, 1.35))
    ps = select([high_p, value], c, dt())
    assert ps.daily == [value]


def test_without_a_sharp_reference_there_is_no_edge_and_no_edge_gate():
    s = cand(_fx(0), p=0.70)
    e = evmod.assess(s, Config())
    assert s.p_fair is None and s.edge_ev is None and e.edge is None and e.has_sharp is False
    assert select([s], Config(), dt()).daily == [s]       # absence of a reference is not a rejection


def test_missing_reference_costs_confidence_not_a_fabricated_edge():
    c = Config()
    with_ref = cand(_fx(0), p=0.70, p_fair=0.70, edge_ev=0.0)
    without = cand(_fx(1), p=0.70)
    assert evmod.assess(without, c).se > evmod.assess(with_ref, c).se
    assert evmod.assess(without, c).rank < evmod.assess(with_ref, c).rank


def test_an_unsupported_claim_over_the_market_is_rejected():
    big = cand(_fx(0), p=0.80, price=1.4, edge=0.12)                      # model says +12 points, nothing independent
    backed = cand(_fx(1), p=0.80, price=1.4, edge=0.12, independent=("elo",))
    ps = select([big, backed], Config(), dt())
    assert big not in ps.daily and backed in ps.daily
    assert ps.diagnostics["daily_pool_rejections"]["UNSUPPORTED_EDGE"] == 1


def test_ranking_score_and_probability_stay_separate():
    s = cand(_fx(0), p=0.70, price=1.5, p_fair=0.70, edge_ev=0.06)
    before = s.p_model
    select([s], Config(), dt())
    assert s.p_model == before                              # ranking never rewrites the probability
    assert s.rank_score != s.p_model and s.p_lcb is not None and s.p_lcb < s.p_model
    assert "rank" in s.notes["evidence"]


def test_selection_stores_probability_and_rank_in_different_columns(db):
    s = cand(_fx(0), p=0.70, price=1.5, p_fair=0.70, edge_ev=0.06)
    ps = select([s], Config(), dt())
    daily_picks.store_picks(ps, db, "2030-06-01")
    row = db.query("SELECT * FROM picks")[0]
    assert row["prob"] == pytest.approx(0.70) and row["rank_score"] != row["prob"]
    assert row["p_fair"] == pytest.approx(0.70) and row["edge_ev"] == pytest.approx(0.06)
    assert row["books"] == 5 and row["quality"] == "main"
    assert row["closing_price"] is None and row["clv"] is None          # ready for CLV, not faked


# =========================================================================== de-vigging (Shin)
def test_shin_probabilities_sum_to_one():
    for prices in ({"h": 2.1, "d": 3.4, "a": 3.6}, {"o": 1.9, "u": 1.95}, {"h": 1.2, "d": 6.5, "a": 14.0}):
        assert sum(shin_devig(prices).values()) == pytest.approx(1.0, abs=1e-9)


def test_shin_takes_more_margin_off_longshots_than_the_multiplicative_method():
    prices = {"fav": 1.25, "d": 6.0, "dog": 12.0}
    q = {k: 1 / v for k, v in prices.items()}
    mult = {k: v / sum(q.values()) for k, v in q.items()}
    shin = shin_devig(prices)
    assert shin["dog"] < mult["dog"] and shin["fav"] > mult["fav"]       # favourite-longshot bias
    assert all(0 < v < 1 for v in shin.values())


def test_shin_two_way_even_market_is_even():
    assert shin_devig({"a": 1.909, "b": 1.909}) == pytest.approx({"a": 0.5, "b": 0.5})


def test_shin_without_an_overround_is_only_normalised():
    got = shin_devig({"a": 2.1, "b": 2.1})                               # implied 0.952: arbitrage
    assert sum(got.values()) == pytest.approx(1.0) and got["a"] == pytest.approx(0.5)


def test_shin_handles_degenerate_input():
    assert shin_devig({}) == {} and shin_devig({"a": 1.0}) == {}
    assert shin_devig({"a": 2.0}) == {"a": 1.0}


def test_existing_devig_is_unchanged():
    assert sum(devig({"h": 2.1, "d": 3.4, "a": 3.6}).values()) == pytest.approx(1.0)


# =========================================================================== model: sharp reference vs soft price
def _model_event(with_sharp=True, sharp_complete=True):
    e = Event("test", "m1", "football", "L", "Alpha FC", "Beta FC", dt(hh=20), sources=["test"])
    sharp = {"home": 2.10, "draw": 3.40, "away": 3.60}
    soft = {"home": (2.25, 2.20, 2.30), "draw": (3.30, 3.25, 3.35), "away": (3.40, 3.35, 3.45)}
    for sel in ("home", "draw", "away"):
        quotes = {f"soft{i}": v for i, v in enumerate(soft[sel])}
        if with_sharp and (sharp_complete or sel != "away"):
            quotes["pinnacle"] = sharp[sel]
        e.markets.append(line("result3", sel, statistics.median(list(quotes.values())), quotes=quotes))
    return e


def _model(db, e, **kw):
    return EventModel(e, Elo(db), Calibrator(db), sharp_books=["pinnacle", "betfair_ex_eu"], **kw)


def _score(model, sel):
    return next(s for s in model.score_all() if s.line.selection == sel)


def test_fair_probability_comes_from_the_sharp_book_alone(db):
    e = _model_event()
    m = _model(db, e)
    home = _score(m, "home")
    expected = shin_devig({"home": 2.10, "draw": 3.40, "away": 3.60})["home"]
    assert home.p_fair == pytest.approx(expected) and home.fair_books == ["pinnacle"]
    assert home.p_model == pytest.approx(expected, abs=0.02)             # not dragged toward the soft consensus
    assert "sharp" in home.independent


def test_edge_is_fair_probability_times_the_best_soft_price(db):
    e = _model_event()
    home = _score(_model(db, e), "home")
    soft_median, soft_best = statistics.median([2.25, 2.20, 2.30]), 2.30
    assert home.line.price == pytest.approx(soft_median)                  # the card price: conservative, sharp excluded
    assert home.edge_ev == pytest.approx(home.p_fair * soft_best - 1.0)  # the edge: the price a punter can shop for


def test_edge_can_be_measured_against_the_median_soft_price(db):
    home = _score(_model(db, _model_event(), edge_price="median"), "home")
    assert home.edge_ev == pytest.approx(home.p_fair * statistics.median([2.25, 2.20, 2.30]) - 1.0)


def test_a_median_book_carries_its_margin_so_the_best_price_is_the_fair_yardstick(db):
    """Why EDGE_PRICE=best: against the median, an ordinary line looks several points negative."""
    e = _model_event()
    best = _score(_model(db, e), "home").edge_ev
    median = _score(_model(db, _model_event(), edge_price="median"), "home").edge_ev
    assert best > median


def test_the_sharp_price_is_not_averaged_into_the_available_price(db):
    e = _model_event()
    _model(db, e)
    assert all("pinnacle" in ml.book_quotes for ml in e.markets)             # the quote is kept (it is the reference) ...
    prices = {ml.selection: ml.price for ml in e.markets}
    assert prices == pytest.approx({"home": 2.25, "draw": 3.30, "away": 3.40})   # ... but the offer is the soft median


def test_no_sharp_quotes_means_no_reference_and_no_edge(db):
    e = _model_event(with_sharp=False)
    home = _score(_model(db, e), "home")
    assert home.p_fair is None and home.edge_ev is None and home.independent == []
    assert home.fair_books == []


def test_a_partial_sharp_market_is_not_used(db):
    e = _model_event(sharp_complete=False)                                # pinnacle quotes home and draw only
    scored = _model(db, e).score_all()
    assert all(s.p_fair is None and s.edge_ev is None for s in scored)


def test_unconfigured_sharp_books_are_never_assumed(db):
    e = _model_event()
    scored = EventModel(e, Elo(db), Calibrator(db)).score_all()           # no sharp_books configured
    assert all(s.p_fair is None for s in scored)


def test_elo_only_counts_once_both_teams_have_enough_games(db):
    elo = Elo(db)
    for _ in range(3):
        elo.update("football", "Alpha FC", "Beta FC", 3, 0)
    e = _model_event(with_sharp=False)
    few = EventModel(e, elo, Calibrator(db), elo_min_games=10)
    assert "elo" not in _score(few, "home").sources and _score(few, "home").independent == []
    many = EventModel(e, elo, Calibrator(db), elo_min_games=3)
    assert "elo" in _score(many, "home").sources and _score(many, "home").independent == ["elo"]


def test_the_score_model_is_never_counted_as_an_independent_signal(db):
    e = _model_event(with_sharp=False)
    e.markets.append(line("total", "over", 1.9, quotes=books(4, 1.9), line=2.5))
    e.markets.append(line("total", "under", 1.9, quotes=books(4, 1.9), line=2.5))
    for s in _model(db, e).score_all():
        assert "score_model" not in s.independent


# =========================================================================== providers: quotes, merging, fallback
def test_oddsapi_keeps_who_quoted_each_price():
    prov = OddsApiProvider("k", budget=None)
    outcomes = [{"name": "Alpha FC", "price": 2.2}, {"name": "Draw", "price": 3.3}, {"name": "Beta FC", "price": 3.4}]
    lines = prov._market_to_lines({"key": "h2h", "outcomes": outcomes}, "Pinnacle", "Alpha FC", "Beta FC")
    assert [ml.book_quotes for ml in lines] == [{"pinnacle": 2.2}, {"pinnacle": 3.3}, {"pinnacle": 3.4}]
    assert norm_book("Betfair Exchange") == norm_book("betfair_exchange") == "betfair_exchange"


def test_oddsapi_rows_merge_quotes_per_book():
    prov = OddsApiProvider("k", budget=None)
    row = {"id": "e1", "home_team": "Alpha FC", "away_team": "Beta FC", "commence_time": "2030-06-01T20:00:00Z",
           "sport_key": "soccer_epl", "sport_title": "EPL",
           "bookmakers": [{"key": b, "markets": [{"key": "h2h", "outcomes": [
               {"name": "Alpha FC", "price": p}, {"name": "Draw", "price": 3.3}, {"name": "Beta FC", "price": 3.4}]}]}
               for b, p in (("pinnacle", 2.10), ("bet365", 2.2), ("unibet", 2.25))]}
    events = prov._rows_to_events([("football", row)], dt(hh=0), dt(d=2))
    home = next(m for m in events[0].markets if m.selection == "home")
    assert home.book_quotes == {"pinnacle": 2.10, "bet365": 2.2, "unibet": 2.25} and home.distinct_books() == 3


def test_the_same_book_through_two_providers_counts_once_and_both_providers_are_recorded():
    a = Event("sharpapi", "1", "football", "L", "Alpha FC", "Beta FC", dt(hh=20), sources=["sharpapi"],
              markets=[line("result3", "home", 2.0, quotes={"draftkings": 2.0, "fanduel": 2.05})])
    b = Event("sportsgameodds", "2", "football", "L", "Alpha FC", "Beta FC", dt(hh=20), sources=["sportsgameodds"],
              markets=[line("result3", "home", 2.1, quotes={"draftkings": 2.0, "bet365": 2.1})])
    out = merge_events([], [a], "median")
    out = merge_events(out, [b], "median")
    ml = out[0].markets[0]
    assert ml.distinct_books() == 3 and sorted(ml.book_quotes) == ["bet365", "draftkings", "fanduel"]
    assert ml.providers == ["sharpapi", "sportsgameodds"] and ml.provider_count() == 2


class _Prov:
    def __init__(self, name, events=None, fail=False):
        self.name, self._events, self.fail = name, events or [], fail

    def enabled(self):
        return True

    def fetch_events(self, start, end):
        if self.fail:
            raise ProviderError("down")
        return self._events


def test_provider_fallback_still_works_when_the_priority_provider_fails():
    good = Event("sharpapi", "1", "football", "L", "Alpha FC", "Beta FC", dt(hh=20), sources=["sharpapi"],
                 markets=[line("result3", "home", 2.0, quotes={"draftkings": 2.0})])
    chain = ProviderChain({"oddsapi": _Prov("oddsapi", fail=True), "sharpapi": _Prov("sharpapi", [good])},
                          ["oddsapi", "sharpapi"])
    pool = chain.collect(dt(hh=0), dt(d=2))
    assert len(pool) == 1 and chain.last_report["oddsapi"]["status"] == "failed"
    assert chain.last_report["sharpapi"]["status"] == "complete"


# =========================================================================== accuracy tracking
def _row(status, tier="daily", day="2030-06-01", prob=0.7, sport="football", market="result3", **kw):
    return {"status": status, "tier": tier, "pick_date": day, "prob": prob, "p_market": prob, "prob_raw": prob,
            "sport": sport, "market": market, "stat": "points", "price": 1.5, **kw}


def test_individual_hit_rate_is_not_the_ticket_result():
    rows = [_row("won") for _ in range(9)] + [_row("lost")]               # ONE leg failed on a 10-leg Daily
    rep = stats_mod.accuracy(rows)
    assert rep["legs"]["hit_rate"] == pytest.approx(90.0)
    assert rep["legs"]["wins"] == 9 and rep["legs"]["losses"] == 1
    t = rep["tickets"]["daily"]
    assert (t["days"], t["hit"], t["one_leg_short"]) == (1, 0, 1)         # the ticket lost; the model did not get 3/10


def test_three_of_ten_is_reported_as_thirty_percent():
    rows = [_row("won") for _ in range(3)] + [_row("lost") for _ in range(7)]
    rep = stats_mod.accuracy(rows)
    assert rep["legs"]["hit_rate"] == pytest.approx(30.0) and rep["tickets"]["daily"]["one_leg_short"] == 0


def test_pending_push_and_void_are_not_in_the_hit_rate():
    rows = [_row("won"), _row("lost"), _row("push"), _row("void"), _row("pending")]
    legs = stats_mod.accuracy(rows)["legs"]
    assert (legs["wins"], legs["losses"], legs["push"], legs["void"], legs["decided"]) == (1, 1, 1, 1, 2)
    assert legs["hit_rate"] == pytest.approx(50.0)


def test_half_results_count_as_the_side_they_landed_on():
    legs = stats_mod.accuracy([_row("half_won"), _row("half_lost")])["legs"]
    assert (legs["wins"], legs["losses"]) == (1, 1)


def test_brier_needs_enough_data_and_compares_with_the_market():
    few = stats_mod.accuracy([_row("won"), _row("lost")])["legs"]
    assert few["brier"] is None and few["brier_market"] is None
    rng = random.Random(7)
    rows = []
    for _ in range(60):
        p = 0.7
        rows.append(_row("won" if rng.random() < p else "lost", prob=p, p_market=0.6, prob_raw=p))
    legs = stats_mod.accuracy(rows)["legs"]
    assert legs["brier"] is not None and legs["brier_market"] is not None and legs["brier"] < legs["brier_market"]


def test_accuracy_is_sliced_by_tier_sport_market_confidence_and_books():
    rows = [_row("won", tier="top", sport="football", prob=0.80, books=5, quality="main"),
            _row("lost", tier="daily", sport="tennis", market="result2", prob=0.60, books=2, quality="secondary")]
    rep = stats_mod.accuracy(rows)
    assert rep["by_tier"]["top"]["wins"] == 1 and rep["by_tier"]["daily"]["losses"] == 1
    assert {b["key"] for b in rep["by_sport"]} == {"football", "tennis"}
    assert {b["key"] for b in rep["by_market"]} == {"result3/points", "result2/points"}
    assert {b["key"] for b in rep["by_confidence"]} == {"75-85%", "55-65%"}
    assert {b["key"] for b in rep["by_books"]} == {"5+", "2"}
    assert {b["key"] for b in rep["by_quality"]} == {"main", "secondary"}


def test_clv_helper_and_unrecorded_clv():
    assert stats_mod.clv(2.0, 1.8) == pytest.approx(0.1111, abs=1e-3)
    assert stats_mod.clv(1.8, 2.0) < 0 and stats_mod.clv(None, 2.0) is None and stats_mod.clv(2.0, 1.0) is None
    assert stats_mod.accuracy([_row("won")])["clv"] == {"n": 0, "mean": None}
    assert stats_mod.accuracy([_row("won", clv=0.05), _row("lost", clv=-0.01)])["clv"]["n"] == 2


def test_accuracy_report_reads_the_database_and_text_renders(db):
    for status in ("won", "lost", "pending"):
        db.insert_pick({"pick_date": "2030-06-01", "tier": "daily", "rank": 1, "provider": "t", "provider_event_id": status,
                        "sport": "football", "league": "L", "home": f"H{status}", "away": f"A{status}",
                        "start_utc": "2030-06-01T18:00:00Z", "market": "result3", "period": "full", "stat": "points",
                        "entity": "all", "line": None, "selection": "home", "label": "x", "price": 1.5, "prob": 0.7,
                        "ev": 0.0, "status": status})
    rep = stats_mod.accuracy_report(db)
    assert rep["legs"]["decided"] == 2
    text = stats_mod.accuracy_text(rep)
    assert "per-leg hit rate: 50.0%" in text and "Brier: not reported" in text


# =========================================================================== database compatibility
def test_an_old_database_without_the_evidence_columns_is_migrated(tmp_path):
    import sqlite3
    from predbot.storage.db import Database
    path = str(tmp_path / "old.db")
    raw = sqlite3.connect(path)
    raw.executescript("""CREATE TABLE picks (id INTEGER PRIMARY KEY AUTOINCREMENT, pick_date TEXT NOT NULL, tier TEXT NOT NULL,
        rank INTEGER NOT NULL, provider TEXT NOT NULL, provider_event_id TEXT NOT NULL, sport TEXT NOT NULL, league TEXT,
        home TEXT NOT NULL, away TEXT NOT NULL, start_utc TEXT NOT NULL, market TEXT NOT NULL, period TEXT NOT NULL,
        stat TEXT NOT NULL, entity TEXT NOT NULL, line REAL, selection TEXT NOT NULL, label TEXT NOT NULL, price REAL NOT NULL,
        prob REAL NOT NULL, ev REAL NOT NULL, p_market REAL, prob_raw REAL, sources TEXT, provider_odd_id TEXT,
        status TEXT NOT NULL DEFAULT 'pending', settled_at TEXT, result_json TEXT, created_at TEXT NOT NULL);
        INSERT INTO picks(pick_date,tier,rank,provider,provider_event_id,sport,league,home,away,start_utc,market,period,stat,
        entity,selection,label,price,prob,ev,status,created_at) VALUES('2030-01-01','top',1,'p','e','football','L','A','B',
        '2030-01-01T10:00:00Z','result3','full','points','all','home','x',1.5,0.7,0.0,'won','2030-01-01T00:00:00Z');""")
    raw.commit()
    raw.close()
    db = Database(path)
    cols = {r["name"] for r in db.query("PRAGMA table_info(picks)")}
    assert {"p_fair", "edge_ev", "rank_score", "books", "quality", "closing_price", "clv"} <= cols
    assert stats_mod.accuracy_report(db)["legs"]["wins"] == 1                       # old rows still read
    db.close()
