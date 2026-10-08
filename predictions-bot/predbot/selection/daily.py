"""Daily portfolio policy over stored All Predictions; no generation dependencies.

Only legs that passed every Daily gate are ever used - nothing here lowers a quality bar or adds a weaker leg. Among
them:

1. best-first build (offer order: main/secondary markets before restricted ones, each by evidence ranking), one leg
   per fixture, never above ``DAILY_COMBINED_MAX_ODDS``, inside the diversity quotas, stopping as soon as the
   combined odds reach ``DAILY_COMBINED_MIN_ODDS``;
2. if that ends below the target, a bounded end-game search: keep the best-ranked legs and replace at most the last
   ``REPAIR_DROP`` of them with 1..``REPAIR_ADD`` legs from the ``REPAIR_POOL`` next-best unused fixtures, so the
   ticket lands inside [MIN, MAX] (the plain build could stop at 9.92 when every remaining leg overshot 12). The
   replacement keeps as many of the best-ranked legs as possible, then prefers the best-ranked additions, then fewer
   legs, then the odds closest to the target;
3. otherwise the plain build is posted as it is - a slate that only holds 6.4 of qualifying odds posts 6.4.

Diversity is never relaxed: the market, stat, sport and league quotas bind the build AND the end-game search alike.
(Older documentation promised a relaxation of the market / sport / league shares that was never implemented; the
code and its tests have always held the quotas, and that is the contract.)
"""
import math
from collections import Counter
from itertools import combinations

from predbot.selection.evidence import OFFER_ORDER
from predbot.selection.top import promotion_reason, ranked

REPAIR_DROP, REPAIR_ADD, REPAIR_POOL = 3, 3, 20


def _event(row):
    return row.get('canonical_event_id') or row['fixture_key']


def _dimensions(row, cfg, cap):
    dims = ((('market', row['market']), math.ceil(cap * cfg.daily_max_market_share)),
            (('stat', row['stat']), math.ceil(cap * cfg.daily_max_stat_share)),
            (('sport', row['sport']), math.ceil(cap * cfg.daily_max_sport_share)),
            (('league', row.get('canonical_competition_id') or row['league']), cfg.daily_max_per_league))
    return [d for d in dims if d[0] != ('stat', 'points')]


def _fits(rows, cfg, cap):
    """Whether ``rows`` together respect one-leg-per-fixture and every diversity quota."""
    if len({_event(r) for r in rows}) != len(rows):
        return False
    counts = Counter()
    for row in rows:
        for key, limit in _dimensions(row, cfg, cap):
            counts[key] += 1
            if counts[key] > max(1, limit):
                return False
    return True


def _build(ordered, cfg, cap):
    selected, odds, counts, events, rejected = [], 1.0, Counter(), set(), Counter()
    for row in ordered:
        if len(selected) >= cap or selected and odds >= cfg.daily_odds_min:
            break
        eid = _event(row)
        if eid in events:
            rejected['duplicate_event'] += 1
            continue
        if odds * row['price'] > cfg.daily_odds_max:
            rejected['combined_odds_ceiling'] += 1
            continue
        dimensions = _dimensions(row, cfg, cap)
        if any(counts[key] >= max(1, limit) for key, limit in dimensions):
            rejected['diversity_limit'] += 1
            continue
        selected.append(row)
        events.add(eid)
        odds *= row['price']
        for key, _ in dimensions:
            counts[key] += 1
    return selected, odds, rejected


def _repair(selected, ordered, cfg, cap):
    """Bounded end-game search (see the module docstring). Returns a ticket inside [MIN, MAX] or None."""
    position = {id(r): i for i, r in enumerate(ordered)}
    for drop in range(0, min(REPAIR_DROP, len(selected)) + 1):
        prefix = selected[:len(selected) - drop]
        base = math.prod(r['price'] for r in prefix)
        used = {_event(r) for r in prefix}
        pool = []
        for row in ordered:
            if _event(row) not in used and all(_event(row) != _event(p) for p in pool):
                pool.append(row)
            if len(pool) == REPAIR_POOL:
                break
        best = None
        for n in range(1, min(REPAIR_ADD, cap - len(prefix)) + 1):
            for combo in combinations(pool, n):
                odds = base * math.prod(r['price'] for r in combo)
                if not cfg.daily_odds_min <= odds <= cfg.daily_odds_max:
                    continue
                if not _fits(prefix + list(combo), cfg, cap):
                    continue
                key = (tuple(position[id(r)] for r in combo), n, abs(odds - cfg.daily_odds_min))
                if best is None or key < best[0]:
                    best = (key, list(combo))
        if best:
            return prefix + best[1]
    return None


def _ticket(ordered, cfg, cap):
    selected, odds, rejected = _build(ordered, cfg, cap)
    repaired = False
    if selected and odds < cfg.daily_odds_min:
        better = _repair(selected, ordered, cfg, cap)
        if better is not None:
            selected, odds, repaired = better, math.prod(r['price'] for r in better), True
    return selected, odds, rejected, repaired


def select_daily(rows, cfg, now_utc):
    candidates, rejected = [], Counter()
    for row in rows:
        why = promotion_reason(row, cfg, now_utc)
        if not why and not cfg.daily_min_odds <= row['price'] <= cfg.daily_max_odds:
            why = 'price_range'
        if why:
            rejected[why] += 1
        else:
            candidates.append(row)
    cap = max(0, cfg.daily_target)
    # thin (restricted) markets are offered only after the main/secondary ones, whatever their probability
    ordered = sorted(ranked(candidates), key=lambda r: OFFER_ORDER.get(r.get('quality'), 0))
    selected, odds, build_rejected, repaired = _ticket(ordered, cfg, cap)
    rejected.update(build_rejected)
    diagnostics = {'candidates': len(candidates), 'selected': len(selected), 'combined_odds': odds,
                   'target_reached': bool(selected) and odds >= cfg.daily_odds_min,
                   'rejections': dict(rejected)}
    if repaired:
        diagnostics['search'] = 'end_game'
    return selected, diagnostics
