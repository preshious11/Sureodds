"""Reject known ambiguous legacy mappings without rewriting stored forecasts."""
import re


def ambiguous_contract(market_name):
    name = str(market_name or '').lower().replace('_', ' ')
    # Old Sharp normalization discarded the unknown segment but retained its
    # original market name. Quarantine at evaluation/read boundaries as well
    # as in the raw parser, so a cached row cannot reintroduce the bad contract.
    simple_total = re.fullmatch(r'total ([a-z]+(?: [a-z]+)?)', name)
    unknown_stat = simple_total and simple_total[1] not in {
        'points','goals','runs','games','sets','rounds','corners','cards'}
    return bool(unknown_stat or re.search(r'\b(?:\d+(?:st|nd|rd|th)|first|second|third|fourth) inning\b', name))
