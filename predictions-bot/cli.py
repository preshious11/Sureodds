"""Manual controls:  python cli.py picks [--dry-run] [--force] | settle | recap [--dry-run] | stats | accuracy | stats-card | test-post | budget | sgo-usage | reset-day [YYYY-MM-DD] [--yes] | history-status | history-import-csv FILE [competition] | history-backfill [--competitions PL,PD] [--seasons 2023,2024] [--yes] | history-elo | identity-repair [--apply --backup PATH]"""
from __future__ import annotations

import os
import sys

from predbot.bot import pagination
from predbot.bot.format import stats_message
from predbot.bot.telegram import Telegram
from predbot.cache.models import DailySnapshot
from predbot.config import load_config, require_provider_keys
from predbot.jobs import daily_picks, recap, settle, stats as stats_mod
from predbot.log import setup_logging
from predbot.providers.budget import Budget
from predbot.markets.settlement import log_coverage
from predbot.providers.chain import ProviderChain, ResultsResolver, build_providers
from predbot.storage.db import Database
from predbot.util.timeutil import to_local, utcnow


def reset_day(cfg, args):
    """Force the next prediction read to generate once with the current code.

    Deletes only today's shared snapshot row and its unpublished, not-yet-started pending predictions. Settled
    rows, started fixtures, other days, subscribers, provider caches and quota counters are untouched. Without
    --yes this only reports what would be removed."""
    day = next((a for a in args if not a.startswith("--")), to_local(utcnow(), cfg.timezone).date().isoformat())
    db = Database(cfg.db_path)
    try:
        result = db.reset_prediction_day(day, cfg.timezone, utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                                         dry_run="--yes" not in args)
    except ValueError as exc:
        print(f"refused: {exc}")
        return
    print(result)
    if result["dry_run"]:
        print("dry run only - nothing deleted. Re-run with --yes to apply.")


def identity_repair(cfg, args):
    """Report (default) or remove identity LOOKUP rows written by the old identity rules - see
    predbot/storage/identity_repair.py. Never touches predictions, results or ratings; applying requires a backup
    path, written and integrity-checked first. No provider or Telegram call."""
    from predbot.storage.identity_repair import repair
    apply = "--apply" in args
    backup_path = args[args.index("--backup") + 1] if "--backup" in args and args.index("--backup") + 1 < len(args) else ""
    if apply and not backup_path:
        print("refused: --apply needs --backup PATH (a new file)")
        return
    try:
        result = repair(Database(cfg.db_path), apply=apply, backup_path=backup_path)
    except ValueError as exc:
        print(f"refused: {exc}")
        return
    print(result)
    if result["dry_run"]:
        print("dry run only - nothing changed. Re-run with --apply --backup PATH to remove the listed lookup rows.")


def history_command(cfg, cmd, args):
    """Historical football results (the independent model's only training data). Never part of normal bot operation:
    every command is explicit, and the only one that can call a provider (``history-backfill``) is a dry run until
    ``--yes`` and reports the exact request count first (one request per competition-season, free tier 10/minute)."""
    import time
    from predbot.history import ingest
    from predbot.history.store import HistoryStore, season_of
    db = Database(cfg.db_path)
    store = HistoryStore(db)
    if cmd == "history-status":
        cov = store.coverage()
        print(f"{sum(c['matches'] for c in cov.values())} football results in {len(cov)} competitions")
        for comp, c in cov.items():
            print(f"  {comp:14s} {c['matches']:6d} matches {c['teams']:3d} teams {c['first'][:10]}..{c['last'][:10]} "
                  f"seasons {','.join(c['seasons'])} odds on {c['with_odds']}")
        pending = db.query("SELECT COUNT(*) AS n FROM match_results WHERE elo_applied=0")[0]["n"]
        print(f"{pending} results not yet applied to Elo")
    elif cmd == "history-import-csv":
        if not args:
            print("usage: history-import-csv FILE [competition_id]")
            return
        text = open(args[0], encoding="utf-8-sig", errors="replace").read()
        print(dict(ingest.ingest_csv(db, text, args[1] if len(args) > 1 else None)))
    elif cmd == "history-backfill":
        flags = {a.split("=")[0]: a.split("=")[1] for a in args if a.startswith("--") and "=" in a}
        codes = (flags.get("--competitions") or "PL,ELC,PD,SA,BL1,FL1,DED,PPL").split(",")
        this = int(season_of(utcnow()))
        seasons = (flags.get("--seasons") or ",".join(str(y) for y in range(this - 2, this + 1))).split(",")
        plan = ingest.plan_football_data(db, codes, seasons)
        print(f"{len(plan)} requests needed (~{len(plan) * ingest.REQUEST_SPACING_SECONDS / 60:.1f} min at the free-tier pace): {plan}")
        if "--yes" not in args:
            print("dry run only - nothing requested. Re-run with --yes to fetch (each season is fetched once, ever).")
            return
        if not cfg.football_data_key:
            print("FOOTBALL_DATA_KEY is not set")
            return
        from predbot.providers.football_data import FootballDataProvider
        provider = FootballDataProvider(cfg.football_data_key)
        report = ingest.ingest_football_data(db, provider.http.get, sorted({c for c, _ in plan}), sorted({s for _, s in plan}),
                                             sleep=time.sleep)
        for key, row in report.items():
            print(key, row)
        print("Elo learned from", store.apply_pending_elo(), "new results")
    elif cmd == "history-elo":
        print("Elo learned from", store.apply_pending_elo(), "pending results")
    else:
        print(__doc__)


def main(argv):
    # attribute every SGO request this process makes to a person at a terminal, not to the scheduler
    os.environ.setdefault("PREDBOT_SGO_ORIGIN", "manual")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")   # emoji-safe terminal output on Windows
    cfg = load_config()
    if len(argv) > 1 and argv[1] == "reset-day":
        return reset_day(cfg, argv[2:])
    if len(argv) > 1 and argv[1].startswith("history-"):
        return history_command(cfg, argv[1], argv[2:])
    if len(argv) > 1 and argv[1] == "identity-repair":
        return identity_repair(cfg, argv[2:])
    setup_logging(cfg.log_level, cfg.log_file, secrets=[cfg.sgo_key, cfg.sharpapi_key, cfg.odds_api_key,
                                                         cfg.football_data_key, cfg.telegram_token])
    require_provider_keys(cfg)
    db = Database(cfg.db_path)
    providers = build_providers(cfg, db)
    log_coverage(cfg, providers)
    chain = ProviderChain(providers, cfg.provider_order)
    resolver = ResultsResolver(providers)
    tg = Telegram(cfg.telegram_token, cfg.telegram_chat_id)
    cmd = argv[1] if len(argv) > 1 else "help"
    flags = set(argv[2:])
    if cmd == "picks":
        daily_picks.run(chain, db, cfg, tg, force="--force" in flags, dry_run="--dry-run" in flags)
    elif cmd == "settle":
        print(settle.run(resolver, db, cfg))
    elif cmd == "recap":
        recap.run(db, cfg, tg, force=True, dry_run="--dry-run" in flags)
    elif cmd == "stats":
        print(stats_message(stats_mod.compute(db, to_local(utcnow(), cfg.timezone))).replace("<b>", "").replace("</b>", ""))
    elif cmd == "accuracy":
        print(stats_mod.accuracy_text(stats_mod.accuracy_report(db)))
    elif cmd == "stats-card":
        # Exactly what the 📊 Stats button renders: SUREODDS — YESTERDAY card
        text, _ = pagination.render_stats(DailySnapshot(date=""), db=db, tz=cfg.timezone)
        print(text.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", ""))
    elif cmd == "test-post":
        print("sent" if tg.send("predbot online ✅") else "failed")
    elif cmd == "sgo-usage":
        from predbot.providers.sgo_meter import usage_lines
        print("\n".join(usage_lines(db)))
    elif cmd == "budget":
        b = Budget(db)
        for p in providers:
            print(f"{p}: today={b.used_today(p)} month={b.used_month(p)}")
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv)
