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
from predbot.util.timeutil import iso, to_local, utcnow, day_bounds, parse_iso

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

    def get_daily_snapshot(self, force_refresh=False):
        now = utcnow()
        day = to_local(now, self.cfg.timezone).date().isoformat()
        # All service instances for the same SQLite file share this in-process lock.
        with self.db.prediction_lock:
            cached, row = self._read(day, now)
            if cached is not None and not force_refresh:
                retry = parse_iso(row['retry_after'] or '')
                if row['status'] == 'COMPLETE' or (retry and now < retry):
                    log.info('Cache HIT | date=%s | status=%s', day, row['status'])
                    return cached
            log.info('Cache %s | date=%s', 'PARTIAL' if cached else 'MISS', day)
            try:
                self.api_batches += 1
                if isinstance(self.chain, ProviderChain):
                    self.chain.force_retry = force_refresh
                try:
                    pickset = daily_picks.build_picks(self.chain, self.db, self.cfg, now)
                finally:
                    if isinstance(self.chain, ProviderChain):
                        self.chain.force_retry = False
                snapshot = self._snapshot_from_pickset(pickset, day)
                # Keep usable previous selections if a catastrophic model/generator
                # failure yields no replacement. Successful valid empties may replace.
                if cached and cached.total_picks() and not snapshot.total_picks() and not pickset.complete:
                    snapshot.top_picks = cached.top_picks
                    snapshot.daily_picks = cached.daily_picks
                with self.db.transaction():
                    daily_picks.store_picks(pickset, self.db, day)
                    self._store(snapshot, now)
                return snapshot
            except Exception as exc:
                log.warning('Cache refresh failed | date=%s | error=%s', day, type(exc).__name__)
                if cached is not None:
                    # Preserve the previous atomic snapshot, and throttle subsequent
                    # attempts even when failure happened after provider collection.
                    self.db.defer_prediction_retry(self._key(day), iso(now + timedelta(seconds=self.cfg.cache_retry_seconds)))
                    return cached
                snapshot = DailySnapshot(date=day, generated_at=iso(now), origin='sqlite',
                                         status='PARTIAL', generation={'complete': False,
                                         'error_type': type(exc).__name__})
                self._store(snapshot, now)
                return snapshot

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

    def published_snapshot(self):
        """Today's same canonical snapshot; never yesterday's latest picks table."""
        return self.get_daily_snapshot()

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
            stats=self._stats_summary())

    def _pick_from_scored(self, scored, rank: int, tier: str) -> PickRecord:
        event, line = scored.event, scored.line
        return PickRecord(
            provider=event.provider, provider_event_id=event.provider_event_id,
            provider_odd_id=line.provider_odd_id, period=line.period, stat=line.stat, entity=line.entity,
            pick_id=None, tier=tier, rank=rank, sport=event.sport, league=event.league,
            home=event.home, away=event.away, kickoff_utc=iso(event.start),
            label=scored.label, market=line.market, selection=line.selection,
            line=line.line, price=float(scored.price), prob=float(scored.p_model),
            ev=float(scored.ev),
            p_market=(float(scored.p_market) if scored.p_market is not None else None),
            bookmaker=line.bookmaker, n_books=int(line.n_books),
            sources=sorted(scored.sources.keys()), status="pending",
            pick_date=to_local(utcnow(), self.cfg.timezone).strftime("%Y-%m-%d"),
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
            pick_id=int(row["id"]), tier=str(row["tier"]), rank=int(row["rank"]),
            sport=str(row["sport"]), league=str(row["league"] or ""),
            home=str(row["home"]), away=str(row["away"]),
            kickoff_utc=str(row["start_utc"]), label=str(row["label"]),
            market=str(row["market"]), selection=str(row["selection"]),
            line=(float(row["line"]) if row["line"] is not None else None),
            price=float(row["price"]), prob=float(row["prob"]), ev=float(row["ev"]),
            p_market=(float(row["p_market"]) if row["p_market"] is not None else None),
            bookmaker="", n_books=1, sources=sources,
            status=str(row["status"]), pick_date=str(row["pick_date"]),
        )

    def _stats_summary(self) -> StatsSummary:
        raw = stats_mod.compute(self.db, to_local(utcnow(), self.cfg.timezone))
        return StatsSummary.from_raw(raw)

    def _snapshot_from_rows(self, rows, day, origin):
        """Legacy row conversion for offline display; never a canonical cache read."""
        picks = [self._pick_from_row(row) for row in rows]
        return DailySnapshot(date=str(day), origin=origin,
                             top_picks=[p for p in picks if p.tier == 'top'],
                             daily_picks=[p for p in picks if p.tier != 'top'])

