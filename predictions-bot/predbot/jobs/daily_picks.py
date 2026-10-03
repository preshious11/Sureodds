"""Daily picks pipeline."""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta
from collections import Counter
from types import SimpleNamespace
from typing import Dict, List, Optional

from predbot.bot.telegram import Telegram
from predbot.config import Config
from predbot.jobs import stats as stats_mod
from predbot.model.calibration import Calibrator
from predbot.model.elo import Elo
from predbot.model.ensemble import EventModel, Scored
from predbot.providers.base import Event
from predbot.providers.budget import Budget
from predbot.markets.settlement import make_results_gate
from predbot.providers.chain import ProviderChain, merge_events
from predbot.selection.picker import PickSet, same_fixture, select
from predbot.storage.db import Database
from predbot.util.fixtures import fixture_key
from predbot.util.timing import Timings
from predbot.util.timeutil import iso, parse_iso, to_local, utcnow, day_bounds

log = logging.getLogger(__name__)


def score_events(events: List[Event], db: Database, cfg: Config) -> List[Scored]:
    elo = Elo(db)
    cal = Calibrator(db)
    scored: List[Scored] = []
    for ev in events:
        try:
            model = EventModel(ev, elo, cal, shrink=cfg.model_shrink, sharp_books=getattr(cfg, "sharp_books", None),
                               elo_min_games=getattr(cfg, "elo_min_games", 10),
                               edge_price=getattr(cfg, "edge_price", "best"))
            scored.extend(model.score_all())
        except Exception as e:  # noqa: BLE001
            log.exception("model failed for %s: %s", ev.name, e)
    return scored


def _api_usage(chain, before=None):
    """Per-provider API calls used today. Called before collection for a
    baseline, then again with that baseline to get {provider: {cycle, today,
    limit}} - the calls THIS generation cycle spent, measured from the budget
    table rather than guessed."""
    out = {}
    providers = getattr(chain, "providers", None)
    if not isinstance(providers, dict):
        return out
    for name, prov in providers.items():
        budget = getattr(prov, "budget", None)
        if not isinstance(budget, Budget):
            continue
        today = budget.used_today(name)
        if before is None:
            out[name] = today
        else:
            out[name] = {"cycle": today - before.get(name, today), "today": today,
                         "limit": getattr(prov, "daily_limit", None)}
    return out


def build_picks(chain: ProviderChain, db: Database, cfg: Config, now: Optional[datetime] = None) -> PickSet:
    now = now or utcnow()
    day = to_local(now, cfg.timezone).date().isoformat()
    start = now + timedelta(minutes=cfg.min_lead_minutes)
    _, midnight = day_bounds(now, cfg.timezone)
    midnight_end = midnight + timedelta(hours=max(0, cfg.day_window_end_offset_hours))
    end = min(midnight_end, now + timedelta(hours=cfg.pick_window_hours))
    if start >= end:
        return PickSet()
    timings = Timings()
    usage_before = _api_usage(chain)
    with timings.stage('collect'):
        events = chain.collect(start, end)

    # Dedupe against PREVIOUS days' stored picks: the window runs to local
    # midnight + DAY_WINDOW_END_OFFSET_HOURS, so a fixture picked late
    # yesterday would otherwise re-enter today's generation. Uses the same
    # fuzzy fixture matcher as the Top search (sport + kickoff within 3h +
    # same_team on both sides), so a match named differently by another
    # provider is still recognised - and a genuine rematch at another time is
    # not. Same-day regeneration is not deduped here (store_picks already
    # refuses duplicate selections per day).
    prior = []
    for r in db.previous_pick_fixtures(day):
        st = parse_iso(r["start_utc"])
        if st:
            prior.append(SimpleNamespace(sport=r["sport"], home=r["home"], away=r["away"], start=st))
    if prior:
        before = len(events)
        events = [e for e in events if not any(same_fixture(e, p) for p in prior)]
        if before - len(events):
            log.info("dropped %d events already picked on a previous day", before - len(events))

    # Top ticket priority source: The Odds API is tried first for the Top
    # picks combo (kept out of provider_order so it never joins the Daily
    # ticket's three-provider pool on its own). Its events are merged into the
    # shared pool purely so a match it shares with the other three still
    # dedupes/enriches normally; picker.select() decides who gets to build Top.
    top_priority_source: Optional[str] = None
    top_priority_report: Dict = {}
    providers_map = chain.providers if isinstance(chain, ProviderChain) else None
    # Driven entirely by cfg.top_provider_priority - not hardcoded to any one
    # provider name, so setting TOP_PROVIDER_PRIORITY to something other than
    # "oddsapi" actually takes effect instead of silently doing nothing.
    priority_available = providers_map and any(
        (p := providers_map.get(name)) is not None and p.enabled() for name in cfg.top_provider_priority)
    # A priority provider that is already a full member of PROVIDER_ORDER was
    # collected by the main chain above: do not spend its credits twice.
    in_main_chain = next((n for n in cfg.top_provider_priority if n in chain.order
                          and (p := providers_map.get(n)) is not None and p.enabled()), None) if providers_map else None
    if priority_available and in_main_chain:
        top_priority_source = in_main_chain
        top_priority_report = {in_main_chain: getattr(chain, "last_report", {}).get(in_main_chain, {})}
    elif priority_available:
        top_chain = ProviderChain(providers_map, cfg.top_provider_priority)
        top_chain.db, top_chain.cfg = chain.db, chain.cfg
        priority_events = top_chain.collect(start, end)
        top_priority_report = getattr(top_chain, "last_report", {})
        if priority_events:
            events = merge_events(events, priority_events, cfg.price_mode)
            top_priority_source = top_chain.order[0] if top_chain.order else None

    api_usage = _api_usage(chain, usage_before)
    log.info("API usage this cycle: %s", {n: f"+{u['cycle']} ({u['today']}/{u['limit'] or '?'} today)"
                                          for n, u in api_usage.items()})
    log.info("collected %d events (%d markets)", len(events), sum(len(e.markets) for e in events))
    merged_pairs = [{"league": e.league, "sport": e.sport, "kept": f"{e.provider}: {m['kept_home']} v {m['kept_away']}",
                     "merged": f"{m['provider']}: {m['home']} v {m['away']}", "swapped": m["swapped"]}
                    for e in events for m in e.extra.get("merged", [])]
    if merged_pairs:
        log.info("merged %d events across providers", len(merged_pairs))
    with timings.stage('score'):
        scored = score_events(events, db, cfg)
    gate = make_results_gate(cfg, providers_map) if providers_map else None
    with timings.stage('select'):
        ps = select(scored, cfg, now, top_priority_source=top_priority_source, results_gate=gate)
    timings.stages.update({k: v for k, v in (ps.diagnostics.pop('select_timings', None) or {}).items()})
    # Completeness is judged on the three-provider Daily chain only, unchanged
    # from before the Odds API existed; the Top-priority source has its own
    # fallback (picker.select) so its own outages never flip the snapshot to PARTIAL.
    providers = getattr(chain, "last_report", {})
    ps.complete = bool(providers) and all(p["status"] == "complete" for p in providers.values())
    last = getattr(chain, 'last_timings', None)
    for name, t in (last.items() if isinstance(last, dict) else ()):
        if isinstance(t, dict):
            timings.add('collect:' + name, t['wall'], t['cpu'])
    ps.diagnostics['timings'] = timings.to_dict()
    ps.diagnostics.update({"merged_events": len(merged_pairs),
                           "merged_pairs_by_league": dict(Counter(p["league"] for p in merged_pairs)),
                           "merged_pairs": merged_pairs})
    ps.diagnostics.update({"providers": providers, "api_usage": api_usage,
                           "top_priority_provider_report": top_priority_report,
                           "events_by_sport": dict(Counter(e.sport for e in events)),
                           "markets_by_sport": dict(Counter(e.sport for e in events for m in e.markets)),
                           "scored_by_sport": dict(Counter(s.event.sport for s in scored)),
                           "complete": ps.complete, "generated_at": iso(now),
                           "retry_after": iso(now + timedelta(seconds=cfg.cache_retry_seconds)),
                           "retry_until": cfg.delivery_time})
    skipped = {n: [x for x in getattr(p, "skipped_no_results", None) or [] if isinstance(x, str)]
               for n, p in (providers_map or {}).items()
               if isinstance(getattr(p, "skipped_no_results", None), list)}
    ps.diagnostics["leagues_skipped_no_results"] = {n: v for n, v in skipped.items() if v}
    log.info("Prediction generation %s: %s", day, ps.diagnostics)
    return ps


def _selection_identity(tier: str, s) -> tuple:
    e, m = s.event, s.line
    return (tier, e.provider, e.provider_event_id, m.market, m.period, m.stat, m.entity, m.line, m.selection)


def store_picks(ps: PickSet, db: Database, pick_date: str, replace_pending: bool = True) -> None:
    """Persist a pickset. Regenerating a day that nobody has been sent yet REPLACES its
    pending picks (those not in the new set are deleted, in the caller's transaction),
    so a retry cannot leave stale legs that settlement and stats would count. Once a
    card has been delivered the stored rows are what people saw: they are never touched."""
    if replace_pending and not db.deliveries_exist("picks", pick_date):
        keep = [_selection_identity(tier, s) for tier, items in (("top", ps.top), ("daily", ps.daily)) for s in items]
        removed = db.delete_pending_picks_not_in(pick_date, keep)
        if removed:
            log.info("regeneration replaced %d stale pending pick(s) for %s", removed, pick_date)
    for tier, items in (("top", ps.top), ("daily", ps.daily)):
        for rank, s in enumerate(items, 1):
            if db.has_pick_selection(pick_date, tier, s):
                continue
            ml = s.line
            e = s.event
            key = fixture_key(e)
            if db.has_fixture_selection(pick_date, key, s):
                log.warning("not storing %s pick '%s' (%s v %s): the same selection on this match is already stored "
                            "for %s in another tier/provider view", tier, s.label, e.home, e.away, pick_date)
                continue
            row = {
                "pick_date": pick_date, "tier": tier, "rank": rank,
                "provider": e.provider, "provider_event_id": e.provider_event_id,
                "sport": e.sport, "league": e.league, "home": e.home, "away": e.away,
                "start_utc": iso(e.start), "market": ml.market, "period": ml.period, "stat": ml.stat,
                "entity": ml.entity, "line": ml.line, "selection": ml.selection, "label": s.label,
                "price": ml.price, "prob": s.p_model, "prob_raw": s.p_raw, "ev": s.ev, "p_market": s.p_market,
                # evidence at selection time. ``prob`` is the calibrated probability; ``rank_score`` is the (separate)
                # ranking value and is never a probability. p_fair / edge_ev stay NULL without a sharp reference.
                "p_fair": s.p_fair, "edge_ev": s.edge_ev, "rank_score": s.rank_score if s.p_lcb is not None else None,
                "books": ml.distinct_books(), "quality": s.quality or None,
                "sources": {**{k: round(v, 4) for k, v in s.sources.items()},
                            "_alt_ids": e.extra.get("alt_ids", {}), "_providers": e.sources,
                            "_sport_key": e.extra.get("sport_key", ""), "_league_id": e.league_id,
                            "_notes": s.notes},
                "provider_odd_id": ml.provider_odd_id, "fixture_key": key,
            }
            try:
                db.insert_pick(row)
            except sqlite3.IntegrityError:
                log.warning("not storing %s pick '%s' (%s v %s): unique (date, fixture, selection) constraint", tier,
                            s.label, e.home, e.away)


def run(chain: ProviderChain, db: Database, cfg: Config, tg: Optional[Telegram], force: bool = False,
        dry_run: bool = False, service=None):
    from predbot.cache.fetcher import UnifiedFetcher
    from predbot.bot.broadcast import picks_messages, picks_text
    service = service or UnifiedFetcher(chain, db, cfg)
    # dry_run must be genuinely read-only: it used to skip only the Telegram send
    # while still writing the snapshot and picks that /daily serves.
    snapshot = service.get_daily_snapshot(force_refresh=force, persist=not dry_run, scheduled=True)
    if not snapshot.total_picks():
        return snapshot
    messages = picks_messages(snapshot, cfg.timezone)
    if not messages:
        return snapshot
    if dry_run:
        print(picks_text(snapshot, cfg.timezone))
        return snapshot
    if tg and tg.enabled():
        # the channel gets the same two messages as a subscriber, each recorded on its own: a retry after a
        # partial failure posts only the missing one, and the Daily is never posted before the Top
        for kind, text, markup in messages:
            if not force and db.post_done(kind, snapshot.date):
                continue
            mid = tg.send(text, reply_markup=markup)
            if not mid:
                break
            db.mark_post(kind, snapshot.date, mid)
    return snapshot
