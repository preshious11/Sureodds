"""Offline-only evaluation: market baseline vs the independent football model
vs the deployed blended p_model, against REAL settled outcomes.

Never wired into live scoring - predbot/model/independent.py's own
INDEPENDENT_MODEL_MODE gate (default "shadow") is the only thing that
decides whether the independent estimate drives p_model in production, and
this module has no path back into that decision. It exists purely to read
back what was already recorded (picks.p_market / picks.prob / picks.sources
-> _notes.independent_model / _notes.model_basis - see
predbot/jobs/daily_picks.py:prediction_row and ensemble.py:447-463) and
score it against settled results, so the three approaches can be compared
on real history instead of guessed at.

Deliberately does NOT tune any threshold, does NOT decide a policy, and
flags every metric's sample size - a result from fewer than MIN_TRUSTED_N
settled picks is reported, never hidden, but marked low-confidence so it
cannot be mistaken for a validated finding. No data leakage: nothing here
reads or writes a threshold, so there is nothing to leak between a
"training" and "evaluation" split - this is read-only measurement of
already-settled history, not a fitting procedure.
"""
from __future__ import annotations

import json
import math
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

from predbot.storage.db import Database

OUTCOME_SCORE = {"won": 1.0, "lost": 0.0, "half_won": 0.75, "half_lost": 0.25}
# push/void are RESOLVED (the fixture happened) but carry no win/loss signal
# at all - included in the query so the report can show how many resolved
# picks exist in total, same "settled" universe calibration.py's own
# all_settled_for_calibration() uses, but excluded from every metric below
# exactly like calibration.py's brier()/refit() already exclude them (the
# OUTCOME_SCORE dict simply has no entry for them, same discipline).
RESOLVED_STATUSES = ("won", "lost", "half_won", "half_lost", "push", "void")
MIN_TRUSTED_N = 30


def settled_rows(db: Database, limit: int = 5000) -> List:
    """Every RESOLVED prediction row (won/lost/half_won/half_lost/push/void)
    with enough fields to compare all three probability sources. Read-only.
    Still-pending picks are never included - there is no outcome to score
    them against yet, never a guessed one."""
    return db.query(
        "SELECT pick_date, tier, sport, market, period, prob, prob_raw, p_market, price, status, sources "
        f"FROM picks WHERE status IN {RESOLVED_STATUSES} ORDER BY id DESC LIMIT ?",
        (limit,),
    )


def _valid_probability(value) -> Optional[float]:
    """A probability outside [0, 1] is corrupt data, not a value to clamp
    into range (clamping would hide a real data-quality problem behind a
    plausible-looking number) - excluded from every metric, counted
    separately so a corrupt-data problem is visible rather than silent."""
    if value is None or isinstance(value, bool):
        return None
    try:
        p = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(p) or not 0.0 <= p <= 1.0:
        return None
    return p


def _independent_probability(sources_json: Optional[str]) -> Optional[float]:
    """Only when the independent model actually priced this line (status
    'ok') - never a market-derived fallback value mistaken for one."""
    try:
        notes = (json.loads(sources_json or "{}").get("_notes") or {})
    except (TypeError, ValueError):
        return None
    model = notes.get("independent_model") or {}
    if model.get("status") != "ok":
        return None
    return _valid_probability(model.get("p"))


def _blended_probability(row) -> Optional[float]:
    """prob_raw (pre-calibration) when VALID, else prob - same fallback
    Calibrator._raw_probability() already uses, for the same reason: never
    score a probability against the outcome it was itself calibrated on. A
    present-but-corrupt prob_raw still falls back to prob (a bad value in
    one column must never discard a good value in the other)."""
    raw = row["prob_raw"] if "prob_raw" in row.keys() else None
    valid_raw = _valid_probability(raw) if raw is not None else None
    if valid_raw is not None:
        return valid_raw
    prob = row["prob"] if "prob" in row.keys() else None
    return _valid_probability(prob)


def brier_score(pairs: List[Tuple[float, float]]) -> Optional[float]:
    if not pairs:
        return None
    return sum((p - y) ** 2 for p, y in pairs) / len(pairs)


def log_loss(pairs: List[Tuple[float, float]], eps: float = 1e-6) -> Optional[float]:
    if not pairs:
        return None
    total = 0.0
    for p, y in pairs:
        pc = min(max(p, eps), 1 - eps)
        total += -(y * math.log(pc) + (1 - y) * math.log(1 - pc))
    return total / len(pairs)


def _metric_block(pairs: List[Tuple[float, float]]) -> Dict:
    n = len(pairs)
    avg_p = sum(p for p, _ in pairs) / n if n else None
    realized = sum(y for _, y in pairs) / n if n else None
    return {
        "n": n,
        "low_confidence": n < MIN_TRUSTED_N,
        "brier": brier_score(pairs),
        "log_loss": log_loss(pairs),
        "avg_predicted_probability": avg_p,
        "realized_outcome_rate": realized,
    }


def evaluate(rows: List) -> Dict:
    """Builds the comparison report. ``rows`` must come from settled_rows()
    (or an equivalent query) - this function does no DB access itself, so
    it's trivially testable against synthetic rows.

    Duplicate settlements: not re-guarded here by design - predbot/storage/
    db.py's own `_store_representative()` already enforces at most one
    `picks` row per (pick_date, fixture, market) in the 'all' collection
    (first-write-wins, never replaced), so a settled row counted once here
    is one real forecast, not a repeat. This function trusts that
    invariant rather than re-deriving fixture identity from scratch."""
    approach_pairs: Dict[str, List[Tuple[float, float]]] = defaultdict(list)
    by_market: Dict[str, List[Tuple[float, float]]] = defaultdict(list)
    by_sport: Dict[str, List[Tuple[float, float]]] = defaultdict(list)
    ev_values: List[float] = []
    resolved_unscored = defaultdict(int)       # push/void: resolved, no win/loss signal
    invalid_probability = defaultdict(int)     # present but outside [0, 1] - never silently clamped

    for r in rows:
        y = OUTCOME_SCORE.get(r["status"])
        if y is None:
            resolved_unscored[r["status"]] += 1
            continue
        market_raw = r["p_market"] if "p_market" in r.keys() else None
        market_p = _valid_probability(market_raw)
        if market_raw is not None and market_p is None:
            invalid_probability["market_baseline"] += 1
        blended_p = _blended_probability(r)
        independent_p = _independent_probability(r["sources"] if "sources" in r.keys() else None)
        if market_p is not None:
            approach_pairs["market_baseline"].append((market_p, y))
        if blended_p is not None:
            approach_pairs["blended_deployed"].append((blended_p, y))
        if independent_p is not None:
            approach_pairs["independent_model"].append((independent_p, y))
        if blended_p is not None:
            by_market[r["market"]].append((blended_p, y))
            by_sport[r["sport"]].append((blended_p, y))
            price = r["price"] if "price" in r.keys() else None
            if price:
                ev_values.append(blended_p * float(price) - 1.0)

    return {
        "sample_total_resolved": len(rows),
        "resolved_but_unscored": dict(resolved_unscored),
        "invalid_probability_excluded": dict(invalid_probability),
        "by_approach": {name: _metric_block(pairs) for name, pairs in approach_pairs.items()},
        "by_market": {m: _metric_block(pairs) for m, pairs in sorted(by_market.items())},
        "by_sport": {s: _metric_block(pairs) for s, pairs in sorted(by_sport.items())},
        "ev_distribution": {
            "n": len(ev_values),
            "avg": sum(ev_values) / len(ev_values) if ev_values else None,
            "min": min(ev_values) if ev_values else None,
            "max": max(ev_values) if ev_values else None,
            "low_confidence": len(ev_values) < MIN_TRUSTED_N,
        },
    }


def build_report(db: Database, limit: int = 5000) -> Dict:
    """The one entry point: read settled history, evaluate it, return the
    report dict (callers print/json-dump it - this module never writes
    anywhere, never mutates `picks`, and takes no action on its output)."""
    return evaluate(settled_rows(db, limit))


if __name__ == "__main__":
    import sys

    from predbot.storage.db import Database as _Database

    db_path = sys.argv[1] if len(sys.argv) > 1 else "sureodds.db"
    print(json.dumps(build_report(_Database(db_path)), indent=2, default=str))
