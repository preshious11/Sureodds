"""Market-family classification, and the Daily selector's ordering, gates and caps."""
from __future__ import annotations

import logging
from collections import Counter

import pytest

from predbot.config import Config
from predbot.providers.base import MarketLine
from predbot.selection import picker
from predbot.selection.families import main_line_table
from tests.pipeline_helpers import _daily_score, select as _select


def select(scored, cfg, now, **kw):
    """Top/Daily mechanics on the given predictions (see pipeline_helpers.select)."""
    kw.setdefault("raw", True)
    return _select(scored, cfg, now, **kw)
from tests.conftest import dt
from tests.test_picker import ev, sc


def _pair(line, p_over, market="total", margin=1.03, period="full", stat="points", entity="all", first="over", second="under"):
    """Two sides of one line priced from a de-vigged probability p of the first side."""
    second_line = -line if market == "handicap" else line          # a handicap's other side is the mirrored line
    return [MarketLine(market, first, 1 / (p_over * margin), line=line, period=period, stat=stat, entity=entity, n_books=2),
            MarketLine(market, second, 1 / ((1 - p_over) * margin), line=second_line, period=period, stat=stat, entity=entity, n_books=2)]


def _totals_event(i=0):
    e = ev(i)
    e.markets = _pair(1.5, 0.82) + _pair(2.5, 0.53) + _pair(3.5, 0.27)
    return e


# ------------------------------------------------------------------ main-line detection
def test_the_main_total_is_the_line_closest_to_even_probability():
    e = _totals_event()
    assert main_line_table(e)[("total", "full", "points", "all")] == 2.5


def test_a_lone_line_is_the_main_line():
    e = ev(0)
    e.markets = _pair(2.5, 0.60)
    assert main_line_table(e)[("total", "full", "points", "all")] == 2.5


def test_handicap_main_line_matches_a_home_line_with_its_away_mirror():
    e = ev(0)
    e.markets = (_pair(-0.5, 0.52, "handicap", first="home", second="away")
                 + _pair(-1.5, 0.30, "handicap", first="home", second="away")
                 + _pair(0.5, 0.75, "handicap", first="home", second="away"))
    table = main_line_table(e)
    assert table[("handicap", "full", "points", "all")] == -0.5            # keyed by the HOME line; away +0.5 is the same line


# ------------------------------------------------------------------ fairness
_WORDS = ["Aberdeen", "Bologna", "Cadiz", "Dortmund", "Espanyol", "Feyenoord", "Genoa", "Hamburg", "Inverness",
          "Juventus", "Kilmarnock", "Lazio", "Marseille", "Nantes", "Osasuna", "Porto", "Quimper", "Rennes",
          "Sevilla", "Torino", "Udinese", "Valencia", "Wolfsburg", "Xamax", "Ypres", "Zaragoza", "Ajax", "Brugge",
          "Celtic", "Dundee", "Everton", "Fulham", "Girona", "Hearts", "Ipswich", "Jena", "Koln", "Leeds", "Monaco", "Napoli"]


def _match(i):
    """A fixture whose team names cannot be fuzzy-matched to any other one (so no 'same match' surprises)."""
    return ev(i, home=_WORDS[2 * (i % 20)], away=_WORDS[2 * (i % 20) + 1])


def _synthetic_pool():
    """8 safe alternate handicaps, 6 straight wins and 6 main-line totals - on DIFFERENT matches, all
    passing every gate (DAILY_MIN_PROB 0.55, book coverage, lower bound). Wins/totals are less 'confident' than
    +1.5 lines, as in real data."""
    out = []
    for i in range(8):
        e = _match(i)
        e.markets = (_pair(-0.5, 0.52, "handicap", first="home", second="away")
                     + _pair(-1.5, 0.30, "handicap", first="home", second="away"))
        out.append(sc(e, "handicap", "away", 0.80 + 0.005 * i, 1.25, line=1.5))            # away +1.5: alt
    for j in range(6):
        out.append(sc(_match(8 + j), "result3", "home", 0.57 + 0.015 * j, 1.60))
        e = _match(14 + j)
        e.markets = _pair(2.5, 0.53) + _pair(1.5, 0.80)
        out.append(sc(e, "total", "over", 0.56 + 0.01 * j, 1.85, line=2.5))
    return out


def _cfg():
    # caps loosened ONLY so the effect of ORDER is isolated; test_gates_and_caps_are_untouched keeps the defaults
    return Config(daily_max_market_share=1.0, daily_max_per_league=99, daily_max_sport_share=1.0)


# ------------------------------------------------------------------ gates and caps


def test_daily_order_is_the_stored_evidence_rank_order():
    """The Daily selector orders stored rows by their stored rank_score only (restricted markets last)."""
    ps = select(_synthetic_pool(), _cfg(), dt())
    scores = [s.rank_score for s in ps.daily]
    assert scores == sorted(scores, reverse=True) and scores == [_daily_score(s) for s in ps.daily]


def test_gates_and_caps_are_untouched():
    cfg = Config()
    weak = sc(ev(1), "result3", "home", 0.45, 2.20)                                # below the probability floor
    cheap = sc(ev(2), "result3", "home", 0.90, 1.05)                               # below DAILY_MIN_ODDS
    strong = [sc(ev(10 + i, league="Same League"), "result3", "home", 0.62 + 0.01 * i, 1.7) for i in range(6)]
    same_event = ev(30)
    twins = [sc(same_event, "result3", "home", 0.70, 1.6), sc(same_event, "total", "over", 0.68, 1.7, line=2.5)]
    ps = select([weak, cheap, *strong, *twins], cfg, dt())
    daily_events = [s.event for s in ps.daily]
    assert weak.event not in daily_events and cheap.event not in daily_events      # gates ...
    assert weak.event in [s.event for s in ps.all()] and cheap.event in [s.event for s in ps.all()]   # ... not deletion
    assert sum(1 for s in ps.daily if s.event.league == "Same League") <= cfg.daily_max_per_league   # league cap
    assert sum(1 for s in ps.all() if s.event is same_event) == 2                  # two families: both are predictions
    assert sum(1 for s in ps.daily if s.event is same_event) <= 1
