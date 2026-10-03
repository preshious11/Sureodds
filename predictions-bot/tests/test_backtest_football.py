"""Backtest integrity: no future leakage, validation-only weight selection, sane scoring (synthetic data)."""
from __future__ import annotations

import math
from dataclasses import replace
from datetime import timedelta

import pytest

from predbot.backtest.football import (BTMatch, add_blends, binary_log_loss, brier, calibration, from_history, log_loss, pool,
                                       rps, run_backtest, walk_forward)
from predbot.backtest.synthetic import synthetic_league
from predbot.model.football_strength import StrengthParams
from tests.history_helpers import load_history

PARAMS = StrengthParams(half_life_days=365.0, prior_strength=6.0)


@pytest.fixture(scope="module")
def league():
    matches, oracle = synthetic_league(seed=5, teams=10, seasons=3)
    return matches, oracle


def _key(p):
    return (p.match.kickoff, p.match.home, p.match.away)


def test_scoring_rules_on_known_cases():
    uniform = (1 / 3, 1 / 3, 1 / 3)
    assert log_loss([(uniform, 0), (uniform, 2)]) == pytest.approx(math.log(3))
    assert brier([((1.0, 0.0, 0.0), 0)]) == 0.0 and brier([((0.0, 1.0, 0.0), 0)]) == pytest.approx(2.0)
    assert rps([((1.0, 0.0, 0.0), 0)]) == 0.0
    assert binary_log_loss([(0.5, 1), (0.5, 0)]) == pytest.approx(math.log(2))
    perfect = calibration([((1.0, 0.0, 0.0), 0)] * 10)
    assert perfect["ece"] == 0.0
    assert pool([(0.5, 0.3, 0.2), (0.5, 0.3, 0.2)], (0.5, 0.5)) == pytest.approx((0.5, 0.3, 0.2))


def test_changing_the_future_cannot_change_earlier_predictions(league):
    matches, _ = league
    base = {_key(p): p for p in walk_forward(matches, PARAMS)}
    cut = matches[int(len(matches) * 0.7)].kickoff
    mutated = [replace(m, hg=(m.hg + 4), ag=0) if m.kickoff >= cut else m for m in matches]
    changed = {_key(p): p for p in walk_forward(mutated, PARAMS)}
    earlier = [k for k in base if k[0] < cut]
    assert earlier and all(k in changed for k in earlier)
    for k in earlier:
        assert base[k].p1x2["poisson"] == changed[k].p1x2["poisson"]
        assert base[k].p1x2["elo"] == changed[k].p1x2["elo"]


def test_a_match_is_predicted_before_it_is_learned(league):
    matches, _ = league
    # replace ONE mid-sample result by an absurd score: its own prediction must not move, later ones may
    target = matches[len(matches) // 2]
    mutated = [replace(m, hg=20, ag=0) if m is target else m for m in matches]
    a = {_key(p): p for p in walk_forward(matches, PARAMS)}
    b = {_key(p): p for p in walk_forward(mutated, PARAMS)}
    k = (target.kickoff, target.home, target.away)
    assert a[k].p1x2["elo"] == b[k].p1x2["elo"] and a[k].p1x2["poisson"] == b[k].p1x2["poisson"]


def test_warmup_matches_are_training_only(league):
    matches, _ = league
    preds = walk_forward(matches, PARAMS, warmup_days=240)
    assert min(p.match.kickoff for p in preds) >= matches[0].kickoff + timedelta(days=240)


def test_blend_weights_are_chosen_on_validation_only_and_test_results_cannot_move_them(league):
    matches, _ = league
    full = run_backtest(matches, PARAMS)
    preds = walk_forward(matches, PARAMS)
    from predbot.backtest.football import split_point
    cut = split_point(preds, 0.4)
    boundary = preds[cut - 1].match.kickoff                  # last validation kickoff: mutate strictly after it
    mutated = [replace(m, hg=(m.hg + 3), ag=0) if m.kickoff > boundary else m for m in matches]
    again = run_backtest(mutated, PARAMS)
    assert full["blend_weights_chosen_on_validation"] == again["blend_weights_chosen_on_validation"]
    assert full["validation_window"][1] < full["test_window"][0]


def test_report_has_scores_for_every_model_and_a_calibration_table(league):
    matches, _ = league
    report = run_backtest(matches, PARAMS)
    models = report["test_matches_with_market"]
    assert {"market", "elo", "poisson", "poisson+elo", "poisson+market", "poisson+elo+market"} <= set(models)
    for row in models.values():
        assert 0.5 < row["log_loss"] < 1.5 and 0 <= row["calibration"]["ece"] < 0.2 and row["calibration"]["bins"]
    assert report["test_all_matches"]["poisson"]["over25_log_loss"] > 0


def test_the_strength_model_beats_a_no_information_baseline_and_approaches_the_oracle(league):
    matches, oracle = league
    index = {(m.kickoff, m.home, m.away): i for i, m in enumerate(matches)}
    preds = walk_forward(matches, PARAMS)
    test = preds[int(len(preds) * 0.4):]
    model = log_loss([(p.p1x2["poisson"], p.match.outcome) for p in test])
    best = log_loss([(oracle[index[_key(p)]], p.match.outcome) for p in test])
    assert model < math.log(3) - 0.05 and abs(model - best) < 0.05      # a sample can favour either by luck


def test_backtest_reads_history_from_the_store_with_odds(db):
    ids, matches, store = load_history(db, seed=3, seasons=2, teams=8)
    loaded = from_history(store, ["epl"])
    assert len(loaded) == len(matches) and all(m.market is None for m in loaded)         # synthetic store has no odds
    assert [m.kickoff for m in loaded] == sorted(m.kickoff for m in loaded)


# ------------------------------------------------------------------------------------------------ dated protocol
from collections import Counter  # noqa: E402

from predbot.backtest.football import disagreement, run_dated_backtest, select_elo, select_strength  # noqa: E402


def _bounds(matches):
    ks = sorted(m.kickoff for m in matches)
    return ks[int(len(ks) * 0.45)], ks[int(len(ks) * 0.7)]


def test_dated_selection_and_weights_never_read_the_test_window(league):
    matches, _ = league
    v, t = _bounds(matches)
    base = run_dated_backtest(matches, v, t)
    mutated = [replace(m, hg=m.hg + 5, ag=0) if m.kickoff >= t else m for m in matches]
    again = run_dated_backtest(mutated, v, t)
    assert base["selected"] == again["selected"]
    assert base["blend_weights_chosen_on_validation"] == again["blend_weights_chosen_on_validation"]
    assert base["selection_grids"] == again["selection_grids"]
    assert base["validation_1x2"] == again["validation_1x2"] and base["test_1x2"] != again["test_1x2"]
    assert base["boundaries"]["validation"][1] < t.isoformat() <= base["boundaries"]["test"][0]


def test_selection_walks_stop_at_the_boundary(league):
    matches, _ = league
    v, t = _bounds(matches)
    mutated = [replace(m, hg=m.hg + 5, ag=0) if m.kickoff >= t else m for m in matches]
    assert select_elo(matches, v, t) == select_elo(mutated, v, t)
    assert select_strength(matches, v, t, half_lives=(180.0,), priors=(2.0,)) == select_strength(mutated, v, t, half_lives=(180.0,), priors=(2.0,))


def test_a_result_is_not_learned_by_a_match_kicking_off_with_it():
    """Two fixtures with one kickoff instant are both unfinished when either is predicted."""
    from datetime import datetime, timezone
    t0 = datetime(2025, 1, 1, tzinfo=timezone.utc)
    base = [BTMatch(t0 + timedelta(days=7 * i), "x", f"h{i % 4}", f"a{(i + 1) % 4}", 1 + i % 2, i % 3) for i in range(60)]
    last = t0 + timedelta(days=500)
    pair = [BTMatch(last, "x", "h0", "a1", 1, 0), BTMatch(last, "x", "h0", "a2", 1, 0)]
    changed = [replace(pair[0], hg=9, ag=0), pair[1]]
    kw = dict(score_from=last, with_strength=False)
    a = {p.match.away: p for p in walk_forward(base + pair, PARAMS, **kw)}
    b = {p.match.away: p for p in walk_forward(base + changed, PARAMS, **kw)}
    assert a["a2"].p1x2["elo"] == b["a2"].p1x2["elo"] and a["a2"].p1x2["climatology"] == b["a2"].p1x2["climatology"]


def test_until_never_predicts_or_reads_matches_at_or_after_it(league):
    matches, _ = league
    _, t = _bounds(matches)
    preds = walk_forward(matches, PARAMS, until=t)
    assert preds and max(p.match.kickoff for p in preds) < t


def test_report_contains_baselines_breakdowns_and_paired_differences(league):
    matches, _ = league
    v, t = _bounds(matches)
    r = run_dated_backtest(matches, v, t, select=False)
    assert {"climatology", "elo", "poisson", "poisson+elo"} <= set(r["test_1x2"])
    assert set(r["test_binary_markets"]) == {"over25", "btts"} and r["test_by_competition"] and r["test_by_period"]
    assert r["paired_vs_baselines"]["climatology vs poisson"]["n"] == r["counts"]["test_predicted"]
    assert r["counts"]["test_predicted"] + sum(r["counts"]["test_skipped"].values()) == r["counts"]["test_matches_in_window"]


def test_without_odds_there_is_no_market_baseline_and_nothing_is_invented():
    from datetime import datetime, timezone
    matches, _ = synthetic_league(seed=9, teams=10, seasons=3)
    stripped = [replace(m, market=None) for m in matches]
    v, t = _bounds(stripped)
    r = run_dated_backtest(stripped, v, t, select=False)
    assert r["test_1x2_with_market"] == "no historical odds in the store" and r["counts"]["test_with_market"] == 0
    assert "market" not in r["test_1x2"] and not any("market" in k for k in r["blend_weights_chosen_on_validation"])
    assert r["disagreement_poisson_vs_market"] == "no historical odds in the store"


def test_disagreement_bins_cover_every_match_and_order_by_gap(league):
    matches, _ = league
    v, t = _bounds(matches)
    r = run_dated_backtest(matches, v, t, select=False)
    assert r["counts"]["test_with_market"] > 0
    d = r["disagreement_poisson_vs_market"]
    assert sum(b["n"] for b in d["bins"]) == d["n"]
    lows = [b["gap_range"][0] for b in d["bins"]]
    assert lows == sorted(lows)
