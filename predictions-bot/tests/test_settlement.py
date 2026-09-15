"""Grade every market family against a known final score.

Reference match: home 2 - 1 away, HT 1 - 1, home corners 6 / away 3.
"""
from __future__ import annotations

from predbot.markets.settlement import grade, WON, LOST, PUSH, HALF_WON, HALF_LOST, VOID, PENDING
from predbot.providers.base import MarketLine, MatchResult


def res(status="final"):
    return MatchResult(
        status=status,
        scores={"full": (2.0, 1.0), "reg": (2.0, 1.0), "1h": (1.0, 1.0)},
        stats={("corners", "full"): (6.0, 3.0)},
    )


def test_result3():
    assert grade(MarketLine("result3", "home", 2.0), res()) == WON
    assert grade(MarketLine("result3", "away", 4.0), res()) == LOST
    assert grade(MarketLine("result3", "draw", 3.4), res()) == LOST


def test_double_chance():
    assert grade(MarketLine("dc", "home_draw", 1.3), res()) == WON
    assert grade(MarketLine("dc", "away_draw", 4.0), res()) == LOST
    assert grade(MarketLine("dc", "home_away", 1.2), res()) == WON


def test_dnb_push_on_draw():
    draw = MatchResult(status="final", scores={"full": (1.0, 1.0)})
    assert grade(MarketLine("dnb", "home", 1.9), draw) == PUSH
    assert grade(MarketLine("dnb", "home", 1.9), res()) == WON


def test_btts():
    assert grade(MarketLine("btts", "yes", 1.8), res()) == WON
    assert grade(MarketLine("btts", "no", 2.0), res()) == LOST


def test_totals_over_under():
    assert grade(MarketLine("total", "over", 1.9, line=2.5), res()) == WON   # 3 > 2.5
    assert grade(MarketLine("total", "under", 1.9, line=2.5), res()) == LOST
    assert grade(MarketLine("total", "over", 1.9, line=3.0), res()) == PUSH  # exactly 3
    assert grade(MarketLine("total", "over", 1.9, line=3.5), res()) == LOST


def test_team_total_and_corners():
    assert grade(MarketLine("total", "over", 1.9, line=1.5, entity="home"), res()) == WON  # home 2
    assert grade(MarketLine("total", "over", 1.9, line=5.5, stat="corners", entity="home"), res()) == WON
    # corners not available -> pending
    r = res(); r.stats.clear()
    assert grade(MarketLine("total", "over", 1.9, line=5.5, stat="corners", entity="home"), r) == PENDING


def test_handicap_asian_lines():
    # home -1 vs 2-1 => adj 1 vs 1 => push
    assert grade(MarketLine("handicap", "home", 1.9, line=-1.0), res()) == PUSH
    # home -1.5 => 0.5 vs 1 => loss
    assert grade(MarketLine("handicap", "home", 1.9, line=-1.5), res()) == LOST
    # away +1.5 => 2.5 vs 2 => win
    assert grade(MarketLine("handicap", "away", 1.9, line=1.5), res()) == WON
    # quarter line home -1.25 => split of -1 (push) and -1.5 (loss) => half lost
    assert grade(MarketLine("handicap", "home", 1.9, line=-1.25), res()) == HALF_LOST
    # quarter line home -0.75 => split of -0.5 (win) and -1 (push) => half won
    assert grade(MarketLine("handicap", "home", 1.9, line=-0.75), res()) == HALF_WON


def test_odd_even():
    assert grade(MarketLine("odd_even", "odd", 1.9), res()) == WON   # total 3
    assert grade(MarketLine("odd_even", "even", 1.9), res()) == LOST


def test_ht_ft():
    assert grade(MarketLine("ht_ft", "draw/home", 3.0), res()) == WON   # HT 1-1, FT home
    assert grade(MarketLine("ht_ft", "home/home", 4.0), res()) == LOST


def test_correct_score():
    assert grade(MarketLine("correct_score", "2-1", 8.0), res()) == WON
    assert grade(MarketLine("correct_score", "1-0", 8.0), res()) == LOST


def test_result_total_combo():
    assert grade(MarketLine("result_total", "home_over", 3.5, line=2.5), res()) == WON
    assert grade(MarketLine("result_total", "home_under", 3.5, line=2.5), res()) == LOST
    assert grade(MarketLine("result_total", "away_over", 6.0, line=2.5), res()) == LOST


def test_result_btts_combo():
    assert grade(MarketLine("result_btts", "home_yes", 4.0), res()) == WON
    assert grade(MarketLine("result_btts", "home_no", 4.0), res()) == LOST


def test_clean_sheet_win_to_nil_team_to_score():
    assert grade(MarketLine("clean_sheet", "no", 1.5, entity="home"), res()) == WON   # home conceded 1
    assert grade(MarketLine("win_to_nil", "no", 1.5, entity="home"), res()) == WON    # won but conceded
    assert grade(MarketLine("team_to_score", "yes", 1.2, entity="away"), res()) == WON


def test_winning_margin():
    assert grade(MarketLine("winning_margin", "home_1", 4.5), res()) == WON
    assert grade(MarketLine("winning_margin", "home_2-3", 5.0), res()) == LOST
    assert grade(MarketLine("winning_margin", "draw_0", 5.0), res()) == LOST


def test_void_and_pending():
    assert grade(MarketLine("result3", "home", 2.0), res("postponed")) == VOID
    assert grade(MarketLine("result3", "home", 2.0), res("cancelled")) == VOID
    assert grade(MarketLine("result3", "home", 2.0), res("in_progress")) == PENDING


def test_provider_settled_prop_value():
    r = res()
    r.odd_scores["player:pts"] = 27.0
    ml = MarketLine("prop", "over", 1.9, line=24.5, provider_odd_id="player:pts", is_player_prop=True)
    assert grade(ml, r) == WON
    ml_u = MarketLine("prop", "under", 1.9, line=24.5, provider_odd_id="player:pts", is_player_prop=True)
    assert grade(ml_u, r) == LOST
