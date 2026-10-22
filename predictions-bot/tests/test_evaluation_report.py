"""Offline evaluation (predbot.jobs.evaluation_report): market baseline vs
independent model vs deployed blended probability, scored against settled
outcomes - read-only measurement, no threshold tuning, explicit low-n flags."""
from __future__ import annotations

import json

import pytest

from predbot.jobs.evaluation_report import (
    MIN_TRUSTED_N,
    brier_score,
    evaluate,
    log_loss,
)


def row(status, p_market, prob, prob_raw=None, independent_p=None, independent_status="ok",
        price=2.0, market="result3", sport="football"):
    sources = {"_notes": {"independent_model": {"p": independent_p, "status": independent_status}}} \
        if independent_p is not None or independent_status != "ok" else {"_notes": {}}
    return {
        "status": status, "p_market": p_market, "prob": prob, "prob_raw": prob_raw,
        "price": price, "market": market, "sport": sport, "sources": json.dumps(sources),
    }


class Row(dict):
    """Minimal sqlite3.Row stand-in: dict-style __getitem__ plus .keys()."""
    def keys(self):
        return dict.keys(self)


def rows(*dicts):
    return [Row(d) for d in dicts]


def test_brier_and_log_loss_match_hand_computation():
    pairs = [(0.8, 1.0), (0.3, 0.0), (0.6, 1.0)]
    assert brier_score(pairs) == pytest.approx(((0.2) ** 2 + (0.3) ** 2 + (0.4) ** 2) / 3)
    assert log_loss(pairs) == pytest.approx(
        -(math_log(0.8) + math_log(0.7) + math_log(0.6)) / 3, abs=1e-9)
    assert brier_score([]) is None and log_loss([]) is None


def math_log(x):
    import math
    return math.log(x)


def test_evaluate_separates_market_independent_and_blended_approaches():
    data = rows(
        row("won", p_market=0.70, prob=0.72, prob_raw=0.71, independent_p=0.65),
        row("lost", p_market=0.60, prob=0.62, prob_raw=0.61, independent_p=0.55),
    )
    report = evaluate(data)
    assert report["sample_total_resolved"] == 2
    assert report["by_approach"]["market_baseline"]["n"] == 2
    assert report["by_approach"]["blended_deployed"]["n"] == 2
    assert report["by_approach"]["independent_model"]["n"] == 2
    # prob_raw preferred over prob for the blended approach (same discipline as Calibrator._raw_probability).
    blended_pairs_avg = report["by_approach"]["blended_deployed"]["avg_predicted_probability"]
    assert blended_pairs_avg == pytest.approx((0.71 + 0.61) / 2)


def test_independent_model_rows_without_status_ok_are_excluded_never_substituted():
    data = rows(
        row("won", p_market=0.70, prob=0.72, independent_p=None, independent_status="unavailable:no_history"),
    )
    report = evaluate(data)
    assert "independent_model" not in report["by_approach"]  # never a fake n=0 block either
    assert report["by_approach"]["market_baseline"]["n"] == 1  # unaffected


def test_small_samples_are_reported_but_flagged_low_confidence_never_hidden():
    data = rows(row("won", p_market=0.7, prob=0.7, independent_p=0.7))
    report = evaluate(data)
    for block in report["by_approach"].values():
        if block["n"]:
            assert block["n"] < MIN_TRUSTED_N
            assert block["low_confidence"] is True


def test_a_sample_at_or_above_the_trusted_floor_is_not_flagged():
    data = rows(*[row("won" if i % 2 == 0 else "lost", p_market=0.6, prob=0.6) for i in range(MIN_TRUSTED_N)])
    report = evaluate(data)
    assert report["by_approach"]["market_baseline"]["n"] == MIN_TRUSTED_N
    assert report["by_approach"]["market_baseline"]["low_confidence"] is False


def test_ev_distribution_uses_blended_probability_and_real_price_never_fabricated():
    data = rows(row("won", p_market=0.7, prob=0.75, prob_raw=0.75, price=1.5))
    report = evaluate(data)
    assert report["ev_distribution"]["n"] == 1
    assert report["ev_distribution"]["avg"] == pytest.approx(0.75 * 1.5 - 1.0)


def test_pending_and_void_statuses_never_enter_any_metric():
    data = rows(
        {"status": "pending", "p_market": 0.9, "prob": 0.9, "prob_raw": None, "price": 2.0,
         "market": "result3", "sport": "football", "sources": "{}"},
        {"status": "void", "p_market": 0.9, "prob": 0.9, "prob_raw": None, "price": 2.0,
         "market": "result3", "sport": "football", "sources": "{}"},
    )
    report = evaluate([Row(d) for d in data])
    assert report["sample_total_resolved"] == 2  # counted as "rows seen", not "scored"
    assert report["by_approach"] == {}  # but nothing pending/void ever contributes a metric
    assert report["resolved_but_unscored"] == {"pending": 1, "void": 1}, "visible, not silently dropped"


def test_push_is_resolved_but_unscored_same_as_void():
    data = rows(row("push", p_market=0.6, prob=0.6))
    report = evaluate(data)
    assert report["resolved_but_unscored"] == {"push": 1}
    assert report["by_approach"] == {}


def test_a_probability_outside_0_1_is_excluded_and_counted_never_silently_clamped():
    data = rows(row("won", p_market=1.4, prob=0.6, prob_raw=0.6))
    report = evaluate(data)
    assert "market_baseline" not in report["by_approach"] or report["by_approach"]["market_baseline"]["n"] == 0
    assert report["invalid_probability_excluded"] == {"market_baseline": 1}
    assert report["by_approach"]["blended_deployed"]["n"] == 1  # the other, valid source is unaffected

    negative = rows(row("won", p_market=-0.2, prob=0.6, prob_raw=0.6))
    report2 = evaluate(negative)
    assert report2["invalid_probability_excluded"] == {"market_baseline": 1}


def test_half_won_and_half_lost_use_the_same_0_75_0_25_convention_as_calibration_py():
    half_won = rows(row("half_won", p_market=0.6, prob=0.6, prob_raw=0.6))
    half_lost = rows(row("half_lost", p_market=0.6, prob=0.6, prob_raw=0.6))
    assert evaluate(half_won)["by_approach"]["market_baseline"]["realized_outcome_rate"] == pytest.approx(0.75)
    assert evaluate(half_lost)["by_approach"]["market_baseline"]["realized_outcome_rate"] == pytest.approx(0.25)


def test_build_report_runs_the_real_query_against_a_real_sqlite_database(db):
    # Integration check: settled_rows()'s actual SQL (not just evaluate()'s
    # pure logic) against the real `picks` schema/migrations.
    from predbot.jobs.evaluation_report import build_report

    db.conn.execute(
        "INSERT INTO picks (pick_date, tier, rank, provider, provider_event_id, sport, home, away, "
        "start_utc, market, period, stat, entity, selection, label, price, prob, prob_raw, ev, p_market, "
        "sources, status, created_at) VALUES ('2030-01-01','all',0,'t','1','football','A','B', "
        "'2030-01-01T00:00:00Z','result3','full','points','all','home','A win',1.9,0.7,0.68,0.33,0.6, "
        "'{}','won','2030-01-01T00:00:00Z')"
    )
    db.conn.commit()
    report = build_report(db)
    assert report["sample_total_resolved"] == 1
    assert report["by_approach"]["market_baseline"]["n"] == 1
    assert report["by_approach"]["blended_deployed"]["n"] == 1
