"""Provider collection: visit every configured enabled provider within its budget.
Events seen from more than one provider are merged so the
model sees every book's price for the same selection.

Also hosts the results resolver used for settlement: origin provider first,
then any provider that can look a result up by teams + kick-off time.
"""
from __future__ import annotations

import logging
import copy
from collections import Counter
from datetime import datetime
from typing import Dict, List, Optional

from predbot.config import Config
from predbot.providers.base import BaseProvider, Event, MarketLine, MatchResult, apply_price_mode
from predbot.providers.budget import Budget
from predbot.providers.football_data import FootballDataProvider
from predbot.providers.oddsapi import OddsApiProvider
from predbot.providers.sharpapi import SharpApiProvider
from predbot.providers.sportsgameodds import SportsGameOddsProvider
from predbot.storage.db import Database
from predbot.util.http import ProviderError
from predbot.util.names import same_team

log = logging.getLogger(__name__)


def build_providers(cfg: Config, db: Database) -> Dict[str, BaseProvider]:
    budget = Budget(db)
    out: Dict[str, BaseProvider] = {}
    if cfg.sgo_key:
        out["sportsgameodds"] = SportsGameOddsProvider(
            cfg.sgo_key, budget, sports=cfg.sgo_sports, leagues=cfg.sgo_leagues,
            event_limit=cfg.sgo_event_limit, monthly_limit=cfg.sgo_monthly_object_limit,
            allowed_stats=cfg.sgo_allowed_stats, allowed_periods=cfg.sgo_allowed_periods,
            max_player_props=cfg.max_player_props)
    if cfg.sharpapi_key:
        out["sharpapi"] = SharpApiProvider(cfg.sharpapi_key, budget, leagues=cfg.sharpapi_leagues,
                                           min_interval=cfg.sharpapi_min_interval,
                                           max_pages=cfg.sharpapi_max_pages, markets=cfg.sharpapi_markets,
                                           sports=cfg.sharpapi_sports,
                                           events_per_target=cfg.sharpapi_events_per_target,
                                           price_mode=cfg.price_mode)
    if cfg.odds_api_key:
        out["oddsapi"] = OddsApiProvider(cfg.odds_api_key, budget, min_interval=cfg.odds_api_min_interval,
                                         price_mode=cfg.price_mode,
                                         regions=cfg.odds_api_regions,
                                         max_leagues_per_sport=cfg.odds_api_max_leagues_per_sport,
                                         monthly_limit=cfg.odds_api_monthly_limit,
                                         max_credits_per_cycle=cfg.oddsapi_max_credits_per_cycle,
                                         reserve_credits=cfg.oddsapi_reserve_credits,
                                         min_keys_per_sport=cfg.oddsapi_min_keys_per_sport,
                                         league_allowlist=cfg.odds_api_league_allowlist)
    if cfg.football_data_key:
        out["football_data"] = FootballDataProvider(cfg.football_data_key)
    return out


class ProviderChain:
    def __init__(self, providers: Dict[str, BaseProvider], order: List[str], min_events: Optional[int] = None):
        self.providers = providers
        self.order = [p for p in order if p in providers]
        self.min_events = min_events
        self.last_report = {}
        self._successful = {}
        self.db = None
        self.cfg = None

    def collect(self, start: datetime, end: datetime) -> List[Event]:
        if self.db is not None:
            from predbot.cache.providers import collect_persisted
            return collect_persisted(self, start, end)
        return self._collect_uncached(start, end)

    def _collect_uncached(self, start: datetime, end: datetime) -> List[Event]:
        pool: List[Event] = []
        self.last_report = {}
        for name in self.order:
            prov = self.providers[name]
            if not prov.enabled():
                continue
            key = (name, end.isoformat())
            try:
                prov.last_fetch_complete = True
                if key in self._successful:
                    events = copy.deepcopy(self._successful[key])
                else:
                    events = prov.fetch_events(start, end)
                    if getattr(prov, 'last_fetch_complete', True):
                        self._successful[key] = copy.deepcopy(events)
            except ProviderError as e:
                self.last_report[name] = {"status": "failed", "error_type": type(e).__name__}
                log.warning("provider %s failed (%s)", name, type(e).__name__)
                continue
            except Exception as e:  # noqa: BLE001
                self.last_report[name] = {"status": "failed", "error_type": type(e).__name__}
                log.warning("provider %s crashed (%s)", name, type(e).__name__)
                continue
            counts: Dict[str, int] = {}
            events = filter_allowed([e for e in events if start <= e.start < end], self.cfg, counts)
            self.last_report[name] = {"status": "complete" if getattr(prov, 'last_fetch_complete', True) else "partial",
                                      "sport_rejected": counts.get("sport_rejected", 0),
                                      "fetch": getattr(prov, 'fetch_counts', {}),
                                      "normalization": getattr(prov, 'normalization_counts', {}),
                                      "events": dict(Counter(e.sport for e in events)),
                                      "markets": dict(Counter(e.sport for e in events for m in e.markets))}
            pool = merge_events(pool, events, getattr(self.cfg, "price_mode", "median"))
            priced = sum(1 for e in pool if _has_main_market(e))
            log.info("chain: after %s -> %d events (%d with main markets)", name, len(pool), priced)
            # A global event count cannot establish coverage of other sports.
        self._successful = {k: v for k, v in self._successful.items() if k[1] == end.isoformat()}
        log.info("provider collection: %s", self.last_report)
        return pool


def filter_allowed(events: List[Event], cfg: Optional[Config], counts: Optional[Dict[str, int]] = None) -> List[Event]:
    """Keep only events whose sport is in ``cfg.allowed_sports`` (no cfg = no gate).

    Runs before events are merged, so a banned sport can never enter the pool.
    Rejections are added to ``counts["sport_rejected"]`` when a dict is given."""
    allowed = getattr(cfg, "allowed_sports", None)
    if not allowed:
        return events
    kept = [e for e in events if e.sport in allowed]
    if counts is not None:
        counts["sport_rejected"] = counts.get("sport_rejected", 0) + len(events) - len(kept)
    return kept


def _has_main_market(e: Event) -> bool:
    return any(m.market in ("result3", "result2", "dnb", "total", "handicap") for m in e.markets)


def merge_events(pool: List[Event], new: List[Event], price_mode: str = "median") -> List[Event]:
    out = list(pool)
    for ev in new:
        match = None
        for ex in out:
            if ex.sport != ev.sport:
                continue
            if abs((ex.start - ev.start).total_seconds()) > 3 * 3600:
                continue
            if ex.league_id and ev.league_id and ex.league_id != ev.league_id:
                continue
            if ex.league and ev.league and ex.league != ev.league:
                continue
            if same_team(ex.home, ev.home) and same_team(ex.away, ev.away):
                match = ex
                break
        if match is None:
            out.append(ev)
            continue
        _merge_markets(match, ev, price_mode)
    return out


def _merge_markets(dst: Event, src: Event, price_mode: str = "median") -> None:
    dst.sources = sorted(set(dst.sources) | set(src.sources))
    for k, v in src.extra.items():
        dst.extra.setdefault(k, v)
    dst.extra.setdefault("alt_ids", {})[src.provider] = src.provider_event_id
    existing = {m.key(): m for m in dst.markets}
    for ml in src.markets:
        k = ml.key()
        if k in existing:
            m = existing[k]
            best_before = max(m.book_prices or [m.price])
            best_new = max(ml.book_prices or [ml.price])
            m.book_prices.extend(ml.book_prices or [ml.price])
            m.n_books = len(m.book_prices)
            if ml.fair_price and not m.fair_price:
                m.fair_price = ml.fair_price
            if best_new > best_before:
                # provider_odd_id must always name the provider of the BEST price
                # (the median has no single owner) - a stale id would make
                # settlement look up the wrong provider's odd_scores for this line.
                m.price, m.bookmaker, m.provider_odd_id = best_new, ml.bookmaker, ml.provider_odd_id
            else:
                m.price = best_before
            apply_price_mode(m, price_mode)
        else:
            existing[k] = ml
    dst.markets = list(existing.values())


class ResultsResolver:
    """Find a MatchResult for a stored pick using any provider we have."""

    def __init__(self, providers: Dict[str, BaseProvider]):
        self.providers = providers

    def resolve(self, provider: str, provider_event_id: str, sport: str, home: str, away: str,
                start: datetime, alt_ids: Optional[Dict[str, str]] = None,
                cache: Optional[Dict[str, MatchResult]] = None,
                hints: Optional[Dict[str, str]] = None) -> Optional[MatchResult]:
        """``hints`` carries what was stored with the pick: ``sport_key`` (Odds API),
        ``league_id`` and ``league`` (football-data competition lookup)."""
        cache = cache if cache is not None else {}
        hints = hints or {}
        ck = f"{provider}:{provider_event_id}"
        if ck in cache:
            return cache[ck]
        result: Optional[MatchResult] = None

        def unresolved() -> bool:
            return result is None or result.status == "unknown"

        # 1) origin provider by id
        prov = self.providers.get(provider)
        if prov:
            try:
                result = prov.fetch_result(provider_event_id, event_hint=hints)
            except ProviderError as e:
                log.warning("result fetch (%s %s) failed: %s", provider, provider_event_id, e)
        # 2) alternate ids recorded when the event was merged across providers
        if unresolved() and alt_ids:
            for pname, pid in alt_ids.items():
                p = self.providers.get(pname)
                if not p or pname == provider:
                    continue
                try:
                    r = p.fetch_result(pid, event_hint=hints)
                except ProviderError:
                    r = None
                if r and r.status != "unknown":
                    result = r
                    break
        # 3) football-data.org by competition + teams + kick-off (football only)
        fd = self.providers.get("football_data")
        if unresolved() and fd and sport == "football" and provider != "football_data":
            try:
                r = fd.find_result(sport, home, away, start, league_id=hints.get("league_id", ""),
                                   league=hints.get("league", ""))
            except ProviderError as e:
                log.warning("football-data lookup (%s v %s) failed: %s", home, away, e)
                r = None
            if r and r.status != "unknown":
                result = r
        # 4) SportsGameOdds by teams + time
        sgo = self.providers.get("sportsgameodds")
        if unresolved() and sgo and provider != "sportsgameodds":
            try:
                r = sgo.find_result(sport, home, away, start)
            except ProviderError as e:
                log.warning("SGO lookup (%s v %s) failed: %s", home, away, e)
                r = None
            if r and r.status != "unknown":
                result = r
        if result is not None:
            cache[ck] = result
        return result
