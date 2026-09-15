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

from predbot.bot.format import PickLine, kickoff_label, picks_card, sport_name
from predbot.bot.telegram import Telegram
from predbot.cache.models import DailySnapshot
from predbot.storage.db import Database
from predbot.util.timeutil import parse_iso, to_local, utcnow

log = logging.getLogger(__name__)


def picks_text(snapshot: DailySnapshot, tz: str) -> str:
    """The daily predictions card (top picks first, then the daily ones) from a snapshot."""
    picks = [PickLine(league=p.league or sport_name(p.sport), label=p.label,
                      confidence=p.prob, when=kickoff_label(parse_iso(p.kickoff_utc), tz),
                      price=p.price)
             for p in snapshot.top_picks + snapshot.daily_picks]
    return picks_card(picks, snapshot.date, _record_line(snapshot))


def _record_line(snapshot: DailySnapshot) -> Optional[str]:
    """7-day record footer - only once there is settled history worth showing."""
    if snapshot.stats is None or not snapshot.stats.all.n:
        return None
    seven = snapshot.stats.to_raw()["last7"]
    return f"📊 7d: {seven['won']}W-{seven['lost']}L · ROI {seven['roi']:+.1f}%"


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
