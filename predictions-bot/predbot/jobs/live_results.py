"""Track only stored selections; free live status, durable final settlement/delivery."""
from __future__ import annotations

import json
import logging
import time
from collections import defaultdict
from datetime import timedelta

from predbot.bot.broadcast import deliver_community_live, deliver_live, deliver_match_event, deliver_owner_live
from predbot.jobs import settle
from predbot.markets.competitions import lookup
from predbot.markets.settlement import PENDING, VOID, grade
from predbot.model.calibration import Calibrator
from predbot.model.elo import Elo
from predbot.providers.base import MatchResult
from predbot.util.duration import DURATION_H, LIVE_GRACE_H, live_cutoff
from predbot.util.names import same_team
from predbot.util.timeutil import iso, parse_iso, utcnow

log = logging.getLogger(__name__)
TERMINAL = {"final", "postponed", "cancelled"}
LIVE_SOURCES = ("espn", "football_data", "euroleague")


def _field(row, key) -> str:
    try:
        return str(row[key] or "")
    except (KeyError, IndexError, TypeError):
        return ""


def same_orientation(reference, row):
    """Does ``row`` list home and away the same way as ``reference`` (another leg / record of the same fixture)?
    True / False, or None when it cannot be proven. Canonical participant ids decide first; names only when ids are
    missing, and a name must match one side WITHOUT matching the other."""
    for key in ("sport", "canonical_competition_id"):
        left, right = _field(reference, key), _field(row, key)
        if left and right and left != right:
            return None
    rh, ra, h, a = (_field(reference, "home_id"), _field(reference, "away_id"),
                    _field(row, "home_id"), _field(row, "away_id"))
    if rh and ra and h and a:
        if (rh, ra) == (h, a):
            return True
        if (rh, ra) == (a, h):
            return False
        return None  # known conflicting identities cannot be repaired by fuzzy names
    sport = _field(row, "sport")
    ref_home, ref_away, home, away = (_field(reference, "home"), _field(reference, "away"),
                                      _field(row, "home"), _field(row, "away"))
    for ours, theirs, flip in ((ref_home, home, away), (ref_away, away, home)):
        same, crossed = same_team(ours, theirs, sport=sport), same_team(ours, flip, sport=sport)
        if same != crossed:
            return same
    return None


def orient_result(result, reference, row):
    """Fixture keys ignore home/away order; scores must follow each stored leg. When the orientation cannot be
    proven the result is returned as UNKNOWN (the leg stays unresolved) instead of being graded on a guess."""
    same = same_orientation(reference, row)
    if same is None:
        log.warning("result orientation could not be proven for a leg; leaving it unresolved")
        return MatchResult("unknown", source=result.source)
    if same:
        return result
    return MatchResult(result.status, {k: (v[1], v[0]) for k, v in result.scores.items()},
                       {k: (v[1], v[0]) for k, v in result.stats.items()},
                       dict(result.odd_scores), result.source, result.overtime)


def _restore(state):
    data = json.loads(state["result_json"])
    return MatchResult(data["status"], {k: tuple(v) for k, v in data["scores"].items()},
                       {(stat, period): tuple(value) for stat, period, value in data["stats"]},
                       data["odd_scores"], data["source"], data.get("overtime"))


def _context(rows):
    alt_ids, hints = {}, {}
    for row in rows:
        alt, hint = settle._resolve_args(row)
        alt_ids.update(alt)
        alt_ids.setdefault(row["provider"], row["provider_event_id"])
        for key, value in hint.items():
            if value:
                hints.setdefault(key, value)
    if not hints.get("league_id"):
        hints["league_id"] = hints.get("sport_key", "")
    return alt_ids, hints


def _ask(resolver, name, row, alt_ids, hints, db, clock, *, live=True):
    return resolver._ask(name, row["provider"], row["provider_event_id"], alt_ids,
                         row["sport"], row["home"], row["away"], parse_iso(row["start_utc"]), hints,
                         lookup_allowed=lambda: settle.lookup_allowed(row, db, clock(), live=live,
                                                                      confirmed_final=not live))


def _live_result(resolver, rows, cache, db, clock):
    row = rows[0]
    alt_ids, hints = _context(rows)
    comp = lookup(hints.get("league_id", ""), hints.get("league", ""))
    if comp is None:
        comp = lookup(hints.get("sport_key", ""))
    if comp is None or comp.sport != row["sport"]:
        return MatchResult("unknown")
    hints["league_id"] = comp.sharp or comp.oddsapi or comp.sgo
    for name in LIVE_SOURCES:
        if not getattr(comp, name) or name not in resolver.providers:
            continue
        key = (row["fixture_key"], name)
        if key not in cache:
            cache[key] = (row, _ask(resolver, name, row, alt_ids, hints, db, clock))
        reference, result = cache[key]
        if result is None or result.status == "unknown":
            continue
        # played=false means scheduled OR live. It cannot prove a start.
        if name == "euroleague" and result.status != "final":
            continue
        return orient_result(result, reference, row)
    return MatchResult("unknown")


def _settle_fixture(resolver, db, rows, result, now, cache, begun, clock=None):
    """Try richer existing result paths once, only for ungradable confirmed finals."""
    reference = rows[0]
    clock = clock or (lambda: now)
    alt_ids, hints = _context(rows)
    prepared = []
    for row in rows:
        if row["status"] != PENDING:
            continue
        candidate = orient_result(result, reference, row)
        if result.status == "final" and grade(settle._row_to_line(row), candidate, row["sport"]) == PENDING:
            for name in resolver.paths_for(row["provider"], row["sport"], alt_ids, hints):
                if name == result.source:
                    continue
                key = (row["fixture_key"], name)
                if key not in cache:
                    if not settle.lookup_allowed(row, db, clock(), confirmed_final=True):
                        continue
                    provider = resolver.providers.get(name)
                    if provider is not None and name not in begun:
                        begin = getattr(provider, "begin_run", None)
                        if callable(begin):
                            begin()
                        begun.add(name)
                    # Use this pending leg's eligibility even if the reference leg is already settled.
                    cache[key] = (row, _ask(resolver, name, row, alt_ids, hints, db, clock, live=False))
                source_row, richer = cache[key]
                if richer is None or richer.status != "final":
                    continue
                richer = orient_result(richer, source_row, row)
                # A stat-only response must retain the already-confirmed score
                # (including the full score used for Elo learning).
                candidate = MatchResult("final", {**candidate.scores, **richer.scores},
                                        {**candidate.stats, **richer.stats},
                                        {**candidate.odd_scores, **richer.odd_scores}, richer.source,
                                        richer.overtime if richer.overtime is not None else candidate.overtime)
                if grade(settle._row_to_line(row), candidate, row["sport"]) != PENDING:
                    break
        prepared.append((row, candidate))
    counts = {"settled": 0, "pending": 0, "void": 0, "errors": 0}
    with db.transaction():
        for row, candidate in prepared:
            settle.settle_result(row, candidate, db, Elo(db), clock(), counts, expire_missing=True)
        if counts["settled"]:
            Calibrator(db, scope="settlement_v2").refit()
        db.complete_live_settlement(reference["pick_date"], reference["fixture_key"])
    log.info("live: settled %d picks", counts["settled"] + counts["void"])
    return counts


def run(resolver, db, cfg, tg, now=None, sink=None):
    """``sink``, when given, switches delivery to D1-event-sink mode (the
    Cloudflare/GitHub-Actions path, SureOdds migration 2026-10-15): status
    detection and settlement are unchanged, but instead of calling
    deliver_live/deliver_community_live/deliver_owner_live (which send
    Telegram directly — never done from GitHub Actions), each start/end
    transition calls ``deliver_match_event`` to record the event for the
    Cloudflare Worker to deliver. See predbot/jobs/live_monitor_d1.py.
    """
    if not cfg.live_updates_enabled:
        return
    # IntervalJob is threaded and can overlap a slow previous poll. Skip that tick;
    # also keep fallback settlement from resetting shared provider caches mid-run.
    if not resolver.run_lock.acquire(blocking=False):
        return
    try:
        _run(resolver, db, cfg, tg, now or utcnow(), sink=sink)
    finally:
        resolver.run_lock.release()


def _run(resolver, db, cfg, tg, now, sink=None):
    # Subscriber pushes can be disabled independently of configured community
    # delivery. Result tracking and settlement are unchanged in either mode.
    notify = getattr(cfg, "subscriber_delivery_enabled", True)
    started = time.monotonic()
    # Honor an explicit run timestamp while advancing eligibility during slow provider requests.
    clock = lambda: now + timedelta(seconds=time.monotonic() - started)
    expired = settle.expire_pending(db, now)
    if expired["settled"]:
        Calibrator(db, scope="settlement_v2").refit()
    # The SQL range is a broad candidate bound; each fixture uses its own sport cutoff below.
    lookback = max(DURATION_H.values()) + max(LIVE_GRACE_H.values())
    # D1 path: only collections with a persisted publication receipt are tracked (selection alone is not publication).
    rows = db.live_picks(iso(now - timedelta(hours=lookback)), iso(now), require_receipt=sink is not None)
    groups = defaultdict(list)
    for row in rows:
        groups[(row["pick_date"], row["fixture_key"])].append(row)
    states = {key: db.live_state(*key) for key in groups}
    tracking = {key for (day, key), state in states.items()
                if (not state or state["status"] not in TERMINAL)
                and any(row["status"] != VOID and (start := parse_iso(row["start_utc"])) is not None
                        and start <= now < live_cutoff(start, row["sport"])
                        for row in groups[(day, key)])}
    log.info("live: tracking %d fixtures", len(tracking))
    cache, begun = {}, set()
    for (pick_date, fixture_key), picks in groups.items():
        try:
            state = states[(pick_date, fixture_key)]
            if all(row["status"] == VOID for row in picks):
                db.complete_live_settlement(pick_date, fixture_key)
                db.complete_live_notifications(pick_date, fixture_key)
                continue
            if state and state["status"] in TERMINAL:
                if state["settlement_done"] and state["notifications_done"]:
                    continue
                result = _restore(state)
            else:
                reference = next((row for row in picks if row["status"] != VOID), picks[0])
                start = parse_iso(reference["start_utc"])
                if start is None or not start <= clock() < live_cutoff(start, reference["sport"]):
                    continue
                # Do not let a VOID reference leg authorize a lookup for this fixture.
                if reference is not picks[0]:
                    original = picks
                    picks = [reference] + [row for row in picks if row is not reference]
                else:
                    original = picks
                for name in LIVE_SOURCES:
                    if name in resolver.providers and name not in begun:
                        resolver.providers[name].begin_run()
                        begun.add(name)
                result = _live_result(resolver, picks, cache, db, clock)
                result = orient_result(result, picks[0], original[0])
                picks = original
                old = state["status"] if state else None
                db.save_live_result(pick_date, fixture_key, result, iso(now))
                log.info("live: %s vs %s %s%s", picks[0]["home"], picks[0]["away"],
                         "-> " if old != result.status and result.status != "scheduled" else "", result.status)
            if result.status in TERMINAL:
                if not state or not state["settlement_done"]:
                    _settle_fixture(resolver, db, picks, result, now, cache, begun, clock)
                if result.status == "final":
                    fresh = db.picks_where("p.pick_date=? AND p.fixture_key=?", (pick_date, fixture_key))
                    if sink is not None:
                        # D1-event-sink mode: one durable event record stands
                        # in for all three legacy destinations — the Worker
                        # owns group/owner delivery and its own receipts.
                        complete = community_complete = owner_complete = deliver_match_event(
                            db, sink, pick_date, fixture_key, fresh, result, reference=picks[0], ended=True,
                            now=clock(), start_window_min=cfg.match_start_window_min)
                    else:
                        complete = True
                        if notify:
                            _, complete = deliver_live(db, tg, pick_date, fixture_key, fresh, result, ended=True)
                        _, community_complete = deliver_community_live(
                            db, tg, pick_date, fixture_key, fresh, result, ended=True)
                        _, owner_complete = deliver_owner_live(
                            db, tg, cfg, pick_date, fixture_key, fresh, result, reference=picks[0], ended=True)
                    if complete and community_complete and owner_complete:
                        # Disabled subscriber pushes are treated as handled;
                        # a failed community or owner delivery still keeps
                        # the final retryable, independently of the other.
                        db.complete_live_notifications(pick_date, fixture_key)
                else:
                    db.complete_live_notifications(pick_date, fixture_key)
            elif result.status == "in_progress":
                if sink is not None:
                    deliver_match_event(db, sink, pick_date, fixture_key, picks, result, reference=picks[0],
                                        now=clock(), start_window_min=cfg.match_start_window_min)
                else:
                    if notify:
                        deliver_live(db, tg, pick_date, fixture_key, picks, result)
                    deliver_community_live(db, tg, pick_date, fixture_key, picks, result)
                    deliver_owner_live(db, tg, cfg, pick_date, fixture_key, picks, result, reference=picks[0])
        except settle.PICK_ERRORS:
            log.exception("live: fixture processing failed; will retry")
