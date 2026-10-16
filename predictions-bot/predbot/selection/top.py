"""Top tickets from validated alternatives, under unchanged odds and leg limits.

A bounded search maximizes joint evidence-adjusted strict-win confidence within
 the odds band, then expected return. Below-band best-first fallback is retained.
"""
import json
import math
from collections import Counter
from predbot.selection.search import search

from predbot.selection.evidence import min_books
from predbot.model.payoff import nfl_contract_valid, nfl_tie_sensitive


def notes(row):
    return (json.loads(row.get('sources') or '{}').get('_notes') or {})


def ranked(rows):
    return sorted(rows, key=lambda r: (-(r.get('rank_score') if r.get('rank_score') is not None else r['prob']),
                                        -r.get('ev', 0), -r['prob'], r['start_utc'], str(r['id'])))


def promotion_reason(row, cfg, now_utc, top=False):
    if row['status'] != 'pending' or row['start_utc'] <= now_utc:
        return 'started_or_settled'
    if row['market'] in cfg.disabled_markets:
        return 'disabled_market'      # a product choice about what to PROMOTE; the prediction itself stays in All
    if not nfl_contract_valid(row, notes(row)):
        return 'unknown_nfl_tie_probability'
    if row['prob'] < (cfg.top_min_prob if top else cfg.daily_min_prob):
        return 'below_probability'
    quality_books = min_books(row.get('quality') or 'main', cfg)
    need = max(cfg.top_min_books, quality_books) if top else quality_books
    if (row.get('books') or 1) < need:
        return 'insufficient_books'
    meta = notes(row)
    if row.get('sport') == 'american_football' and nfl_tie_sensitive(
            row['market'], row.get('period', 'full'), row.get('line')):
        if len(set(meta['nfl_tie_evidence']['books'])) < need:
            return 'insufficient_tie_books'
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


def _event(row):
    return row.get('canonical_event_id') or row['fixture_key']


def _confidence(row):
    """A leg's evidence-adjusted confidence: its ranking score (probability lower bound plus sharp edge), else its
    probability; kept positive so a product of them orders tickets."""
    score = row.get('rank_score') if row.get('rank_score') is not None else row['prob']
    return min(1.0, max(1e-6, float(score)))


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
    found, search_diag = search(ranked(candidates), cfg.top_odds_target, cfg.top_odds_max,
                                max(0, min(3, cfg.top_legs_max)))
    diagnostics["search_details"] = search_diag
    if found and [r['id'] for r in found[0]] != [r['id'] for r in selected]:
        selected, odds = found
        diagnostics.update(selected=len(selected), combined_odds=odds, target_reached=True, search='combination')
    return selected, diagnostics
