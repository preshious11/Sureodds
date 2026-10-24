"""Daily picks pipeline."""
from __future__ import annotations

import logging
import sqlite3
import os
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
from predbot.selection.picker import PickSet
from predbot.model.engine import generate, ModelSettings
from predbot.model.independent import IndependentModels
from predbot.storage.identity import failure_record, resolve_events_detailed, unique_failed_fixtures
from predbot.selection.evidence import assess
from predbot.util.fixtures import fixture_orientation
from predbot.storage.db import Database
from predbot.util.fixtures import fixture_key
from predbot.util.http import request_counts, request_delta
from predbot.util.pipeline_report import pipeline_lines
from predbot.util.timing import Timings
from predbot.util.timeutil import iso, parse_iso, to_local, utcnow, day_bounds

log = logging.getLogger(__name__)


def score_events(events: List[Event], db: Database, cfg: Config, independent=None) -> List[Scored]:
    elo = Elo(db)
    cal = Calibrator(db, scope="settlement_v2")
    mode = getattr(cfg, "independent_model_mode", "shadow")
    if independent is None and mode != "off":
        independent = IndependentModels.default(db, cfg)
    scored: List[Scored] = []
    for ev in events:
        try:
            model = EventModel(ev, elo, cal, shrink=cfg.model_shrink, sharp_books=getattr(cfg, "sharp_books", None),
                               elo_min_games=getattr(cfg, "elo_min_games", 10),
                               edge_price=getattr(cfg, "edge_price", "best"),
                               independent=independent, independent_mode=mode)
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
        # No usable generation window remains (e.g. a late-night retry after the
        # day window closed). This is NOT a valid empty day: mark it PARTIAL so a
        # previously cached valid snapshot is preserved instead of replaced.
        ps = PickSet(complete=False)
        ps.diagnostics["empty_reason"] = "no_remaining_window"
        return ps
    timings = Timings()
    providers_map = chain.providers if isinstance(chain, ProviderChain) else None
    usage_before = _api_usage(chain)
    http_before = request_counts()
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
        events = [e for e in events if not any(fixture_orientation(e, p) is not None for p in prior)]
        if before - len(events):
            log.info("dropped %d events already picked on a previous day", before - len(events))

    api_usage = _api_usage(chain, usage_before)
    http_requests = request_delta(http_before)
    log.info("API usage this cycle: %s", {n: f"+{u['cycle']} ({u['today']}/{u['limit'] or '?'} today)"
                                          for n, u in api_usage.items()})
    log.info("collected %d events (%d markets)", len(events), sum(len(e.markets) for e in events))
    merged_pairs = [{"league": e.league, "sport": e.sport, "kept": f"{e.provider}: {m['kept_home']} v {m['kept_away']}",
                     "merged": f"{m['provider']}: {m['home']} v {m['away']}", "swapped": m["swapped"]}
                    for e in events for m in e.extra.get("merged", [])]
    if merged_pairs:
        log.info("merged %d events across providers", len(merged_pairs))
    events, own_failed = resolve_events_detailed(events, db, cfg.supported_competitions)   # no-op for resolved events
    providers = getattr(chain, 'last_report', {})
    # Counts are per UNIQUE canonical fixture: the same fixture failing through two providers is one failure,
    # and one that another provider resolved is not a failure. Per-provider attempt counts stay separate.
    records = [failure_record(e, r) for e, r in own_failed]
    provider_failures = {}
    for name, report in providers.items():
        records += report.get('identity_failed_fixtures', [])
        if report.get('identity_failures'):
            provider_failures[name] = dict(report['identity_failures'])
    unique_failed = unique_failed_fixtures(records, events)
    identity_failures = dict(Counter(unique_failed.values()))
    discovered = len(events) + len(unique_failed)

    gate = make_results_gate(cfg, providers_map) if providers_map else None
    independent = IndependentModels.default(db, cfg, now) if cfg.independent_model_mode != "off" else None
    # Real recent-form/head-to-head for Pro Picks' reason line (see
    # ESPNProvider.team_insight) - reuses the SAME espn provider instance
    # (and its scoreboard cache) settlement already uses; skipped entirely
    # when ESPN results aren't configured. Never required: engine.py already
    # treats a None/failing fetcher as "no insight for this fixture".
    espn_provider = (providers_map or {}).get('espn')
    # canonical_competition_id (set by storage/identity.py from the SAME
    # sport_key/league_id/league resolution competition_for_event() does)
    # is tried first inside team_insight() - plain league_id/league alone
    # miss for any event whose surviving provider is Odds API, which never
    # populates Event.league_id at all (predbot/providers/oddsapi.py).
    insight_fetcher = (lambda event: espn_provider.team_insight(
        event.sport, event.home, event.away, event.start, event.league_id, event.league,
        event.canonical_competition_id)) if espn_provider else None
    with timings.stage('analyze'):
        generation = generate(events, db, ModelSettings(cfg.model_shrink, tuple(cfg.sharp_books),
                                                       cfg.elo_min_games, cfg.edge_price), gate,
                              scorer=lambda batch: score_events(batch, db, cfg, independent),
                              insight_fetcher=insight_fetcher)
    tables = {}
    for s in generation.ticket_predictions:
        evidence = assess(s, cfg, tables)
        s.quality, s.se, s.p_lcb, s.rank_score = evidence.quality, evidence.se, evidence.p_lcb, evidence.rank
        s.notes['evidence'] = evidence.as_notes()
        s.notes['independent'] = s.independent
    ps = PickSet(predictions=generation.predictions, pool_size=discovered, eligible=len(events),
                 diagnostics=generation.diagnostics, candidates=generation.candidates,
                 candidate_timezone=cfg.timezone, ticket_predictions=generation.ticket_predictions)
    unsupported = identity_failures.get("unsupported_competition", 0)
    ps.diagnostics.update(fixtures_discovered=discovered, supported_fixtures=discovered - unsupported,
                          normalized=len(events),
                          normalization_failures=sum(identity_failures.values()) - unsupported,
                          normalization_rejections={k: v for k, v in identity_failures.items()
                                                    if k != "unsupported_competition"},
                          unsupported_competition=unsupported,
                          provider_identity_failures=provider_failures)
    # Completeness = every REQUIRED provider fetched cleanly. Optional
    # providers (SGO, Odds API by default) may be quota-gated or failed: their
    # valid predictions still count, and the degraded coverage is recorded in
    # diagnostics instead of forcing PARTIAL. A required name outside this
    # run's provider set (custom order, test doubles) cannot report, so it is
    # skipped but logged - never silently. Unknown required names inside the
    # set can never be satisfied, so a typo fails closed to PARTIAL.
    run_order = getattr(chain, "order", [])
    considered = set(run_order if isinstance(run_order, (list, tuple)) else []) | set(providers)
    required = [p for p in dict.fromkeys(getattr(cfg, "required_providers", ["sharpapi"]) or [])
                if p in considered]
    skipped_required = [p for p in dict.fromkeys(getattr(cfg, "required_providers", ["sharpapi"]) or [])
                        if p not in considered]
    statuses = {n: (r.get("status") if isinstance(r, dict) else None) for n, r in providers.items()}
    # Custom provider chains have no named backbone. Preserve their all-sources
    # completeness contract rather than letting an empty required set pass failures.
    if not required and providers:
        required = list(providers)
    missing_required = [n for n in required if statuses.get(n) != "complete"]
    optional_degraded = {n: s for n, s in statuses.items()
                         if n not in required and s != "complete"}
    if skipped_required:
        log.info("completeness: required provider(s) not in this run's set, skipped: %s",
                 skipped_required)
    if missing_required:
        log.warning("incomplete generation: required provider(s) not complete: %s",
                    {n: statuses.get(n) for n in missing_required})
    if optional_degraded:
        log.info("degraded generation: optional provider(s) not complete: %s", optional_degraded)
    ps.complete = bool(providers) and not missing_required
    ps.diagnostics["provider_coverage"] = {
        "required": required,
        "missing_required": missing_required,
        "optional_degraded": optional_degraded,
        "complete": ps.complete,
    }
    last = getattr(chain, 'last_timings', None)
    for name, t in (last.items() if isinstance(last, dict) else ()):
        if isinstance(t, dict):
            timings.add('collect:' + name, t['wall'], t['cpu'])
    ps.diagnostics['timings'] = timings.to_dict()
    ps.diagnostics.update({"merged_events": len(merged_pairs),
                           "merged_pairs_by_league": dict(Counter(p["league"] for p in merged_pairs)),
                           "merged_pairs": merged_pairs})
    ps.diagnostics.update({"providers": providers, "api_usage": api_usage, "http_requests": http_requests,
                           "pipeline_version": 5,
                           "runtime_revision": os.environ.get("RAILWAY_GIT_COMMIT_SHA") or os.environ.get("SOURCE_VERSION") or "unknown",

                           "events_by_sport": dict(Counter(e.sport for e in events)),
                           "markets_by_sport": dict(Counter(e.sport for e in events for m in e.markets)),
                           "scored_by_sport": dict(Counter(s.event.sport for s in generation.predictions)),
                           "complete": ps.complete, "generated_at": iso(now),
                           "retry_after": iso(now + timedelta(seconds=cfg.cache_retry_seconds)),
                           "retry_until": cfg.delivery_time})
    skipped = {n: [x for x in getattr(p, "skipped_no_results", None) or [] if isinstance(x, str)]
               for n, p in (providers_map or {}).items()
               if isinstance(getattr(p, "skipped_no_results", None), list)}
    ps.diagnostics["leagues_skipped_no_results"] = {n: v for n, v in skipped.items() if v}
    # WHY: collections are selected from stored rows; preview the same selectors
    # in scratch storage before allowing a degraded provider slate to publish.
    coverage = ps.diagnostics["provider_coverage"]
    complete_providers = [n for n, status in statuses.items() if status == "complete"]
    coverage["complete_providers"] = complete_providers
    sharp = providers.get("sharpapi", {})
    if sharp:
        coverage["sharpapi"] = {"status": sharp.get("status"),
                                "sport_status": sharp.get("sport_status") or {},
                                "fetch": sharp.get("fetch") or {}}
    if (missing_required and cfg.provider_fallback_enabled and complete_providers
            and len(ps.predictions) >= cfg.fallback_min_predictions):
        from predbot.selection.picker import PredictionPool
        pool = PredictionPool.scratch(day)
        try:
            pool.store(ps)
            selected = pool.package(cfg, iso(now))
        finally:
            pool.close()
        apply_provider_fallback(ps, cfg, selected["top_selected"], selected["daily_selected"])
    log.info("Prediction generation %s: %s", day, ps.diagnostics)
    log.info("Generation funnel %s | %s", day, " | ".join(l for l in pipeline_lines(ps.diagnostics) if l))
    return ps


def apply_provider_fallback(ps, cfg, top_count, daily_count):
    """Only a usable card from a complete backup source may bypass the veto."""
    coverage = ps.diagnostics["provider_coverage"]
    if (coverage["missing_required"] and cfg.provider_fallback_enabled
            and coverage.get("complete_providers")
            and len(ps.predictions) >= cfg.fallback_min_predictions
            and (top_count or daily_count)):
        statuses = ps.diagnostics["providers"]
        coverage.update(fallback_used=True, fallback_reason={
            name: statuses.get(name, {}).get("status") for name in coverage["missing_required"]},
            complete=True)
        ps.complete = True
        ps.diagnostics["complete"] = True
        log.warning("Publishing with backup odds providers: missing=%s complete=%s",
                    coverage["fallback_reason"], coverage["complete_providers"])


def _price_detail(ml):
    """Who supplied the published price and what every book quoted, for later price/EV-gap analysis.
    Informational only: nothing reads these keys to choose or rank a pick. ``price_source`` is the
    MarketLine's own ``bookmaker`` ("median" when the price is a median across several books)."""
    quotes = {str(b): round(float(p), 4) for b, p in (ml.book_quotes or {}).items() if p and float(p) > 1.0}
    return {'price_source': ml.bookmaker or None, 'book_quotes': dict(sorted(quotes.items()))}


def prediction_row(s, pick_date, rank=0):
    """Canonical immutable forecast row, independent of its display collection."""
    ml, e = s.line, s.event
    row = {
        'pick_date': pick_date, 'tier': 'all', 'rank': rank,
        'provider': e.provider, 'provider_event_id': e.provider_event_id,
        'sport': e.sport, 'league': e.league, 'home': e.home, 'away': e.away,
        'start_utc': iso(e.start), 'market': ml.market, 'period': ml.period, 'stat': ml.stat,
        'entity': ml.entity, 'line': ml.line, 'selection': ml.selection, 'label': s.label,
        'price': ml.price, 'prob': s.p_model, 'prob_raw': s.p_raw, 'ev': s.ev,
        'p_market': s.p_market, 'p_fair': s.p_fair, 'edge_ev': s.edge_ev,
        'rank_score': s.rank_score, 'books': ml.distinct_books(), 'quality': s.quality,
        'sources': {**s.sources, '_alt_ids': e.extra.get('alt_ids', {}), '_providers': e.sources,
                    '_sport_key': e.extra.get('sport_key', ''), '_league_id': e.league_id,
                    '_provider_market_id': ml.provider_market_id,
                    '_notes': {**s.notes, 'edge': s.edge, **_price_detail(ml)}},
        'provider_odd_id': ml.provider_odd_id, 'fixture_key': fixture_key(e),
        'canonical_event_id': e.canonical_event_id, 'canonical_competition_id': e.canonical_competition_id,
        'home_id': e.home_id, 'away_id': e.away_id,
    }
    return row


def store_picks(ps: PickSet, db: Database, pick_date: str) -> None:
    """Store every All Prediction (one per fixture x market family) before any collection selector runs.

    First-write-wins: refresh and settlement never replace a recorded forecast.
    Explicit safe reset is the sole path that can remove unpublished future rows.
    """
    with db.transaction():
        for rank, s in enumerate(ps.predictions, 1):
            row = prediction_row(s, pick_date, rank)
            db.store_prediction(row)
        for s in ps.ticket_predictions:
            db.archive_ticket_candidate(prediction_row(s, pick_date))
        if ps.candidate_timezone:
            counts = db.store_custom_pick_candidates(
                pick_date, ps.candidate_timezone, ps.candidates,
                generated_at=ps.diagnostics['generated_at'], complete=ps.complete,
                diagnostics=ps.diagnostics)
            log.info('Custom Picks candidate archive | date=%s | candidates=%s | fixtures=%s | captured=%s',
                     pick_date, counts['candidates'], counts['fixtures'], len(ps.candidates))
    ps.diagnostics['stored_all_predictions'] = len(db.collection_predictions('all', pick_date))


def run(chain: ProviderChain, db: Database, cfg: Config, tg: Optional[Telegram], force: bool = False,
        dry_run: bool = False, service=None):
    from predbot.cache.fetcher import UnifiedFetcher
    from predbot.bot.broadcast import picks_messages, picks_text
    from predbot.bot import callbacks, pagination
    service = service or UnifiedFetcher(chain, db, cfg)
    # dry_run must be genuinely read-only: it used to skip only the Telegram send
    # while still writing the snapshot and picks that /daily serves.
    snapshot = service.get_daily_snapshot(force_refresh=force, persist=not dry_run, scheduled=True)
    evaluated = pagination.predictions_evaluated(snapshot)
    if not snapshot.total_picks() and not evaluated:
        return snapshot
    messages = picks_messages(snapshot, cfg.timezone, include_empty=evaluated)
    if not messages:
        return snapshot
    if dry_run:
        print(picks_text(snapshot, cfg.timezone))
        return snapshot
    if tg and tg.enabled():
        # the community gets the same two static cards, each recorded on its own: a retry after a
        # partial failure posts only the missing one, and the Daily is never posted before the Top.
        # ``force`` only refreshes the snapshot (regeneration/recovery); it never reposts a card that is
        # already recorded in ``posts`` - regeneration and reposting are separate concepts and there is
        # deliberately no repost path here.
        # Shared across services/DB handles for this file. Receipt check, send and
        # receipt commit are serialized, without holding the generation lock.
        with db.publication_lock:
            destination = str(tg.chat_id)
            for kind, text, _markup in messages:
                community_done = db.delivery_done(f"community_{kind}", snapshot.date, destination)
                legacy_done = db.post_done(kind, snapshot.date)
                log.info('Community publication check | day=%s | kind=%s | destination=%s | '
                         'community_receipt=%s | legacy_receipt=%s | db=%s',
                         snapshot.date, kind, destination, community_done, legacy_done, os.path.abspath(db.path))
                if community_done or legacy_done:
                    continue
                markup = None
                if kind == 'picks_top' and text == pagination.empty_view_message(snapshot, 'top', cfg.timezone):
                    username = tg.get_bot_username()
                    if not username:
                        log.warning('Community empty Top publication deferred: bot username unavailable')
                        break  # never publish a dead-end empty card; leave it retryable
                    markup = callbacks.top_empty_markup(username)
                mid = tg.send(text, reply_markup=markup) if markup else tg.send(text)
                if not mid:
                    log.warning('Community publication failed | day=%s | kind=%s; remains retryable', snapshot.date, kind)
                    break
                with db.transaction():
                    db.mark_post(kind, snapshot.date, mid)
                    db.mark_delivery(f"community_{kind}", snapshot.date, destination)
                log.info('Community publication recorded | day=%s | kind=%s | destination=%s | message_id=%s',
                         snapshot.date, kind, destination, mid)
    return snapshot
