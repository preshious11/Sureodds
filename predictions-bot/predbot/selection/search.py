"""Bounded fixture-group search: every alternative competes, one leg per fixture.

The objective is joint evidence-adjusted strict-win confidence, then expected
unit-stake return, then target proximity. EV never bypasses an eligibility gate.
Daily is a beam approximation, not a claim of exhaustive global optimality.
"""
import math
from collections import defaultdict

ODDS_BUCKETS, PER_BUCKET = 32, 8


def event(row):
    return row.get('canonical_event_id') or row['fixture_key']


def confidence(row):
    score = row.get('rank_score')
    return min(1.0, max(1e-6, score if score is not None else row['prob']))


def ticket_key(rows, target):
    odds = math.prod(r['price'] for r in rows)
    return (-sum(math.log(confidence(r)) for r in rows),
            -sum(math.log(max(1e-9, 1+r.get('ev', 0))) for r in rows),
            abs(odds-target), len(rows), tuple(str(r['id']) for r in rows))


def search(ordered, target, ceiling, cap, fits=None):
    """Keep odds buckets at every leg count so short-price favourites cannot
    eliminate all paths capable of meeting the odds band. Grouping prevents
    same-fixture outcomes from entering a ticket, without discarding alternatives.
    """
    if cap <= 0 or not ordered:
        return None, {'expansions': 0, 'pruned_states': 0, 'method': 'fixture_beam'}
    groups = defaultdict(list)
    for row in ordered:
        groups[event(row)].append(row)
    states = {0: [((), 1.0)]}
    best, expansions, pruned = None, 0, 0
    log_ceiling = math.log(ceiling) if ceiling > 1 else 1
    for alternatives in groups.values():
        offers = {n: list(values) for n, values in states.items()}
        for n, values in states.items():
            if n >= cap:
                continue
            for prefix, odds in values:
                for row in alternatives:
                    expansions += 1
                    combined = odds*row['price']
                    if combined > ceiling:
                        continue
                    ticket = prefix+(row,)
                    if fits and not fits(ticket):
                        continue
                    if combined >= target:
                        key = ticket_key(ticket, target)
                        if best is None or key < best[0]:
                            best = key, ticket, combined
                    else:
                        offers.setdefault(n+1, []).append((ticket, combined))
        states = {}
        for n, values in offers.items():
            buckets = defaultdict(list)
            for ticket, odds in values:
                bucket = min(ODDS_BUCKETS-1, int(math.log(odds)/log_ceiling*ODDS_BUCKETS))
                buckets[bucket].append((ticket, odds))
            retained = []
            for values in buckets.values():
                # Different concentration profiles can have different feasible
                # continuations. Keep more than one state in every odds bucket.
                values.sort(key=lambda item: ticket_key(item[0], target))
                retained.extend(values[:PER_BUCKET])
                pruned += max(0, len(values)-PER_BUCKET)
            states[n] = retained
    return ((list(best[1]), best[2]) if best else None,
            {'expansions': expansions, 'pruned_states': pruned, 'method': 'fixture_beam'})
