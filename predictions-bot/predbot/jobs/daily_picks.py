"""Daily picks pipeline."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from collections import Counter
from typing import Dict, List, Optional

from predbot.bot.format import picks_message, stats_line
from predbot.bot.telegram import Telegram
from predbot.config import Config
from predbot.jobs import stats as stats_mod
from predbot.model.calibration import Calibrator
from predbot.model.elo import Elo
from predbot.model.ensemble import EventModel, Scored
from predbot.providers.base import Event
from predbot.providers.chain import ProviderChain
from predbot.selection.picker import PickSet, select
from predbot.storage.db import Database
from predbot.util.timeutil import iso, to_local, utcnow, day_bounds

log = logging.getLogger(__name__)


def score_events(events: List[Event], db: Database, cfg: Config) -> List[Scored]:
    elo = Elo(db)
    cal = Calibrator(db)
    scored: List[Scored] = []
    for ev in events:
        try:
            model = EventModel(ev, elo, cal, shrink=cfg.model_shrink)
            scored.extend(model.score_all())
        except Exception as e:  # noqa: BLE001
            log.exception("model failed for %s: %s", ev.name, e)
    return scored


def build_picks(chain: ProviderChain, db: Database, cfg: Config, now: Optional[datetime] = None) -> PickSet:
    now = now or utcnow()
    day = to_local(now, cfg.timezone).date().isoformat()
    start = now + timedelta(minutes=cfg.min_lead_minutes)
    _, midnight = day_bounds(now, cfg.timezone)
    end = min(midnight, now + timedelta(hours=cfg.pick_window_hours))
    if start >= end:
        return PickSet()
    events = chain.collect(start, end)
    log.info("collected %d events (%d markets)", len(events), sum(len(e.markets) for e in events))
    scored = score_events(events, db, cfg)
    ps = select(scored, cfg, now)
    providers = getattr(chain, "last_report", {})
    ps.complete = bool(providers) and all(p["status"] == "complete" for p in providers.values())
    ps.diagnostics.update({"providers": providers,
                           "events_by_sport": dict(Counter(e.sport for e in events)),
                           "markets_by_sport": dict(Counter(e.sport for e in events for m in e.markets)),
                           "scored_by_sport": dict(Counter(s.event.sport for s in scored)),
                           "complete": ps.complete, "generated_at": iso(now),
                           "retry_after": iso(now + timedelta(seconds=cfg.cache_retry_seconds))})
    log.info("Prediction generation %s: %s", day, ps.diagnostics)
    return ps


def store_picks(ps: PickSet, db: Database, pick_date: str) -> None:
    for tier, items in (("top", ps.top), ("daily", ps.daily)):
        for rank, s in enumerate(items, 1):
            if db.has_pick_selection(pick_date, tier, s):
                continue
            ml = s.line
            e = s.event
            db.insert_pick({
                "pick_date": pick_date, "tier": tier, "rank": rank,
                "provider": e.provider, "provider_event_id": e.provider_event_id,
                "sport": e.sport, "league": e.league, "home": e.home, "away": e.away,
                "start_utc": iso(e.start), "market": ml.market, "period": ml.period, "stat": ml.stat,
                "entity": ml.entity, "line": ml.line, "selection": ml.selection, "label": s.label,
                "price": ml.price, "prob": s.p_model, "ev": s.ev, "p_market": s.p_market,
                "sources": {**{k: round(v, 4) for k, v in s.sources.items()},
                            "_alt_ids": e.extra.get("alt_ids", {}), "_providers": e.sources,
                            "_notes": s.notes},
                "provider_odd_id": ml.provider_odd_id,
            })


def run(chain: ProviderChain, db: Database, cfg: Config, tg: Optional[Telegram], force: bool = False,
        dry_run: bool = False, service=None):
    from predbot.cache.fetcher import UnifiedFetcher
    from predbot.bot.broadcast import picks_text
    service = service or UnifiedFetcher(chain, db, cfg)
    snapshot = service.get_daily_snapshot(force_refresh=force)
    if not snapshot.total_picks():
        return snapshot
    text = picks_text(snapshot, cfg.timezone)
    if dry_run:
        print(text)
        return snapshot
    if tg and tg.enabled() and (force or not db.post_done('picks', snapshot.date)):
        mid = tg.send(text)
        if mid:
            db.mark_post('picks', snapshot.date, mid)
    return snapshot
