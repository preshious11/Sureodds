"""Settle pending picks whose events should be over."""
from __future__ import annotations

import json
import logging
from contextlib import nullcontext
from datetime import datetime, timedelta
from typing import Dict, Optional

from predbot.config import Config
from predbot.markets.settlement import PENDING, VOID, grade
from predbot.model.calibration import Calibrator
from predbot.model.elo import Elo
from predbot.providers.base import MarketLine, MatchResult
from predbot.providers.chain import ResultsResolver
from predbot.storage.db import Database
from predbot.util.http import ProviderError
from predbot.util.timeutil import iso, parse_iso, utcnow

log = logging.getLogger(__name__)

# typical duration before we bother asking for a result (shared with the cards)
from predbot.util.duration import DURATION_H  # noqa: E402
MAX_PENDING_DAYS = 4
SETTLEMENT_BUFFER_MIN = 15  # buffer after last match to ensure all results are available


def compute_settlement_deadline(db: Database, today_local_date: str) -> Optional[datetime]:
    """Compute when to run settlement: when the latest match today will finish + buffer.

    Returns UTC datetime, or None if no picks found for today.
    """
    picks = db.picks_for_date(today_local_date)
    if not picks:
        return None

    latest_start = None
    latest_sport = "other"
    for p in picks:
        start = parse_iso(p["start_utc"])
        if start is None:
            continue                       # an unparseable kick-off must not break the deadline
        if latest_start is None or start > latest_start:
            latest_start = start
            latest_sport = p["sport"]

    if latest_start is None:
        return None

    duration_h = DURATION_H.get(latest_sport, 3.0)
    deadline = latest_start + timedelta(hours=duration_h, minutes=SETTLEMENT_BUFFER_MIN)
    log.info("settlement deadline: %s (%s + %.1fh for %s)", iso(deadline), iso(latest_start), duration_h, latest_sport)
    return deadline


def _row_to_line(r) -> MarketLine:
    return MarketLine(market=r["market"], selection=r["selection"], price=float(r["price"]),
                      period=r["period"], stat=r["stat"], entity=r["entity"],
                      line=float(r["line"]) if r["line"] is not None else None,
                      provider_odd_id=r["provider_odd_id"] or "")


#: Errors a single pick may raise without stopping the run: provider trouble or a
#: malformed stored/returned value. Anything else is a bug and should surface.
PICK_ERRORS = (ProviderError, ValueError, KeyError, TypeError, AttributeError)


def _settle_row(r, resolver: ResultsResolver, db: Database, elo: Elo, now: datetime,
                cache: Dict[str, MatchResult], counts: Dict[str, int]) -> None:
    start = parse_iso(r["start_utc"])
    if start is None:
        log.error("invalid start time for pick %s: %s", r["id"], r["start_utc"])
        counts["errors"] += 1
        return
    if start + timedelta(hours=DURATION_H.get(r["sport"], 3)) > now:
        counts["pending"] += 1
        return
    try:
        src = json.loads(r["sources"]) if r["sources"] else {}
    except ValueError as e:
        log.warning("malformed sources JSON for pick %s: %s", r["id"], e)
        src = {}
    hints = {"sport_key": src.get("_sport_key") or "", "league_id": src.get("_league_id") or "",
             "league": r["league"] or ""}
    res = resolver.resolve(r["provider"], r["provider_event_id"], r["sport"], r["home"], r["away"],
                           start, alt_ids=src.get("_alt_ids") or {}, cache=cache, hints=hints)
    if res is None:
        if start < now - timedelta(days=MAX_PENDING_DAYS):
            db.settle_pick(r["id"], VOID, {"reason": "no result available"})
            counts["void"] += 1
        else:
            counts["pending"] += 1
        return
    settle_result(r, res, db, elo, now, counts, expire_missing=True)


def settle_result(r, res: MatchResult, db: Database, elo: Elo, now: datetime,
                  counts: Dict[str, int], *, expire_missing: bool = False) -> None:
    """Grade a known result without a duration gate; learning and grading commit together.

    Recheck the stored status under SQLite's write lock: concurrent live/fallback
    runs and stale row snapshots must never grade or learn the same pick twice.
    """
    with db.transaction():
        current = db.query("SELECT status FROM picks WHERE id=?", (r["id"],))
        if not current or current[0]["status"] != PENDING:
            return
        _apply_result(r, res, db, elo, now, counts, expire_missing)


def _apply_result(r, res, db, elo, now, counts, expire_missing):
    start = parse_iso(r["start_utc"])
    outcome = grade(_row_to_line(r), res, r["sport"])
    if outcome == PENDING:
        if expire_missing and res.status == "final" and start < now - timedelta(days=MAX_PENDING_DAYS):
            outcome = VOID
        else:
            counts["pending"] += 1
            return
    db.settle_pick(r["id"], outcome, {"status": res.status, "scores": {k: list(v) for k, v in res.scores.items()},
                                      "source": res.source})
    counts["settled" if outcome != VOID else "void"] += 1
    # learn from the final score
    sc = res.score("full")
    if res.status == "final" and sc:
        # The fixture key unifies providers, including reversed home/away names.
        # Always learn in the first stored leg's orientation so another market
        # on that fixture cannot create a second results_seen/Elo update.
        canonical = db.query("SELECT * FROM picks WHERE fixture_key=? ORDER BY id LIMIT 1",
                             (r["fixture_key"],))
        if canonical:
            from predbot.util.names import same_team
            first = canonical[0]
            if not same_team(r["home"], first["home"], sport=r["sport"]):
                sc = (sc[1], sc[0])
            r = first
            start = parse_iso(r["start_utc"])
        key = f"{r['sport']}|{r['home']}|{r['away']}|{start.strftime('%Y%m%d')}"
        if not db.result_seen(key):
            db.mark_result_seen(key, sc[0], sc[1])
            elo.update(r["sport"], r["home"], r["away"], sc[0], sc[1])


def _resolve_args(r):
    try:
        src = json.loads(r["sources"]) if r["sources"] else {}
    except ValueError:
        src = {}
    hints = {"sport_key": src.get("_sport_key") or "", "league_id": src.get("_league_id") or "",
             "league": r["league"] or ""}
    return src.get("_alt_ids") or {}, hints


def _prefetch(resolver: ResultsResolver, rows, now: datetime, cache: Dict[str, MatchResult]) -> None:
    """Look results up for every due pick BEFORE grading, one thread per first-choice source, so football-data.org's
    6.5 s pacing overlaps ESPN's and EuroLeague's instead of adding to them. Each source is asked by one thread at a
    time and its per-run caches are keyed by (competition, day), so N picks from one competition and day still cost
    one request. Every answer - including "nothing found" - goes into ``cache``, so the serial pass below grades from
    exactly what a serial run would have fetched and never asks twice."""
    if not callable(getattr(resolver, "paths_for", None)):
        return                                          # a stub resolver: nothing to schedule
    groups: Dict[str, list] = {}
    for r in rows:
        start = parse_iso(r["start_utc"])
        if start is None or start + timedelta(hours=DURATION_H.get(r["sport"], 3)) > now:
            continue
        alt_ids, hints = _resolve_args(r)
        names = resolver.paths_for(r["provider"], r["sport"], alt_ids, hints)
        groups.setdefault(names[0] if names else "", []).append((r, start, alt_ids, hints))
    if len(groups) < 2:
        return                                          # nothing to overlap

    def work(items):
        for r, start, alt_ids, hints in items:
            ck = f"{r['provider']}:{r['provider_event_id']}"
            try:
                res = resolver.resolve(r["provider"], r["provider_event_id"], r["sport"], r["home"], r["away"], start,
                                       alt_ids=alt_ids, cache=cache, hints=hints)
            except PICK_ERRORS as e:
                log.warning("result prefetch failed for pick %s: %s", r["id"], e)
                res = None
            cache.setdefault(ck, res)

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=len(groups), thread_name_prefix="settle") as pool:
        list(pool.map(work, groups.values()))


def _harvest_elo_from_espn(resolver: "ResultsResolver", db: Database, elo: Elo) -> int:
    """Feed every final ESPN scoreboard result into Elo, so ratings accumulate
    from the full day's slate rather than only from the bot's own picks.
    Zero extra API calls: ESPN caches boards in memory for the run.
    Returns the number of new results learned."""
    from predbot.markets.competitions import COMPETITIONS
    from predbot.util.timeutil import utcnow

    espn = resolver.providers.get("espn")
    if espn is None or not hasattr(espn, "_days") or not hasattr(espn, "_candidates"):
        return 0

    learned = 0
    for comp in COMPETITIONS:
        if not comp.espn or not comp.sport:
            continue
        for (espn_slug, day_str, _groups), events in list(espn._days.items()):
            if espn_slug != comp.espn:
                continue
            for c in espn._candidates(comp, events):
                # Only process finals
                from predbot.providers.espn import FINAL as ESPN_FINAL
                name = str(((c.get("status") or {}).get("type") or {}).get("name", "")).upper()
                if name not in ESPN_FINAL:
                    continue
                sides = c.get("competitors") or []
                from predbot.providers.espn import _by_side, _num
                pair = _by_side(sides)
                if pair is None:
                    continue
                h, a = pair
                hs = _num(h.get("score"))
                as_ = _num(a.get("score"))
                if hs is None or as_ is None:
                    continue
                # Use the same names ESPN would match on (first displayName)
                from predbot.providers.espn import _names
                h_name = next(iter(_names(h)), None)
                a_name = next(iter(_names(a)), None)
                if not h_name or not a_name:
                    continue
                when_str = str(c.get("date") or "")
                from predbot.util.timeutil import parse_iso
                when = parse_iso(when_str)
                if when is None:
                    continue
                key = f"{comp.sport}|{h_name}|{a_name}|{when.strftime('%Y%m%d')}"
                if not db.result_seen(key):
                    db.mark_result_seen(key, hs, as_)
                    elo.update(comp.sport, h_name, a_name, hs, as_)
                    learned += 1
    return learned


def run(resolver: ResultsResolver, db: Database, cfg: Config) -> Dict[str, int]:
    with getattr(resolver, "run_lock", nullcontext()):
        return _run(resolver, db, cfg)


def _run(resolver: ResultsResolver, db: Database, cfg: Config) -> Dict[str, int]:
    """Settle every pending pick whose event should be over. Never raises for a
    single pick: a provider or data error is counted in ``errors`` and the run
    moves on, so one bad row cannot stop the rest (or crash the caller)."""
    now = utcnow()
    elo = Elo(db)
    counts = {"settled": 0, "pending": 0, "void": 0, "errors": 0}
    cache: Dict[str, MatchResult] = {}
    for provider in resolver.providers.values():
        begin = getattr(provider, "begin_run", None)      # per-run response caches (credits / rate limits)
        if callable(begin):
            begin()
    rows = db.pending_picks(before_utc=iso(now))
    _prefetch(resolver, rows, now, cache)

    for r in rows:
        try:
            _settle_row(r, resolver, db, elo, now, cache, counts)
        except PICK_ERRORS as e:
            log.exception("settle failed for pick %s: %s", r["id"], e)
            counts["errors"] += 1

    if counts["settled"]:
        try:
            Calibrator(db).refit()
        except (ValueError, KeyError, TypeError) as e:
            log.error("calibration refit failed (probabilities may be stale): %s", e)
    try:
        harvested = _harvest_elo_from_espn(resolver, db, elo)
        if harvested:
            log.info("settlement: harvested %d new Elo results from ESPN scoreboards", harvested)
    except Exception as e:  # noqa: BLE001
        log.warning("Elo harvest from ESPN failed (non-fatal): %s", e)
    log.info("settlement: %s", counts)
    return counts
