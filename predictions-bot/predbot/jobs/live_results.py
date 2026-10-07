"""Track only stored selections; free live status, durable final settlement/delivery."""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import timedelta

from predbot.bot.broadcast import deliver_community_live, deliver_live
from predbot.jobs import settle
from predbot.markets.competitions import lookup
from predbot.markets.settlement import PENDING, grade
from predbot.model.calibration import Calibrator
from predbot.model.elo import Elo
from predbot.providers.base import MatchResult
from predbot.util.names import same_team
from predbot.util.timeutil import iso, parse_iso, utcnow

log = logging.getLogger(__name__)
TERMINAL = {"final", "postponed", "cancelled"}
LIVE_SOURCES = ("espn", "football_data", "euroleague")


def orient_result(result, reference, row):
    """Fixture keys ignore home/away order; scores must follow each stored leg."""
    if same_team(reference["home"], row["home"], sport=row["sport"]):
        return result
    return MatchResult(result.status, {k: (v[1], v[0]) for k, v in result.scores.items()},
                       {k: (v[1], v[0]) for k, v in result.stats.items()},
                       dict(result.odd_scores), result.source)


def _restore(state):
    data = json.loads(state["result_json"])
    return MatchResult(data["status"], {k: tuple(v) for k, v in data["scores"].items()},
                       {(stat, period): tuple(value) for stat, period, value in data["stats"]},
                       data["odd_scores"], data["source"])


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


def _ask(resolver, name, row, alt_ids, hints):
    return resolver._ask(name, row["provider"], row["provider_event_id"], alt_ids,
                         row["sport"], row["home"], row["away"], parse_iso(row["start_utc"]), hints)


def _live_result(resolver, rows, cache):
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
            cache[key] = (row, _ask(resolver, name, row, alt_ids, hints))
        reference, result = cache[key]
        if result is None or result.status == "unknown":
            continue
        # played=false means scheduled OR live. It cannot prove a start.
        if name == "euroleague" and result.status != "final":
            continue
        return orient_result(result, reference, row)
    return MatchResult("unknown")


def _settle_fixture(resolver, db, rows, result, now, cache, begun):
    """Try richer existing result paths once, only for ungradable confirmed finals."""
    reference = rows[0]
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
                    provider = resolver.providers.get(name)
                    if provider is not None and name not in begun:
                        begin = getattr(provider, "begin_run", None)
                        if callable(begin):
                            begin()
                        begun.add(name)
                    cache[key] = (reference, _ask(resolver, name, reference, alt_ids, hints))
                source_row, richer = cache[key]
                if richer is None or richer.status != "final":
                    continue
                richer = orient_result(richer, source_row, row)
                # A stat-only response must retain the already-confirmed score
                # (including the full score used for Elo learning).
                candidate = MatchResult("final", {**candidate.scores, **richer.scores},
                                        {**candidate.stats, **richer.stats},
                                        {**candidate.odd_scores, **richer.odd_scores}, richer.source)
                if grade(settle._row_to_line(row), candidate, row["sport"]) != PENDING:
                    break
        prepared.append((row, candidate))
    counts = {"settled": 0, "pending": 0, "void": 0, "errors": 0}
    with db.transaction():
        for row, candidate in prepared:
            settle.settle_result(row, candidate, db, Elo(db), now, counts)
        if counts["settled"]:
            Calibrator(db).refit()
        db.complete_live_settlement(reference["pick_date"], reference["fixture_key"])
    log.info("live: settled %d picks", counts["settled"] + counts["void"])
    return counts


def run(resolver, db, cfg, tg, now=None):
    if not cfg.live_updates_enabled:
        return
    # IntervalJob is threaded and can overlap a slow previous poll. Skip that tick;
    # also keep fallback settlement from resetting shared provider caches mid-run.
    if not resolver.run_lock.acquire(blocking=False):
        return
    try:
        _run(resolver, db, cfg, tg, now or utcnow())
    finally:
        resolver.run_lock.release()


def _run(resolver, db, cfg, tg, now):
    # Subscriber pushes can be disabled independently of configured community
    # delivery. Result tracking and settlement are unchanged in either mode.
    notify = getattr(cfg, "subscriber_delivery_enabled", True)
    rows = db.live_picks(iso(now - timedelta(days=settle.MAX_PENDING_DAYS)),
                         iso(now + timedelta(minutes=10)))
    groups = defaultdict(list)
    for row in rows:
        groups[(row["pick_date"], row["fixture_key"])].append(row)
    states = {key: db.live_state(*key) for key in groups}
    tracking = {key for (day, key), state in states.items()
                if not state or state["status"] not in TERMINAL}
    log.info("live: tracking %d fixtures", len(tracking))
    cache, begun = {}, set()
    for (pick_date, fixture_key), picks in groups.items():
        try:
            state = states[(pick_date, fixture_key)]
            if state and state["status"] in TERMINAL:
                if state["settlement_done"] and state["notifications_done"]:
                    continue
                result = _restore(state)
            else:
                for name in LIVE_SOURCES:
                    if name in resolver.providers and name not in begun:
                        resolver.providers[name].begin_run()
                        begun.add(name)
                result = _live_result(resolver, picks, cache)
                old = state["status"] if state else None
                db.save_live_result(pick_date, fixture_key, result, iso(now))
                log.info("live: %s vs %s %s%s", picks[0]["home"], picks[0]["away"],
                         "-> " if old != result.status and result.status != "scheduled" else "", result.status)
            if result.status in TERMINAL:
                if not state or not state["settlement_done"]:
                    _settle_fixture(resolver, db, picks, result, now, cache, begun)
                if result.status == "final":
                    fresh = db.picks_where("p.pick_date=? AND p.fixture_key=?", (pick_date, fixture_key))
                    complete = True
                    if notify:
                        _, complete = deliver_live(db, tg, pick_date, fixture_key, fresh, result, ended=True)
                    _, community_complete = deliver_community_live(
                        db, tg, pick_date, fixture_key, fresh, result, ended=True)
                    if complete and community_complete:
                        # Disabled subscriber pushes are treated as handled;
                        # failed community delivery still keeps the final retryable.
                        db.complete_live_notifications(pick_date, fixture_key)
                else:
                    db.complete_live_notifications(pick_date, fixture_key)
            elif result.status == "in_progress":
                if notify:
                    deliver_live(db, tg, pick_date, fixture_key, picks, result)
                deliver_community_live(db, tg, pick_date, fixture_key, picks, result)
        except settle.PICK_ERRORS:
            log.exception("live: fixture processing failed; will retry")
