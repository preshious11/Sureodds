"""Railway worker entrypoint: schedules recap -> picks -> settlement forever."""
from __future__ import annotations

import logging

from predbot.bot import broadcast
from predbot.bot.commands import CommandLoop
from predbot.bot.telegram import Telegram
from predbot.cache.fetcher import UnifiedFetcher
from predbot.cache.manager import CacheManager
from predbot.config import load_config
from predbot.jobs import daily_picks, recap, settle
from predbot.jobs.scheduler import DailyJob, DynamicJob, loop
from predbot.log import setup_logging
from predbot.providers.chain import ProviderChain, ResultsResolver, build_providers
from predbot.storage.db import Database
from predbot.util.timeutil import to_local, utcnow

log = logging.getLogger("main")


def main() -> None:
    cfg = load_config()
    setup_logging(cfg.log_level)
    db = Database(cfg.db_path)
    providers = build_providers(cfg, db)
    if not providers:
        log.error("No API keys configured - set at least one of API_FOOTBALL_KEY / SPORTSGAMEODDS_KEY / SHARPAPI_KEY")
    chain = ProviderChain(providers, cfg.provider_order, cfg.chain_min_events)
    resolver = ResultsResolver(providers)
    tg = Telegram(cfg.telegram_token, cfg.telegram_chat_id)
    cache = CacheManager(redis_url=cfg.redis_url, cache_dir=cfg.cache_dir)
    service = UnifiedFetcher(chain, db, cfg, cache)
    log.info("providers: %s | timezone %s | picks at %s | recap at %s | cache: %s",
             ", ".join(chain.order) or "none", cfg.timezone, cfg.post_time, cfg.recap_time,
             cache.backend_name)

    def job_settle():
        settle.run(resolver, db, cfg)

    def job_recap():
        job_settle()               # make sure yesterday is graded first
        text = recap.run(db, cfg, tg)                # channel mirror (optional)
        broadcast.deliver_recap(db, cfg, tg, text)   # DM yesterday's results

    def job_picks():
        daily_picks.run(chain, db, cfg, tg)          # channel mirror (optional)
        service.refresh_cache_from_db()  # write-once-per-day: feed the interactive cache
        broadcast.deliver_picks(db, cfg, tg, service.get_daily_snapshot())

    def compute_settle_deadline(now):
        today = to_local(now, cfg.timezone).strftime("%Y-%m-%d")
        return settle.compute_settlement_deadline(db, today)

    jobs = [
        DailyJob("recap", cfg.recap_time, cfg.timezone, job_recap),
        DailyJob("picks", cfg.post_time, cfg.timezone, job_picks),
        DynamicJob("settle", cfg.timezone, compute_settle_deadline, job_settle),
    ]

    if cfg.enable_commands and cfg.telegram_token:
        # Users DM the bot directly (/start menu); TELEGRAM_CHAT_ID is optional
        # and only mirrors the daily posts to a channel when set.
        actions = {
            "/run": lambda: (daily_picks.run(chain, db, cfg, tg, force=True) and "Picks posted.") or "Picks posted.",
            "/settle": lambda: f"Settlement: {settle.run(resolver, db, cfg)}",
            "/recap": lambda: recap.run(db, cfg, tg, force=True) or "Nothing to recap.",
        }
        CommandLoop(tg, db, cfg, actions, service=service, page_size=cfg.page_size).start()

    loop(jobs)


if __name__ == "__main__":
    main()
