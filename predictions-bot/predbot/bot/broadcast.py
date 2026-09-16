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
from typing import Callable, Optional, Union

from predbot.bot.format import PickLine, kickoff_label, outcome_label, picks_card, sport_name
from predbot.bot.telegram import Telegram
from predbot.cache.models import DailySnapshot
from predbot.markets.catalog import short_selection
from predbot.providers.base import MarketLine
from predbot.storage.db import Database
from predbot.util.timeutil import resolve_timezone, local_reached, parse_iso, to_local, utcnow

log = logging.getLogger(__name__)

#: Resolves the canonical card to deliver; ``UnifiedFetcher.published_snapshot``.
SnapshotProvider = Callable[[], Optional[DailySnapshot]]


#: Picks land at 9:30 in each subscriber's OWN zone, not one shared instant.
BROADCAST_HOUR, BROADCAST_MINUTE = 9, 30


def subscriber_tz(row, fallback_tz: str) -> str:
    """The zone a subscriber's delivery is measured in (stored zone, else config).

    ``row`` is a ``sqlite3.Row``, which has no ``.get()`` - hence the try/except
    rather than a dict lookup.
    """
    try:
        stored = str(row["timezone"] or "").strip()
    except (KeyError, IndexError, TypeError):
        stored = ""
    return resolve_timezone(stored, fallback_tz)


def picks_text(snapshot: DailySnapshot, tz: str) -> str:
    """The daily predictions card (top picks first, then the daily ones) from a snapshot."""
    picks = []
    for p in snapshot.top_picks + snapshot.daily_picks:
        ml = MarketLine(p.market, p.selection, p.price, line=p.line)
        picks.append(PickLine(sport=sport_name(p.sport),
                              match=f"{p.home} vs {p.away}",
                              outcome=outcome_label(p.label, ml, p.home, p.away, p.sport),
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


def deliver_picks(db: Database, cfg, tg: Telegram,
                  snapshot: Union[DailySnapshot, SnapshotProvider, None],
                  now: Optional[datetime] = None) -> int:
    """Send today's card to each subscriber at 9:30 AM in THEIR OWN timezone.

    Deliberately not one shared broadcast instant: a subscriber in Lagos and one
    in New York each get the card at their own 9:30, and the ``deliveries`` table
    keeps it to once per person per day.

    Accepts either a snapshot or a zero-argument provider (``published_snapshot``),
    so the scheduler can poll without forcing a provider fetch.
    """
    if not tg.token:
        return 0
    if callable(snapshot):
        snapshot = snapshot()
    if snapshot is None or not (snapshot.top_picks or snapshot.daily_picks):
        return 0
    return _deliver_per_user_at_time(db, cfg, tg, "picks", snapshot.date, snapshot,
                                     BROADCAST_HOUR, BROADCAST_MINUTE, "daily picks",
                                     now=now)


def deliver_recap(db: Database, cfg, tg: Telegram, text: Optional[str]) -> int:
    """Send recap to each subscriber after last fixture ends in their timezone."""
    if not text:
        return 0
    yesterday = (to_local(utcnow(), cfg.timezone) - timedelta(days=1)).strftime("%Y-%m-%d")
    return _deliver_recap_after_fixtures(db, cfg, tg, "recap", yesterday, text, "recap")


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


def _deliver_per_user_at_time(db: Database, cfg, tg: Telegram, kind: str, date_str: str,
                              snapshot: DailySnapshot, target_hour: int, target_minute: int,
                              label: str, now: Optional[datetime] = None) -> int:
    """Send to each subscriber when target time arrives in their timezone (within 2-minute window)."""
    if not tg.token:
        return 0
    now = now or utcnow()
    sent = 0
    for row in db.subscribers_active():
        chat_id = str(row["chat_id"])
        if db.delivery_done(kind, date_str, chat_id):
            continue
        user_tz = subscriber_tz(row, cfg.timezone)
        local = to_local(now, user_tz)
        if local.strftime("%Y-%m-%d") < date_str:
            continue                      # their day hasn't started yet
        if (local.hour, local.minute) < (target_hour, target_minute):
            continue                      # their 9:30 hasn't arrived
        # Deliberately "at or past" rather than an exact-minute window: the
        # scheduler polls every few minutes, so a narrow window would silently
        # skip a subscriber whose minute was never sampled.  delivery_done()
        # is what keeps this to one card per person per day.
        if tg.send(picks_text(snapshot, user_tz), chat_id=chat_id):
            db.mark_delivery(kind, date_str, chat_id)
            sent += 1
    if sent:
        log.info("broadcast: %s delivered to %d subscriber(s) at their %02d:%02d",
                 label, sent, target_hour, target_minute)
    return sent


def _get_last_fixture_end_time(db: Database, pick_date: str) -> Optional[datetime]:
    """Get the latest fixture end time for a date (latest kickoff + 4 hour buffer for results)."""
    rows = db.picks_for_date(pick_date)
    if not rows:
        return None
    latest_kickoff = None
    for row in rows:
        if row["start_utc"]:
            kt = parse_iso(row["start_utc"])
            if latest_kickoff is None or kt > latest_kickoff:
                latest_kickoff = kt
    if latest_kickoff is None:
        return None
    return latest_kickoff + timedelta(hours=4)  # buffer for match duration + results processing


def _deliver_recap_after_fixtures(db: Database, cfg, tg: Telegram, kind: str, date_str: str,
                                   text: str, label: str) -> int:
    """Send recap to each subscriber after last fixture ends in their timezone, personalized per timezone."""
    if not tg.token or not text:
        return 0
    last_fixture_end = _get_last_fixture_end_time(db, date_str)
    if not last_fixture_end:
        log.warning("recap: no fixtures found for %s", date_str)
        return 0

    from predbot.bot.format import recap_message
    from predbot.jobs import stats as stats_mod

    now = utcnow()
    sent = 0
    for row in db.subscribers_active():
        chat_id = str(row["chat_id"])
        if db.delivery_done(kind, date_str, chat_id):
            continue
        user_tz = subscriber_tz(row, cfg.timezone)
        local = to_local(now, user_tz)
        fixture_end_local = to_local(last_fixture_end, user_tz)

        if local >= fixture_end_local:
            yesterday_local = (local - timedelta(days=1)).strftime("%Y-%m-%d")
            recap_rows = stats_mod.rows_for_recap(db, yesterday_local)
            stats = stats_mod.compute(db, local)
            personalized_text = recap_message("YESTERDAY", stats_mod.by_sport(recap_rows), stats)
            if tg.send(personalized_text, chat_id=chat_id):
                db.mark_delivery(kind, date_str, chat_id)
                sent += 1
    if sent:
        log.info("broadcast: %s delivered to %d subscriber(s) after last fixture ended", label, sent)
    return sent
