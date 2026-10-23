"""One-shot engine runner for GitHub Actions (Stage 2).

Calls the SAME job functions the long-running worker calls
(predbot.jobs.daily_picks.run / settle.run / recap.run) exactly once and
exits. No thresholds, selection logic, provider priority or settlement rules
are changed here — this file only handles process setup, timing and reporting.

Safety:
  * Default mode is OFFLINE (fixtures only, zero provider/Telegram calls).
  * `--live` performs real provider + Telegram calls and requires the
    PREDBOT_ALLOW_LIVE=1 environment guard, so a stray manual run cannot
    spend quota. No scheduled `--live` workflow exists yet (Stage 3).
  * Lagos times are logged (POST 09:00, DELIVERY 09:30, RECAP 07:45) but never
    altered.

Usage:
  python run_once.py generate --offline
  python run_once.py settle --offline
  python run_once.py recap --offline
  PREDBOT_ALLOW_LIVE=1 python run_once.py closing_price --live   # honours CLOSING_PRICE_ENABLED
  PREDBOT_ALLOW_LIVE=1 python run_once.py generate --live [--force] [--report report.json]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

JOB_NAMES = ("generate", "settle", "recap", "closing_price")


def parse_args(argv):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("job", choices=JOB_NAMES)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--offline", action="store_true",
                      help="Fixtures-only benchmark path: no provider or Telegram calls.")
    mode.add_argument("--live", action="store_true",
                      help="Real run. Requires PREDBOT_ALLOW_LIVE=1.")
    ap.add_argument("--force", action="store_true", help="Force regeneration (live generate only).")
    ap.add_argument("--report", default="", help="Write JSON timing report to PATH.")
    return ap.parse_args(argv)


def offline_benchmark(job: str) -> dict:
    """Deterministic offline measurement of the compute stages. No network."""
    from predbot.storage.db import Database
    from predbot.util.timeutil import to_local, utcnow
    from predbot.config import load_config

    cfg = load_config()
    lagos_now = to_local(utcnow(), cfg.timezone)
    result: dict = {"job": job, "mode": "offline", "lagos_now": lagos_now.isoformat(),
                    "timezone": cfg.timezone, "post_time": cfg.post_time,
                    "delivery_time": cfg.delivery_time, "recap_time": cfg.recap_time}
    if job == "generate":
        # Whole-pipeline replay on the committed recorded provider day
        # (tools/replay.py, no network): collect(normalized fixture) ->
        # representative selection -> evidence -> store -> Top/Daily package.
        import sys as _sys
        from pathlib import Path as _Path
        _sys.path.insert(0, str(_Path(__file__).resolve().parent / "tools"))
        import replay as replay_mod
        t0 = time.perf_counter()
        result_run = replay_mod.run(send_delay=0.0)
        wall = time.perf_counter() - t0
        slate = result_run["slate"]
        diag = slate["diagnostics"]
        result.update({"compute_wall_s": round(wall, 3),
                       "valid_predictions_generated": diag.get("valid_predictions_generated"),
                       "fixtures_with_predictions": diag.get("fixtures_with_predictions"),
                       "top_selected": diag.get("top_selected"),
                       "daily_selected": diag.get("daily_selected")})
    elif job == "settle":
        from predbot.jobs import settle as settle_mod
        db = Database(":memory:")
        try:
            t0 = time.perf_counter()
            deadline = settle_mod.compute_settlement_deadline(db, lagos_now.date().isoformat())
            wall = time.perf_counter() - t0
            result.update({"compute_wall_s": round(wall, 3),
                           "deadline": deadline.isoformat() if deadline else None})
        finally:
            db.close()
    elif job == "closing_price":
        result["note"] = "no offline path: the closing-price job only ever runs live (CLOSING_PRICE_ENABLED)"
    elif job == "recap":
        from predbot.jobs import stats as stats_mod
        db = Database(":memory:")
        try:
            t0 = time.perf_counter()
            stats_mod.compute(db, lagos_now)
            wall = time.perf_counter() - t0
            result.update({"compute_wall_s": round(wall, 3)})
        finally:
            db.close()
    return result


def summarize_snapshot(snapshot) -> dict:
    """Sanitized generation summary for CI artifacts and logs.

    Explicit allowlist only: snapshot status/date, pick counts, and the
    provider-coverage record (required/missing/degraded/complete). Never the
    raw generation diagnostics (which carry quota counters and provider
    internals) and never any credential. Safe to upload as a workflow
    artifact and to print in logs.
    """
    generation = getattr(snapshot, "generation", None) or {}
    coverage = generation.get("provider_coverage") or {}
    return {
        "snapshot_status": getattr(snapshot, "status", "unknown"),
        "snapshot_date": getattr(snapshot, "date", ""),
        "top_picks": len(getattr(snapshot, "top_picks", None) or []),
        "daily_picks": len(getattr(snapshot, "daily_picks", None) or []),
        "all_predictions": len(getattr(snapshot, "all_predictions", None) or []),
        "provider_coverage": {
            "required": list(coverage.get("required", [])),
            "missing_required": list(coverage.get("missing_required", [])),
            "optional_degraded": dict(coverage.get("optional_degraded", {})),
            "complete": bool(coverage.get("complete", False)),
        },
        "error_type": generation.get("error_type"),
        "error_traceback": generation.get("error_traceback"),
    }


def live_run(job: str, force: bool) -> dict:
    from predbot.bot.telegram import Telegram
    from predbot.cache.fetcher import UnifiedFetcher
    from predbot.config import load_config, require_provider_keys
    from predbot.jobs import daily_picks, recap, settle
    from predbot.jobs.scheduler import loop  # noqa  (imported to prove scheduler is NOT started)
    from predbot.log import setup_logging
    from predbot.providers.chain import ProviderChain, ResultsResolver, build_providers
    from predbot.storage.db import Database
    from predbot.util.timeutil import to_local, utcnow

    cfg = load_config()
    setup_logging(cfg.log_level, cfg.log_file,
                  secrets=[cfg.sgo_key, cfg.sharpapi_key, cfg.odds_api_key,
                           cfg.football_data_key, cfg.telegram_token])
    require_provider_keys(cfg)
    lagos_now = to_local(utcnow(), cfg.timezone)
    db = Database(cfg.db_path)
    try:
        providers = build_providers(cfg, db)
        chain = ProviderChain(providers, cfg.provider_order)
        resolver = ResultsResolver(providers)
        tg = Telegram(cfg.telegram_token, cfg.telegram_chat_id)
        service = UnifiedFetcher(chain, db, cfg)
        t0 = time.perf_counter()
        if job == "generate":
            snapshot = daily_picks.run(chain, db, cfg, tg, force=force, service=service)
            wall = time.perf_counter() - t0
            report = {"job": job, "mode": "live", "lagos_now": lagos_now.isoformat(),
                      "wall_s": round(wall, 3)}
            # daily_picks.run returns None only on paths that print nothing to
            # post (empty unevaluated slate); a missing snapshot is itself
            # diagnostic evidence, recorded without any further provider calls.
            report["generation"] = summarize_snapshot(snapshot) if snapshot is not None else None
            return report
        elif job == "settle":
            outcome = settle.run(resolver, db, cfg)
            wall = time.perf_counter() - t0
            return {"job": job, "mode": "live", "lagos_now": lagos_now.isoformat(),
                    "wall_s": round(wall, 3), "outcome": outcome}
        elif job == "closing_price":
            from predbot.jobs import closing_price
            counts = closing_price.run(db, cfg, providers=providers)
            return {"job": job, "mode": "live", "lagos_now": lagos_now.isoformat(),
                    "wall_s": round(time.perf_counter() - t0, 3), "counts": counts}
        elif job == "recap":
            text = recap.run(db, cfg, tg)
            wall = time.perf_counter() - t0
            return {"job": job, "mode": "live", "lagos_now": lagos_now.isoformat(),
                    "wall_s": round(wall, 3), "recap_chars": len(text or "")}
        wall = time.perf_counter() - t0
        return {"job": job, "mode": "live", "lagos_now": lagos_now.isoformat(),
                "wall_s": round(wall, 3)}
    finally:
        db.close()


def main(argv) -> int:
    args = parse_args(argv)
    if args.live and os.environ.get("PREDBOT_ALLOW_LIVE") != "1":
        print("refused: --live requires PREDBOT_ALLOW_LIVE=1 (no scheduled live jobs exist yet)",
              file=sys.stderr)
        return 2
    t0 = time.perf_counter()
    if args.live:
        report = live_run(args.job, args.force)
    else:
        report = offline_benchmark(args.job)
    report["total_wall_s"] = round(time.perf_counter() - t0, 3)
    print(json.dumps(report, indent=2))
    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
