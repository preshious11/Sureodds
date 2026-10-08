"""Worker entrypoint (the Docker image's CMD): schedules recap -> picks -> settlement forever.

Exactly one worker per database runs the scheduler and Telegram polling: the database leader lease
(``predbot.storage.lease``) is taken before either starts. A second process against the same database waits in
standby and takes over only when the holder stops or stops renewing; a holder that loses the lease stops both and
exits (code 3) so its supervisor restarts it into standby."""
from __future__ import annotations

import logging
import threading

from predbot.bot import broadcast
from predbot.bot.commands import CommandLoop
from predbot.bot.runtime import CommandLoopSupervisor
from predbot.bot.telegram import Telegram
from predbot.cache.fetcher import UnifiedFetcher
from predbot.config import load_config
from predbot.jobs import daily_picks, live_results, recap, settle
from predbot.jobs.scheduler import DailyJob, DynamicJob, IntervalJob, drain, loop
from predbot.log import setup_logging
from predbot.config import require_provider_keys, startup_lines
from predbot.markets.settlement import log_coverage
from predbot.providers.chain import ProviderChain, ResultsResolver, build_providers
from predbot.storage.db import Database
from predbot.storage.lease import LeaderLease, wait_for_leadership
from predbot.util import lifecycle
from predbot.util.timeutil import to_local, utcnow

log = logging.getLogger("main")

#: Retry subscriber delivery after the shared daily publication time.
DELIVERY_POLL_MINUTES = 5
#: exit status when the leader lease is lost mid-run (the process must not keep working)
EXIT_LEASE_LOST = 3
#: on shutdown, how long runs in progress may finish (Docker's default stop grace is 10 s)
SHUTDOWN_DRAIN_SECONDS = 6.0
#: then how long the Telegram poller may take to leave its long poll (it is abandoned after this)
POLLER_JOIN_SECONDS = 2.0


def _standby_until_leader(lease: LeaderLease) -> bool:
    """Block until this process holds the worker lease. SIGTERM/SIGINT while waiting ends the wait (False)."""
    import signal
    stop = threading.Event()
    previous = {}

    def handle(signum, frame):
        log.info("received signal %d while in standby; exiting", signum)
        stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            previous[sig] = signal.signal(sig, handle)
        except ValueError:                         # not the main thread (tests): keep the default handlers
            pass
    try:
        return wait_for_leadership(lease, stop)
    finally:
        for sig, old in previous.items():
            signal.signal(sig, old)


def main() -> None:
    lifecycle.reset()
    cfg = load_config()
    setup_logging(cfg.log_level, cfg.log_file, secrets=[cfg.sgo_key, cfg.sharpapi_key, cfg.odds_api_key,
                                                         cfg.football_data_key, cfg.telegram_token])
    for line in startup_lines(cfg):
        log.info(line)
    require_provider_keys(cfg)
    db = Database(cfg.db_path)
    providers = build_providers(cfg, db)
    log_coverage(cfg, providers)
    chain = ProviderChain(providers, cfg.provider_order)
    resolver = ResultsResolver(providers)
    tg = Telegram(cfg.telegram_token, cfg.telegram_chat_id)
    # a subscriber Telegram definitively refuses (blocked the bot, deleted the account) stops being retried
    tg.on_permanent_failure = broadcast.deactivate_unreachable(db)
    service = UnifiedFetcher(chain, db, cfg)
    log.info("providers: %s | timezone %s | picks at %s local | recap at %s | "
             "subscriber delivery: %s local, checked every %dm | cache: %s",
             ", ".join(chain.order) or "none", cfg.timezone, cfg.post_time, cfg.recap_time,
             cfg.post_time, DELIVERY_POLL_MINUTES, "sqlite")
    lease = LeaderLease(db)
    if not _standby_until_leader(lease):
        return
    log.info("leader lease acquired | owner=%s | ttl=%.0fs | renew every %.0fs - this process runs the scheduler "
             "and Telegram polling for this database. A worker on ANOTHER database (another host with its own "
             "DB_PATH) is not covered: still run exactly one deployment per Telegram token.",
             lease.owner, lease.ttl, lease.renew_every)
    lost = threading.Event()
    lease.start_heartbeat(on_lost=lost.set)

    def job_settle():
        settle.run(resolver, db, cfg)

    def job_live_results():
        # Settlement inside still runs when subscriber DMs are disabled; only
        # the pushes are skipped (see live_results._run).
        live_results.run(resolver, db, cfg, tg)

    def job_recap():
        try:
            job_settle()           # make sure yesterday is graded first
        except Exception:  # noqa: BLE001 - a settlement failure must not cancel the recap
            log.exception("settlement before recap failed; recap continues")
        text = recap.run(db, cfg, tg)                # channel mirror (optional)
        if text:
            db.set_kv("recap_text", text)            # cache recap for broadcast job

    def job_broadcast_recap():
        """Send recap to each subscriber after last fixture ends in their timezone."""
        if not getattr(cfg, "subscriber_delivery_enabled", True):
            return  # channel-only mode: the channel recap above still runs
        text = db.get_kv("recap_text")
        if text:
            broadcast.deliver_recap(db, cfg, tg, text)

    def job_deliver():
        """Poll: hand the card to every subscriber once DELIVERY_TIME has arrived."""
        if not getattr(cfg, "subscriber_delivery_enabled", True):
            return  # channel-only mode: generation and channel posts continue
        # Prevent API calls during delivery polling; only use cached data.
        snapshot = service.get_daily_snapshot(allow_api_call=False)
        if not snapshot.total_picks():
            return
        broadcast.deliver_picks(db, cfg, tg, snapshot)

    def job_retry_partial():
        """A PARTIAL slate is retried until DELIVERY_TIME, while nothing has been sent."""
        service.retry_partial()

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
        IntervalJob("deliver", DELIVERY_POLL_MINUTES, job_deliver),   # everyone's card at DELIVERY_TIME
        IntervalJob("retry_partial", max(1, cfg.cache_retry_seconds // 60), job_retry_partial),
        IntervalJob("broadcast_recap", 5, job_broadcast_recap),       # their last fixture ending
        DynamicJob("settle", cfg.timezone, compute_settle_deadline, job_settle),
    ]

    if cfg.live_updates_enabled:
        jobs.append(IntervalJob("live_results", cfg.live_poll_minutes, job_live_results))

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
            loop_thread.leader_check = lease.held
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
        loop(jobs, health_check=check_commands, leader_check=lease.held, stop_event=lost)
    finally:
        # Ordered, bounded shutdown: no new scheduled work (the loop has returned) and no new updates (the poller
        # is told to stop), runs in progress get a short drain and stop between sends, then the lease is released
        # so a successor starts at once. Nothing here can wait indefinitely.
        lifecycle.request_shutdown()
        if supervisor is not None:
            supervisor.stop(join_timeout=0)
        drain(jobs, SHUTDOWN_DRAIN_SECONDS)
        if supervisor is not None:
            supervisor.stop(join_timeout=POLLER_JOIN_SECONDS)
        lease.release()
        log.info("worker stopped")
    if lease.lost:
        log.critical("exiting: the leader lease was lost; restart this process to rejoin as standby")
        raise SystemExit(EXIT_LEASE_LOST)


if __name__ == "__main__":
    main()
