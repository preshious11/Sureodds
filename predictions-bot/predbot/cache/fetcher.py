"""Canonical application-day prediction snapshots persisted only in SQLite."""
from __future__ import annotations
import json
import logging
from datetime import timedelta
from typing import Optional

from predbot.cache.models import DailySnapshot, PickRecord, StatsSummary, SCHEMA_VERSION
from predbot.cache.providers import sport_states
from predbot.jobs import daily_picks, stats as stats_mod
from predbot.providers.chain import ProviderChain
from predbot.selection.picker import PredictionPool
from predbot.util.timeutil import iso, parse_hhmm, to_local, utcnow, day_bounds, parse_iso

log = logging.getLogger(__name__)


class UnifiedFetcher:
    def __init__(self, chain, db, cfg):
        self.chain, self.db, self.cfg = chain, db, cfg
        self.api_batches = 0
        if isinstance(chain, ProviderChain):
            chain.db, chain.cfg = db, cfg
        log.info('Prediction cache backend=sqlite | Database path=%s | Cache version=%s',
                 db.path, SCHEMA_VERSION)

    def _key(self, day):
        return f'daily_predictions:{self.cfg.timezone}:{day}'

    def _read(self, day, now):
        row = self.db.get_prediction_cache(self._key(day))
        if not row:
            return None, None
        try:
            if (row['cache_version'] != SCHEMA_VERSION or row['local_date'] != day or
                    row['timezone'] != self.cfg.timezone or parse_iso(row['expires_at']) <= now):
                return None, None
            snapshot = DailySnapshot.from_dict(json.loads(row['payload_json']))
            if snapshot.date != day or row['status'] != snapshot.status:
                return None, None
            return snapshot, row
        except (ValueError, TypeError, KeyError):
            log.warning('Cache STALE | date=%s | invalid payload', day)
            return None, None

    def get_daily_snapshot(self, force_refresh=False, allow_api_call=True, persist=True, scheduled=False):
        """Today's snapshot, generating it when missing.

        ``persist=False`` is the read-only mode for dry runs and diagnostics: the
        snapshot is generated and returned but NOTHING is written - not the
        prediction_cache row (what /top and /daily serve), not the picks table
        (what settlement, recaps and /stats read). A test tool must never change
        what subscribers are served.

        ``scheduled=True`` marks the deliberate generators (the POST_TIME job, the
        admin /run, cli). A plain user read on a cache miss BEFORE POST_TIME does not
        generate: whichever subscriber asked first used to freeze the whole day's
        slate for everyone at a random early moment (its window starts at "now").
        Such a read gets a placeholder that says when the picks are ready."""
        now = utcnow()
        day = to_local(now, self.cfg.timezone).date().isoformat()
        # All service instances for the same SQLite file share this in-process lock.
        # Scheduled/admin generation waits for it; a user command or delivery poll
        # must never queue behind a generation that can take minutes.
        lock = self.db.prediction_lock
        if not lock.acquire(blocking=bool(scheduled)):
            cached, _ = self._read(day, now)
            if cached is not None and not force_refresh:
                return cached                      # today's card is already published: serve it
            log.info('Generation in progress | date=%s | read answered without waiting', day)
            return DailySnapshot(date=day, generated_at=iso(now), origin='sqlite', status='PARTIAL',
                                 generation={'complete': False, 'reason': 'generating'},
                                 stats=self._stats_summary())
        try:
            return self._snapshot_locked(day, now, force_refresh, allow_api_call, persist, scheduled)
        finally:
            lock.release()

    def _snapshot_locked(self, day, now, force_refresh, allow_api_call, persist, scheduled):
        cached, row = self._read(day, now)
        if cached is not None and not force_refresh:
            # Once today's snapshot is cached (COMPLETE or PARTIAL), serve it
            # for every subsequent read. retry_after only throttles the error
            # path (defer_prediction_retry on refresh failure); it must NOT
            # trigger provider API calls from user views like /top, /daily,
            # button taps, delivery polling or post-restart reads.
            log.info('Cache HIT | date=%s | status=%s', day, row['status'])
            if persist:
                self._count('hit', day)
            return cached
        log.info('Cache %s | date=%s', 'PARTIAL (force refresh)' if cached else 'MISS', day)
        if not cached and not force_refresh and not scheduled \
                and self._before_post_time(now):
            ready = self._ready_label(now)
            log.info('Not generating before POST_TIME | date=%s | ready at %s', day, ready)
            return DailySnapshot(date=day, generated_at=iso(now), origin='sqlite', status='PARTIAL',
                                 generation={'complete': False, 'reason': 'before_post_time',
                                             'ready_at': ready},
                                 stats=self._stats_summary())
        # If cache miss and API calls disabled (e.g., delivery polling), return empty snapshot
        if not allow_api_call and not cached:
            log.info('Skipping API call (delivery polling) | cache_miss=%s', day)
            snapshot = DailySnapshot(date=day, generated_at=iso(now), origin='sqlite',
                                     status='PARTIAL', generation={'complete': False,
                                     'reason': 'awaiting scheduled fetch'})
            return snapshot
        try:
            self.api_batches += 1
            if isinstance(self.chain, ProviderChain):
                self.chain.force_retry = force_refresh
            try:
                pickset = daily_picks.build_picks(self.chain, self.db, self.cfg, now)
            finally:
                if isinstance(self.chain, ProviderChain):
                    self.chain.force_retry = False
            pickset.diagnostics['cycle'] = {
                'trigger': 'scheduled/admin job' if scheduled else 'user command',
                'cache': 'FORCE REFRESH' if cached else 'MISS', 'at': iso(now)}
            if persist:
                self._count('miss', day)
            snapshot = self._snapshot_from_pickset(pickset, day)
            # Keep usable previous selections if a catastrophic model/generator
            # failure yields no replacement. Successful valid empties may replace.
            kept_previous = bool(cached and cached.total_picks() and not snapshot.total_picks()
                                 and not pickset.complete)
            if kept_previous:
                snapshot.top_picks = cached.top_picks
                snapshot.daily_picks = cached.daily_picks
            # One path for production and dry runs: store the canonical predictions in a pool, run the same
            # selectors over what the pool stored, read the collections back. A dry run's pool is scratch memory.
            pool = PredictionPool.persistent(self.db, day) if persist else PredictionPool.scratch(day)
            try:
                with pool.transaction():
                    pool.store(pickset)
                    snapshot.generation.update(pool.package(self.cfg, iso(now)))
                    self._hydrate_collections(snapshot, pool)
                    if persist:
                        self._store(snapshot, now)
            finally:
                pool.close()
            return snapshot
        except Exception as exc:
            log.warning('Cache refresh failed | date=%s | error=%s', day, type(exc).__name__, exc_info=exc)
            if cached is not None:
                # Preserve the previous atomic snapshot, and throttle subsequent
                # attempts even when failure happened after provider collection.
                if persist:
                    self.db.defer_prediction_retry(self._key(day), iso(now + timedelta(seconds=self.cfg.cache_retry_seconds)))
                return cached
            # If all providers exhausted their quota, use a 24h backoff instead of
            # the short retry so a spent-out day doesn't keep re-attempting calls.
            if 'quota exhausted' in str(exc).lower() or 'exhausted' in str(exc).lower():
                backoff = 86400  # 24 hours
                log.info('Provider quota exhausted; backoff 24h for %s', day)
            else:
                backoff = self.cfg.cache_retry_seconds
            snapshot = DailySnapshot(date=day, generated_at=iso(now), origin='sqlite',
                                     status='PARTIAL', generation={'complete': False,
                                     'error_type': type(exc).__name__})
            if persist:
                self.db.defer_prediction_retry(self._key(day), iso(now + timedelta(seconds=backoff)))
                self._store(snapshot, now)
            return snapshot

    def retry_partial(self, now=None):
        """Regenerate today's PARTIAL snapshot once its ``retry_after`` has passed.

        Only while nothing has been delivered yet and DELIVERY_TIME has not arrived:
        after that the published card is what people saw and must not change under
        them. Returns the new snapshot, or None when there was nothing to retry."""
        now = now or utcnow()
        day = to_local(now, self.cfg.timezone).date().isoformat()
        cached, row = self._read(day, now)
        if cached is None or row['status'] != 'PARTIAL':
            return None
        retry_at = parse_iso(row['retry_after'] or '')
        if retry_at is None or now < retry_at:
            return None
        if self.db.deliveries_exist('picks', day):
            return None
        local = to_local(now, self.cfg.timezone)
        if (local.hour, local.minute) >= parse_hhmm(self.cfg.delivery_time, '09:30'):
            return None
        log.info('Retrying PARTIAL snapshot | date=%s | retry_after passed', day)
        return self.get_daily_snapshot(force_refresh=True, scheduled=True)

    def _count(self, what, day):
        """Per-day cache read counters (kv), so /lastrun can say hit/miss."""
        key = f'cache:{what}:{day}'
        try:
            self.db.set_kv(key, str(int(self.db.get_kv(key) or 0) + 1))
        except Exception:  # noqa: BLE001 - bookkeeping must never break a read
            log.debug('cache counter failed', exc_info=True)

    def _post_hm(self):
        h, m = self.cfg.post_time.split(':')
        return int(h), int(m)

    def _before_post_time(self, now):
        local = to_local(now, self.cfg.timezone)
        return (local.hour, local.minute) < self._post_hm()

    def _ready_label(self, now):
        h, m = self._post_hm()
        local = to_local(now, self.cfg.timezone)
        return f'{h:02d}:{m:02d} {local.tzname() or self.cfg.timezone}'

    def _store(self, snapshot, now):
        _, midnight = day_bounds(now, self.cfg.timezone)
        retry = iso(now + timedelta(seconds=self.cfg.cache_retry_seconds))
        self.db.upsert_prediction_cache(dict(
            cache_key=self._key(snapshot.date), local_date=snapshot.date, timezone=self.cfg.timezone,
            status=snapshot.status, payload_json=json.dumps(snapshot.to_dict(), sort_keys=True, allow_nan=False),
            sport_status_json=json.dumps(snapshot.sport_status, sort_keys=True, allow_nan=False),
            generated_at=snapshot.generated_at, updated_at=iso(now), expires_at=iso(midnight),
            retry_after=retry if snapshot.status == 'PARTIAL' else None, cache_version=SCHEMA_VERSION))
        cutoff = (to_local(now, self.cfg.timezone).date() - timedelta(days=self.cfg.cache_retention_days)).isoformat()
        self.db.prune_prediction_cache(cutoff)
        log.info('Cache UPDATED | date=%s | status=%s | sports=%s | top=%d | daily=%d',
                 snapshot.date, snapshot.status, snapshot.sport_status,
                 len(snapshot.top_picks), len(snapshot.daily_picks))

    def snapshot_for(self, day):
        """The picks issued for ``day`` (YYYY-MM-DD): today's cached snapshot, or an earlier day rebuilt from the
        picks table. Never generates and never calls a provider - a page turn on an old message reads history."""
        current = self.get_daily_snapshot(allow_api_call=False)
        if str(day) == current.date:
            return current
        return self._snapshot_from_rows(self.db.picks_for_date(str(day)), str(day), 'db')

    def published_snapshot(self):
        """Today's same canonical snapshot; never yesterday's latest picks table."""
        return self.get_daily_snapshot(allow_api_call=False)

    def refresh_cache_from_db(self):
        # Compatibility hook: the canonical table is already the database cache.
        return bool(self.get_daily_snapshot().total_picks())

    def _snapshot_from_pickset(self, pickset, day):
        providers = pickset.diagnostics.get('providers', {})
        return DailySnapshot(date=str(day), generated_at=iso(utcnow()), origin='sqlite',
            status='COMPLETE' if pickset.complete else 'PARTIAL',
            sport_status=sport_states(providers), generation=pickset.diagnostics,
            top_picks=[self._pick_from_scored(s, i, 'top') for i, s in enumerate(pickset.top, 1)],
            daily_picks=[self._pick_from_scored(s, i, 'daily') for i, s in enumerate(pickset.daily, 1)],
            all_predictions=[self._pick_from_scored(s, i, 'all') for i, s in enumerate(pickset.predictions, 1)],
            stats=self._stats_summary())

    def _pick_from_scored(self, scored, rank: int, tier: str) -> PickRecord:
        event, line = scored.event, scored.line
        return PickRecord(
            provider=event.provider, provider_event_id=event.provider_event_id,
            provider_odd_id=line.provider_odd_id, period=line.period, stat=line.stat, entity=line.entity,
            pick_id=None, tier=tier, rank=rank, sport=event.sport,
            country=str(event.extra.get("country") or ""), league=event.league,
            home=event.home, away=event.away, kickoff_utc=iso(event.start),
            label=scored.label, market=line.market, selection=line.selection,
            line=line.line, price=float(scored.price), prob=float(scored.p_model),
            ev=float(scored.ev),
            p_market=(float(scored.p_market) if scored.p_market is not None else None),
            bookmaker=line.bookmaker, n_books=int(line.n_books),
            sources=sorted(scored.sources.keys()), status="pending",
            pick_date=to_local(utcnow(), self.cfg.timezone).strftime("%Y-%m-%d"),
            fixture_id=str(event.canonical_event_id or ""),
        )

    def _pick_from_row(self, row) -> PickRecord:
        try:
            raw = json.loads(row["sources"]) if row["sources"] else {}
            sources = sorted(str(k) for k in raw.keys())
        except (ValueError, TypeError):
            sources = []
        return PickRecord(
            provider=str(row['provider']), provider_event_id=str(row['provider_event_id']),
            provider_odd_id=str(row['provider_odd_id'] or ''), period=str(row['period']),
            stat=str(row['stat']), entity=str(row['entity']),
            pick_id=int(row["id"]), tier=str(dict(row).get('collection', row['tier'])),
            rank=int(dict(row).get('collection_rank', row['rank'])),
            sport=str(row["sport"]), country="", league=str(row["league"] or ""),
            home=str(row["home"]), away=str(row["away"]),
            kickoff_utc=str(row["start_utc"]), label=str(row["label"]),
            market=str(row["market"]), selection=str(row["selection"]),
            line=(float(row["line"]) if row["line"] is not None else None),
            price=float(row["price"]), prob=float(row["prob"]), ev=float(row["ev"]),
            p_market=(float(row["p_market"]) if row["p_market"] is not None else None),
            bookmaker="", n_books=int(dict(row).get('books') or 1), sources=sources,
            status=str(row["status"]), pick_date=str(row["pick_date"]),
            fixture_id=str(dict(row).get('canonical_event_id') or dict(row).get('fixture_key') or ''),
        )

    def _stats_summary(self) -> StatsSummary:
        raw = stats_mod.compute(self.db, to_local(utcnow(), self.cfg.timezone))
        return StatsSummary.from_raw(raw)

    def _snapshot_from_rows(self, rows, day, origin):
        """Legacy row conversion for offline display; never a canonical cache read."""
        snapshot = DailySnapshot(date=str(day), origin=origin)
        self._hydrate_collections(snapshot)
        return snapshot

    def _hydrate_collections(self, snapshot, pool=None):
        pool = pool or PredictionPool.persistent(self.db, snapshot.date)
        for collection, attribute in (('all', 'all_predictions'), ('top', 'top_picks'), ('daily', 'daily_picks')):
            setattr(snapshot, attribute, [self._pick_from_row(r) for r in pool.rows(collection)])

