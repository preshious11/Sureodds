"""Detached records of evaluated candidates; no evaluation or persistence here."""
from copy import deepcopy
from dataclasses import asdict, fields

from predbot.util.timeutil import iso


def capture_candidate(scored, insight=None):
    """Copy at the validation boundary, before representative/evidence annotations.

    The event's entire market list is deliberately not repeated in every record.
    Evidence defaults are not assessments: unavailable ranking fields stay None.

    ``insight`` (optional): real recent-form/head-to-head data for this
    fixture - see predbot/providers/espn.py ``ESPNProvider.team_insight()``.
    Never fabricated and never required: when absent (the common case for
    any fixture ESPN can't positively identify, or when no fetcher was
    wired in at all), the three fields below are simply omitted, exactly as
    before this parameter existed.
    """
    event, line = scored.event, scored.line
    record = {f.name: deepcopy(getattr(event, f.name)) for f in fields(event)
              if f.name not in ('markets', 'start', 'sources')}
    record.update(start_utc=iso(event.start), event_sources=deepcopy(event.sources))
    market = asdict(line)
    market['market_label'] = market.pop('label')
    record.update(market)
    record.update({f.name: deepcopy(getattr(scored, f.name)) for f in fields(scored)
                   if f.name not in ('event', 'line', 'rank_score', 'quality', 'se', 'p_lcb', 'tier')})
    record.update(rank_score=None, quality=None, se=None, p_lcb=None,
                  bookmaker_count=line.distinct_books(), provider_count=line.provider_count())
    if insight:
        record.update(home_form=insight.get('home_form') or [],
                      away_form=insight.get('away_form') or [],
                      h2h=insight.get('h2h') or [],
                      home_form_dates=insight.get('home_form_dates') or [],
                      away_form_dates=insight.get('away_form_dates') or [],
                      h2h_dates=insight.get('h2h_dates') or [])
    return record
