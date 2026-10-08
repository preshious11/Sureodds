"""Small, explainable Top selector. A target is never a minimum ticket requirement.

Which ticket (among candidates that already passed every Top gate - nothing here makes a pick easier to qualify):

1. the 1-3 leg combination of different fixtures, from the ``TOP_SEARCH_K`` best-ranked fixtures, whose combined odds
   lie inside ``TOP_COMBINED_ODDS`` .. ``TOP_ODDS_MAX`` and whose JOINT evidence-adjusted confidence (the product of
   the legs' ranking scores) is highest; then the one closest to the target; then the one with fewer legs. A single
   exceptional leg is simply the best 1-leg combination;
2. when no combination fits the band, the previous best-first build (it may end below the target - the target is
   not a minimum) - but never above ``TOP_ODDS_MAX``, not even with its first leg;
3. no candidates: no Top Picks today.
"""
import json
import math
from collections import Counter
from itertools import combinations

from predbot.selection.evidence import min_books


def notes(row):
    return (json.loads(row.get('sources') or '{}').get('_notes') or {})


def ranked(rows):
    return sorted(rows, key=lambda r: (-(r.get('rank_score') if r.get('rank_score') is not None else r['prob']),
                                        -r['prob'], r['start_utc'], r['id']))


def promotion_reason(row, cfg, now_utc, top=False):
    if row['status'] != 'pending' or row['start_utc'] <= now_utc:
        return 'started_or_settled'
    if row['market'] in cfg.disabled_markets:
        return 'disabled_market'      # a product choice about what to PROMOTE; the prediction itself stays in All
    if row['prob'] < (cfg.top_min_prob if top else cfg.daily_min_prob):
        return 'below_probability'
    need = cfg.top_min_books if top else min_books(row.get('quality') or 'main', cfg)
    if (row.get('books') or 1) < need:
        return 'insufficient_books'
    if top and row.get('quality') != 'main':
        return 'market_quality'
    meta = notes(row)
    evidence = meta.get('evidence') or {}
    if evidence.get('dispersion') is not None and evidence['dispersion'] > cfg.max_book_dispersion:
        return 'book_disagreement'
    if row.get('edge_ev') is not None and row['edge_ev'] < cfg.min_edge_vs_sharp:
        return 'poor_reference_value'
    if not meta.get('independent') and (meta.get('edge') or 0) > cfg.max_unsupported_edge:
        return 'unsupported_edge'
    if meta.get('independent') and row['ev'] < (cfg.top_max_neg_ev if top else cfg.daily_max_neg_ev):
        return 'poor_model_value'
    if not top and evidence.get('lcb', evidence.get('p_lcb', row['prob'])) < cfg.daily_min_lcb:
        return 'low_confidence_bound'
    return None


#: how many of the best-ranked fixtures the combination search considers (C(12,1..3) = 298 combinations at most)
TOP_SEARCH_K = 12


def _event(row):
    return row.get('canonical_event_id') or row['fixture_key']


def _confidence(row):
    """A leg's evidence-adjusted confidence: its ranking score (probability lower bound plus sharp edge), else its
    probability; kept positive so a product of them orders tickets."""
    score = row.get('rank_score') if row.get('rank_score') is not None else row['prob']
    return min(1.0, max(1e-6, float(score)))


def best_combination(pool, cfg):
    """The best in-band 1..3 leg combination of ``pool`` (already ranked, best first), or None. Deterministic."""
    legs_max = max(0, min(3, cfg.top_legs_max))
    fixtures, seen = [], set()
    for row in pool:
        if _event(row) not in seen:
            seen.add(_event(row))
            fixtures.append(row)
        if len(fixtures) == TOP_SEARCH_K:
            break
    best = None
    for n in range(1, legs_max + 1):
        for combo in combinations(fixtures, n):
            odds = math.prod(r['price'] for r in combo)
            if not cfg.top_odds_target <= odds <= cfg.top_odds_max:
                continue
            key = (-math.prod(_confidence(r) for r in combo), abs(odds - cfg.top_odds_target), n,
                   tuple(fixtures.index(r) for r in combo))
            if best is None or key < best[0]:
                best = (key, list(combo), odds)
    return (best[1], best[2]) if best else None


def select_top(rows, cfg, now_utc):
    candidates, rejected = [], Counter()
    for row in rows:
        why = promotion_reason(row, cfg, now_utc, top=True)
        if why:
            rejected[why] += 1
        else:
            candidates.append(row)
    selected, odds, events = [], 1.0, set()
    for row in ranked(candidates):
        if len(selected) >= max(0, min(3, cfg.top_legs_max)):
            break
        if selected and odds >= cfg.top_odds_target:
            break
        # a fixture may hold several All Predictions (one per market family); Top promotes at most ONE of them
        eid = row.get('canonical_event_id') or row['fixture_key']
        if eid in events:
            rejected['duplicate_event'] += 1
            continue
        if odds * row['price'] > cfg.top_odds_max:
            continue                      # TOP_ODDS_MAX is a hard ceiling, for the first leg too
        selected.append(row)
        events.add(eid)
        odds *= row['price']
    diagnostics = {'candidates': len(candidates), 'selected': len(selected), 'combined_odds': odds,
                   'target_reached': bool(selected) and odds >= cfg.top_odds_target,
                   'rejections': dict(rejected)}
    found = best_combination(ranked(candidates), cfg)
    if found and [r['id'] for r in found[0]] != [r['id'] for r in selected]:
        selected, odds = found
        diagnostics.update(selected=len(selected), combined_odds=odds, target_reached=True, search='combination')
    return selected, diagnostics
