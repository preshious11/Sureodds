"""Post the previous Sunday-Saturday results recap on Sunday."""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Optional

from predbot.bot.format import compact_recap_message
from predbot.bot.telegram import Telegram
from predbot.config import Config
from predbot.jobs import stats as stats_mod
from predbot.storage.db import Database
from predbot.util.timeutil import to_local, utcnow

log = logging.getLogger(__name__)


def run(db: Database, cfg: Config, tg: Optional[Telegram], force: bool = False, dry_run: bool = False) -> Optional[str]:
    now = utcnow()
    local = to_local(now, cfg.timezone)
    through = (local - timedelta(days=1)).date()
    since = through - timedelta(days=6)
    through_str = through.isoformat()
    since_str = since.isoformat()
    # A Sunday report covers the previous Sunday through Saturday.
    if local.weekday() != 6:
        return None
    if not dry_run and db.post_done("weekly_recap", through_str):
        return None
    days = [(since + timedelta(days=offset)).isoformat() for offset in range(7)]
    statuses = {c: [] for c in ("top", "daily")}
    for day in days:
        if not stats_mod.rows_for_recap(db, day):
            continue
        for collection in statuses:
            statuses[collection].extend(
                row["status"] for row in sorted(
                    db.collection_predictions(collection, day),
                    key=lambda row: row["collection_rank"] or 0,
                )
            )
    text = compact_recap_message(
        statuses["top"], statuses["daily"],
        heading="📊 SUREODDS WEEKLY RESULTS",
        period=f"{since_str} – {through_str}",
    )
    if text is None:
        log.info("no picks for weekly recap %s through %s, skipping", since_str, through_str)
        return None
    if dry_run:
        print(text)
        return text
    if tg and tg.enabled():
        mid = tg.send(text)
        if mid:
            db.mark_post("weekly_recap", through_str, mid)
    return text
