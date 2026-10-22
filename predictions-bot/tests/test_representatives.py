"""One eligible representative per FIXTURE x MARKET FAMILY: the rules of every family, pushes, contradiction prevention,
determinism, and the engine's diagnostics."""
from __future__ import annotations

import random

import pytest

from predbot.model.engine import representative_predictions
from predbot.selection import representatives as rep
from predbot.util.fixtures import fixture_key
from tests.test_picker import ev, sc


def cands(event, rows):
    """rows: (market, selection, p, line) -> Scored lines of one fixture."""
    return [sc(event, m, s, p, round(1 / p, 3), line=line) for m, s, p, line in rows]


def one_x_two(h, d, a):
    return [("result3", "home", h, None), ("result3", "draw", d, None), ("result3", "away", a, None)]


def reps_of(rows, event=None):
    event = event or ev(1)
    reps, omitted = rep.event_representatives(cands(event, rows))
    return {f: (s.line.selection, s.line.line, round(s.p_model, 3)) for f, s in reps.items()}, omitted


# ------------------------------------------------------------------------------------------------ 1X2
def test_1x2_takes_the_modal_outcome_with_no_probability_floor():
    got, _ = reps_of(one_x_two(0.38, 0.33, 0.29))
    assert got["1X2"] == ("home", None, 0.38)                                  # a 38% modal outcome still qualifies
    got, _ = reps_of(one_x_two(0.31, 0.38, 0.31))
    assert got["1X2"][0] == "draw"                                             # the draw can be the mode


def test_an_incomplete_1x2_qualifies_only_above_50_percent():
    assert reps_of([("result3", "home", 0.70, None)])[0]["1X2"][0] == "home"   # above 50%: necessarily the mode
    got, omitted = reps_of([("result3", "home", 0.45, None)])
    assert "1X2" not in got and omitted["1X2"] == rep.INCOMPLETE_RESULT


# ------------------------------------------------------------------------------------------------ Double Chance
def test_double_chance_covers_the_two_most_likely_outcomes():
    rows = one_x_two(0.50, 0.30, 0.20) + [("dc", "home_draw", 0.80, None), ("dc", "home_away", 0.70, None), ("dc", "away_draw", 0.50, None)]
    got, _ = reps_of(rows)
    assert got["Double Chance"][0] == "home_draw"                              # excludes away, the least likely
    rows = one_x_two(0.45, 0.20, 0.35) + [("dc", "home_draw", 0.65, None), ("dc", "home_away", 0.80, None), ("dc", "away_draw", 0.55, None)]
    assert reps_of(rows)[0]["Double Chance"][0] == "home_away"                 # the draw is the least likely outcome here


def test_double_chance_is_read_from_its_own_three_lines_without_a_1x2_and_never_from_one_lone_line():
    rows = [("dc", "home_draw", 0.80, None), ("dc", "home_away", 0.70, None), ("dc", "away_draw", 0.50, None)]
    assert reps_of(rows)[0]["Double Chance"][0] == "home_draw"
    got, omitted = reps_of([("dc", "home_draw", 0.80, None)])
    assert "Double Chance" not in got and omitted["Double Chance"] == rep.INCOMPLETE_RESULT   # high p alone never qualifies


# ------------------------------------------------------------------------------------------------ Draw No Bet, Moneyline, BTTS
def test_draw_no_bet_is_the_favourite_side_and_a_lone_underdog_is_omitted():
    assert reps_of([("dnb", "home", 0.40, None), ("dnb", "away", 0.60, None)])[0]["Draw No Bet"][0] == "away"
    got, omitted = reps_of([("dnb", "home", 0.45, None)])
    assert "Draw No Bet" not in got and omitted["Draw No Bet"] == rep.NO_ELIGIBLE_SIDE


def test_moneyline_is_the_favourite_and_btts_the_likelier_side_above_50_percent():
    assert reps_of([("result2", "home", 0.41, None), ("result2", "away", 0.59, None)])[0]["Moneyline"][0] == "away"
    assert reps_of([("btts", "yes", 0.44, None), ("btts", "no", 0.56, None)])[0]["BTTS"][0] == "no"
    got, omitted = reps_of([("btts", "yes", 0.50, None), ("btts", "no", 0.50, None)])
    assert "BTTS" not in got and omitted["BTTS"] == rep.NO_ELIGIBLE_SIDE         # not above 50%: omitted
    assert "BTTS" not in reps_of([("btts", "yes", 0.49, None)])[0]


# ------------------------------------------------------------------------------------------------ totals
def totals(*lines):
    return [r for line, p_over in lines for r in (("total", "over", p_over, line), ("total", "under", round(1 - p_over, 4), line))]


def test_over_under_uses_the_main_line_never_the_most_probable_one():
    got, _ = reps_of(totals((0.5, 0.93), (1.5, 0.78), (2.5, 0.55), (3.5, 0.30), (4.5, 0.14)))
    assert got["Over/Under"] == ("over", 2.5, 0.55)                            # not Over 0.5 at 93%, not Under 4.5 at 86%
    got, _ = reps_of(totals((1.5, 0.80), (2.5, 0.46), (3.5, 0.27)))
    assert got["Over/Under"] == ("under", 2.5, 0.54)                           # the likelier side of the main line


def test_a_perfectly_balanced_main_line_has_no_likelier_side_and_is_omitted():
    got, omitted = reps_of(totals((2.5, 0.50), (1.5, 0.78)))
    assert "Over/Under" not in got and omitted["Over/Under"] == rep.BALANCED_MAIN_LINE


def test_push_lines_use_the_effective_probability_so_above_50_means_win_more_likely_than_lose():
    win, push, lose = 0.40, 0.25, 0.35                                         # P(win) > P(lose)
    assert win + 0.5 * push > 0.5
    win, push, lose = 0.30, 0.40, 0.30                                         # equal: exactly even, not above 50%
    assert win + 0.5 * push == pytest.approx(0.5)
    got, _ = reps_of(totals((2.0, 0.525), (2.5, 0.60)))                        # the push line (2.0) is the more even one
    assert got["Over/Under"][1] == 2.0 and got["Over/Under"][0] == "over"


def test_lines_quoted_on_one_side_only_are_used_only_when_no_line_has_both():
    both = totals((2.5, 0.60))
    lone = [("total", "over", 0.51, 3.5)]
    assert reps_of(both + lone)[0]["Over/Under"][1] == 2.5
    assert reps_of(lone)[0]["Over/Under"][1] == 3.5


# ------------------------------------------------------------------------------------------------ handicap
def handicaps(*lines):
    """(home line, p of the home side): the away side is the mirror line -L with the complementary probability."""
    return [r for line, p_home in lines for r in (("handicap", "home", p_home, line), ("handicap", "away", round(1 - p_home, 4), -line))]


def test_mirrored_handicap_sides_are_one_line_and_the_likelier_side_represents_it():
    got, _ = reps_of(one_x_two(0.60, 0.22, 0.18) + handicaps((-1.5, 0.47), (-2.5, 0.25), (-0.5, 0.60)) )
    h = got.get("Handicap")
    assert h == ("away", 1.5, 0.53)                                            # home -1.5 and away +1.5 are one line; the likelier side


def test_a_handicap_below_one_goal_restates_a_result_market_and_is_omitted():
    got, omitted = reps_of(one_x_two(0.45, 0.30, 0.25) + handicaps((-0.5, 0.45), (0.5, 0.75)))
    assert "Handicap" not in got and omitted["Handicap"] == rep.HANDICAP_RESTATES_RESULT


def test_a_handicap_that_can_never_win_alongside_another_representative_is_omitted():
    # modal home win, but the main handicap line's likelier side is away +1: if the home team wins, away +1 cannot win
    got, omitted = reps_of(one_x_two(0.45, 0.30, 0.25) + handicaps((-1.0, 0.40)))
    assert got["1X2"][0] == "home" and "Handicap" not in got
    assert omitted["Handicap"] == rep.HANDICAP_CANNOT_WIN_WITH


def test_a_consistent_genuine_margin_handicap_is_kept():
    got, _ = reps_of(one_x_two(0.60, 0.22, 0.18) + handicaps((-1.5, 0.47)))
    assert got["Handicap"] == ("away", 1.5, 0.53)                              # can win together with a home win by one goal


def test_a_far_safe_handicap_line_is_never_chosen_for_being_probable():
    got, _ = reps_of(one_x_two(0.60, 0.22, 0.18) + handicaps((-1.5, 0.47), (-3.5, 0.08), (2.5, 0.99)))
    assert got["Handicap"][1] == 1.5


# ------------------------------------------------------------------------------------------------ pool boundaries
def test_team_totals_period_markets_stats_and_props_are_outside_the_pool():
    e = ev(1)
    lines = cands(e, one_x_two(0.5, 0.3, 0.2) + totals((2.5, 0.55)))
    team = sc(e, "total", "over", 0.62, 1.6, line=1.5)
    team.line.entity = "home"
    first_half = sc(e, "result3", "home", 0.71, 1.4)
    first_half.line.period = "1h"
    corners = sc(e, "total", "over", 0.60, 1.7, line=9.5)
    corners.line.stat = "corners"
    reps, _ = rep.event_representatives(lines + [team, first_half, corners])
    assert set(reps) == {"1X2", "Over/Under"}
    assert all(s.line.entity == "all" and s.line.period == "full" and s.line.stat == "points" for s in reps.values())
    assert reps["Over/Under"].line.line == 2.5


def test_selection_is_deterministic_whatever_the_input_order():
    rows = one_x_two(0.46, 0.30, 0.24) + [("dc", "home_draw", 0.76, None), ("dc", "home_away", 0.70, None), ("dc", "away_draw", 0.54, None)] \
        + totals((1.5, 0.78), (2.5, 0.55), (3.5, 0.31)) + handicaps((-1.5, 0.47), (-0.5, 0.55)) + [("btts", "yes", 0.57, None), ("btts", "no", 0.43, None)]
    base, _ = reps_of(rows)
    for seed in range(5):
        shuffled = list(rows)
        random.Random(seed).shuffle(shuffled)
        assert reps_of(shuffled)[0] == base


def test_family_order_is_stable():
    e = ev(1)
    reps, _ = rep.event_representatives(cands(e, [("btts", "yes", 0.6, None), ("total", "over", 0.55, 2.5), ("total", "under", 0.45, 2.5)] + one_x_two(0.6, 0.2, 0.2)))
    assert list(reps) == ["1X2", "Over/Under", "BTTS"]


# ------------------------------------------------------------------------------------------------ engine
def test_the_engine_keeps_one_prediction_per_event_and_family_and_reports_why_families_are_missing():
    a, b = ev(1), ev(2)
    for e in (a, b):
        e.canonical_event_id = fixture_key(e)
    pool = cands(a, one_x_two(0.5, 0.3, 0.2) + totals((2.5, 0.55)) + [("btts", "yes", 0.50, None), ("btts", "no", 0.50, None)]) \
        + cands(b, [("dc", "home_draw", 0.8, None)])
    gen = representative_predictions(pool)
    d = gen.diagnostics
    assert sorted((s.event.provider_event_id, s.line.market) for s in gen.predictions) == [("1", "result3"), ("1", "total")]
    assert d["valid_predictions_generated"] == 2 and d["fixtures_with_valid_candidates"] == 2 and d["fixtures_with_predictions"] == 1
    assert d["valid_candidate_lines"] == len(pool) and d["representatives_by_family"] == {"1X2": 1, "Over/Under": 1}
    assert d["omitted_families"] == {"BTTS": {rep.NO_ELIGIBLE_SIDE: 1}, "Double Chance": {rep.INCOMPLETE_RESULT: 1}}
    assert d["event_no_valid_market"] == {"no_representative": 1}


def test_every_representative_records_which_rule_chose_it():
    e = ev(1)
    e.canonical_event_id = fixture_key(e)
    gen = representative_predictions(cands(e, one_x_two(0.5, 0.3, 0.2) + totals((1.5, 0.78), (2.5, 0.55))))
    notes = {s.line.market: s.notes["representative"] for s in gen.predictions}
    assert notes["result3"] == {"family": "1X2", "rule": "modal outcome"}
    assert notes["total"] == {"family": "Over/Under", "rule": "main line 2.5"}


# ------------------------------------------------------------------------------------------------ ESPN insight diagnostics
def test_insight_fetcher_outcomes_are_counted_not_just_silently_swallowed():
    # Regression for the ESPN H2H forensic audit: insight_fetcher exceptions
    # were swallowed with zero visibility, making a total fleet-wide outage
    # indistinguishable from an ordinary day with no history. This proves the
    # counts without changing the "never let enrichment break generation" rule.
    a, b, c = ev(1), ev(2), ev(3)
    for e in (a, b, c):
        e.canonical_event_id = fixture_key(e)
    pool = cands(a, one_x_two(0.5, 0.3, 0.2)) + cands(b, one_x_two(0.5, 0.3, 0.2)) + cands(c, one_x_two(0.5, 0.3, 0.2))

    def fetcher(event):
        if event.provider_event_id == "1":
            return {"home_form": ["W"]}
        if event.provider_event_id == "2":
            return None
        raise ValueError("boom")

    gen = representative_predictions(pool, insight_fetcher=fetcher)
    stats = gen.diagnostics["espn_insight"]
    assert stats["attempted"] == 3
    assert stats["returned_data"] == 1
    assert stats["returned_none"] == 2
    assert stats["exception:ValueError"] == 1


def test_insight_fetcher_is_called_at_most_once_per_fixture_regardless_of_market_count():
    e = ev(1)
    e.canonical_event_id = fixture_key(e)
    pool = cands(e, one_x_two(0.5, 0.3, 0.2) + totals((2.5, 0.55)))
    calls = []
    gen = representative_predictions(pool, insight_fetcher=lambda event: calls.append(event) or {"home_form": []})
    assert len(calls) == 1
    assert gen.diagnostics["espn_insight"] == {"attempted": 1, "returned_data": 1}


def test_no_insight_fetcher_configured_reports_no_espn_insight_stats():
    e = ev(1)
    e.canonical_event_id = fixture_key(e)
    gen = representative_predictions(cands(e, one_x_two(0.5, 0.3, 0.2)))
    assert gen.diagnostics["espn_insight"] == {}
