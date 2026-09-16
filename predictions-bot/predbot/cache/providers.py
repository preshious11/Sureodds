"""SQLite provider portions: completed sources are reused during partial retries."""
import json
import logging
from collections import Counter
from dataclasses import asdict
from datetime import timedelta

from predbot.providers.base import Event, MarketLine
from predbot.util.timeutil import iso, parse_iso, to_local, utcnow, day_bounds

log = logging.getLogger(__name__)
PROVIDER_CACHE_VERSION = 1


def encode_events(events):
    out = []
    for event in events:
        raw = asdict(event)
        raw['start'] = iso(event.start)
        out.append(raw)
    return out


def decode_events(payload):
    events = []
    for item in payload:
        raw = dict(item)
        raw['start'] = parse_iso(raw['start'])
        if raw['start'] is None or raw['start'].tzinfo is None:
            raise ValueError('invalid event timestamp')
        raw['markets'] = [MarketLine(**m) for m in raw['markets']]
        events.append(Event(**raw))
    return events


def expected_sports(name, provider):
    if name == 'api_football':
        return ['football']
    if name == 'betminer':
        return ['football']
    if name == 'sportsgameodds':
        from predbot.providers.sportsgameodds import SPORT_MAP
        return sorted({SPORT_MAP.get(s.upper(), s.lower()) for s in provider.sports})
    if name == 'sharpapi':
        leagues = {'mls': 'football', 'epl': 'football', 'ucl': 'football',
                   'mlb': 'baseball', 'nba': 'basketball', 'ncaab': 'basketball',
                   'wnba': 'basketball', 'nfl': 'american_football',
                   'ncaaf': 'american_football', 'nhl': 'hockey'}
        return sorted({leagues.get(s.lower(), 'other') for s in provider.leagues}) or ['other']
    return list(getattr(provider, 'sports', [])) or ['other']


def collect_persisted(chain, start, end):
    from predbot.providers.chain import merge_events
    now = utcnow()
    day = to_local(now, chain.cfg.timezone).date().isoformat()
    day_start, midnight = day_bounds(now, chain.cfg.timezone)
    pool = []
    chain.last_report = {}
    with chain.db.prediction_lock:
        for name in chain.order:
            provider = chain.providers[name]
            if not provider.enabled():
                continue
            key = f'provider:v1:{chain.cfg.timezone}:{day}:{name}'
            row = chain.db.get_provider_cache(key)
            events, report = [], None
            if row and row['cache_version'] == PROVIDER_CACHE_VERSION:
                try:
                    if parse_iso(row['expires_at']) > now:
                        events = decode_events(json.loads(row['payload_json']))
                        report = json.loads(row['metadata_json'])
                except (ValueError, TypeError, KeyError):
                    report = None
            retry_at = parse_iso(report.get('retry_after', '')) if report else None
            reuse = report and (report['status'] == 'complete' or
                                (retry_at and now < retry_at and not getattr(chain, 'force_retry', False)))
            if not reuse:
                previous = events
                report = {'expected_sports': expected_sports(name, provider), 'status': 'failed'}
                log.info('Cache RETRY | provider=%s | date=%s', name, day)
                try:
                    provider.last_fetch_complete = True
                    fetched = provider.fetch_events(day_start, midnight)
                    # Validate before calling a malformed response complete.
                    events = decode_events(encode_events(fetched))
                    complete = bool(getattr(provider, 'last_fetch_complete', True))
                    if not complete:
                        # Keep previously usable rows when the failed retry is emptier.
                        by_id = {(e.provider, e.provider_event_id): e for e in previous}
                        by_id.update({(e.provider, e.provider_event_id): e for e in events})
                        events = list(by_id.values())
                    report.update(status='complete' if complete else 'partial',
                                  outcome=('success' if events else 'empty') if complete else 'failed',
                                  fetch=getattr(provider, 'fetch_counts', {}),
                                  normalization=getattr(provider, 'normalization_counts', {}))
                except Exception as exc:
                    events = previous
                    report.update(outcome='failed', error_type=type(exc).__name__)
                    log.warning('Provider refresh failed | provider=%s | error=%s', name, type(exc).__name__)
                report.update(events=dict(Counter(e.sport for e in events)),
                              retry_after=iso(now + timedelta(seconds=chain.cfg.cache_retry_seconds)),
                              updated_at=iso(now))
                chain.db.upsert_provider_cache(key, day, encode_events(events), report, iso(midnight))
            chain.last_report[name] = report
            pool = merge_events(pool, [e for e in events if start <= e.start < end])
    return pool


def sport_states(providers):
    states = {}
    for name, report in providers.items():
        counts = report.get('events', {})
        for sport in set(report.get('expected_sports', [])) | set(counts):
            item = states.setdefault(sport, {'status': 'empty', 'events': 0, 'providers': {}})
            failed = report['status'] != 'complete'
            count = counts.get(sport, 0)
            state = 'failed' if failed else ('success' if count else 'empty')
            item['providers'][name] = state
            item['events'] += count
            if failed:
                item['status'] = 'failed'
                item['reason'] = 'provider_incomplete'
            elif count and item['status'] != 'failed':
                item['status'] = 'success'
    return states
