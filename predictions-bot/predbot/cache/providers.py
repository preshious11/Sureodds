"""SQLite provider portions: completed sources are reused during partial retries."""
import json
import logging
import time
from collections import Counter
from dataclasses import asdict
from datetime import timedelta

from predbot.providers.base import Event, MarketLine
from predbot.providers.base import consolidate_lines
from predbot.providers.chain import filter_allowed, merge_events
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


def expected_sports(name, provider, cfg=None):
    if name == 'sportsgameodds':
        from predbot.providers.sportsgameodds import SPORT_MAP
        sports = sorted({SPORT_MAP.get(s.upper(), s.lower()) for s in provider.sports})
    elif name == 'sharpapi':
        from predbot.providers.sharpapi import sport_for_filter, sport_for_league
        found = {sport_for_league(s) for s in provider.leagues}
        found |= {sport_for_filter(s) for s in getattr(provider, 'sports', [])}
        sports = sorted(found) or ['other']
    else:
        sports = list(getattr(provider, 'sports', [])) or ['other']
    return sports or ['other']


def _quota_state(provider):
    """Provider-reported quota/gate state (Odds API), or None. Anything that is
    not a plain dict (e.g. a mock) is ignored so the report stays serialisable."""
    fn = getattr(provider, 'quota_state', None)
    state = fn() if callable(fn) else None
    return state if isinstance(state, dict) else None


def _request_summary(provider):
    """Requests this generation spent versus the budget it was given (providers that meter requests)."""
    spent, budget = getattr(provider, 'requests_spent', None), getattr(provider, 'requests_budget', None)
    if not isinstance(spent, int):
        return None
    return {'spent': spent, 'budget': budget if isinstance(budget, int) else None}


def collect_persisted(chain, start, end):
    from predbot.providers.chain import merge_events
    now = utcnow()
    day = to_local(now, chain.cfg.timezone).date().isoformat()
    day_start, midnight = day_bounds(now, chain.cfg.timezone)
    # Providers must be asked for the SAME window build_picks() filters to:
    # local midnight + DAY_WINDOW_END_OFFSET_HOURS. Fetching only to midnight
    # made the extended window inert on this (production) path - late-evening
    # kickoffs were never fetched, so the wider filter downstream had nothing
    # to find. The cache row still expires at midnight (a new day = a new key).
    fetch_end = midnight + timedelta(hours=max(0, chain.cfg.day_window_end_offset_hours))
    pool = []
    chain.last_report = {}
    chain.last_timings = {}
    cfg = chain.cfg
    with chain.db.prediction_lock:
        for name in chain.order:
            provider = chain.providers[name]
            if not provider.enabled():
                continue
            _w, _c = time.perf_counter(), time.process_time()
            key = f'provider:v1:{cfg.timezone}:{day}:{name}'
            row = chain.db.get_provider_cache(key)
            events, report = [], None
            if row and row['cache_version'] == PROVIDER_CACHE_VERSION:
                try:
                    if parse_iso(row['expires_at']) > now:
                        # The gate applies to restored rows too: a cached event
                        # from a now-banned sport can never re-enter the pool.
                        events = filter_allowed(decode_events(json.loads(row['payload_json'])), cfg)
                        report = json.loads(row['metadata_json'])
                except (ValueError, TypeError, KeyError):
                    report = None
            retry_at = parse_iso(report.get('retry_after', '')) if report else None
            reuse = report and (report['status'] == 'complete' or
                                (retry_at and now < retry_at and not getattr(chain, 'force_retry', False)))
            if not reuse:
                previous = events
                report = {'expected_sports': expected_sports(name, provider, cfg), 'status': 'failed'}
                log.info('Cache RETRY | provider=%s | date=%s', name, day)
                try:
                    provider.last_fetch_complete = True
                    fetched = provider.fetch_events(day_start, fetch_end)
                    # Validate before calling a malformed response complete.
                    gate = {}
                    events = filter_allowed(decode_events(encode_events(fetched)), cfg, gate)
                    complete = bool(getattr(provider, 'last_fetch_complete', True))
                    if not complete:
                        # Keep previously usable rows when the failed retry is emptier.
                        by_id = {(e.provider, e.provider_event_id): e for e in previous}
                        by_id.update({(e.provider, e.provider_event_id): e for e in events})
                        events = list(by_id.values())
                    report.update(status='complete' if complete else 'partial',
                                  sport_rejected=gate.get('sport_rejected', 0),
                                  outcome=('success' if events else 'empty') if complete else 'failed',
                                  fetch=getattr(provider, 'fetch_counts', {}),
                                  requests=_request_summary(provider),
                                  normalization=getattr(provider, 'normalization_counts', {}),
                                  rejections=dict(getattr(provider, 'parse_counts', {}) or {}),
                                  sport_status=getattr(provider, 'sport_status', None),
                                  quota=_quota_state(provider))
                except Exception as exc:
                    events = previous
                    report.update(outcome='failed', error_type=type(exc).__name__)
                    log.warning('Provider refresh failed | provider=%s | error=%s', name, type(exc).__name__)
                report.update(events=dict(Counter(e.sport for e in events)),
                              retry_after=iso(now + timedelta(seconds=chain.cfg.cache_retry_seconds)),
                              updated_at=iso(now))
                chain.db.upsert_provider_cache(key, day, encode_events(events), report, iso(midnight))
            chain.last_report[name] = report
            for e in events:
                e.markets = consolidate_lines(e.markets)         # one line per bet, cached rows included
            pool = merge_events(pool, [e for e in events if start <= e.start < end],
                                getattr(cfg, 'price_mode', 'median'))
            chain.last_timings[name] = {'wall': round(time.perf_counter() - _w, 4), 'cpu': round(time.process_time() - _c, 4)}
    return pool


def sport_states(providers):
    """Per-sport coverage state (SUCCESS/EMPTY/PARTIAL/FAILED) across providers.

    Providers that report a per-sport ``sport_status`` (sportsgameodds, sharpapi) drive the aggregation: one provider's failed
    Basketball fetch must not mark Football complete or vice versa.  Mock/stub
    providers without per-sport status fall back to the coarse whole-provider
    state so the same shape stays available in tests.
    """
    states = {}
    for name, report in providers.items():
        counts = report.get('events', {})
        per_sport = report.get('sport_status') or {}
        if per_sport:
            for sport, info in per_sport.items():
                if isinstance(info, str):
                    info = {'status': info}
                st = str(info.get('status') or 'empty')
                item = states.setdefault(sport, {'status': None, 'events': 0, 'providers': {}, 'reason': ''})
                item['providers'][name] = st
                item['events'] += int(info.get('events') or 0)
            continue
        for sport in set(report.get('expected_sports', [])) | set(counts):
            count = counts.get(sport, 0)
            failed = report['status'] != 'complete'
            st = 'failed' if failed else ('success' if count else 'empty')
            item = states.setdefault(sport, {'status': None, 'events': 0, 'providers': {}, 'reason': ''})
            item['providers'][name] = st
            item['events'] += count
            if failed:
                item['reason'] = 'provider_incomplete'
    for sport, item in states.items():
        item['status'] = _aggregate_sport_state(item['providers'])
    return states


def _aggregate_sport_state(providers):
    """One FAILED provider fails the sport; else PARTIAL wins; else SUCCESS
    once any provider actually fetched events; else EMPTY."""
    values = list(providers.values())
    if not values:
        return 'empty'
    if 'failed' in values:
        return 'failed'
    if 'partial' in values:
        return 'partial'
    if 'success' in values:
        return 'success'
    return 'empty'
