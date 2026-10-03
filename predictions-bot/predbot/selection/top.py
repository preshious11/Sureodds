"""Small, explainable Top selector. A target is never a minimum ticket requirement."""
import json
from collections import Counter

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
        if selected and odds * row['price'] > cfg.top_odds_max:
            continue
        selected.append(row)
        events.add(eid)
        odds *= row['price']
    return selected, {'candidates': len(candidates), 'selected': len(selected), 'combined_odds': odds,
                      'target_reached': bool(selected) and odds >= cfg.top_odds_target,
                      'rejections': dict(rejected)}
