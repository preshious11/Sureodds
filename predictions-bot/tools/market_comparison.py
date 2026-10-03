"""Does the independent football model contain information the bookmakers' prices do not?

    python tools/market_comparison.py --db data/predbot.db [--validation-start 2025-08-01] [--test-start 2026-01-01] [--json out.json]

Offline. Reads results and the stored reference odds; never calls a provider. Every bookmaker (pinnacle, bet365,
market_avg) is evaluated SEPARATELY, on its closing prices and on its earlier pre-match prices - books are never mixed.
The odds are evaluation data only: no model input depends on them, hyper-parameters come from market-free validation
walks, and blend weights are chosen on the validation window alone. Models are fixed before the test window is scored.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from predbot.backtest.football import from_history, log_loss, run_dated_backtest  # noqa: E402
from predbot.history.store import HistoryStore  # noqa: E402
from predbot.model.football_strength import StrengthParams  # noqa: E402
from predbot.storage.db import Database  # noqa: E402

BOOKS = ("pinnacle", "bet365", "market_avg")
KINDS = ("close", "pre")


def _date(text):
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


def _key(p):
    return (p.match.kickoff.isoformat(), p.match.home, p.match.away)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/predbot.db")
    ap.add_argument("--validation-start", default="2025-08-01")
    ap.add_argument("--test-start", default="2026-01-01")
    ap.add_argument("--json")
    args = ap.parse_args(argv)
    v, t = _date(args.validation_start), _date(args.test_start)
    store = HistoryStore(Database(args.db))
    reports, kept = {}, {}
    params = elo = None
    for kind in KINDS:
        for book in BOOKS:
            name = f"{book}:{kind}"
            matches = from_history(store, None, baseline=(book, kind))
            keep: dict = {}
            r = run_dated_backtest(matches, v, t, params=params, elo=elo, select=params is None, keep=keep)
            if params is None:                                       # selection is market-free: do it once, reuse it
                sel = r["selected"]
                params = StrengthParams(half_life_days=sel["strength"]["half_life_days"], prior_strength=sel["strength"]["prior_strength"])
                elo = (sel["elo"]["k_factor"], sel["elo"]["home_adv"])
            reports[name] = r
            kept[name] = keep
    out = {"boundaries": reports["pinnacle:close"]["boundaries"], "selected": reports["pinnacle:close"]["selected"], "baselines": reports}

    # ---- common sample: test matches that every book quotes (closing), same matches for every source
    common_keys = None
    for book in BOOKS:
        ks = {_key(p) for p in kept[f"{book}:close"]["test"] if "market" in p.p1x2}
        common_keys = ks if common_keys is None else common_keys & ks
    table = {}
    base = {_key(p): p for p in kept["pinnacle:close"]["test"] if _key(p) in common_keys}
    for src in ("climatology", "elo", "poisson", "poisson+elo"):
        table[src] = round(log_loss([(p.p1x2[src], p.match.outcome) for p in base.values()]), 5)
    for book in BOOKS:
        mine = {_key(p): p for p in kept[f"{book}:close"]["test"] if _key(p) in common_keys}
        for src in ("market", "poisson+market", "poisson+elo+market"):
            table[f"{book}:{src}"] = round(log_loss([(mine[k].p1x2[src], mine[k].match.outcome) for k in mine if src in mine[k].p1x2]), 5)
    out["common_sample_closing"] = {"n": len(common_keys), "log_loss": table}
    if args.json:
        Path(args.json).write_text(json.dumps(out, indent=1), encoding="utf8")

    print("boundaries:", json.dumps(out["boundaries"]))
    print("selected (market-free validation):", json.dumps(out["selected"]))
    print(f"\nCOMMON SAMPLE (test matches quoted by all three books at close), n={len(common_keys)}")
    for k, val in table.items():
        print(f"  {k:32s} {val:.5f}")
    for name, r in reports.items():
        if "error" in r:
            print(name, r)
            continue
        print(f"\n===== {name}: test n with market = {r['counts']['test_with_market']} of {r['counts']['test_predicted']} predicted; validation weights:")
        print("  ", json.dumps({k: v_["weights"] for k, v_ in r["blend_weights_chosen_on_validation"].items() if "market" in k}),
              "validation n", {k: v_["validation_n"] for k, v_ in r["blend_weights_chosen_on_validation"].items() if "market" in k})
        print(f"  {'model':20s} {'n':>5s} {'logloss':>8s} {'brier':>8s} {'rps':>8s} {'ECE':>7s}")
        for m, row in r["test_1x2_with_market"].items():
            print(f"  {m:20s} {row['n']:5d} {row['log_loss']:8.4f} {row['brier']:8.4f} {row['rps']:8.4f} {row['calibration']['ece']:7.4f}")
        for k, pr in r["paired_vs_baselines"].items():
            if k.startswith("market vs") and pr:
                print(f"  paired {k:34s} diff={pr['mean_ll_a_minus_b']:+.5f} se={pr['se']:.5f} z={pr['z']:+.2f}  (positive: second is better)")
    for name in ("market_avg:close", "bet365:close", "pinnacle:close"):
        _detail(name, reports[name])
    return out


def _detail(name, r):
    print(f"########## DETAIL {name}")
    d = r["disagreement_bins"]
    print(f"disagreement poisson - market: {d['n_matches']} matches, {d['n_instances']} outcome instances; slope {d['slope_outcome_minus_market_on_delta']}")
    for b in d["bins"]:
        print(f"  |delta| {b['gap_pp']:>6s}pp n={b['n']:5d} brier poisson={b['brier_poisson']:.4f} market={b['brier_market']:.4f} "
              f"logloss poisson={b['logloss_poisson']:.4f} market={b['logloss_market']:.4f} obs={b['observed']:.3f} "
              f"p_pois={b['mean_p_poisson']:.3f} p_mkt={b['mean_p_market']:.3f} closer={b['closer_by_brier']}")
    for c in d["cells"]:
        print(f"    {c['gap_pp']:>6s}pp {c['direction']:8s} n={c['n']:5d} obs={c['observed']:.3f} p_pois={c['mean_p_poisson']:.3f} "
              f"p_mkt={c['mean_p_market']:.3f} brier pois={c['brier_poisson']:.4f} mkt={c['brier_market']:.4f} closer={c['closer_by_brier']}")
    print("by competition (1X2 log-loss; paired market-minus-model z, positive favours second):")
    for comp, row in r["test_by_competition_with_market"].items():
        pr = r["test_by_competition_paired_vs_market"].get(comp, {}).get("market vs poisson")
        print(f"  {comp:12s} n={row['market']['n']:4d} market={row['market']['log_loss']:.4f} poisson={row['poisson']['log_loss']:.4f} "
              f"poisson+elo={row['poisson+elo']['log_loss']:.4f} elo={row['elo']['log_loss']:.4f} paired(market vs poisson) diff={pr['mean_ll_a_minus_b']:+.4f} z={pr['z']:+.2f}")
    ou = r["test_over25_with_market"]
    if isinstance(ou, dict):
        print(f"Over 2.5 with market (n={r['test_over25_with_market_n']}):")
        for m, row in ou["over25"].items():
            print(f"  {m:12s} logloss={row['log_loss']:.4f} brier={row['brier']:.4f} ece={row['reliability']['ece']} base_rate={row['base_rate']}")


if __name__ == "__main__":
    main()
