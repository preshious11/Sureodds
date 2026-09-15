"""UnifiedFetcher: the normalization engine behind write-once-per-day reads.

Execution flow for :meth:`UnifiedFetcher.get_daily_snapshot`
    1. Cache hit for today's UTC key  -> return immediately. No external call.
    2. Cache miss -> acquire the per-day mutex:
         a. double-check the cache (another writer may have just filled it)
         b. rebuild from the SQLite picks table (zero external calls) whenever
            today's picks are already published - the normal case, because the
            08:00 scheduler run persists them and calls refresh_cache_from_db()
         c. otherwise run the full pipeline once (ProviderChain -> pricing
            model -> picker), persist the picks for settlement, and normalise
         d. write the snapshot with TTL = end of the UTC day
    3. If the mutex is busy, wait for the writer's cache entry instead of
       dispatching a duplicate API batch.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import date
from typing import List, Optional

from predbot.cache.manager import CacheManager, ttl_until_end_of_utc_day, utc_day
from predbot.cache.models import DailySnapshot, PickRecord, StatsSummary
from predbot.config import Config
from predbot.jobs import daily_picks, stats as stats_mod
from predbot.providers.chain import ProviderChain
from predbot.storage.db import Database
from predbot.util.timeutil import iso, to_local, utcnow

log = logging.getLogger(__name__)

LOCK_TTL_SECONDS = 30.0     # a single build must finish within this window
LOCK_WAIT_SECONDS = 60.0    # how long a caller waits for the current builder
CACHE_POLL_SECONDS = 0.5


class UnifiedFetcher:
    """Cache-first facade over the provider chain + pricing model + picker."""

    def __init__(self, chain: ProviderChain, db: Database, cfg: Config,
                 cache: CacheManager) -> None:
        self.chain = chain
        self.db = db
        self.cfg = cfg
        self.cache = cache
        self.api_batches = 0  # times the external pipeline actually ran

    # ------------------------------------------------------------------ API
    def get_daily_snapshot(self, force_refresh: bool = False) -> DailySnapshot:
        """Today's snapshot; at most one provider batch per UTC day."""
        day = utc_day()
        key = self.cache.daily_key(day)
        if not force_refresh:
            cached = self._read_cache(key)
            if cached is not None:
                return cached

        lock_key = self.cache.lock_key(day)
        token = f"{os.getpid()}-{threading.get_ident()}"
        with self.cache.lock(lock_key, token=token, ttl_seconds=LOCK_TTL_SECONDS,
                             wait_seconds=LOCK_WAIT_SECONDS) as acquired:
            if not acquired:
                # Another writer owns the build; consume its cache entry.
                waited = self._wait_for_cache(key, LOCK_WAIT_SECONDS)
                if waited is not None:
                    return waited
                return self._build_and_store(day, key)
            if not force_refresh:
                cached = self._read_cache(key)  # double-check inside the lock
                if cached is not None:
                    return cached
            return self._build_and_store(day, key)

    def refresh_cache_from_db(self) -> bool:
        """Scheduler hook after the daily publish: cache today's DB picks.

        Zero external calls. Returns True when the cache was written."""
        day = utc_day()
        snapshot = self._snapshot_from_db(day)
        if snapshot is None or not (snapshot.top_picks or snapshot.daily_picks):
            return False
        self._store(day, snapshot)
        log.info("cache: daily snapshot written (%d top, %d daily) from db",
                 len(snapshot.top_picks), len(snapshot.daily_picks))
        return True

    # ------------------------------------------------------------------ flow
    def _build_and_store(self, day: date, key: str) -> DailySnapshot:
        snapshot = self._build(day)
        self._store(day, snapshot)
        return snapshot

    def _store(self, day: date, snapshot: DailySnapshot) -> None:
        self.cache.set(self.cache.daily_key(day), snapshot.to_dict(),
                       ttl_seconds=ttl_until_end_of_utc_day())

    def _build(self, day: date) -> DailySnapshot:
        snapshot = self._snapshot_from_db(day)
        if snapshot is not None:
            return snapshot
        return self._snapshot_from_providers(day)

    def _read_cache(self, key: str) -> Optional[DailySnapshot]:
        raw = self.cache.get(key)
        if raw is None:
            return None
        try:
            return DailySnapshot.from_dict(raw)
        except Exception:  # noqa: BLE001
            log.exception("unreadable cache entry %s - rebuilding", key)
            self.cache.delete(key)
            return None

    def _wait_for_cache(self, key: str, timeout: float) -> Optional[DailySnapshot]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            snapshot = self._read_cache(key)
            if snapshot is not None:
                return snapshot
            time.sleep(CACHE_POLL_SECONDS)
        return None


    # ------------------------------------------------------- normalisation
    def _candidate_dates(self, day: date) -> List[str]:
        """UTC date first, then the local publication date when it differs
        (picks are stored under the bot's configured local date)."""
        dates = [day.isoformat()]
        local = to_local(utcnow(), self.cfg.timezone).strftime("%Y-%m-%d")
        if local not in dates:
            dates.append(local)
        return dates

    def _snapshot_from_db(self, day: date) -> Optional[DailySnapshot]:
        """Rebuild the snapshot from persisted picks. Zero external calls."""
        for date_str in self._candidate_dates(day):
            rows = self.db.picks_for_date(date_str)
            if rows:
                return self._snapshot_from_rows(rows, day, origin="db")
        return None

    def _snapshot_from_rows(self, rows: List, day: date, origin: str) -> DailySnapshot:
        picks = [self._pick_from_row(r) for r in rows]
        top = sorted((p for p in picks if p.tier == "top"), key=lambda p: p.rank)
        daily = sorted((p for p in picks if p.tier != "top"), key=lambda p: p.rank)
        return DailySnapshot(date=day.isoformat(), generated_at=iso(utcnow()),
                             origin=origin, top_picks=top, daily_picks=daily,
                             stats=self._stats_summary())

    def _snapshot_from_providers(self, day: date) -> DailySnapshot:
        """Full pipeline: ProviderChain -> model -> picker -> persist -> normalise.

        This is the once-per-day external API batch, reached only when neither
        the cache nor the picks table has data for today.
        """
        self.api_batches += 1
        local_date = to_local(utcnow(), self.cfg.timezone).strftime("%Y-%m-%d")
        pickset = daily_picks.build_picks(self.chain, self.db, self.cfg)
        if (pickset.top or pickset.daily) and not self.db.has_picks_for_date(local_date):
            # Persist so the scheduled settlement/recap keeps working.
            daily_picks.store_picks(pickset, self.db, local_date)
        if pickset.top or pickset.daily:
            return self._snapshot_from_pickset(pickset, day)
        # Empty pipeline result: still prefer any DB rows that appeared.
        snapshot = self._snapshot_from_db(day)
        if snapshot is not None:
            return snapshot
        return self._snapshot_from_pickset(pickset, day)

    def _snapshot_from_pickset(self, pickset, day: date) -> DailySnapshot:
        top = [self._pick_from_scored(s, i, "top") for i, s in enumerate(pickset.top, 1)]
        daily = [self._pick_from_scored(s, i, "daily") for i, s in enumerate(pickset.daily, 1)]
        return DailySnapshot(date=day.isoformat(), generated_at=iso(utcnow()),
                             origin="api", top_picks=top, daily_picks=daily,
                             stats=self._stats_summary())

    def _pick_from_scored(self, scored, rank: int, tier: str) -> PickRecord:
        event, line = scored.event, scored.line
        return PickRecord(
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

