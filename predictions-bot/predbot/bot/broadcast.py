"""Subscriber broadcasts: daily picks + results, built from the cached snapshot.

Picks use the same canonical SQLite snapshot as commands; its service may retry
failed providers after the configured cooldown. Recaps use already-fetched text.  Every
send is de-duplicated per (kind, card date, chat) in the ``deliveries`` table, so
restarts and repeated job runs never double-send.

Picks land at ``DELIVERY_TIME`` (default 09:30) in ``TIMEZONE`` - ONE shared instant for
every subscriber. A subscriber's own zone only changes how kick-off times are shown on
their card. Only people whose subscription already existed at that moment get it:
joining, /start or re-subscribing after it is never itself a reason to be sent today's
card, which arrives with the next scheduled broadcast instead.
Onboarding is therefore menu-only - it never triggers a prediction send.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Callable, Optional, Union

from predbot.bot.format import generated_line, pick_line_from_record, summary_card
from predbot.bot.telegram import Telegram
from predbot.cache.models import DailySnapshot
from predbot.storage.db import Database
from predbot.util.timeutil import parse_hhmm, resolve_timezone, local_reached, parse_iso, to_local, utcnow

log = logging.getLogger(__name__)

#: Resolves the canonical card to deliver; ``UnifiedFetcher.published_snapshot``.
SnapshotProvider = Callable[[], Optional[DailySnapshot]]



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


def _joined_before_target(row, date_str: str, hour: int, minute: int, user_tz: str) -> bool:
    """True when this subscription existed by the time today's card was due.

    A subscriber who joins AFTER their own 9:30 has already missed today's
    broadcast, so they start with the next one.  Without this gate, merely sending
    /start after 9:30 was enough to be handed the day's card on the next poll -
    onboarding silently became an instant prediction send, and a fresh joiner got
    a ticket for matches that had already started.

    Falls back to "eligible" when ``subscribed_at`` is missing or unparseable
    (rows created before this rule existed), i.e. the historical behaviour.
    """
    try:
        joined = parse_iso(str(row["subscribed_at"] or ""))
    except (KeyError, IndexError, TypeError):
        return True
    if joined is None:
        return True
    local = to_local(joined, user_tz)
    local_date = local.strftime("%Y-%m-%d")
    if local_date > date_str:
        return False                       # joined after the card's day - not due yet
    if local_date == date_str:
        due = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return local <= due                # subscribed by, or before, their 9:30
    return True


def picks_text(snapshot: DailySnapshot, tz: str, now: Optional[datetime] = None) -> str:
    """THE subscriber summary card (single canonical renderer, no drift).

    Every pick line is built by ``pagination.pick_line_from_record`` - the same
    builder the /top and /daily views use - so a pick cannot render one way in
    the views and another way here. Ambiguous picks (no explicit outcome) are
    dropped BEFORE any totals are computed.
    """
    top = [pick_line_from_record(p, tz, None, now) for p in snapshot.top_picks]
    daily = [pick_line_from_record(p, tz, None, now) for p in snapshot.daily_picks]
    top = [p for p in top if p.outcome]
    daily = [p for p in daily if p.outcome]
    if not top and not daily:
        return ""
    card = summary_card(top, daily, generated=generated_line(snapshot.generated_at, tz))
    footer = _record_line(snapshot)
    return f"{card}\n{footer}" if footer else card


def _record_line(snapshot: DailySnapshot) -> Optional[str]:
    """7-day record footer - only once there is settled history worth showing."""
    if snapshot.stats is None or not snapshot.stats.all.n:
        return None
    seven = snapshot.stats.to_raw()["last7"]
    return f"📊 7d: {seven['won']}W-{seven['lost']}L · ROI {seven['roi']:+.1f}%"


def deliver_picks(db: Database, cfg, tg: Telegram,
                  snapshot: Union[DailySnapshot, SnapshotProvider, None],
                  now: Optional[datetime] = None) -> int:
    """Send today's card to every subscriber at ``DELIVERY_TIME`` in ``TIMEZONE``.

    One shared instant (default 09:30 Lagos): a subscriber in New York gets it at the
    same moment as one in Lagos, with kick-offs shown in their own zone, and the
    ``deliveries`` table keeps it to once per person per day.

    Accepts either a snapshot or a zero-argument provider (``published_snapshot``),
    so the scheduler can poll without forcing a provider fetch.
    """
    if not tg.token:
        return 0
    if callable(snapshot):
        snapshot = snapshot()
    if snapshot is None or not (snapshot.top_picks or snapshot.daily_picks):
        return 0
    hour, minute = parse_hhmm(cfg.delivery_time, "09:30")
    return _deliver_per_user_at_time(db, cfg, tg, "picks", snapshot.date, snapshot,
                                     hour, minute, "daily picks", now=now)


def deliver_recap(db: Database, cfg, tg: Telegram, text: Optional[str]) -> int:
    """Send recap to each subscriber after last fixture ends in their timezone."""
    if not text:
        return 0
    yesterday = (to_local(utcnow(), cfg.timezone) - timedelta(days=1)).strftime("%Y-%m-%d")
    return _deliver_recap_after_fixtures(db, cfg, tg, "recap", yesterday, text, "recap")


def _deliver_per_user_at_time(db: Database, cfg, tg: Telegram, kind: str, date_str: str,
                              snapshot: DailySnapshot, target_hour: int, target_minute: int,
                              label: str, now: Optional[datetime] = None) -> int:
    """Send to each subscriber once the target time has arrived in ``cfg.timezone``.

    "At or past" rather than an exact-minute window: the scheduler polls every few
    minutes, so a narrow window would silently skip a subscriber whose minute was
    never sampled.  ``delivery_done()`` keeps it to one card per person per day.

    Subscribers whose subscription started AFTER that moment are skipped for the
    day (``_joined_before_target``): joining must never be what puts a card in
    someone's chat, so onboarding cannot cause an instant prediction send.
    """
    if not tg.token:
        return 0
    now = now or utcnow()
    sent = 0
    for row in db.subscribers_active():
        chat_id = str(row["chat_id"])
        if db.delivery_done(kind, date_str, chat_id):
            continue
        user_tz = subscriber_tz(row, cfg.timezone)      # display only
        local = to_local(now, cfg.timezone)
        if local.strftime("%Y-%m-%d") < date_str:
            continue                      # the card's day hasn't started yet
        if (local.hour, local.minute) < (target_hour, target_minute):
            continue                      # DELIVERY_TIME hasn't arrived
        if not _joined_before_target(row, date_str, target_hour, target_minute, cfg.timezone):
            continue                      # joined after today's card was due - starts tomorrow
        # Deliberately "at or past" rather than an exact-minute window: the
        # scheduler polls every few minutes, so a narrow window would silently
        # skip a subscriber whose minute was never sampled.  delivery_done()
        # is what keeps this to one card per person per day.
        text = picks_text(snapshot, user_tz, now=now)
        if not text:
            continue                      # nothing explicit to show - never send a blank card
        if tg.send(text, chat_id=chat_id):
            db.mark_delivery(kind, date_str, chat_id)
            sent += 1
    if sent:
        log.info("broadcast: %s delivered to %d subscriber(s) at %02d:%02d",
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
