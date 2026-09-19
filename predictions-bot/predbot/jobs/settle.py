"""Settle pending picks whose events should be over."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Dict, Optional

from predbot.config import Config
from predbot.markets.settlement import PENDING, VOID, grade
from predbot.model.calibration import Calibrator
from predbot.model.elo import Elo
from predbot.providers.base import MarketLine, MatchResult
from predbot.providers.chain import ResultsResolver
from predbot.storage.db import Database
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


def run(resolver: ResultsResolver, db: Database, cfg: Config) -> Dict[str, int]:
    now = utcnow()
    elo = Elo(db)
    counts = {"settled": 0, "pending": 0, "void": 0, "errors": 0}
    cache: Dict[str, MatchResult] = {}
    rows = db.pending_picks(before_utc=iso(now))

    for r in rows:
        start = parse_iso(r["start_utc"])
        if start is None:
            log.error("invalid start time for pick %s: %s", r["id"], r["start_utc"])
            counts["errors"] += 1
            continue
        if start + timedelta(hours=DURATION_H.get(r["sport"], 3)) > now:
            counts["pending"] += 1
            continue
        try:
            src = json.loads(r["sources"]) if r["sources"] else {}
        except ValueError as e:
            log.warning("malformed sources JSON for pick %s: %s", r["id"], e)
            src = {}
        alt_ids = src.get("_alt_ids") or {}
        try:
            res = resolver.resolve(r["provider"], r["provider_event_id"], r["sport"], r["home"], r["away"],
                                  start, alt_ids=alt_ids, cache=cache)
        except (ValueError, KeyError, TypeError, AttributeError) as e:
            log.exception("resolve failed for pick %s: %s", r["id"], e)
            counts["errors"] += 1
            continue
        if res is None:
            if start < now - timedelta(days=MAX_PENDING_DAYS):
                db.settle_pick(r["id"], VOID, {"reason": "no result available"})
                counts["void"] += 1
            else:
                counts["pending"] += 1
            continue
        ml = _row_to_line(r)
        outcome = grade(ml, res, r["sport"])
        if outcome == PENDING:
            if res.status == "final" and start < now - timedelta(days=MAX_PENDING_DAYS):
                outcome = VOID
            else:
                counts["pending"] += 1
                continue
        db.settle_pick(r["id"], outcome, {"status": res.status, "scores": {k: list(v) for k, v in res.scores.items()},
                                          "source": res.source})
        counts["settled" if outcome != VOID else "void"] += 1
        # learn from the final score
        sc = res.score("full")
        if res.status == "final" and sc:
            key = f"{r['sport']}|{r['home']}|{r['away']}|{start.strftime('%Y%m%d')}"
            if not db.result_seen(key):
                db.mark_result_seen(key, sc[0], sc[1])
                elo.update(r["sport"], r["home"], r["away"], sc[0], sc[1])

    if counts["settled"]:
        try:
            Calibrator(db).refit()
        except (ValueError, KeyError, TypeError) as e:
            log.error("calibration refit failed (probabilities may be stale): %s", e)
    log.info("settlement: %s", counts)
    return counts
