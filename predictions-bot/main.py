"""Railway worker entrypoint: schedules recap -> picks -> settlement forever."""
from __future__ import annotations

import logging

from predbot.bot import broadcast
from predbot.bot.commands import CommandLoop
from predbot.bot.runtime import CommandLoopSupervisor
from predbot.bot.telegram import Telegram
from predbot.cache.fetcher import UnifiedFetcher
from predbot.config import load_config
from predbot.jobs import daily_picks, recap, settle
from predbot.jobs.scheduler import DailyJob, DynamicJob, IntervalJob, loop
from predbot.log import setup_logging
from predbot.providers.chain import ProviderChain, ResultsResolver, build_providers
from predbot.storage.db import Database
from predbot.util.timeutil import to_local, utcnow

log = logging.getLogger("main")

#: Retry subscriber delivery after the shared daily publication time.
DELIVERY_POLL_MINUTES = 5


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
    service = UnifiedFetcher(chain, db, cfg)
    log.info("providers: %s | timezone %s | picks at %s local | recap at %s | "
             "subscriber delivery: %s local, checked every %dm | cache: %s",
             ", ".join(chain.order) or "none", cfg.timezone, cfg.post_time, cfg.recap_time,
             cfg.post_time, DELIVERY_POLL_MINUTES, "sqlite")

    def job_settle():
        settle.run(resolver, db, cfg)

    def job_recap():
        job_settle()               # make sure yesterday is graded first
        text = recap.run(db, cfg, tg)                # channel mirror (optional)
        if text:
            db.set_kv("recap_text", text)            # cache recap for broadcast job

    def job_broadcast_recap():
        """Send recap to each subscriber after last fixture ends in their timezone."""
        text = db.get_kv("recap_text")
        if text:
            broadcast.deliver_recap(db, cfg, tg, text)

    def job_deliver():
        """Poll: hand the card to every subscriber whose own 9:30 AM has arrived."""
        # Prevent API calls during delivery polling; only use cached data.
        snapshot = service.get_daily_snapshot(allow_api_call=False)
        if not snapshot.total_picks():
            return
        broadcast.deliver_picks(db, cfg, tg, snapshot)

    def job_picks():
        daily_picks.run(chain, db, cfg, tg, service=service)          # channel mirror (optional)
        service.refresh_cache_from_db()  # write-once-per-day: feed the interactive cache

    def compute_settle_deadline(now):
        today = to_local(now, cfg.timezone).strftime("%Y-%m-%d")
        return settle.compute_settlement_deadline(db, today)

    jobs = [
        DailyJob("recap", cfg.recap_time, cfg.timezone, job_recap),
        DailyJob("picks", cfg.post_time, cfg.timezone, job_picks),
        # Per-subscriber local-time gates, so both must poll rather than fire once.
        IntervalJob("deliver", DELIVERY_POLL_MINUTES, job_deliver),   # each subscriber's 9:30 AM
        IntervalJob("broadcast_recap", 5, job_broadcast_recap),       # their last fixture ending
        DynamicJob("settle", cfg.timezone, compute_settle_deadline, job_settle),
    ]

    commands = None
    supervisor = None
    if cfg.enable_commands and cfg.telegram_token:
        # Users DM the bot directly (/start menu); TELEGRAM_CHAT_ID is optional
        # and only mirrors the daily posts to a channel when set.
        actions = {
            "/run": lambda: (daily_picks.run(chain, db, cfg, tg, force=True, service=service) and "Picks posted.") or "Picks posted.",
            "/settle": lambda: f"Settlement: {settle.run(resolver, db, cfg)}",
            "/recap": lambda: recap.run(db, cfg, tg, force=True) or "Nothing to recap.",
        }

        def start_commands():
            loop_thread = CommandLoop(tg, db, cfg, actions, service=service,
                                      page_size=cfg.page_size)
            loop_thread.start()
            return loop_thread

        supervisor = CommandLoopSupervisor(start_commands)
        commands = supervisor.start()

    def check_commands():
        """A dead Telegram poller costs commands, never picks or delivery.

        It used to raise here and kill the worker: one transient Telegram outage
        silenced the whole bot.  Now the poller is restarted (with backoff while
        the restart keeps failing) and the scheduler keeps running.
        """
        if supervisor is not None:
            supervisor.ensure()

    try:
        loop(jobs, health_check=check_commands)
    finally:
        if supervisor is not None:
            supervisor.stop()


if __name__ == "__main__":
    main()
