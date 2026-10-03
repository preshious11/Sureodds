"""Offline football backtest. Reads results from a database's history store or generates a SYNTHETIC league.

    python tools/backtest_football.py --synthetic [--seed 1] [--select]
    python tools/backtest_football.py --db data/predbot.db --validation-start 2025-08-01 --test-start 2026-01-01 [--json out.json]

Never touches the network. With ``--validation-start/--test-start`` the dated protocol runs: everything before the
validation start is training only, hyper-parameters (strength half-life and prior, Elo K and home advantage) and blend
weights are chosen on the validation window alone (selection walks stop at the test start), and the headline numbers
come from the untouched test window. Without dates (synthetic or quick checks) the fraction-based split is used."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from predbot.backtest.football import from_history, run_backtest, run_dated_backtest, select_hyperparameters  # noqa: E402
from predbot.model.football_strength import StrengthParams  # noqa: E402


def _table(title, rows):
    print(f"\n{title}\n{'model':22s} {'n':>5s} {'logloss':>8s} {'brier':>8s} {'rps':>8s} {'ECE':>7s}")
    for name, r in rows.items():
        print(f"{name:22s} {r['n']:5d} {r['log_loss']:8.4f} {r['brier']:8.4f} {r['rps']:8.4f} {r['calibration']['ece']:7.4f}")


def _date(text):
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


def print_dated(report):
    if "error" in report:
        print(report)
        return
    print("boundaries:", json.dumps(report["boundaries"]))
    print("counts:", json.dumps(report["counts"]))
    print("selected:", json.dumps(report["selected"]))
    print("blend weights (validation):", json.dumps(report["blend_weights_chosen_on_validation"]))
    _table("VALIDATION 1X2", report["validation_1x2"])
    _table("TEST 1X2 (untouched)", report["test_1x2"])
    if isinstance(report["test_1x2_with_market"], dict):
        _table("TEST 1X2, matches with market odds", report["test_1x2_with_market"])
    else:
        print("\nmarket baseline:", report["test_1x2_with_market"])
    for market, rows in report["test_binary_markets"].items():
        print(f"\nTEST {market}")
        for name, r in rows.items():
            print(f"  {name:12s} n={r['n']} logloss={r['log_loss']:.4f} brier={r['brier']:.4f} base_rate={r['base_rate']} ece={r['reliability']['ece']}")
    print("\nBY COMPETITION (test)")
    for comp, row in report["test_by_competition"].items():
        print(f"  {comp:12s} " + "  ".join(f"{n}={r['log_loss']:.4f}(n={r['n']})" for n, r in row.items()))
    print("\nBY PERIOD (test)")
    for per, row in report["test_by_period"].items():
        print(f"  {per:8s} " + "  ".join(f"{n}={r['log_loss']:.4f}(n={r['n']})" for n, r in row.items()))
    print("\nPAIRED log-loss differences (positive: second model better)")
    for k, v in report["paired_vs_baselines"].items():
        print(f"  {k:28s} {v}")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--db")
    ap.add_argument("--competitions", default="")
    ap.add_argument("--synthetic", action="store_true")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--select", action="store_true")
    ap.add_argument("--validation-start")
    ap.add_argument("--test-start")
    ap.add_argument("--json")
    args = ap.parse_args(argv)
    label = "REAL history"
    if args.synthetic:
        from predbot.backtest.synthetic import synthetic_league
        matches, _ = synthetic_league(seed=args.seed)
        label = f"SYNTHETIC league (seed {args.seed}) - machinery check only, says nothing about real football"
    else:
        if not args.db:
            ap.error("give --db or --synthetic")
        from predbot.history.store import HistoryStore
        from predbot.storage.db import Database
        matches = from_history(HistoryStore(Database(args.db)), [c for c in args.competitions.split(",") if c] or None)
    if args.validation_start and args.test_start:
        report = run_dated_backtest(matches, _date(args.validation_start), _date(args.test_start))
        report["data"] = label
        if args.json:
            Path(args.json).write_text(json.dumps(report, indent=1), encoding="utf8")
        print(label)
        print_dated(report)
        return report
    params = StrengthParams()
    selection = None
    if args.select:
        selection = select_hyperparameters(matches)
        if selection["best"]:
            params = StrengthParams(half_life_days=selection["best"]["half_life_days"], prior_strength=selection["best"]["prior_strength"])
    report = run_backtest(matches, params)
    report["data"] = label
    report["hyperparameter_selection"] = selection
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=1), encoding="utf8")
    print(label)
    if "error" in report:
        print(report["error"])
        return report
    print(f"matches {report['matches_total']} | predicted {report['predicted']} | validation {report['validation_n']} | test {report['test_n']}")
    print("weights (validation):", json.dumps(report["blend_weights_chosen_on_validation"]))
    for title, key in (("TEST, all matches", "test_all_matches"), (f"TEST, matches with market odds (n={report['test_with_market_n']})", "test_matches_with_market")):
        _table(title, report[key])
    return report


if __name__ == "__main__":
    main()
