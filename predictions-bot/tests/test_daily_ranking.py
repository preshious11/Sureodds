"""Family-fair Daily ranking: only the ORDER candidates are offered in changes; gates and caps do not."""
from __future__ import annotations

import logging
from collections import Counter

import pytest

from predbot.config import Config
from predbot.providers.base import MarketLine
from predbot.selection import picker
from predbot.selection.families import daily_family, main_line_table, order_by_family_z
from predbot.selection.picker import _daily_score, select
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
    tables = {}
    fam = {ln: daily_family(sc(e, "total", "over", 0.6, 1.8, line=ln), tables) for ln in (1.5, 2.5, 3.5)}
    assert fam == {1.5: "total_alt", 2.5: "total_main", 3.5: "total_alt"}


def test_a_lone_line_is_the_main_line():
    e = ev(0)
    e.markets = _pair(2.5, 0.60)
    assert daily_family(sc(e, "total", "over", 0.6, 1.8, line=2.5), {}) == "total_main"
    e2 = ev(1)                                     # a market with no line information at all
    assert daily_family(sc(e2, "total", "over", 0.6, 1.8, line=2.5), {}) == "total_main"


def test_handicap_main_line_matches_a_home_line_with_its_away_mirror():
    e = ev(0)
    e.markets = (_pair(-0.5, 0.52, "handicap", first="home", second="away")
                 + _pair(-1.5, 0.30, "handicap", first="home", second="away")
                 + _pair(0.5, 0.75, "handicap", first="home", second="away"))
    tables = {}
    home = lambda ln: sc(e, "handicap", "home", 0.6, 1.9, line=ln)
    away = lambda ln: sc(e, "handicap", "away", 0.6, 1.9, line=ln)
    assert daily_family(home(-0.5), tables) == "handicap_main"
    assert daily_family(away(0.5), tables) == "handicap_main"       # the same line seen from the other side
    assert daily_family(home(-1.5), tables) == "handicap_alt"
    assert daily_family(away(-0.5), tables) == "handicap_alt"        # away -0.5 == home +0.5, not the main line


@pytest.mark.parametrize("market,selection,line,entity,period,stat,expected", [
    ("result3", "home", None, "all", "full", "points", "win"), ("result2", "away", None, "all", "full", "points", "win"),
    ("dc", "home_draw", None, "all", "full", "points", "safety"), ("dnb", "home", None, "all", "full", "points", "safety"),
    ("btts", "yes", None, "all", "full", "points", "btts"),
    ("total", "over", 1.5, "home", "full", "points", "team_total"),
    ("total", "over", 9.5, "all", "full", "corners", "other"),
    ("total", "over", 2.5, "all", "1h", "points", "period"), ("result3", "home", None, "all", "2h", "points", "period"),
    ("correct_score", "2-1", None, "all", "full", "points", "exotic"), ("odd_even", "odd", None, "all", "full", "points", "exotic"),
    ("prop", "over", 1.5, "all", "full", "points", "prop")])
def test_every_other_family(market, selection, line, entity, period, stat, expected):
    s = sc(ev(0), market, selection, 0.6, 1.8, line=line)
    s.line.entity, s.line.period, s.line.stat = entity, period, stat
    assert daily_family(s, {}) == expected


# ------------------------------------------------------------------ fairness
_WORDS = ["Aberdeen", "Bologna", "Cadiz", "Dortmund", "Espanyol", "Feyenoord", "Genoa", "Hamburg", "Inverness",
          "Juventus", "Kilmarnock", "Lazio", "Marseille", "Nantes", "Osasuna", "Porto", "Quimper", "Rennes",
          "Sevilla", "Torino", "Udinese", "Valencia", "Wolfsburg", "Xamax", "Ypres", "Zaragoza", "Ajax", "Brugge",
          "Celtic", "Dundee", "Everton", "Fulham", "Girona", "Hearts", "Ipswich", "Jena", "Koln", "Leeds", "Monaco", "Napoli"]


def _match(i):
    """A fixture whose team names cannot be fuzzy-matched to any other one (so no 'same match' surprises)."""
    return ev(i, home=_WORDS[2 * (i % 20)], away=_WORDS[2 * (i % 20) + 1])


def _synthetic_pool():
    """12 safe alternate handicaps, 4 straight wins and 4 main-line totals - on DIFFERENT matches, all
    passing every gate. Wins/totals are less 'confident' than +1.5 lines, as in real data."""
    out = []
    for i in range(12):
        e = _match(i)
        e.markets = (_pair(-0.5, 0.52, "handicap", first="home", second="away")
                     + _pair(-1.5, 0.30, "handicap", first="home", second="away"))
        out.append(sc(e, "handicap", "away", 0.80 + 0.005 * i, 1.25, line=1.5))            # away +1.5: alt
    for j in range(4):
        out.append(sc(_match(12 + j), "result3", "home", 0.58 + 0.02 * j, 1.60))
        e = _match(16 + j)
        e.markets = _pair(2.5, 0.53) + _pair(1.5, 0.80)
        out.append(sc(e, "total", "over", 0.52 + 0.02 * j, 1.85, line=2.5))
    return out


def _cfg(mode):
    # caps loosened ONLY so the effect of ORDER is isolated; test_gates_and_caps_are_untouched keeps the defaults
    return Config(daily_ranking=mode, daily_max_market_share=1.0, daily_max_per_league=99, daily_max_sport_share=1.0)


def _families(ps):
    tables = {}
    return Counter(daily_family(s, tables) for s in ps.daily)


def test_raw_ranking_offers_every_safe_handicap_before_any_win():
    ps = select(_synthetic_pool(), _cfg("raw"), dt())
    fam = _families(ps)
    assert fam["handicap_alt"] >= 8 and fam["win"] == 0 and fam["total_main"] == 0        # today's behaviour


def test_family_ranking_lets_wins_and_main_line_totals_compete():
    ps = select(_synthetic_pool(), _cfg("family_z"), dt())
    fam = _families(ps)
    assert fam["win"] >= 1 and fam["total_main"] >= 1
    assert ps.diagnostics["daily_family_pool"] == {"handicap_alt": 11, "win": 3, "total_main": 4}     # Top took one handicap and one win
    assert ps.diagnostics["daily_family_picks"] == dict(fam)


def test_the_top_ticket_is_identical_under_both_rankings():
    raw = select(_synthetic_pool(), _cfg("raw"), dt())
    fam = select(_synthetic_pool(), _cfg("family_z"), dt())
    key = lambda ps: [(s.event.provider_event_id, s.line.market, s.line.selection, s.line.line) for s in ps.top]
    assert key(raw) == key(fam)


# ------------------------------------------------------------------ rollback
def test_raw_reproduces_the_daily_score_order_exactly(monkeypatch):
    monkeypatch.setattr(picker, "order_by_family_z", lambda *a, **k: (_ for _ in ()).throw(AssertionError("family_z used")))
    ps = select(_synthetic_pool(), _cfg("raw"), dt())
    scores = [s.rank_score for s in ps.daily]
    assert scores == sorted(scores, reverse=True) and scores == [_daily_score(s) for s in ps.daily]
    assert ps.diagnostics["daily_ranking"] == "raw"


def test_family_ranking_is_not_the_raw_order():
    ps = select(_synthetic_pool(), _cfg("family_z"), dt())
    scores = [s.rank_score for s in ps.daily]
    assert scores != sorted(scores, reverse=True)


def test_the_default_is_family_z_and_a_bad_value_falls_back_with_a_warning(monkeypatch, caplog):
    monkeypatch.delenv("DAILY_RANKING", raising=False)
    assert Config().daily_ranking == "family_z"
    monkeypatch.setenv("DAILY_RANKING", "RAW")
    assert Config().daily_ranking == "raw"
    monkeypatch.setenv("DAILY_RANKING", "bogus")
    with caplog.at_level(logging.WARNING):
        assert Config().daily_ranking == "family_z"
    assert any("DAILY_RANKING" in r.message for r in caplog.records)


# ------------------------------------------------------------------ gates and caps
@pytest.mark.parametrize("mode", ["raw", "family_z"])
def test_gates_and_caps_are_untouched(mode):
    cfg = Config(daily_ranking=mode)
    weak = sc(ev(1), "result3", "home", 0.45, 2.20)                                # below 'preferred p >= 0.5'
    cheap = sc(ev(2), "result3", "home", 0.90, 1.05)                               # below DAILY_MIN_ODDS
    strong = [sc(ev(10 + i, league="Same League"), "result3", "home", 0.62 + 0.01 * i, 1.7) for i in range(6)]
    same_event = ev(30)
    twins = [sc(same_event, "result3", "home", 0.70, 1.6), sc(same_event, "total", "over", 0.68, 1.7, line=2.5)]
    ps = select([weak, cheap, *strong, *twins], cfg, dt())
    events = [s.event for s in ps.all()]
    assert weak.event not in events and cheap.event not in events                   # gates
    assert sum(1 for s in ps.daily if s.event.league == "Same League") <= cfg.daily_max_per_league   # league cap
    # MAX_PICKS_PER_EVENT counts per tier (CROSS_TIER_SAME_FIXTURE=different_outcome): one Daily pick per match, and
    # never the Top leg's own tip
    assert sum(1 for s in ps.daily if s.event is same_event) <= 1
    assert len({(s.line.market, s.line.selection) for s in ps.all() if s.event is same_event}) ==         sum(1 for s in ps.all() if s.event is same_event)


# ------------------------------------------------------------------ the z-score itself
def test_z_is_computed_within_the_family_and_tagged_on_the_candidate():
    pool = [sc(ev(i), "result3", "home", p, 1.6) for i, p in enumerate((0.55, 0.60, 0.65))] + \
           [sc(ev(10 + i), "dc", "home_draw", p, 1.3) for i, p in enumerate((0.80, 0.85, 0.90))]
    for s in pool:
        s.rank_score = _daily_score(s)
    ordered, counts = order_by_family_z(pool, lambda s: s.rank_score)
    assert counts == {"win": 3, "safety": 3}
    zs = {(s.notes["family"], s.event.provider_event_id): s.notes["z"] for s in ordered}
    assert zs[("win", "2")] > 0 > zs[("win", "0")] and zs[("safety", "12")] > 0 > zs[("safety", "10")]
    best_win, best_dc = ordered[0], ordered[1]
    assert {best_win.notes["family"], best_dc.notes["family"]} == {"win", "safety"}     # each family's best leads
    assert ordered[0].notes["z"] == pytest.approx(ordered[1].notes["z"], abs=0.05)


def test_a_family_smaller_than_three_borrows_the_whole_pools_mean_and_std():
    pool = [sc(ev(i), "dc", "home_draw", 0.80 + 0.02 * i, 1.3) for i in range(5)] + [sc(ev(50), "result3", "home", 0.58, 1.6)]
    for s in pool:
        s.rank_score = _daily_score(s)
    ordered, _ = order_by_family_z(pool, lambda s: s.rank_score)
    lone = next(s for s in ordered if s.notes["family"] == "win")
    scores = [s.rank_score for s in pool]
    mu = sum(scores) / len(scores)
    sd = (sum((x - mu) ** 2 for x in scores) / len(scores)) ** 0.5
    assert lone.notes["z"] == pytest.approx((lone.rank_score - mu) / max(sd, 0.02), abs=0.01)


def test_identical_scores_do_not_blow_up_z():
    pool = [sc(ev(i), "result3", "home", 0.6, 1.6) for i in range(4)]
    for s in pool:
        s.rank_score = 0.5
    ordered, _ = order_by_family_z(pool, lambda s: s.rank_score)
    assert all(s.notes["z"] == 0 for s in ordered) and len(ordered) == 4
