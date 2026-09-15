"""Turn scored market lines into the day's Top Picks and Daily Picks.

Rules (from the brief):
  * top picks are the most solid selections: high probability *and* positive
    expected value with at least two agreeing sources
  * daily picks are filled only with selections that clear the quality bar -
    if only 11 qualify, 11 are posted (never padded to 20)
  * an outcome that is a top pick is never repeated in the daily list; the same
    match may appear with a *different* outcome, but never a contradictory one
  * the bot must not lean on one outcome type: caps on home/away/over/under
    shares, favourites, one market type, player props, exotics
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from predbot.config import Config
from predbot.markets.catalog import contradicts, is_equivalent, selection_family, spec
from predbot.model.ensemble import Scored

log = logging.getLogger(__name__)


@dataclass
class PickSet:
    top: List[Scored] = field(default_factory=list)
    daily: List[Scored] = field(default_factory=list)
    pool_size: int = 0
    eligible: int = 0

    def all(self) -> List[Scored]:
        return self.top + self.daily


def _settleable(s: Scored) -> bool:
    ml = s.line
    if spec(ml.market).score_settled:
        return True
    # provider-settled props: only SportsGameOdds gives us the settled value
    return ml.market == "prop" and "sportsgameodds" in s.event.sources and bool(ml.provider_odd_id)


def _top_score(s: Scored) -> float:
    return 0.70 * s.p_model + 0.20 * min(max(s.ev, 0.0), 0.12) / 0.12 + 0.10 * s.agreement


def _daily_score(s: Scored) -> float:
    base = 0.50 * s.p_model + 0.30 * min(max(s.ev, 0.0), 0.15) / 0.15 + 0.20 * s.agreement
    base -= 0.05 * (1 - s.liquidity)
    if s.line.market == "prop":
        base -= 0.05
    return base


def _cap(frac: float, target: int) -> int:
    return max(1, int(math.ceil(frac * target)))


def select(scored: List[Scored], cfg: Config, now: datetime) -> PickSet:
    ps = PickSet(pool_size=len(scored))
    lead = now + timedelta(minutes=cfg.min_lead_minutes)
    base = [s for s in scored if s.event.start >= lead and _settleable(s) and s.price >= 1.15]
    ps.eligible = len(base)
    for s in base:
        s.rank_score = _daily_score(s)

    # ------------------------------------------------------------ top picks
    top_pool = [s for s in base
                if cfg.top_min_odds <= s.price <= cfg.top_max_odds
                and s.p_model >= cfg.top_min_prob and s.ev >= cfg.top_min_ev
                and s.agreement >= 0.5 and len(s.sources) >= 2 and s.line.market != "prop"]
    top_pool.sort(key=_top_score, reverse=True)
    top: List[Scored] = []
    for s in top_pool:
        if len(top) >= cfg.top_target:
            break
        if any(t.event.dedupe_key() == s.event.dedupe_key() for t in top):
            continue
        if top and selection_family(s.line) == selection_family(top[0].line) and len(top_pool) > 6:
            # prefer a different outcome type for the second top pick when the pool allows
            alt = next((x for x in top_pool if x is not s and selection_family(x.line) != selection_family(top[0].line)
                        and x.event.dedupe_key() != top[0].event.dedupe_key()), None)
            if alt is not None and _top_score(alt) >= _top_score(s) - 0.03:
                s = alt
        s.tier = "top"
        top.append(s)
    ps.top = top

    # ---------------------------------------------------------- daily picks
    target = cfg.daily_target
    daily_pool = [s for s in base
                  if cfg.daily_min_odds <= s.price <= cfg.daily_max_odds
                  and s.p_model >= cfg.daily_min_prob and s.ev >= cfg.daily_min_ev
                  and s.agreement >= 0.30 and s not in top]
    daily_pool.sort(key=lambda s: s.rank_score, reverse=True)

    per_event: Dict[str, int] = {}
    for t in top:
        per_event[t.event.dedupe_key()] = per_event.get(t.event.dedupe_key(), 0) + 1
    family_count: Dict[str, int] = {}
    market_count: Dict[str, int] = {}
    sport_count: Dict[str, int] = {}
    favs = props = exotics = 0
    sports_available = {s.event.sport for s in daily_pool}
    caps = {"family": _cap(0.45, target), "market": _cap(0.50, target), "favs": _cap(0.50, target),
            "exotic": _cap(0.25, target), "sport": _cap(0.75, target) if len(sports_available) > 1 else target}

    chosen: List[Scored] = []

    def conflicts(s: Scored) -> bool:
        for other in top + chosen:
            if other.event.dedupe_key() != s.event.dedupe_key():
                continue
            if is_equivalent(other.line, s.line) or contradicts(other.line, s.line):
                return True
        return False

    def try_add(s: Scored, enforce_sport_cap: bool) -> bool:
        nonlocal favs, props, exotics
        ek = s.event.dedupe_key()
        fam = selection_family(s.line)
        mk = s.line.market
        grp = spec(mk).group
        if per_event.get(ek, 0) >= cfg.max_picks_per_event:
            return False
        if conflicts(s):
            return False
        if family_count.get(fam, 0) >= caps["family"] and fam != "other":
            return False
        if market_count.get(mk, 0) >= caps["market"]:
            return False
        if s.price < 1.50 and favs >= caps["favs"]:
            return False
        if mk == "prop" and props >= cfg.max_player_props:
            return False
        if grp == "exotic" and exotics >= caps["exotic"]:
            return False
        if enforce_sport_cap and sport_count.get(s.event.sport, 0) >= caps["sport"]:
            return False
        chosen.append(s)
        per_event[ek] = per_event.get(ek, 0) + 1
        family_count[fam] = family_count.get(fam, 0) + 1
        market_count[mk] = market_count.get(mk, 0) + 1
        sport_count[s.event.sport] = sport_count.get(s.event.sport, 0) + 1
        favs += 1 if s.price < 1.50 else 0
        props += 1 if mk == "prop" else 0
        exotics += 1 if grp == "exotic" else 0
        return True

    for s in daily_pool:
        if len(chosen) >= target:
            break
        try_add(s, enforce_sport_cap=True)
    if len(chosen) < target:
        for s in daily_pool:
            if len(chosen) >= target:
                break
            if s in chosen:
                continue
            try_add(s, enforce_sport_cap=False)

    for s in chosen:
        s.tier = "daily"
    ps.daily = chosen
    log.info("picker: pool=%d eligible=%d top_pool=%d daily_pool=%d -> top=%d daily=%d",
             ps.pool_size, ps.eligible, len(top_pool), len(daily_pool), len(top), len(chosen))
    return ps
