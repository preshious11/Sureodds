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
from predbot.util.duration import expected_end, live_cutoff, settlement_deadline  # noqa: E402
SETTLEMENT_BUFFER_MIN = 15  # buffer after last match to ensure all results are available


def compute_settlement_deadline(db: Database, today_local_date: str) -> Optional[datetime]:
    """Compute when to run settlement: when the latest match today will finish + buffer.

    Returns UTC datetime, or None if no picks found for today.
    """
    picks = db.picks_for_date(today_local_date)
    if not picks:
        return None

    latest_end = None
    for p in picks:
        start = parse_iso(p["start_utc"])
        if start is None:
            continue                       # an unparseable kick-off must not break the deadline
        end = expected_end(start, p["sport"])
        if latest_end is None or end > latest_end:
            latest_end = end

    if latest_end is None:
        return None

    deadline = latest_end + timedelta(minutes=SETTLEMENT_BUFFER_MIN)
    log.info("settlement schedule: %s (latest expected completion + %dmin)", iso(deadline), SETTLEMENT_BUFFER_MIN)
    return deadline


def _row_to_line(r) -> MarketLine:
    return MarketLine(market=r["market"], selection=r["selection"], price=float(r["price"]),
                      period=r["period"], stat=r["stat"], entity=r["entity"],
                      line=float(r["line"]) if r["line"] is not None else None,
                      provider_odd_id=r["provider_odd_id"] or "")


#: Errors a single pick may raise without stopping the run: provider trouble or a
#: malformed stored/returned value. Anything else is a bug and should surface.
PICK_ERRORS = (ProviderError, ValueError, KeyError, TypeError, AttributeError)


def lookup_allowed(r, db: Database, now: datetime, *, live: bool = False,
                   confirmed_final: bool = False) -> bool:
    """Recheck time/status immediately before each result-source lookup."""
    current = db.query("SELECT status FROM picks WHERE id=?", (r["id"],))
    if not current or current[0]["status"] == VOID:
        return False
    start = parse_iso(r["start_utc"])
    if start is None:
        return False
    if live:
        return start <= now < live_cutoff(start, r["sport"])
    due = start if confirmed_final else expected_end(start, r["sport"])
    return current[0]["status"] == PENDING and due <= now < settlement_deadline(start, r["sport"])


def _decode_result(payload):
    data = json.loads(payload)
    if data.get("status") not in ("final", "postponed", "cancelled"):
        return None
    return MatchResult(data["status"], {k: tuple(v) for k, v in data.get("scores", {}).items()},
                       {(stat, period): tuple(value) for stat, period, value in data.get("stats", [])},
                       data.get("odd_scores", {}), data.get("source", ""), data.get("overtime"))


def stored_result(r, db: Database) -> Optional[MatchResult]:
    """Read confirmed results from live state, settled fixture legs or canonical history.

    No provider methods are called. Scores are oriented to the requested prediction.
    Unknown/live statuses never prove an outcome or immediately void a prediction.
    """
    from predbot.jobs.live_results import orient_result

    rows = db.picks_where("p.pick_date=? AND p.fixture_key=?", (r["pick_date"], r["fixture_key"]))
    state = db.live_state(r["pick_date"], r["fixture_key"])
    candidates = [(rows[0], state["result_json"])] if rows and state and state["result_json"] else []
    candidates += [(row, row["result_json"]) for row in rows if row["result_json"]]
    usable = []
    for reference, payload in candidates:
        try:
            result = _decode_result(payload)
            if result is not None:
                result = orient_result(result, reference, r)
                if grade(_row_to_line(r), result, r["sport"]) != PENDING:
                    usable.append(result)
        except (ValueError, TypeError, KeyError, AttributeError):
            log.warning("unusable stored result for pick %s", r["id"])
    if usable:
        for i, left in enumerate(usable):
            for right in usable[i+1:]:
                score_conflict = any(left.scores[k] != right.scores[k]
                                     for k in left.scores.keys() & right.scores.keys())
                stat_conflict = any(left.stats[k] != right.stats[k]
                                    for k in left.stats.keys() & right.stats.keys())
                if left.status != right.status or score_conflict or stat_conflict:
                    log.warning("conflicting stored results for pick %s; leaving unresolved", r["id"])
                    return None
        return usable[0]
    if r["sport"] == "football" and all(_pid(r, k) for k in ("canonical_competition_id", "home_id", "away_id")):
        history = db.query(
            "SELECT * FROM match_results WHERE sport=? AND competition_id=? AND kickoff_utc=? "
            "AND ((home_id=? AND away_id=?) OR (home_id=? AND away_id=?)) LIMIT 1",
            (r["sport"], r["canonical_competition_id"], iso(parse_iso(r["start_utc"])),
             r["home_id"], r["away_id"], r["away_id"], r["home_id"]))
        if history:
            h = history[0]
            scores = {"full": (h["home_goals"], h["away_goals"])}
            if h["home_ht"] is not None and h["away_ht"] is not None:
                # the grader's period names: "1h" is the half-time score, "2h" the second half alone
                scores["1h"] = (h["home_ht"], h["away_ht"])
                scores["2h"] = (h["home_goals"] - h["home_ht"], h["away_goals"] - h["away_ht"])
            if h["home_id"] != r["home_id"]:
                scores = {key: (value[1], value[0]) for key, value in scores.items()}
            result = MatchResult("final", scores, source=h["source"])
            if grade(_row_to_line(r), result, r["sport"]) != PENDING:
                return result
    return None


def _void_expired(r, db: Database, now: datetime, counts: Dict[str, int]) -> None:
    start = parse_iso(r["start_utc"])
    if start is None or now < settlement_deadline(start, r["sport"]):
        return
    with db.transaction():
        current = db.query("SELECT status FROM picks WHERE id=?", (r["id"],))
        if not current or current[0]["status"] != PENDING:
            return
        db.settle_pick(r["id"], VOID, {"reason": "settlement deadline expired",
                                       "deadline": iso(settlement_deadline(start, r["sport"]))})
        counts["void"] += 1
        log.info("settlement: VOID unresolved pick %s | sport=%s | deadline=%s",
                 r["id"], r["sport"], iso(settlement_deadline(start, r["sport"])))


def expire_pending(db: Database, now: datetime, counts: Optional[Dict[str, int]] = None) -> Dict[str, int]:
    """Finalize expired predictions using stored evidence only, before any network work."""
    if counts is None:
        counts = {"settled": 0, "pending": 0, "void": 0, "errors": 0}
    fixtures = set()
    for r in db.pending_picks(before_utc=iso(now), published_only=True):
        start = parse_iso(r["start_utc"])
        if start is None or now < settlement_deadline(start, r["sport"]):
            continue
        try:
            result = stored_result(r, db)
            if result is not None:
                settle_result(r, result, db, Elo(db), now, counts)
            _void_expired(r, db, now, counts)
            fixtures.add((r["pick_date"], r["fixture_key"]))
        except PICK_ERRORS:
            log.exception("expired prediction finalization failed for pick %s", r["id"])
            counts["errors"] += 1
            # Bad stored evidence must not keep a valid, expired fixture retryable.
            _void_expired(r, db, now, counts)
    for day, fixture in fixtures:
        rows = db.picks_where("p.pick_date=? AND p.fixture_key=?", (day, fixture))
        if rows and all(r["status"] != PENDING for r in rows):
            db.complete_live_settlement(day, fixture)
            state = db.live_state(day, fixture)
            # A saved final can still deliver/retry its real Match Ended notification.
            if not state or state["status"] != "final" or all(r["status"] == VOID for r in rows):
                db.complete_live_notifications(day, fixture)
    return counts


def _settle_row(r, resolver: ResultsResolver, db: Database, elo: Elo, now: datetime,
                cache: Dict[str, MatchResult], counts: Dict[str, int],
                needs: Optional[Dict[str, frozenset]] = None) -> None:
    current = db.query("SELECT status FROM picks WHERE id=?", (r["id"],))
    if not current or current[0]["status"] != PENDING:
        return
    now = max(now, utcnow())
    start = parse_iso(r["start_utc"])
    if start is None:
        log.error("invalid start time for pick %s: %s", r["id"], r["start_utc"])
        counts["errors"] += 1
        return
    if expected_end(start, r["sport"]) > now:
        counts["pending"] += 1
        return
    result = stored_result(r, db)
    if result is not None:
        settle_result(r, result, db, elo, now, counts)
        return
    if now >= settlement_deadline(start, r["sport"]):
        _void_expired(r, db, now, counts)
        return
    try:
        src = json.loads(r["sources"]) if r["sources"] else {}
    except ValueError as e:
        log.warning("malformed sources JSON for pick %s: %s", r["id"], e)
        src = {}
    _, hints = _resolve_args(r, needs)
    res = resolver.resolve(r["provider"], r["provider_event_id"], r["sport"], r["home"], r["away"],
                           start, alt_ids=src.get("_alt_ids") or {}, cache=cache, hints=hints,
                           lookup_allowed=lambda: lookup_allowed(r, db, max(now, utcnow())))
    cache.setdefault(f"{r['provider']}:{r['provider_event_id']}", res)
    now = max(now, utcnow())
    if res is None:
        if now >= settlement_deadline(start, r["sport"]):
            _void_expired(r, db, now, counts)
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


def _pid(row, column: str) -> str:
    try:
        return str(row[column] or "")
    except (KeyError, IndexError):
        return ""


def result_keys(sport: str, home: str, away: str, when: datetime, home_id: str = "", away_id: str = ""):
    """The ``results_seen`` keys of one real match: the legacy name key (as each source spells the teams) and, when
    both canonical participants are known, a CANONICAL key - the same for every source, spelling and orientation.
    One day per key: a same-day rematch of the same pair (a doubleheader) is learned once, never twice."""
    day = when.strftime("%Y%m%d")
    keys = [f"{sport}|{home}|{away}|{day}"]
    if home_id and away_id and home_id != away_id:
        low, high = sorted((home_id, away_id))
        keys.append(f"elo|{sport}|{low}|{high}|{day}")
    return keys


def first_sighting(db: Database, keys, score) -> bool:
    """Atomically: True (and every key recorded) when none of ``keys`` was seen before - the one time a result may
    teach Elo. A result seen under ANY of its keys (another source, another spelling, a retry, a restart) is not
    learned again; its other keys are recorded so the next sighting is caught by them too."""
    with db.transaction():
        seen = any(db.result_seen(k) for k in keys)
        for k in keys:
            db.mark_result_seen(k, score[0], score[1])
    return not seen


def _learn(db, elo, sport, home, away, score, when, home_id, away_id, competition_id, source, source_id) -> None:
    """Teach the ratings one final result, keyed by canonical participant ids.

    A football result with a canonical competition and both participants goes through the historical-results store
    (idempotent: the same real match from any source is stored once) and Elo then learns from the store's pending
    rows - so a match that is already in the history is never counted twice. Everything else updates Elo directly."""
    if sport == "football" and competition_id and home_id and away_id and home_id != away_id:
        from predbot.history.store import HistoryStore
        store = HistoryStore(db)
        store.add_result(sport=sport, competition_id=competition_id, kickoff=when, home_id=home_id, away_id=away_id,
                         home_goals=int(score[0]), away_goals=int(score[1]), source=source, source_match_id=source_id)
        store.apply_pending_elo(sport)
        return
    elo.update(sport, home, away, score[0], score[1], home_id=home_id, away_id=away_id)


def _apply_result(r, res, db, elo, now, counts, expire_missing):
    start = parse_iso(r["start_utc"])
    outcome = grade(_row_to_line(r), res, r["sport"])
    if outcome == PENDING:
        if expire_missing and start is not None and now >= settlement_deadline(start, r["sport"]):
            _void_expired(r, db, now, counts)
            return
        else:
            counts["pending"] += 1
            return
    db.settle_pick(r["id"], outcome, {"status": res.status, "scores": {k: list(v) for k, v in res.scores.items()},
                                      "stats": [[stat, period, list(value)] for (stat, period), value in res.stats.items()],
                                      "odd_scores": res.odd_scores, "source": res.source,
                                      "overtime": res.overtime})
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
            from predbot.jobs.live_results import same_orientation
            first = canonical[0]
            same = same_orientation(first, r)
            if same is None:
                log.warning("settlement: orientation of pick %s against its fixture is unproven; Elo not updated",
                            r["id"])
                return
            if not same:
                sc = (sc[1], sc[0])
            r = first
            start = parse_iso(r["start_utc"])
        keys = result_keys(r["sport"], r["home"], r["away"], start, _pid(r, "home_id"), _pid(r, "away_id"))
        if first_sighting(db, keys, sc):
            _learn(db, elo, r["sport"], r["home"], r["away"], sc, start, _pid(r, "home_id"), _pid(r, "away_id"),
                   _pid(r, "canonical_competition_id"), source="settlement", source_id=str(r["fixture_key"]))


def _needs_by_event(rows) -> Dict[str, frozenset]:
    """(period, stat) of every pending pick, per fixture: tells the resolver which result sources can grade ALL of a
    fixture's picks, so a free source is asked first only when it can really settle them."""
    out: Dict[str, set] = {}
    for r in rows:
        out.setdefault(f"{r['provider']}:{r['provider_event_id']}", set()).add((r["period"] or "full",
                                                                                r["stat"] or "points"))
    return {k: frozenset(v) for k, v in out.items()}


def _resolve_args(r, needs: Optional[Dict[str, frozenset]] = None):
    try:
        src = json.loads(r["sources"]) if r["sources"] else {}
    except ValueError:
        src = {}
    hints = {"sport_key": src.get("_sport_key") or "", "league_id": src.get("_league_id") or "",
             "league": r["league"] or ""}
    if needs:
        n = needs.get(f"{r['provider']}:{r['provider_event_id']}")
        if n:
            hints["_needs"] = n
    return src.get("_alt_ids") or {}, hints


def _prefetch(resolver: ResultsResolver, rows, now: datetime, cache: Dict[str, MatchResult],
              needs: Optional[Dict[str, frozenset]] = None, *, db: Optional[Database] = None,
              counts: Optional[Dict[str, int]] = None) -> None:
    """Look results up for every due pick BEFORE grading, one thread per first-choice source, so football-data.org's
    6.5 s pacing overlaps ESPN's and EuroLeague's instead of adding to them. Each source is asked by one thread at a
    time and its per-run caches are keyed by (competition, day), so N picks from one competition and day still cost
    one request. Every answer - including "nothing found" - goes into ``cache``, so the serial pass below grades from
    exactly what a serial run would have fetched and never asks twice.

    ``counts`` (optional, Part 2 settlement-gap hardening, 2026-10-23): a genuinely UNEXPECTED exception here (not
    ``ProviderError`` - a real bug, not a provider/network hiccup) used to be indistinguishable from "nothing found
    yet": it was swallowed into a warning and the pick just looked ``pending``/``void`` like any other unresolved
    fixture, with no signal that something actually broke. That made a real prefetch defect invisible in the same
    counts the recap/health report already surfaces. It is now logged at ``error`` level and tallied into
    ``counts["errors"]`` when the caller passes one, without changing what gets cached or how the pick settles."""
    if not callable(getattr(resolver, "paths_for", None)):
        return                                          # a stub resolver: nothing to schedule
    groups: Dict[str, list] = {}
    for r in rows:
        start = parse_iso(r["start_utc"])
        if start is None or not expected_end(start, r["sport"]) <= now < settlement_deadline(start, r["sport"]):
            continue
        if db is not None and stored_result(r, db) is not None:
            continue
        alt_ids, hints = _resolve_args(r, needs)
        names = resolver.paths_for(r["provider"], r["sport"], alt_ids, hints)
        groups.setdefault(names[0] if names else "", []).append((r, start, alt_ids, hints))
    if len(groups) < 2:
        return                                          # nothing to overlap

    def work(items):
        for r, start, alt_ids, hints in items:
            ck = f"{r['provider']}:{r['provider_event_id']}"
            try:
                allowed = (lambda r=r: lookup_allowed(r, db, max(now, utcnow()))) if db is not None else None
                if allowed is not None and not allowed():
                    continue
                res = resolver.resolve(r["provider"], r["provider_event_id"], r["sport"], r["home"], r["away"], start,
                                       alt_ids=alt_ids, cache=cache, hints=hints, lookup_allowed=allowed)
            except PICK_ERRORS as e:
                log.warning("result prefetch failed for pick %s: %s", r["id"], e)
                res = None
            except Exception as e:  # noqa: BLE001 - one bad pick must not stop the rest (see module docstring)
                log.error("result prefetch hit an unexpected (non-provider) error for pick %s: %s",
                         r["id"], e, exc_info=True)
                if counts is not None:
                    counts["errors"] += 1
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
    from predbot.storage.identity import IdentityRegistry
    registry = IdentityRegistry(db)
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
                if db.result_seen(f"{comp.sport}|{h_name}|{a_name}|{when.strftime('%Y%m%d')}"):
                    continue
                try:
                    hid = registry.participant("espn", comp.sport, h_name, competition=comp.id)
                    aid = registry.participant("espn", comp.sport, a_name, competition=comp.id)
                except ValueError:
                    continue
                # canonical ids make the key the same as settlement's for this match, whatever each source calls it
                if first_sighting(db, result_keys(comp.sport, h_name, a_name, when, hid, aid), (hs, as_)):
                    _learn(db, elo, comp.sport, h_name, a_name, (hs, as_), when, hid, aid, comp.id,
                           source="espn", source_id=f"{comp.espn}:{c.get('id') or (h_name + '|' + a_name + '|' + when_str)}")
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
    expire_pending(db, now, counts)
    for provider in resolver.providers.values():
        begin = getattr(provider, "begin_run", None)      # per-run response caches (credits / rate limits)
        if callable(begin):
            begin()
    rows = db.pending_picks(before_utc=iso(now), published_only=True)
    needs = _needs_by_event(rows)
    _prefetch(resolver, rows, now, cache, needs, db=db, counts=counts)

    for r in rows:
        try:
            _settle_row(r, resolver, db, elo, now, cache, counts, needs)
        except PICK_ERRORS as e:
            log.exception("settle failed for pick %s: %s", r["id"], e)
            counts["errors"] += 1

    if counts["settled"]:
        try:
            Calibrator(db, scope="settlement_v2").refit()
        except (ValueError, KeyError, TypeError) as e:
            log.error("calibration refit failed (probabilities may be stale): %s", e)
    try:
        harvested = _harvest_elo_from_espn(resolver, db, elo)
        if harvested:
            log.info("settlement: harvested %d new Elo results from ESPN scoreboards", harvested)
    except Exception as e:  # noqa: BLE001
        log.warning("Elo harvest from ESPN failed (non-fatal): %s", e)
    log.info("settlement: %s", counts)
    if counts["settled"]:
        try:
            from predbot.jobs import stats as stats_mod
            log.info("accuracy: %s", stats_mod.accuracy_summary(stats_mod.accuracy_report(db)))
        except Exception as e:  # noqa: BLE001 - a report must never break settlement
            log.warning("accuracy report failed (non-fatal): %s", e)
    return counts
