"""One plain-text rendering of a generation run's diagnostics (log line and /lastrun share it)."""
from __future__ import annotations

from typing import Any, Dict, List


def _kv(d) -> str:
    return ", ".join(f"{k}={v}" for k, v in sorted((d or {}).items())) or "none"


def pipeline_lines(gen: Dict[str, Any]) -> List[str]:
    """Funnel from provider discovery to the three collections.

    Event-level counts are UNIQUE canonical fixtures (never provider attempts) and are kept apart from
    market-line counts (one event carries dozens of lines). Every drop names its reason, so an empty collection is
    never reported as "no predictions"."""
    g = gen or {}
    top, daily = g.get("top") or {}, g.get("daily") or {}
    event_rejections = g.get("event_rejections")
    lines = [
        f"Unique fixtures discovered: {g.get('fixtures_discovered', '?')}",
        f"Supported fixtures: {g.get('supported_fixtures', '?')}",
        f"Normalized: {g.get('normalized', '?')}",
        f"Normalization failures: {g.get('normalization_failures', 0)}"
        + (f" ({_kv(g.get('normalization_rejections'))})" if g.get("normalization_rejections") else ""),
        f"Sufficient analytical data: {g.get('sufficient_analytical_data', '?')}"
        + (f" · events not analysed: {_kv(event_rejections)}" if event_rejections else ""),
        f"Fixtures with a valid prediction: {g.get('fixtures_with_predictions', '?')} of {g.get('fixtures_with_valid_candidates', '?')}"
        f" · valid candidate lines: {g.get('valid_candidate_lines', '?')}",
        f"Predictions generated (one per fixture x market family): {g.get('valid_predictions_generated', '?')}"
        + (f" · by family: {_kv(g.get('representatives_by_family'))}" if g.get("representatives_by_family") else ""),
        "",
        f"All Predictions: {g.get('stored_all_predictions', g.get('all_predictions', '?'))}",
        "",
        f"Top candidates: {top.get('candidates', '?')}",
        f"Top selected: {top.get('selected', '?')}"
        + (f" · combined odds {top['combined_odds']:.2f}" if top.get("selected") else "")
        + (f" · not promoted: {_kv(top.get('rejections'))}" if top.get("rejections") else ""),
        f"Daily candidates: {daily.get('candidates', '?')}",
        f"Daily selected: {daily.get('selected', '?')}"
        + (f" · combined odds {daily['combined_odds']:.2f}" if daily.get("selected") else "")
        + (f" · not promoted: {_kv(daily.get('rejections'))}" if daily.get("rejections") else ""),
        "",
        f"Market lines evaluated: {g.get('markets_evaluated', '?')} · rejected: {g.get('line_rejection_count', 0)}"
        + (f" ({_kv(g.get('line_rejections'))})" if g.get("line_rejections") else ""),
    ]
    if g.get("omitted_families"):
        lines.append("Families omitted for a fixture: " + "; ".join(f"{f}: {_kv(r)}" for f, r in sorted(g["omitted_families"].items())))
    by_market = g.get("line_rejections_by_market") or {}
    if by_market:
        lines.append("Rejected lines by market: " + "; ".join(f"{m}: {_kv(r)}" for m, r in sorted(by_market.items())))
    if g.get("provider_identity_failures"):
        lines.append("Provider identity failures (attempts, not fixtures): "
                     + "; ".join(f"{p}: {_kv(r)}" for p, r in sorted(g["provider_identity_failures"].items())))
    if g.get("http_requests") is not None:
        lines += ["", "Provider HTTP requests this run: " + (_kv(g["http_requests"]) if g["http_requests"] else "none (cache)")]
    return lines
