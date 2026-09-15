"""Daily picks pipeline."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
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
from predbot.util.timeutil import iso, to_local, utcnow

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
    start = now + timedelta(minutes=cfg.min_lead_minutes)
    end = now + timedelta(hours=cfg.pick_window_hours)
    events = chain.collect(start, end)
    log.info("collected %d events (%d markets)", len(events), sum(len(e.markets) for e in events))
    scored = score_events(events, db, cfg)
    return select(scored, cfg, now)


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
