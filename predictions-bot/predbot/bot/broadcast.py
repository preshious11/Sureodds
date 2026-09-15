"""Subscriber broadcasts: daily picks + results, built from the cached snapshot.

Zero external API calls: both messages are composed from the write-once-per-day
snapshot (picks) or from the recap job's already-fetched text (results).  Every
send is de-duplicated per (kind, date, chat) in the ``deliveries`` table, so
restarts and repeated job runs never double-send.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Optional

from predbot.bot.pagination import render_pick
from predbot.bot.telegram import Telegram
from predbot.cache.models import DailySnapshot
from predbot.storage.db import Database
from predbot.util.timeutil import to_local, utcnow

log = logging.getLogger(__name__)


def picks_text(snapshot: DailySnapshot, tz: str) -> str:
    """Full picks message (top + daily + record) rendered from a snapshot."""
    lines = [f"<b>🏆 TOP PICKS · {snapshot.date}</b>"]
    if not snapshot.top_picks:
        lines.append("No selection met the top-pick bar today.")
    lines.extend(render_pick(p, tz) for p in snapshot.top_picks)
    lines.append("")
    lines.append(f"<b>📋 DAILY PICKS ({len(snapshot.daily_picks)})</b>")
    if not snapshot.daily_picks:
        lines.append("No further selections cleared the quality bar today.")
    lines.extend(render_pick(p, tz) for p in snapshot.daily_picks)
    if snapshot.stats is not None:
        seven = snapshot.stats.to_raw()["last7"]
        lines.append("")
        lines.append(f"📊 Record: {seven['won']}W-{seven['lost']}L · ROI {seven['roi']:+.1f}%")
    lines.append("")
    lines.append("<i>Tap /start to browse interactively.</i>")
    return "\n".join(lines)


def deliver_picks(db: Database, cfg, tg: Telegram, snapshot: DailySnapshot) -> int:
    """Send today's picks to every active subscriber (once per day per user)."""
    if not (snapshot.top_picks or snapshot.daily_picks):
        return 0
    text = picks_text(snapshot, cfg.timezone)
    return _deliver(db, tg, "picks", snapshot.date, text, "daily picks")


def deliver_recap(db: Database, cfg, tg: Telegram, text: Optional[str]) -> int:
    """Send yesterday's recap text (as produced by jobs.recap.run) to subscribers."""
    if not text:
        return 0
    yesterday = (to_local(utcnow(), cfg.timezone) - timedelta(days=1)).strftime("%Y-%m-%d")
    return _deliver(db, tg, "recap", yesterday, text, "recap")


def _deliver(db: Database, tg: Telegram, kind: str, date_str: str, text: str, label: str) -> int:
    if not tg.token:
        return 0  # no Telegram token configured - nothing to deliver
    sent = 0
    for row in db.subscribers_active():
        chat_id = str(row["chat_id"])
        if db.delivery_done(kind, date_str, chat_id):
            continue
        if tg.send(text, chat_id=chat_id):
            db.mark_delivery(kind, date_str, chat_id)
            sent += 1
    if sent:
        log.info("broadcast: %s delivered to %d subscriber(s)", label, sent)
    return sent
