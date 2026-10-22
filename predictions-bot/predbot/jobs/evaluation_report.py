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

Also reports (all additive, all read-only, same discipline as above):
  * coverage by competition (``by_competition``) and the market/sport breakdowns already had,
  * a missing-data rate per approach (``missing_data_rate`` - how often a resolved, scorable pick had NO usable
    probability for that approach; the independent model never reaching status "ok" in production shows up here
    as a high missing rate instead of hiding inside a small-but-silent "n"),
  * a normal-approximation 95% CI on each bucket's realized outcome rate (``realized_outcome_rate_ci95`` in
    ``_metric_block``) - reported ALONGSIDE ``low_confidence``, never instead of it,
  * a chronological (default weekly) breakdown (``evaluate_by_period`` / ``build_report``'s ``"chronological"``
    key) instead of one pooled number, and
  * an explicit, honestly-scoped temporal sanity check (``temporal_leakage_check``) - see its docstring for
    exactly what it does and does not verify.
"""
from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import datetime
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
CI_Z_95 = 1.96


def settled_rows(db: Database, limit: int = 5000) -> List:
    """Every RESOLVED prediction row (won/lost/half_won/half_lost/push/void)
    with enough fields to compare all three probability sources, plus the
    coverage/chronology columns the extended report needs (competition,
    fixture identity, generation vs kickoff timestamps). Read-only.
    Still-pending picks are never included - there is no outcome to score
    them against yet, never a guessed one."""
    return db.query(
        "SELECT pick_date, tier, sport, market, period, prob, prob_raw, p_market, price, status, sources, "
        "canonical_competition_id, start_utc, created_at, fixture_key "
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


def _normal_ci(p: Optional[float], n: int, z: float = CI_Z_95) -> Optional[Tuple[float, float]]:
    """Normal-approximation confidence interval on a realized outcome rate.

    Deliberately the simple approximation, not an exact Wilson interval: half_won/half_lost give the realized
    rate fractional (0.75/0.25) weight, so it is a weighted average, not a strict count of binary successes over
    trials - a Wilson interval's binomial assumption does not quite hold. The normal approximation is adequate
    once ``n`` is not tiny; it is reported next to ``low_confidence`` (n < MIN_TRUSTED_N), not instead of it, so a
    narrow-looking interval from a small n is never mistaken for a settled result. ``None`` when there is nothing
    to bound (n == 0)."""
    if p is None or n <= 0:
        return None
    se = math.sqrt(max(p * (1 - p), 0.0) / n)
    lo, hi = p - z * se, p + z * se
    return (max(0.0, lo), min(1.0, hi))


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
        "realized_outcome_rate_ci95": _normal_ci(realized, n),
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
    by_competition: Dict[str, List[Tuple[float, float]]] = defaultdict(list)
    ev_values: List[float] = []
    resolved_unscored = defaultdict(int)       # push/void: resolved, no win/loss signal
    invalid_probability = defaultdict(int)     # present but outside [0, 1] - never silently clamped
    scored_total = 0
    missing_counts = {"market_baseline": 0, "blended_deployed": 0, "independent_model": 0}

    for r in rows:
        y = OUTCOME_SCORE.get(r["status"])
        if y is None:
            resolved_unscored[r["status"]] += 1
            continue
        scored_total += 1
        market_raw = r["p_market"] if "p_market" in r.keys() else None
        market_p = _valid_probability(market_raw)
        if market_raw is not None and market_p is None:
            invalid_probability["market_baseline"] += 1
        blended_p = _blended_probability(r)
        independent_p = _independent_probability(r["sources"] if "sources" in r.keys() else None)
        for name, p in (("market_baseline", market_p), ("blended_deployed", blended_p),
                       ("independent_model", independent_p)):
            if p is None:
                missing_counts[name] += 1
        if market_p is not None:
            approach_pairs["market_baseline"].append((market_p, y))
        if blended_p is not None:
            approach_pairs["blended_deployed"].append((blended_p, y))
        if independent_p is not None:
            approach_pairs["independent_model"].append((independent_p, y))
        if blended_p is not None:
            by_market[r["market"]].append((blended_p, y))
            by_sport[r["sport"]].append((blended_p, y))
            competition = (r["canonical_competition_id"] if "canonical_competition_id" in r.keys() else None) \
                or "unknown"
            by_competition[competition].append((blended_p, y))
            price = r["price"] if "price" in r.keys() else None
            if price:
                ev_values.append(blended_p * float(price) - 1.0)

    fixtures_represented = len({r["fixture_key"] for r in rows
                                if "fixture_key" in r.keys() and r["fixture_key"]})

    return {
        "sample_total_resolved": len(rows),
        "fixtures_represented": fixtures_represented or None,
        "resolved_but_unscored": dict(resolved_unscored),
        "invalid_probability_excluded": dict(invalid_probability),
        "by_approach": {name: _metric_block(pairs) for name, pairs in approach_pairs.items()},
        "by_market": {m: _metric_block(pairs) for m, pairs in sorted(by_market.items())},
        "by_sport": {s: _metric_block(pairs) for s, pairs in sorted(by_sport.items())},
        "by_competition": {c: _metric_block(pairs) for c, pairs in sorted(by_competition.items())},
        # how often a RESOLVED, SCORABLE pick had no usable probability for this approach - e.g. the independent
        # model never reaching status "ok" shows up here as a high missing rate, not as a deceptively small "n".
        "missing_data_rate": {
            name: {"missing": missing_counts[name], "of": scored_total,
                  "rate": (missing_counts[name] / scored_total) if scored_total else None}
            for name in ("market_baseline", "blended_deployed", "independent_model")
        },
        "ev_distribution": {
            "n": len(ev_values),
            "avg": sum(ev_values) / len(ev_values) if ev_values else None,
            "min": min(ev_values) if ev_values else None,
            "max": max(ev_values) if ev_values else None,
            "low_confidence": len(ev_values) < MIN_TRUSTED_N,
        },
    }


def _period_key(pick_date, period: str) -> str:
    try:
        d = datetime.strptime(str(pick_date)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return "unknown"
    if period == "day":
        return d.isoformat()
    iso_year, iso_week, _ = d.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def evaluate_by_period(rows: List, period: str = "week") -> Dict[str, Dict]:
    """``evaluate()``, partitioned by calendar period (``pick_date``) instead of pooled - a chronological view
    instead of one aggregate number that could hide a drift, or a single lucky/unlucky stretch, inside an average.

    ``period`` is ``"week"`` (ISO week, the default) or ``"day"``. Each bucket is scored by the exact same
    ``evaluate()`` used for the pooled report, so every n / low_confidence / CI caveat still applies PER BUCKET -
    splitting chronologically shrinks the sample available to each number, it does not manufacture confidence.
    A row with no parseable ``pick_date`` falls into an explicit ``"unknown"`` bucket rather than being dropped
    silently."""
    buckets: Dict[str, List] = defaultdict(list)
    for r in rows:
        pd = r["pick_date"] if "pick_date" in r.keys() else None
        buckets[_period_key(pd, period)].append(r)
    return {k: evaluate(v) for k, v in sorted(buckets.items())}


def temporal_leakage_check(rows: List) -> Dict:
    """Basic point-in-time sanity check visible from the ``picks`` table alone: a pick must have been generated
    (``created_at``) strictly before its own fixture kicked off (``start_utc``). If that ever failed, whatever
    ``as_of`` was used at generation time could not have been a safe cutoff for anything computed for that row.

    This does NOT re-derive ``football_strength.fit_strength()``'s own training-match cutoff - that function
    already raises ``LeakageError`` for any training match not strictly before ``as_of``, and
    ``tests/test_independent_football.py::test_future_results_cannot_change_an_earlier_estimate`` already proves
    it holds at the code level. A genuine per-training-match audit from stored picks alone is not possible: only a
    ``training_matches`` COUNT is recorded (``sources._notes.independent_model.detail``), never the training
    matches' own dates - that is a real, honestly-reported limitation of this check, not papered over by pretending
    to verify something the data does not contain."""
    checked = violations = missing_timestamps = 0
    examples: List[Dict] = []
    for r in rows:
        created = r["created_at"] if "created_at" in r.keys() else None
        start = r["start_utc"] if "start_utc" in r.keys() else None
        if not created or not start:
            missing_timestamps += 1
            continue
        checked += 1
        if str(created) > str(start):          # ISO-8601 UTC strings compare lexically in chronological order
            violations += 1
            if len(examples) < 5:
                examples.append({"pick_date": r["pick_date"] if "pick_date" in r.keys() else None,
                                 "created_at": created, "start_utc": start})
    return {
        "checked": checked,
        "missing_timestamps": missing_timestamps,
        "violations": violations,
        "examples": examples,
        "note": ("verifies created_at <= start_utc only; the training-match cutoff itself is enforced by "
                "football_strength.fit_strength()'s LeakageError and covered by "
                "tests/test_independent_football.py, not re-derived here"),
    }


def build_report(db: Database, limit: int = 5000, period: str = "week") -> Dict:
    """The one entry point: read settled history once, evaluate it pooled AND chronologically, run the temporal
    sanity check, and return one report dict (callers print/json-dump it - this module never writes anywhere,
    never mutates `picks`, and takes no action on its output). ``period`` controls the chronological grouping
    ("week", the default, or "day")."""
    rows = settled_rows(db, limit)
    report = evaluate(rows)
    report["chronological"] = {"period": period, "by_period": evaluate_by_period(rows, period)}
    report["temporal_leakage_check"] = temporal_leakage_check(rows)
    return report


if __name__ == "__main__":
    import sys

    from predbot.storage.db import Database as _Database

    db_path = sys.argv[1] if len(sys.argv) > 1 else "sureodds.db"
    print(json.dumps(build_report(_Database(db_path)), indent=2, default=str))
