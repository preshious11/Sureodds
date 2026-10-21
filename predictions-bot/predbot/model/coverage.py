"""Count market scopes without treating display representatives as lost bets.

Diagnostics only: never rank, filter, fetch or modify a candidate. Line and side
counts describe evaluated contracts, not provider subscription entitlements.
"""
from collections import Counter, defaultdict


def scope_counts(scored):
    counts, lines, sides = Counter(), defaultdict(set), defaultdict(set)
    for value in scored:
        m = value.line
        key = (value.event.sport, m.market, m.period, m.stat, m.entity)
        counts[key] += 1
        lines[key].add(m.line)
        sides[key].add(m.selection)
    return [dict(zip(('sport', 'market', 'period', 'stat', 'entity'), key),
                 outcomes=counts[key],
                 lines=sorted(v for v in lines[key] if v is not None),
                 selections=sorted(sides[key]))
            for key in sorted(counts)]


def coverage_stages(scored, validated, representatives):
    return {'scored': scope_counts(scored),
            'validated': scope_counts(validated),
            'representatives': scope_counts(representatives)}
