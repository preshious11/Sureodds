"""Daily tickets over validated alternatives, preserving existing gates and caps.

Compare qualifying combinations across fixture groups. Search is bounded and
falls back to the unchanged best-first build if no ticket reaches the band.
No minimum diversity quotas; existing maximum concentration limits remain.
"""
import math
from collections import Counter
from itertools import combinations

from predbot.selection.search import search
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


def _build(ordered, cfg, cap, reject_sink=None):
    """``reject_sink``: see ``select_daily`` - purely additive, called at every point this already
    increments ``rejected``, never changing ``selected``/``odds``/``rejected`` themselves."""
    selected, odds, counts, events, rejected = [], 1.0, Counter(), {}, Counter()
    for row in ordered:
        if len(selected) >= cap or selected and odds >= cfg.daily_odds_min:
            break
        eid = _event(row)
        if eid in events:
            rejected['duplicate_event'] += 1
            if reject_sink:
                reject_sink(row, 'duplicate_event', winner_row=events[eid])
            continue
        if odds * row['price'] > cfg.daily_odds_max:
            rejected['combined_odds_ceiling'] += 1
            if reject_sink:
                reject_sink(row, 'combined_odds_ceiling')
            continue
        dimensions = _dimensions(row, cfg, cap)
        if any(counts[key] >= max(1, limit) for key, limit in dimensions):
            rejected['diversity_limit'] += 1
            if reject_sink:
                reject_sink(row, 'diversity_limit')
            continue
        selected.append(row)
        events[eid] = row
        odds *= row['price']
        for key, _ in dimensions:
            counts[key] += 1
    return selected, odds, rejected


def _repair(selected, ordered, cfg, cap):
    found, _ = search(ordered, cfg.daily_odds_min, cfg.daily_odds_max, cap,
                      fits=lambda ticket: _fits(ticket, cfg, cap))
    return found[0] if found else None


def _ticket(ordered, cfg, cap, reject_sink=None):
    selected, odds, rejected = _build(ordered, cfg, cap, reject_sink=reject_sink)
    found, _ = search(ordered, cfg.daily_odds_min, cfg.daily_odds_max, cap,
                      fits=lambda ticket: _fits(ticket, cfg, cap))
    repaired = bool(found and [r['id'] for r in found[0]] != [r['id'] for r in selected])
    if found:
        selected, odds = found
    return selected, odds, rejected, repaired


def select_daily(rows, cfg, now_utc, reject_sink=None):
    """``reject_sink`` (Part 4 rejection logging, 2026-10-23), optional: a
    ``callable(row, reason, winner_row=None)`` invoked at every point this already increments
    ``rejected`` - purely additive instrumentation (predbot/selection/rejections.py persists it). Never
    called, never constructed, when omitted: existing callers and ``rejected``/``selected``/``diagnostics``
    are all byte-identical to before (see tests/test_rejection_logging.py)."""
    candidates, rejected = [], Counter()
    for row in rows:
        why = promotion_reason(row, cfg, now_utc)
        if not why and not cfg.daily_min_odds <= row['price'] <= cfg.daily_max_odds:
            why = 'price_range'
        if why:
            rejected[why] += 1
            if reject_sink:
                reject_sink(row, why)
        else:
            candidates.append(row)
    cap = max(0, cfg.daily_target)
    ordered = ranked(candidates)
    selected, odds, build_rejected, repaired = _ticket(ordered, cfg, cap, reject_sink=reject_sink)
    rejected.update(build_rejected)
    diagnostics = {'candidates': len(candidates), 'selected': len(selected), 'combined_odds': odds,
                   'target_reached': bool(selected) and odds >= cfg.daily_odds_min,
                   'rejections': dict(rejected)}
    if repaired:
        diagnostics['search'] = 'end_game'
    return selected, diagnostics
