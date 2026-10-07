"""Post yesterday's results + rolling stats (runs before the daily picks)."""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Optional

from predbot.bot.format import recap_message
from predbot.bot.telegram import Telegram
from predbot.config import Config
from predbot.jobs import stats as stats_mod
from predbot.storage.db import Database
from predbot.util.timeutil import to_local, utcnow

log = logging.getLogger(__name__)


def run(db: Database, cfg: Config, tg: Optional[Telegram], force: bool = False, dry_run: bool = False) -> Optional[str]:
    now = utcnow()
    local = to_local(now, cfg.timezone)
    y = local - timedelta(days=1)
    y_str = y.strftime("%Y-%m-%d")
    # ``force`` only recomputes the recap text; it never reposts a recap that is
    # already recorded in ``posts`` (same regeneration-vs-repost split as picks).
    if not dry_run and db.post_done("recap", y_str):
        return None
    rows = stats_mod.rows_for_recap(db, y_str)
    if not rows:
        log.info("no picks for %s, skipping recap", y_str)
        return None
    text = recap_message(stats_mod.compute(db, local))
    if dry_run:
        print(text)
        return text
    if tg and tg.enabled():
        mid = tg.send(text)
        if mid:
            db.mark_post("recap", y_str, mid)
    return text
