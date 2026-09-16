"""Daily picks pipeline."""
from __future__ import annotations

import json
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
from predbot.util.timeutil import iso, to_local, utcnow, day_bounds, parse_iso

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
    state_key = "generation:" + day
    state = json.loads(db.get_kv(state_key, "{}"))
    if state.get("complete") is False:
        retry = parse_iso(state.get("retry_after", ""))
        if state.get("attempts", 0) >= 3 or (retry and now < retry):
            return PickSet(complete=False, diagnostics=state)
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
    ps.complete = all(p["status"] == "complete" for p in providers.values())
    ps.diagnostics.update({"providers": providers,
                           "events_by_sport": dict(Counter(e.sport for e in events)),
                           "markets_by_sport": dict(Counter(e.sport for e in events for m in e.markets)),
                           "scored_by_sport": dict(Counter(s.event.sport for s in scored)),
                           "complete": ps.complete, "generated_at": iso(now),
                           "attempts": state.get("attempts", 0) + 1,
                           "retry_after": iso(now + timedelta(minutes=15))})
    db.set_kv(state_key, json.dumps(ps.diagnostics))
    log.info("Prediction generation %s: %s", day, ps.diagnostics)
    return ps


def store_picks(ps: PickSet, db: Database, pick_date: str) -> None:
    for tier, items in (("top", ps.top), ("daily", ps.daily)):
        for rank, s in enumerate(items, 1):
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
        dry_run: bool = False) -> PickSet:
    now = utcnow()
    local_day = to_local(now, cfg.timezone)
    pick_date = local_day.strftime("%Y-%m-%d")
    if not force and not dry_run and db.has_picks_for_date(pick_date):
        log.info("picks already published for %s", pick_date)
        return PickSet()
    ps = build_picks(chain, db, cfg, now)
    if not ps.complete:
        log.warning("Withholding incomplete generation; bounded retry on subsequent snapshot requests. diagnostics=%s", ps.diagnostics)
        return ps
    if not ps.top and not ps.daily and not dry_run and not force:
        # Every provider failed or nothing cleared the bar - don't publish an empty
        # card; the next scheduled run can publish once providers recover.
        log.warning("no picks for %s (providers empty or down) - skipping publication", pick_date)
        return ps
    st = stats_mod.compute(db, local_day)
    text = picks_message(ps.top, ps.daily, now, cfg.timezone,
                         stats_line(st) if st["all"]["n"] else None)
    if dry_run:
        print(text)
        return ps
    store_picks(ps, db, pick_date)
    if tg and tg.enabled():
        mid = tg.send(text)
        db.mark_post("picks", pick_date, mid)
    log.info("published %d top + %d daily picks for %s", len(ps.top), len(ps.daily), pick_date)
    return ps
