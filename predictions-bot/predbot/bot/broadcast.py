"""Subscriber broadcasts: daily picks + results, built from the cached snapshot.

Zero external API calls: both messages are composed from the write-once-per-day
snapshot (picks) or from the recap job's already-fetched text (results).  Every
send is de-duplicated per (kind, card date, chat) in the ``deliveries`` table, so
restarts and repeated job runs never double-send.

Delivery time is LOCAL to each recipient: the scheduler polls, and every
subscriber whose own clock has passed ``POST_TIME`` (09:30) in their own zone
receives the newest published card - once per card.  Subscribers who never set
``/timezone`` fall back to the configured ``TIMEZONE``.
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
from predbot.util.timeutil import is_valid_tz, local_reached, parse_iso, to_local, utcnow

log = logging.getLogger(__name__)

#: Resolves the card to deliver (zero API reads); ``UnifiedFetcher.published_snapshot``.
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
    return stored if stored and is_valid_tz(stored) else fallback_tz


def deliver_picks(db: Database, cfg, tg: Telegram, snapshot_provider: SnapshotProvider,
                  now: Optional[datetime] = None) -> int:
    """Send the published picks to every subscriber whose local clock says 09:30.

    One decision per subscriber, in *their* zone:

    * gate     - ``POST_TIME`` has passed on their local clock (a catch-up rule,
                 so a restart at 09:35 still delivers that day instead of
                 silently skipping it);
    * card     - the newest published picks, rendered with their zone's kickoff
                 times;
    * dedupe   - once per (card date, chat), so restarts and 30s polling cannot
                 double-send.

    A subscriber who joins after 09:30 local therefore receives that day's card
    on the next tick.  Returns the number of messages sent.
    """
    if not tg.token:
        return 0
    now = now or utcnow()
    snapshot = snapshot_provider()
    if snapshot is None or not (snapshot.top_picks or snapshot.daily_picks):
        return 0

    sent = 0
    for row in db.subscribers_active():
        chat_id = str(row["chat_id"])
        tz = subscriber_tz(row, cfg.timezone)
        if not local_reached(now, tz, cfg.post_time):
            continue                                   # not deliverable in *this* zone yet
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
