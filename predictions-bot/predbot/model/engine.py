"""Model prediction existence. No ticket, collection or presentation dependencies."""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field

from predbot.markets.catalog import spec
from predbot.markets.contracts import ambiguous_contract
from predbot.markets.settlement import unsettleable_reason
from predbot.model.calibration import Calibrator
from predbot.model.elo import Elo
from predbot.model.ensemble import EventModel
from predbot.model.candidates import capture_candidate
from predbot.model.coverage import coverage_stages
from predbot.selection.representatives import select_representatives


@dataclass(frozen=True)
class ModelSettings:
    shrink: float = .8
    sharp_books: tuple = ()
    elo_min_games: int = 10
    edge_price: str = "best"


@dataclass
class Generation:
    predictions: list = field(default_factory=list)
    diagnostics: dict = field(default_factory=dict)
    candidates: list = field(default_factory=list)
    ticket_predictions: list = field(default_factory=list)


def representative_predictions(scored, results_gate=None, insight_fetcher=None):
    """Validate mathematical predictions and keep ONE eligible representative per fixture x market family.

    Value is recorded, never used as an existence gate or to choose a representative. A fixture may carry several
    predictions (at most one per family: 1X2 / Moneyline, Double Chance, Draw No Bet, Handicap, Over/Under, BTTS); which
    line and side represent a family is decided by ``selection.representatives`` from the markets' own structure.

    ``insight_fetcher`` (optional): ``event -> dict | None``, e.g.
    ``ESPNProvider.team_insight`` - called AT MOST ONCE per fixture (cached
    here by canonical_event_id, never per market line) regardless of how
    many candidates that fixture produces. Any exception is swallowed and
    treated as "no insight available" - enrichment data must never be able
    to break generation itself."""
    groups, rejected, by_market, per_event = {}, Counter(), {}, {}
    for s in scored:
        m = s.line
        reason = None
        if ambiguous_contract(m.market_name):
            reason = 'ambiguous_contract'
        elif not math.isfinite(s.p_model) or not 0 < s.p_model < 1:
            reason = "invalid_probability"
        elif not math.isfinite(m.price) or m.price <= 1:
            reason = "missing_odds"
        elif not spec(m.market).modelable:
            reason = "invalid_market"
        elif results_gate and (not results_gate(s.event) or
                               unsettleable_reason(results_gate(s.event), m.period, m.stat)):
            reason = "missing_required_stats"
        elif not s.sources:
            reason = "insufficient_model_inputs"
        key = s.event.canonical_event_id
        if not key:
            raise ValueError("prediction engine requires resolved canonical events")
        if reason:
            rejected[reason] += 1
            by_market.setdefault(m.market, Counter())[reason] += 1
            per_event.setdefault(key, Counter())[reason] += 1
            continue
        groups.setdefault(key, []).append(s)
    insight_cache = {}

    def event_insight(event):
        if insight_fetcher is None:
            return None
        key = event.canonical_event_id
        if key not in insight_cache:
            try:
                insight_cache[key] = insight_fetcher(event)
            except Exception:
                insight_cache[key] = None
        return insight_cache[key]

    candidates = [capture_candidate(s, event_insight(s.event)) for values in groups.values() for s in values]
    chosen = select_representatives(groups)
    # An event-level rejection: the event produced market lines but no representative. Its reason is the most common
    # line-level reason (ties by name) when no line was valid, else "no_representative", so event counts stay separate
    # from the (much larger) line counts.
    no_valid = Counter(sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
                       for key, c in per_event.items() if key not in groups)
    no_rep = chosen.without_representative
    if no_rep:
        no_valid["no_representative"] += len(no_rep)
    validated = [s for values in groups.values() for s in values]
    return Generation(chosen.predictions, {
        "valid_predictions_generated": len(chosen.predictions),
        "fixtures_with_valid_candidates": len(groups),
        "fixtures_with_predictions": len(groups) - len(no_rep),
        "valid_candidate_lines": sum(len(v) for v in groups.values()),
        "representatives_by_family": dict(chosen.by_family),
        "omitted_families": {f: dict(c) for f, c in sorted(chosen.omitted.items())},
        "line_rejection_count": sum(rejected.values()),
        "line_rejections": dict(rejected),
        "line_rejections_by_market": {m: dict(c) for m, c in sorted(by_market.items())},
        "event_no_valid_market": dict(no_valid),
        "markets_evaluated": len(scored),
        "market_coverage": coverage_stages(scored, validated, chosen.predictions)},
        candidates=candidates, ticket_predictions=validated)


def generate(events, db, settings: ModelSettings, results_gate=None, scorer=None, insight_fetcher=None):
    elo, calibrator = Elo(db), Calibrator(db, scope="settlement_v2")
    scored, failures, sufficient = [], Counter(), 0
    for event in events:
        if not event.markets:
            failures["missing_odds"] += 1
            continue
        try:
            values = scorer([event]) if scorer else EventModel(event, elo, calibrator, shrink=settings.shrink,
                                sharp_books=settings.sharp_books or None,
                                elo_min_games=settings.elo_min_games,
                                edge_price=settings.edge_price).score_all()
        except (ValueError, TypeError, ArithmeticError, KeyError):
            failures["insufficient_model_inputs"] += 1
            continue
        if not values:
            failures["insufficient_model_inputs"] += 1
            continue
        sufficient += 1
        scored.extend(values)
    result = representative_predictions(scored, results_gate, insight_fetcher)
    for reason, n in result.diagnostics["event_no_valid_market"].items():
        failures[reason] += n
    result.diagnostics.update(sufficient_analytical_data=sufficient, analyzed=sufficient,
                              events_without_prediction=len(events) - result.diagnostics["fixtures_with_predictions"],
                              event_rejections=dict(failures))
    return result
