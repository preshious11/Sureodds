"""Manual controls:  python cli.py picks [--dry-run] [--force] | settle | recap [--dry-run] | stats | stats-card | test-post | budget"""
from __future__ import annotations

import sys

from predbot.bot import pagination
from predbot.bot.format import stats_message
from predbot.bot.telegram import Telegram
from predbot.cache.models import DailySnapshot
from predbot.config import load_config
from predbot.jobs import daily_picks, recap, settle, stats as stats_mod
from predbot.log import setup_logging
from predbot.providers.budget import Budget
from predbot.providers.chain import ProviderChain, ResultsResolver, build_providers
from predbot.storage.db import Database
from predbot.util.timeutil import to_local, utcnow


def main(argv):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")   # emoji-safe terminal output on Windows
    cfg = load_config()
    setup_logging(cfg.log_level)
    db = Database(cfg.db_path)
    providers = build_providers(cfg, db)
    chain = ProviderChain(providers, cfg.provider_order, cfg.chain_min_events)
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
    elif cmd == "stats-card":
        # Exactly what the 📊 Stats button renders: SUREODDS — YESTERDAY card
        text, _ = pagination.render_stats(DailySnapshot(date=""), db=db, tz=cfg.timezone)
        print(text.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", ""))
    elif cmd == "test-post":
        print("sent" if tg.send("predbot online ✅") else "failed")
    elif cmd == "budget":
        b = Budget(db)
        for p in providers:
            print(f"{p}: today={b.used_today(p)} month={b.used_month(p)}")
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv)
