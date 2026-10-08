"""Deterministic ticket composition; no accumulator probability or alternative generation."""
from dataclasses import replace
from .schemas import VERDICTS, number

USABLE = ('STRONG_LOCAL', 'GOOD_LOCAL', 'RESEARCHED')
PRIORITY = {'VERY_RISKY': 0, 'RISKY': 1, 'REASONABLE': 2, 'STRONG': 3}
STRENGTH = {'RESEARCHED': 1, 'GOOD_LOCAL': 2, 'STRONG_LOCAL': 3}


def verdict(leg):
    return leg.verdict if leg.verdict in VERDICTS and leg.evidence_level in USABLE else 'INSUFFICIENT_EVIDENCE'


def local_probability(leg):
    match = leg.evidence.local
    p = number(leg.local_probability)
    return float(p) if (leg.evidence.evidence_source == 'LOCAL' and match and match.status == 'EXACT'
        and leg.evidence_level in ('GOOD_LOCAL', 'STRONG_LOCAL') and p is not None and 0 <= p <= 1
        and p == number(match.metrics.get('p_model'))) else None


def finish(audit):
    legs = tuple(replace(a, verdict=verdict(a)) for a in audit.selections)
    values = [a.verdict for a in legs]
    if all(v == 'INSUFFICIENT_EVIDENCE' for v in values):
        overall = 'INSUFFICIENT_EVIDENCE'
    elif 'VERY_RISKY' in values:
        overall = 'VERY_RISKY'
    elif any(v in ('RISKY', 'INSUFFICIENT_EVIDENCE') for v in values) or len(legs) >= 6:
        overall = 'RISKY'
    elif len(legs) == 1 and values == ['STRONG']:
        overall = 'STRONG'
    else:
        overall = 'REASONABLE'
    candidates = [i for i, a in enumerate(legs) if a.verdict in PRIORITY]
    def key(i):
        a = legs[i]
        p = local_probability(a)
        return PRIORITY[a.verdict], STRENGTH[a.evidence_level], p if p is not None else 2, i
    weakest = (min(candidates, key=key),) if candidates else ()
    return replace(audit, selections=legs, overall_verdict=overall, weakest_selections=weakest)
