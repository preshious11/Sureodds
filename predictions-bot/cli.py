"""Manual controls:  python cli.py picks [--dry-run] [--force] | settle | recap [--dry-run] | stats | accuracy | stats-card | test-post | budget | sgo-usage"""
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


def main(argv):
    # attribute every SGO request this process makes to a person at a terminal, not to the scheduler
    os.environ.setdefault("PREDBOT_SGO_ORIGIN", "manual")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")   # emoji-safe terminal output on Windows
    cfg = load_config()
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
