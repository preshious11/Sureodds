"""Provider collection: visit every configured enabled provider within its budget.
Events seen from more than one provider are merged so the
model sees every book's price for the same selection.

Also hosts the results resolver used for settlement: origin provider first,
then any provider that can look a result up by teams + kick-off time.
"""
from __future__ import annotations

import logging
import copy
import threading
from collections import Counter
from datetime import datetime
from types import SimpleNamespace
from typing import Callable, Dict, List, Optional, Tuple

from predbot.config import Config
from predbot.providers.base import BaseProvider, Event, MarketLine, MatchResult, apply_price_mode, consolidate_lines
from predbot.providers.budget import Budget
from predbot.providers.espn import ESPNProvider
from predbot.providers.euroleague import EuroLeagueProvider
from predbot.providers.football_data import FootballDataProvider
from predbot.providers.oddsapi import OddsApiProvider
from predbot.providers.sharpapi import SharpApiProvider
from predbot.providers.sportsgameodds import SportsGameOddsProvider
from predbot.storage.db import Database
from predbot.util.http import ProviderError
from predbot.util.fixtures import competition_id, fixture_orientation
from predbot.util.names import normalize

log = logging.getLogger(__name__)


def build_providers(cfg: Config, db: Database) -> Dict[str, BaseProvider]:
    budget = Budget(db)
    out: Dict[str, BaseProvider] = {}
    if cfg.sgo_key:
        out["sportsgameodds"] = SportsGameOddsProvider(
            cfg.sgo_key, budget, sports=cfg.sgo_sports, leagues=cfg.sgo_leagues,
            event_limit=cfg.sgo_event_limit, monthly_limit=cfg.sgo_monthly_object_limit,
            allowed_stats=cfg.sgo_allowed_stats, allowed_periods=cfg.sgo_allowed_periods,
            max_player_props=cfg.max_player_props, min_interval=cfg.sgo_min_interval_sec)
    if cfg.sharpapi_key:
        out["sharpapi"] = SharpApiProvider(cfg.sharpapi_key, budget, leagues=cfg.sharpapi_leagues,
                                           min_interval=cfg.sharpapi_min_interval,
                                           max_pages=cfg.sharpapi_max_pages, markets=cfg.sharpapi_markets,
                                           sports=cfg.sharpapi_sports,
                                           events_per_target=cfg.sharpapi_events_per_target,
                                           price_mode=cfg.price_mode,
                                           main_lines_only=cfg.sharpapi_main_lines_only,
                                           sport_markets={"football": cfg.sharpapi_markets_football,
                                                          "baseball": cfg.sharpapi_markets_baseball},
                                           monthly_limit=cfg.sharpapi_monthly_request_limit,
                                           max_requests_per_generation=cfg.sharpapi_max_requests_per_generation,
                                           league_priority=cfg.sharpapi_league_priority)
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
    if cfg.espn_results:
        out["espn"] = ESPNProvider()
    if cfg.euroleague_results:
        out["euroleague"] = EuroLeagueProvider()
    _skip_leagues_without_results(cfg, out)
    return out


def _skip_leagues_without_results(cfg: Config, providers: Dict[str, BaseProvider]) -> None:
    """Tell each fixture provider which configured leagues can never be settled, so it
    does not spend requests / credits fetching them (one warning per league per process)."""
    from predbot.markets.settlement import coverage_table
    warned = set()
    for origin, ident, paths in coverage_table(cfg, providers):
        prov = providers.get(origin)
        if prov is None or paths:
            continue
        skipped = getattr(prov, "skipped_no_results", None)
        if skipped is None:
            skipped = prov.skipped_no_results = []
        skipped.append(ident)
        if (origin, ident) not in warned:
            warned.add((origin, ident))
            log.warning("%s: league %s has no results path (nothing can settle it) - not fetching it", origin, ident)


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
            for e in events:
                e.markets = consolidate_lines(e.markets)
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


def _norm_league(text: str) -> str:
    return normalize(text or "").casefold()


def _same_match(ex: Event, ev: Event) -> Optional[str]:
    """``None`` = different matches, else ``"same"`` / ``"swapped"`` (``ev`` lists home/away the other way
    round). Teams, kickoff and the competition veto come from ``fixture_orientation`` (the one matcher);
    when the competition is not known for BOTH events, the league must agree case-insensitively."""
    if ex.canonical_event_id and ev.canonical_event_id:
        if ex.canonical_event_id != ev.canonical_event_id:
            return None
        return "same" if ex.home_id == ev.home_id else "swapped"
    ca, cb = competition_id(ex), competition_id(ev)
    if not (ca and cb):
        if ex.league_id and ev.league_id and ex.league_id.casefold() != ev.league_id.casefold():
            return None
        if ex.league and ev.league and _norm_league(ex.league) != _norm_league(ev.league):
            return None
    return fixture_orientation(ex, ev, ca, cb)


def _fuller(kept: str, other: str) -> str:
    """The fuller of two spellings of one team: more words wins ('Oakland Athletics' over 'Athletics'), then the
    longer ('Boston Red Sox' over 'BOS Red Sox'). Always one of the two providers' own names, never a blend; equal
    spellings keep the first."""
    return other if (len(other.split()), len(other)) > (len(kept.split()), len(kept)) else kept


def merge_events(pool: List[Event], new: List[Event], price_mode: str = "median") -> List[Event]:
    """Fold each new event into the pool event that is the same real match (any provider, either home/away
    order); a swapped provider's markets are re-expressed on the kept event's sides first. The kept event
    carries, for each side, the fuller of the two providers' team names."""
    out = list(pool)
    for ev in new:
        for ml in ev.markets:
            if not ml.providers:                 # who carried this line (a cached row may predate the tag)
                ml.providers = [ev.provider]
        match, orientation = None, None
        for ex in out:
            if ex.sport != ev.sport:
                continue
            orientation = _same_match(ex, ev)
            if orientation is not None:
                match = ex
                break
        if match is None:
            out.append(ev)
            continue
        swapped = orientation == "swapped"
        match.home = _fuller(match.home, ev.away if swapped else ev.home)
        match.away = _fuller(match.away, ev.home if swapped else ev.away)
        _merge_markets(match, ev, price_mode, swapped=swapped)
    return out


#: Markets whose ``entity`` only repeats the selected side: SportsGameOdds tags a moneyline / handicap on the home
#: team ``entity="home"`` where SharpAPI says ``"all"``, yet it is one bet - so the pooling key ignores it.
_SIDE_MARKETS = {"result2", "result3", "dnb", "dc", "handicap"}


def _pool_key(ml: MarketLine) -> Tuple:
    key = ml.key()
    if ml.market in _SIDE_MARKETS and ml.entity in ("home", "away", "all"):
        return (key[0], key[1], key[2], "all") + key[4:]
    return key


def _merge_markets(dst: Event, src: Event, price_mode: str = "median", swapped: bool = False) -> None:
    dst.sources = sorted(set(dst.sources) | set(src.sources))
    for k, v in src.extra.items():
        dst.extra.setdefault(k, v)
    dst.extra.setdefault("alt_ids", {})[src.provider] = src.provider_event_id
    dst.extra.setdefault("merged", []).append({"provider": src.provider, "home": src.home, "away": src.away,
                                               "with": dst.provider, "kept_home": dst.home, "kept_away": dst.away,
                                               "swapped": swapped})
    markets = list(dst.markets)
    existing: Dict[Tuple, MarketLine] = {}
    for m in markets:
        existing.setdefault(_pool_key(m), m)
    for ml in src.markets:
        if swapped:
            ml = ml.mirrored()
        k = _pool_key(ml)
        if k in existing:
            m = existing[k]
            best_before = max(m.book_prices or [m.price])
            best_new = max(ml.book_prices or [ml.price])
            m.book_prices.extend(ml.book_prices or [ml.price])
            m.n_books = len(m.book_prices)
            for book, quoted in ml.book_quotes.items():
                m.book_quotes.setdefault(book, quoted)
            m.providers = sorted(set(m.providers) | set(ml.providers))
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
            markets.append(ml)
    dst.markets = markets


class ResultsResolver:
    """Find a MatchResult for a stored pick using any provider we have.

    The providers to ask, and in which order, come from
    ``markets.settlement.result_paths``: the origin SportsGameOdds id, football-data.org,
    ESPN, EuroLeague, The Odds API ``/scores`` (last - it costs credits), then a
    SportsGameOdds team+time lookup. The first usable result wins, and every provider
    call is wrapped so one provider being down never stops the rest."""

    def __init__(self, providers: Dict[str, BaseProvider]):
        self.providers = providers
        # Live and fallback settlement share result-provider caches/rate limiters.
        self.run_lock = threading.RLock()
        # settlement looks results up from one thread per source (see jobs.settle): a provider is asked by one
        # thread at a time, so its per-run response caches and rate limiter behave exactly as when serial
        self._locks: Dict[str, threading.RLock] = {}

    def paths_for(self, provider: str, sport: str, alt_ids: Optional[Dict[str, str]] = None,
                  hints: Optional[Dict[str, str]] = None) -> List[str]:
        """Names of the result sources to ask for a pick, in order (the first one is asked first)."""
        from predbot.markets.settlement import result_paths
        hints, alt_ids = hints or {}, alt_ids or {}
        event = SimpleNamespace(sport=sport, league=hints.get("league", ""), league_id=hints.get("league_id", ""),
                                provider=provider, sources=[provider, *alt_ids.keys()],
                                extra={"sport_key": hints.get("sport_key", "")})
        paths = result_paths(event, None, self.providers)
        names = self._prioritise(paths, hints.get("_needs"))
        if not names:
            # A pick stored without a recognisable competition (older rows): the gate would
            # refuse it today, but one that already exists is still worth a best-effort lookup.
            names = (["football_data"] if sport == "football" else []) + ["sportsgameodds"]
        return names

    #: result sources that cost no SportsGameOdds quota
    FREE_SOURCES = ("football_data", "espn", "euroleague")

    @classmethod
    def _prioritise(cls, paths, needs) -> List[str]:
        """Order the paths. ``needs`` = the (period, stat) pairs of the pending picks on this fixture. When some paths can
        grade ALL of them, those go first with the free sources ahead of the metered ones (SportsGameOdds bills per
        object, so it is the last resort, not the first); the paths that cannot grade everything follow, so nothing that
        used to be asked is dropped. With no ``needs`` or no path that covers them all, the original order is kept
        (SportsGameOdds is then the only grader of e.g. first-half / corner / card markets and must stay reachable)."""
        if not needs:
            return [p.name for p in paths]
        covering = [p for p in paths if all(p.allows_period(per) and p.allows_stat(st) for per, st in needs)]
        if not covering:
            return [p.name for p in paths]
        rest = [p for p in paths if p not in covering]
        ordered = [p for p in covering if p.name in cls.FREE_SOURCES] \
            + [p for p in covering if p.name not in cls.FREE_SOURCES] + rest
        return [p.name for p in ordered]

    def resolve(self, provider: str, provider_event_id: str, sport: str, home: str, away: str,
                start: datetime, alt_ids: Optional[Dict[str, str]] = None,
                cache: Optional[Dict[str, MatchResult]] = None,
                hints: Optional[Dict[str, str]] = None,
                lookup_allowed: Optional[Callable[[], bool]] = None) -> Optional[MatchResult]:
        """``hints`` carries what was stored with the pick: ``sport_key`` (Odds API),
        ``league_id`` and ``league`` (competition lookup), and optionally ``_needs`` (see ``_prioritise``)."""
        cache = cache if cache is not None else {}
        hints = hints or {}
        alt_ids = alt_ids or {}
        ck = f"{provider}:{provider_event_id}"
        if ck in cache:
            return cache[ck]
        result: Optional[MatchResult] = None
        names = self.paths_for(provider, sport, alt_ids, hints)
        for i, name in enumerate(names):
            if name == "sportsgameodds" and any(n in self.FREE_SOURCES for n in names[:i]):
                # a free source was asked first and can still deliver: SGO is then only a stand-in, and the first thing
                # the quota policy switches off as usage climbs
                hints = {**hints, "_free_alt": True}
            if lookup_allowed is not None and not lookup_allowed():
                break
            r = self._ask(name, provider, provider_event_id, alt_ids, sport, home, away, start, hints,
                          lookup_allowed=lookup_allowed)
            if r is not None and r.status != "unknown":
                result = r
                break
            if r is not None and result is None:
                result = r                              # remember an "unknown" only if nothing better turns up
        if result is not None:
            cache[ck] = result
        return result

    def _ask(self, name: str, provider: str, provider_event_id: str, alt_ids: Dict[str, str], sport: str,
             home: str, away: str, start: datetime, hints: Dict[str, str], *,
             lookup_allowed: Optional[Callable[[], bool]] = None) -> Optional[MatchResult]:
        prov = self.providers.get(name)
        if prov is None:
            return None
        with self._locks.setdefault(name, threading.RLock()):
            if lookup_allowed is not None and not lookup_allowed():
                return None
            return self._ask_locked(prov, name, provider, provider_event_id, alt_ids, sport, home, away, start, hints,
                                    lookup_allowed=lookup_allowed)

    def _ask_locked(self, prov, name: str, provider: str, provider_event_id: str, alt_ids: Dict[str, str], sport: str,
                    home: str, away: str, start: datetime, hints: Dict[str, str], *,
                    lookup_allowed: Optional[Callable[[], bool]] = None) -> Optional[MatchResult]:
        try:
            if name in ("football_data", "espn", "euroleague"):
                return prov.find_result(sport, home, away, start, league_id=hints.get("league_id", ""),
                                        league=hints.get("league", ""))
            if name == "sportsgameodds":
                if provider == "sportsgameodds":
                    return prov.fetch_result(provider_event_id, event_hint=hints)
                r = None
                if alt_ids.get("sportsgameodds"):
                    r = prov.fetch_result(alt_ids["sportsgameodds"], event_hint=hints)
                if r is not None and r.status != "unknown":
                    return r
                if lookup_allowed is not None and not lookup_allowed():
                    return None
                return prov.find_result(sport, home, away, start, event_hint=hints)
            # The Odds API /scores: by the id of whichever record is an Odds API one
            event_id = provider_event_id if provider == name else alt_ids.get(name)
            return prov.fetch_result(event_id, event_hint=hints) if event_id else None
        except ProviderError as e:
            log.warning("result lookup via %s (%s v %s) failed: %s", name, home, away, e)
            return None
