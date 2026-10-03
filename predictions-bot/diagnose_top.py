"""Operator-only Top audit. Offline by default; never sends or changes stored picks.

python diagnose_top.py --db tests/fixtures/replay/replay.db --json _top_replay.json --csv _top_replay.csv
python diagnose_top.py --live --record _top_current.db --json _top_current.json --csv _top_current.csv

Live collection honours existing quotas on a SQLite backup. The recorded database
can be audited later without network access. Provider scenarios remove sources
BEFORE merging/scoring, so their quotes and probabilities really disappear.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import sqlite3
import tempfile
from contextlib import ExitStack, closing
from collections import Counter
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from unittest.mock import patch

from predbot.config import load_config
from predbot.jobs import daily_picks
from predbot.markets.settlement import make_results_gate
from predbot.providers.chain import ProviderChain, build_providers, filter_allowed, merge_events
from predbot.cache.providers import decode_events
from predbot.selection.picker import select
from predbot.selection.top_diagnostics import effective_config
from predbot.storage.db import Database
from predbot.util.timeutil import day_bounds, iso, parse_iso, utcnow


def backup(source, destination):
    with closing(sqlite3.connect(Path(source).resolve().as_uri() + "?mode=ro", uri=True)) as src:
        with closing(sqlite3.connect(destination)) as dst:
            src.backup(dst)


def audit(events, db, cfg, now, providers, priority=None):
    scored = daily_picks.score_events(events, db, cfg)
    trace = {}
    ps = select(scored, cfg, now, results_gate=make_results_gate(cfg, providers),
                top_priority_source=priority, top_trace=trace)
    top_ids, daily_ids = {id(s) for s in ps.top}, {id(s) for s in ps.daily}
    rows = []
    for s in scored:
        failures = trace.get(id(s), [])
        rows.append({"candidate": s.label, "fixture": s.event.name, "sport": s.event.sport,
                     "league": s.event.league, "market": s.line.market, "period": s.line.period,
                     "stat": s.line.stat, "line": s.line.line, "price": s.price,
                     "probability": round(s.p_model, 6), "books": s.line.distinct_books(),
                     "book_names": sorted(s.line.book_quotes), "providers": s.line.providers or s.event.sources,
                     "model_signals": sorted(s.sources), "quality": s.quality,
                     "lower_bound": s.p_lcb, "rank": s.rank_score, "sharp_fair": s.p_fair,
                     "sharp_edge_ev": s.edge_ev, "dispersion": s.line.price_dispersion(),
                     "daily": id(s) in daily_ids, "top": id(s) in top_ids,
                     "top_eligible": not failures, "top_rejections": failures,
                     "construction": "SELECTED" if id(s) in top_ids else
                         "QUALITY_REJECTED" if failures else "BETTER_TICKET" if ps.top else "NO_IN_BAND_TICKET"})
    summary = {k: v for k, v in ps.diagnostics.items() if k.startswith("top_") or
               k in ("combinations_checked", "base_by_quality", "base_with_sharp_reference",
                     "daily_eligible_by_sport", "daily_pool_rejections", "selection_rejections")}
    summary.update(events=len(events), events_by_provider=dict(Counter(p for e in events for p in e.sources)),
                   raw_by_sport=dict(Counter(s.event.sport for s in scored)),
                   daily_selections=len(ps.daily), daily_odds=math.prod(s.price for s in ps.daily) if ps.daily else None,
                   daily_comparison=[r for r in rows if r["daily"]],
                   top_ticket=[r for r in rows if r["top"]],
                   top_eligible_provider_coverage=dict(Counter("+".join(r["providers"]) for r in rows if r["top_eligible"])))
    return summary, rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="tests/fixtures/replay/replay.db")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--record")
    parser.add_argument("--json")
    parser.add_argument("--csv")
    parser.add_argument("--at", help="Replay instant; defaults to recorded replay:now or provider updated_at")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    cfg = load_config()
    import os
    os.environ["PREDBOT_SGO_ORIGIN"] = "diagnostic"
    with ExitStack() as stack:
        temp = stack.enter_context(tempfile.TemporaryDirectory(prefix="predbot-top-"))
        path = str(Path(temp) / "audit.db")
        source = cfg.db_path if args.live else args.db
        if Path(source).is_file():
            backup(source, path)
        db = Database(path)
        stack.callback(db.close)
        providers = build_providers(cfg, db)
        if args.live:
            now = utcnow()
            chain = ProviderChain(providers, cfg.provider_order)
            chain.db, chain.cfg = db, cfg
            # Run the exact production collection/window/prior-day/priority path.
            captured = []
            original_score = daily_picks.score_events
            def capture(events, *a):
                captured.extend(events)
                return original_score(events, *a)
            with patch.object(daily_picks, "score_events", capture):
                baseline = daily_picks.build_picks(chain, db, cfg, now)
            db.set_kv("replay:now", iso(now))
            provider_report = baseline.diagnostics.get("providers", {})
            priority = baseline.diagnostics.get("top_priority_source")
            events = captured
        else:
            cache = db.query("SELECT * FROM provider_cache ORDER BY cache_key")
            if not cache:
                parser.error("No recorded provider events; use --live to collect once")
            now = parse_iso(args.at or db.get_kv("replay:now") or
                            max(json.loads(r["metadata_json"]).get("updated_at", "") for r in cache))
            if now is None:
                parser.error("Recording has no clock; supply --at")
            provider_report = {r["cache_key"].rsplit(":", 1)[-1]: json.loads(r["metadata_json"]) for r in cache}
            priority = next((p for p in cfg.top_provider_priority if p in provider_report), None)
            events = None
        day = now.astimezone(ZoneInfo(cfg.timezone)).date().isoformat()
        cache = db.query("SELECT * FROM provider_cache WHERE local_date=?", (day,))
        start = now + timedelta(minutes=cfg.min_lead_minutes)
        _, midnight = day_bounds(now, cfg.timezone)
        end = min(midnight + timedelta(hours=cfg.day_window_end_offset_hours), now + timedelta(hours=cfg.pick_window_hours))
        def pool(exclude=()):
            merged = []
            names = list(dict.fromkeys(cfg.provider_order + cfg.top_provider_priority))
            for name in names:
                for row in cache:
                    if row["cache_key"].rsplit(":", 1)[-1] != name or name in exclude:
                        continue
                    portion = filter_allowed(decode_events(json.loads(row["payload_json"])), cfg)
                    portion = [e for e in portion if start <= e.start < end]
                    merged = merge_events(merged, portion, cfg.price_mode)
            # Same prior-day fixture rejection as build_picks.
            from types import SimpleNamespace
            prior = [SimpleNamespace(sport=r['sport'], home=r['home'], away=r['away'], start=parse_iso(r['start_utc']))
                     for r in db.previous_pick_fixtures(day)
                     if parse_iso(r['start_utc'])]
            return [e for e in merged if not any(daily_picks.same_fixture(e, p) for p in prior)]
        summary, rows = audit(events if events is not None else pool(), db, cfg, now, providers, priority)
        no_sgo, _ = audit(pool(("sportsgameodds",)), db, cfg, now, providers, priority)
        no_odds, _ = audit(pool(("oddsapi",)), db, cfg, now, providers, None)
        result = {"at": iso(now), "data_mode": "live" if args.live else "recorded",
                  "source": source, "effective_config": effective_config(cfg), "providers": provider_report,
                  "baseline": summary, "without_sgo": no_sgo, "without_oddsapi": no_odds,
                  "limitations": "Unavailable providers are not fabricated. Historical recordings do not establish current production coverage."}
        if args.record:
            backup(path, args.record)
        if args.json:
            Path(args.json).write_text(json.dumps(result, indent=2, allow_nan=False), encoding="utf-8")
        if args.csv:
            with open(args.csv, "w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=list(rows[0]) if rows else ["candidate"])
                writer.writeheader()
                writer.writerows(rows)
        print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
