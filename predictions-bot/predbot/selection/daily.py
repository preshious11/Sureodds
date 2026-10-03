"""Daily portfolio policy over stored All Predictions; no generation dependencies."""
import math
from collections import Counter
from predbot.selection.evidence import OFFER_ORDER
from predbot.selection.top import promotion_reason, ranked


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
    selected, odds, counts, events = [], 1.0, Counter(), set()
    cap = max(0, cfg.daily_target)
    # thin (restricted) markets are offered only after the main/secondary ones, whatever their probability
    for row in sorted(ranked(candidates), key=lambda r: OFFER_ORDER.get(r.get('quality'), 0)):
        if len(selected) >= cap or selected and odds >= cfg.daily_odds_min:
            break
        eid = row.get('canonical_event_id') or row['fixture_key']
        if eid in events:
            rejected['duplicate_event'] += 1
            continue
        if odds * row['price'] > cfg.daily_odds_max:
            rejected['combined_odds_ceiling'] += 1
            continue
        dimensions = ((('market', row['market']), math.ceil(cap * cfg.daily_max_market_share)),
                      (('stat', row['stat']), math.ceil(cap * cfg.daily_max_stat_share)),
                      (('sport', row['sport']), math.ceil(cap * cfg.daily_max_sport_share)),
                      (('league', row.get('canonical_competition_id') or row['league']), cfg.daily_max_per_league))
        dimensions = [d for d in dimensions if d[0] != ('stat', 'points')]
        if any(counts[key] >= max(1, limit) for key, limit in dimensions):
            rejected['diversity_limit'] += 1
            continue
        selected.append(row)
        events.add(eid)
        odds *= row['price']
        for key, _ in dimensions:
            counts[key] += 1
    return selected, {'candidates': len(candidates), 'selected': len(selected), 'combined_odds': odds,
                      'target_reached': bool(selected) and odds >= cfg.daily_odds_min,
                      'rejections': dict(rejected)}
