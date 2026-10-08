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

Duplicate prevention (picks and recap DMs):

* one delivery run per (database, kind) at a time in a process: a run that is still going makes the next scheduler
  tick (or an overlapping manual call) return at once instead of starting a second full pass;
* each message's receipt is written the moment Telegram accepts it - not at the end of a batch - and is re-checked
  right before each chat is sent to, so a crash or redeploy mid-run can duplicate at most the one message that was
  in flight (Telegram has no idempotency key);
* a receipt is never written before Telegram accepted the message: a failed or interrupted send stays retryable;
* a shutdown request stops a run between chats (``predbot.util.lifecycle``); the next leader sends what is missing.
"""
from __future__ import annotations

import logging
import hashlib
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional, Tuple, Union

from predbot.bot.format import (DAILY_TITLE, TOP_TITLE, pick_line_from_record, picks_card, summary_card,
                                zone_label)
from predbot.bot.telegram import Telegram
from predbot.cache.models import DailySnapshot
from predbot.storage.db import Database
from predbot.util.lifecycle import shutting_down
from predbot.util.timeutil import parse_hhmm, resolve_timezone, local_reached, parse_iso, to_local, utcnow

log = logging.getLogger(__name__)

_RUN_LOCKS: Dict[Tuple[str, str], threading.Lock] = {}
_RUN_LOCKS_GUARD = threading.Lock()


def run_lock(db: Database, name: str) -> threading.Lock:
    """The one lock per (database file, delivery kind) that keeps two delivery runs from overlapping."""
    path = getattr(db, "path", "") or ""
    key = (os.path.normcase(os.path.abspath(path)) if path and path != ":memory:" else str(id(db)), name)
    with _RUN_LOCKS_GUARD:
        return _RUN_LOCKS.setdefault(key, threading.Lock())


def deactivate_unreachable(db: Database) -> Callable[[str, str], None]:
    """``Telegram.on_permanent_failure`` for the worker: a private chat Telegram definitively refuses (the user
    blocked the bot, deleted their account, never started it) stops receiving automatic deliveries. /start
    re-subscribes. Groups and channels are never touched (their failures are configuration, not a subscriber)."""
    def hook(chat_id: str, reason: str) -> None:
        chat = str(chat_id)
        if not chat.isdigit():
            return
        if db.query("SELECT 1 FROM subscribers WHERE chat_id=? AND active=1", (chat,)):
            db.subscriber_remove(chat)
            log.warning("subscriber deactivated: Telegram refuses delivery permanently (%s)", reason.split(":")[0])
    return hook


def live_delivery_kind(fixture_key: str, ended: bool) -> str:
    digest = hashlib.sha256(fixture_key.encode("utf-8")).hexdigest()
    return f"match_{'end' if ended else 'start'}:{digest}"


def _received_any(row, chat_id: str, received) -> bool:
    """A live notification goes to a subscriber who was sent a collection this prediction belongs to."""
    return any(chat_id in received.get(c, ()) for c in (row.get("collections") or ()))


def deliver_live(db: Database, tg: Telegram, pick_date: str, fixture_key: str,
                 rows, result, ended: bool = False):
    """Return (sent, complete), persisting each success before the next send.

    The live job serializes runs. Delivery history supplies both eligibility and
    deduplication; a failed send remains retryable after the fixture is settled.
    """
    from predbot.bot.format import live_match_message
    from predbot.jobs.live_results import orient_result
    kind = live_delivery_kind(fixture_key, ended)
    received = {tier: db.deliveries_done(f"picks_{tier}", pick_date) for tier in ("top", "daily")}
    done = db.deliveries_done(kind, pick_date)
    sent = 0
    subscribers = db.subscribers_active()
    for user in subscribers:
        chat_id = str(user["chat_id"])
        relevant = [r for r in rows if _received_any(r, chat_id, received)]
        if not relevant or chat_id in done:
            continue
        if not tg.token:
            continue
        if shutting_down():
            break                     # receipts so far are recorded; the rest is retried by the next run
        text = live_match_message(relevant, orient_result(result, rows[0], relevant[0]), ended)
        try:
            success = tg.send(text, chat_id=chat_id)
        except Exception:  # one failed recipient must not stop other deliveries
            log.warning("live: notification send failed; will retry")
            continue
        if success:
            db.mark_delivery(kind, pick_date, chat_id)
            done.add(chat_id)
            sent += 1
        time.sleep(1.0 / SENDS_PER_SECOND)
    # Re-evaluate only the recipients attempted in this run, without another API call.
    complete = all(str(u["chat_id"]) in done or not any(
        _received_any(r, str(u["chat_id"]), received) for r in rows)
        for u in subscribers)
    if sent:
        log.info("live: %s notification delivered to %d subscribers", "end" if ended else "start", sent)
    return sent, complete


def deliver_community_live(db: Database, tg: Telegram, pick_date: str, fixture_key: str,
                          rows, result, ended: bool = False):
    """Send only predictions from cards published to this configured destination.

    Community publication receipts are separate from subscriber pick receipts,
    so they do not change generation/retry gates. Live receipts use the existing
    per-fixture, per-date, per-chat key. The caller's live run lock serializes sends.
    Return (sent, complete); failures remain retryable without re-settling.
    """
    from predbot.bot.format import live_match_message
    from predbot.jobs.live_results import orient_result

    if not tg or not tg.enabled():
        return 0, True
    chat_id = str(tg.chat_id)
    published = {tier for tier in ("top", "daily")
                 if db.delivery_done(f"community_picks_{tier}", pick_date, chat_id)}
    relevant = [r for r in rows if published.intersection(r.get("collections") or ())]
    if not relevant:
        return 0, True
    kind = live_delivery_kind(fixture_key, ended)
    if db.delivery_done(kind, pick_date, chat_id):
        return 0, True
    text = live_match_message(relevant, orient_result(result, rows[0], relevant[0]), ended)
    try:
        success = tg.send(text, chat_id=chat_id)
    except Exception:  # one destination failure must remain retryable
        log.warning("live: community notification send failed; will retry")
        return 0, False
    if not success:
        return 0, False
    db.mark_delivery(kind, pick_date, chat_id)
    log.info("live: %s notification delivered to configured community", "end" if ended else "start")
    return 1, True


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


#: the two messages of a day's picks, in the order they are sent; each is recorded in ``deliveries`` / ``posts``
#: under its own kind so a retry after a partial failure sends only what is missing
PICKS_KINDS = ("picks_top", "picks_daily")


def picks_messages(snapshot: DailySnapshot, tz: str, now: Optional[datetime] = None,
                   include_empty: bool = False) -> List[Tuple[str, str, Dict]]:
    """THE subscriber cards: ``[(kind, text, keyboard)]`` - the Top card, then the Daily card, each its own message
    with the four-button keyboard. An empty tier still gets its message (the card says no selection cleared the
    bar); both tiers empty -> ``[]``.

    Every pick line is built by ``format.pick_line_from_record`` - the same builder the /top and /daily views use.
    Ambiguous picks (no explicit outcome) are dropped BEFORE any totals are computed."""
    from predbot.bot.pagination import build_keyboard
    top = [p for p in (pick_line_from_record(r, tz, None, now) for r in snapshot.top_picks) if p.outcome]
    daily = [p for p in (pick_line_from_record(r, tz, None, now) for r in snapshot.daily_picks) if p.outcome]
    if not top and not daily and not include_empty:
        return []
    zone = zone_label(tz, snapshot.date)
    top_text = summary_card(top, [], snapshot.date, zone) if top else picks_card([], snapshot.date, TOP_TITLE, zone=zone)
    daily_text = summary_card([], daily, snapshot.date, zone) if daily else picks_card([], snapshot.date, DAILY_TITLE, zone=zone)
    return [("picks_top", top_text, build_keyboard("top", 0, 1)), ("picks_daily", daily_text, build_keyboard("daily", 0, 1))]


def picks_text(snapshot: DailySnapshot, tz: str, now: Optional[datetime] = None) -> str:
    """Both cards as one text (the /lastrun 'slates as sent' view and the dry-run print). What is SENT is
    :func:`picks_messages`, one message per card."""
    return "\n\n".join(text for _, text, _ in picks_messages(snapshot, tz, now))


def deliver_picks(db: Database, cfg, tg: Telegram,
                  snapshot: Union[DailySnapshot, SnapshotProvider, None],
                  now: Optional[datetime] = None) -> int:
    """Send today's card to every subscriber at ``DELIVERY_TIME`` in ``TIMEZONE``.

    One shared instant (default 09:30 Lagos): a subscriber in New York gets it at the
    same moment as one in Lagos, with kick-offs shown in their own zone, and the
    ``deliveries`` table keeps it to once per person per day.

    Accepts either a snapshot or a zero-argument provider (``published_snapshot``),
    so the scheduler can poll without forcing a provider fetch.

    When ``SUBSCRIBER_DELIVERY_ENABLED`` is false (channel-only mode) no
    subscriber DM is sent; the channel mirror is unaffected.
    """
    if not getattr(cfg, "subscriber_delivery_enabled", True):
        return 0
    if not tg.token:
        return 0
    if callable(snapshot):
        snapshot = snapshot()
    if snapshot is None or not (snapshot.top_picks or snapshot.daily_picks):
        return 0
    hour, minute = parse_hhmm(cfg.delivery_time, "09:30")
    lock = run_lock(db, "picks")
    if not lock.acquire(blocking=False):
        log.info("broadcast: a picks delivery run is still in progress - not starting another")
        return 0
    try:
        return _deliver_per_user_at_time(db, cfg, tg, "picks", snapshot.date, snapshot,
                                         hour, minute, "daily picks", now=now)
    finally:
        lock.release()


def deliver_recap(db: Database, cfg, tg: Telegram, text: Optional[str]) -> int:
    """Send recap to each subscriber after last fixture ends in their timezone.

    Disabled entirely when ``SUBSCRIBER_DELIVERY_ENABLED`` is false; the
    channel recap is unaffected.
    """
    if not getattr(cfg, "subscriber_delivery_enabled", True):
        return 0
    if not text:
        return 0
    yesterday = (to_local(utcnow(), cfg.timezone) - timedelta(days=1)).strftime("%Y-%m-%d")
    lock = run_lock(db, "recap")
    if not lock.acquire(blocking=False):
        log.info("broadcast: a recap delivery run is still in progress - not starting another")
        return 0
    try:
        return _deliver_recap_after_fixtures(db, cfg, tg, "recap", yesterday, text, "recap")
    finally:
        lock.release()


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
    local = to_local(now, cfg.timezone)                  # one instant for every subscriber
    if local.strftime("%Y-%m-%d") < date_str:
        return 0                          # the card's day hasn't started yet
    if (local.hour, local.minute) < (target_hour, target_minute):
        return 0                          # DELIVERY_TIME hasn't arrived
    done = {k: db.deliveries_done(k, date_str) for k in PICKS_KINDS}       # one query per kind, not one per subscriber
    cards: Dict[str, List[Tuple[str, str, Dict]]] = {}     # the cards differ only by the subscriber's zone: render once per zone
    pending: List[Tuple[str, List[Tuple[str, str, Dict]]]] = []
    for row in db.subscribers_active():
        chat_id = str(row["chat_id"])
        if all(chat_id in done[k] for k in PICKS_KINDS):
            continue
        user_tz = subscriber_tz(row, cfg.timezone)      # display only
        if not _joined_before_target(row, date_str, target_hour, target_minute, cfg.timezone):
            continue                      # joined after today's card was due - starts tomorrow
        # Deliberately "at or past" rather than an exact-minute window: the
        # scheduler polls every few minutes, so a narrow window would silently
        # skip a subscriber whose minute was never sampled.  deliveries is
        # what keeps this to one card per person per day.
        if user_tz not in cards:
            cards[user_tz] = picks_messages(snapshot, user_tz, now=now)
        missing = [m for m in cards[user_tz] if chat_id not in done[m[0]]]
        if missing:                       # nothing explicit to show -> never a blank card
            pending.append((chat_id, missing))
    sent = _send_all(db, tg, date_str, pending)
    if sent:
        log.info("broadcast: %s delivered to %d subscriber(s) at %02d:%02d",
                 label, sent, target_hour, target_minute)
    return sent


#: Below this many recipients the sends stay sequential (order, and nothing to gain from threads).
PARALLEL_MIN = 8
#: Telegram allows ~30 messages/s to different chats; stay well under it.
SENDS_PER_SECOND = 20.0
SEND_WORKERS = 8


def _send_chat(tg: Telegram, chat_id: str, messages: List[Tuple[str, str, Dict]],
               db: Optional[Database] = None, date_str: str = "") -> List[str]:
    """One chat's messages, in order. Stops at the first failure (a Daily must never arrive before its Top), and
    returns the kinds that went out - a retry sends only what is missing.

    With ``db``: each kind is re-checked against its receipt just before sending and its receipt is written as soon
    as Telegram accepts it, so an interruption anywhere leaves exactly the accepted messages recorded."""
    ok: List[str] = []
    for kind, text, markup in messages:
        if db is not None and db.delivery_done(kind, date_str, chat_id):
            continue                                   # already delivered (another run, or before a restart)
        if not tg.send(text, chat_id=chat_id, reply_markup=markup):
            break
        if db is not None:
            db.mark_delivery(kind, date_str, chat_id)
        ok.append(kind)
    return ok


def _send_all(db: Database, tg: Telegram, date_str: str,
              pending: List[Tuple[str, List[Tuple[str, str, Dict]]]]) -> int:
    """Send each chat its missing messages, recording every success the moment it is accepted. Returns how many
    chats received at least one message. The 20 msg/s cap counts every message, both cards of a chat included. A
    shutdown request stops submitting new chats; sends already started finish and are recorded."""
    if len(pending) < PARALLEL_MIN:
        served = 0
        for chat_id, messages in pending:
            if shutting_down():
                log.info("broadcast: shutdown requested - stopping; the rest is sent by the next run")
                break
            served += bool(_send_chat(tg, chat_id, messages, db, date_str))
        return served
    served = 0
    interval = 1.0 / SENDS_PER_SECOND
    with ThreadPoolExecutor(max_workers=SEND_WORKERS, thread_name_prefix="deliver") as pool:
        futures, next_slot = [], time.monotonic()
        for chat_id, messages in pending:
            if shutting_down():
                log.info("broadcast: shutdown requested - stopping; the rest is sent by the next run")
                break
            wait = next_slot - time.monotonic()
            if wait > 0:
                time.sleep(wait)                                  # <= SENDS_PER_SECOND messages per second
            next_slot = max(next_slot, time.monotonic()) + interval * len(messages)
            futures.append((chat_id, pool.submit(_send_chat, tg, chat_id, messages, db, date_str)))
        for chat_id, fut in futures:
            try:
                served += bool(fut.result())
            except Exception:  # noqa: BLE001 - one failed send never stops the rest; it is retried on the next poll
                log.exception("broadcast: a subscriber send raised")
    return served


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


def recap_text_for(db: Database, cfg, date_str: str) -> str:
    """The recap of ``date_str`` (a card date in ``cfg.timezone``): computed ONCE for every reader.

    Picks are stored under the card date of TIMEZONE, so the recap must be built for that date explicitly - not
    for "yesterday" in each subscriber's own zone, which is a different date for part of the day (the receipt said
    one date while the content described another)."""
    from predbot.bot.format import recap_message
    from predbot.jobs import stats as stats_mod
    from zoneinfo import ZoneInfo
    day = datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=1, hours=12)
    return recap_message(stats_mod.compute(db, day.replace(tzinfo=ZoneInfo(resolve_timezone("", cfg.timezone)))))


def _deliver_recap_after_fixtures(db: Database, cfg, tg: Telegram, kind: str, date_str: str,
                                   text: str, label: str) -> int:
    """Send the recap of ``date_str`` to each subscriber once that day's last fixture has ended.

    The end is one instant for everybody; a subscriber's zone does not change it. The card is computed once per
    run, the receipt is written as each send is accepted, and a shutdown request stops the run between chats."""
    if not tg.token or not text:
        return 0
    last_fixture_end = _get_last_fixture_end_time(db, date_str)
    if not last_fixture_end:
        log.warning("recap: no fixtures found for %s", date_str)
        return 0
    now = utcnow()
    if now < last_fixture_end:
        return 0
    card: Optional[str] = None
    sent = 0
    for row in db.subscribers_active():
        chat_id = str(row["chat_id"])
        if db.delivery_done(kind, date_str, chat_id):
            continue
        if shutting_down():
            log.info("recap: shutdown requested - stopping; the rest is sent by the next run")
            break
        if card is None:
            card = recap_text_for(db, cfg, date_str)
        if tg.send(card, chat_id=chat_id):
            db.mark_delivery(kind, date_str, chat_id)
            sent += 1
    if sent:
        log.info("broadcast: %s delivered to %d subscriber(s) after last fixture ended", label, sent)
    return sent
