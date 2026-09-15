"""Score-grid model: internal consistency and market recovery."""
from __future__ import annotations

from predbot.model.poisson import (ScoreGrid, lambdas_from_market, price_line,
                                   effective_prob)
from predbot.providers.base import MarketLine


def test_grid_normalised():
    g = ScoreGrid(1.5, 1.2)
    tot = sum(g.grid[i][j] for i in range(g.n + 1) for j in range(g.n + 1))
    assert abs(tot - 1.0) < 1e-6


def test_result_probs_sum_to_one():
    g = ScoreGrid(1.6, 1.1)
    h, d, a = g.result()
    assert abs(h + d + a - 1.0) < 1e-6
    assert h > a          # stronger home lambda -> home favoured


def test_over_under_complementary():
    g = ScoreGrid(1.4, 1.3)
    ov_w, ov_p = g.over_under(2.5, "over")
    un_w, un_p = g.over_under(2.5, "under")
    # 2.5 is a non-integer line: no push, the two sides sum to 1
    assert ov_p == 0.0 and un_p == 0.0
    assert abs(ov_w + un_w - 1.0) < 1e-6


def test_integer_line_push():
    g = ScoreGrid(1.4, 1.3)
    _, push = g.over_under(3, "over")
    assert push > 0.0     # exactly 3 goals returns the stake


def test_btts_matches_grid():
    g = ScoreGrid(1.5, 1.5)
    both = sum(g.grid[i][j] for i in range(1, g.n + 1) for j in range(1, g.n + 1))
    assert abs(g.btts() - both) < 1e-9


def test_handicap_zero_equals_dnb():
    g = ScoreGrid(1.7, 1.0)
    h, d, a = g.result()
    hw, _ = g.handicap(0.0, "home")
    # home +0 wins iff home wins outright; a draw pushes (not counted in win)
    assert abs(hw - h) < 1e-6


def test_clean_sheet_and_win_to_nil_bounds():
    g = ScoreGrid(1.8, 0.7)
    assert 0.0 <= g.clean_sheet("home") <= 1.0
    assert g.win_to_nil("home") <= g.clean_sheet("home")  # win_to_nil is a subset


def test_lambdas_recover_market():
    # feed a known market and check the solved grid reproduces it
    lh, la = lambdas_from_market(0.50, 0.30, 0.20, 2.5, 0.55, "football")
    g = ScoreGrid(lh, la)
    h, d, a = g.result()
    assert h > a                                   # ordering preserved
    ov_w, ov_p = g.over_under(2.5, "over")
    assert abs(effective_prob(ov_w, ov_p) - 0.55) < 0.03


def test_price_line_dispatches_all_markets():
    lh, la = 1.6, 1.1
    cases = [
        MarketLine("result3", "home", 2.0),
        MarketLine("result3", "draw", 3.4),
        MarketLine("dc", "home_draw", 1.3),
        MarketLine("btts", "yes", 1.8),
        MarketLine("total", "over", 1.9, line=2.5),
        MarketLine("handicap", "home", 1.9, line=-1.0),
        MarketLine("odd_even", "odd", 1.9),
        MarketLine("correct_score", "2-1", 8.0),
        MarketLine("clean_sheet", "yes", 2.2, entity="home"),
        MarketLine("win_to_nil", "yes", 3.0, entity="home"),
        MarketLine("team_to_score", "yes", 1.3, entity="home"),
        MarketLine("result_total", "home_over", 3.5, line=2.5),
        MarketLine("result_btts", "home_yes", 4.0),
        MarketLine("winning_margin", "home_1", 4.5),
        MarketLine("ht_ft", "home/home", 3.0),
    ]
    for ml in cases:
        p = price_line(ml, lh, la, "football")
        assert p is not None, ml.market
        assert 0.0 <= p <= 1.0, (ml.market, p)


def test_price_line_none_for_non_goal_stat():
    ml = MarketLine("total", "over", 1.9, line=9.5, stat="corners")
    assert price_line(ml, 1.6, 1.1, "football") is None
