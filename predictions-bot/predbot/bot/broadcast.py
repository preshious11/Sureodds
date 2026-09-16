"""Subscriber broadcasts: daily picks + results, built from the cached snapshot.

Picks use the same canonical SQLite snapshot as commands; its service may retry
failed providers after the configured cooldown. Recaps use already-fetched text.  Every
send is de-duplicated per (kind, card date, chat) in the ``deliveries`` table, so
restarts and repeated job runs never double-send.

Delivery uses one application-zone instant (09:00 Africa/Lagos by default).
Subscriber zones affect displayed kickoff times, not delivery eligibility.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Callable, Optional

from predbot.bot.format import PickLine, kickoff_label, picks_card, sport_name
from predbot.bot.telegram import Telegram
from predbot.cache.models import DailySnapshot
from predbot.markets.catalog import short_selection
from predbot.providers.base import MarketLine
from predbot.storage.db import Database
from predbot.util.timeutil import resolve_timezone, local_reached, parse_iso, to_local, utcnow

log = logging.getLogger(__name__)

#: Resolves the canonical card to deliver; ``UnifiedFetcher.published_snapshot``.
SnapshotProvider = Callable[[], Optional[DailySnapshot]]


def picks_text(snapshot: DailySnapshot, tz: str) -> str:
    """The daily predictions card (top picks first, then the daily ones) from a snapshot."""
    picks = []
    for p in snapshot.top_picks + snapshot.daily_picks:
        ml = MarketLine(p.market, p.selection, p.price, line=p.line)
        picks.append(PickLine(sport=sport_name(p.sport),
                              match=f"{p.home} vs {p.away}",
                              outcome=p.label or short_selection(ml, p.home, p.away, p.sport),
                              league=p.league or sport_name(p.sport),
                              confidence=p.prob, when=kickoff_label(parse_iso(p.kickoff_utc), tz),
                              price=p.price))
    return picks_card(picks, snapshot.date, _record_line(snapshot))


def _record_line(snapshot: DailySnapshot) -> Optional[str]:
    """7-day record footer - only once there is settled history worth showing."""
    if snapshot.stats is None or not snapshot.stats.all.n:
        return None
    seven = snapshot.stats.to_raw()["last7"]
    return f"📊 7d: {seven['won']}W-{seven['lost']}L · ROI {seven['roi']:+.1f}%"


def subscriber_tz(row, fallback_tz: str) -> str:
    """The zone a subscriber's delivery is measured in (stored zone, else config)."""
    try:
        stored = str(row["timezone"] or "").strip()
    except (KeyError, IndexError, TypeError):
        stored = ""
    return resolve_timezone(stored, fallback_tz)


def deliver_picks(db: Database, cfg, tg: Telegram, snapshot_provider: SnapshotProvider,
                  now: Optional[datetime] = None) -> int:
    """Deliver once per card after the shared application-zone broadcast time."""
    if not tg.token:
        return 0
    now = now or utcnow()
    if not local_reached(now, cfg.timezone, cfg.post_time):
        return 0
    snapshot = snapshot_provider()
    if snapshot is None or not (snapshot.top_picks or snapshot.daily_picks):
        return 0

    sent = 0
    for row in db.subscribers_active():
        chat_id = str(row["chat_id"])
        tz = subscriber_tz(row, cfg.timezone)
        if db.delivery_done("picks", snapshot.date, chat_id):
            continue                                   # already got this card
        if tg.send(picks_text(snapshot, tz), chat_id=chat_id):
            db.mark_delivery("picks", snapshot.date, chat_id)
            sent += 1
    if sent:
        log.info("broadcast: daily picks (%s) delivered to %d subscriber(s)",
                 snapshot.date, sent)
    return sent


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
