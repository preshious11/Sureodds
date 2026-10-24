"""SQLite provider portions: completed sources are reused during partial retries."""
import json
import hashlib
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


def collection_signature(name, cfg):
    """Version the inputs that decide which events/quotes are collected. No secrets.

    Date-only keys otherwise keep a completed pre-deploy feed alive even when
    international leagues or market filters were added to the configuration.
    Selection thresholds do not invalidate provider data or spend more credits.
    """
    fields = {
        'sharpapi': ('sharpapi_leagues', 'sharpapi_sports', 'sharpapi_markets',
                     'sharpapi_markets_football', 'sharpapi_markets_baseball',
                     'sharpapi_main_lines_only', 'sharpapi_max_pages', 'sharpapi_events_per_target'),
        'oddsapi': ('odds_api_league_allowlist', 'odds_api_regions', 'odds_api_max_leagues_per_sport'),
        'sportsgameodds': ('sgo_leagues', 'sgo_sports', 'sgo_allowed_stats', 'sgo_allowed_periods',
                          'max_player_props'),
    }.get(name, ())
    inputs = {k: getattr(cfg, k, None) for k in
              (*fields, 'allowed_sports', 'day_window_end_offset_hours', 'price_mode')}
    inputs['normalization_revision'] = 2
    return hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()


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
        # Phase 1 (serial, cheap): read each provider's cached row and decide whether it must be refreshed.
        plan = []
        for name in chain.order:
            provider = chain.providers[name]
            if not provider.enabled():
                continue
            _w, _c = time.perf_counter(), time.process_time()
            key = f'provider:v1:{cfg.timezone}:{day}:{name}'
            signature = collection_signature(name, cfg)
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
            compatible = report and report.get('collection_signature') == signature
            # Unversioned real-provider rows have unknown collection settings.
            # Refresh them once through the existing quota-controlled providers.
            retry_at = parse_iso(report.get('retry_after', '')) if report else None
            complete_nonempty = report and report['status'] == 'complete' and bool(events)
            reuse = compatible and (complete_nonempty or
                                (retry_at and now < retry_at and not getattr(chain, 'force_retry', False)))
            refresh_reason = ('settings_changed' if report and not compatible else
                              'empty_retry' if report and report['status'] == 'complete' and not events else
                              'retry' if report else 'cache_miss')
            if report and not compatible:
                # Cached quotes from removed leagues/markets must not leak into
                # the replacement when a provider fails its fresh request.
                events = []
            plan.append({'name': name, 'provider': provider, 'key': key, 'events': events, 'report': report,
                         'signature': signature, 'refresh_reason': refresh_reason,
                         'reuse': bool(reuse), 'spent': (time.perf_counter() - _w, time.process_time() - _c)})

        # Phase 2: the providers that need a refresh are fetched CONCURRENTLY. Each has its own rate limiter and
        # budget row, so they do not compete; a slow or retrying provider no longer delays the others. Results are
        # consumed below in PROVIDER_ORDER, so the merged pool is exactly what the serial loop produced.
        fetches = {}
        stale = [p for p in plan if not p['reuse']]

        def fetch(p):
            t0, c0 = time.perf_counter(), time.process_time()
            try:
                p['provider'].last_fetch_complete = True
                return p['provider'].fetch_events(day_start, fetch_end), None, (time.perf_counter() - t0, time.process_time() - c0)
            except Exception as exc:  # noqa: BLE001 - handled per provider in phase 3, like the serial loop
                return None, exc, (time.perf_counter() - t0, time.process_time() - c0)

        if len(stale) > 1:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=len(stale), thread_name_prefix='collect') as pool_:
                for p, res in zip(stale, pool_.map(fetch, stale)):
                    fetches[p['name']] = res
        else:
            for p in stale:
                fetches[p['name']] = fetch(p)

        # Phase 3 (serial, in provider order): validate, persist, merge.
        for p in plan:
            name, provider, key, events, report = p['name'], p['provider'], p['key'], p['events'], p['report']
            _w, _c = time.perf_counter(), time.process_time()
            fetch_wall = fetch_cpu = 0.0
            if not p['reuse']:
                previous = events
                report = {'expected_sports': expected_sports(name, provider, cfg), 'status': 'failed'}
                log.info('Cache RETRY | provider=%s | date=%s', name, day)
                fetched, error, (fetch_wall, fetch_cpu) = fetches[name]
                try:
                    if error is not None:
                        raise error
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
                report.update(collection_signature=p['signature'],
                              events=dict(Counter(e.sport for e in events)),
                              retry_after=iso(now + timedelta(seconds=chain.cfg.cache_retry_seconds)),
                              updated_at=iso(now))
                chain.db.upsert_provider_cache(key, day, encode_events(events), report, iso(midnight))
            chain.last_report[name] = dict(report, cache='reused' if p['reuse'] else 'refreshed',
                                          refresh_reason=None if p['reuse'] else p['refresh_reason'])
            for e in events:
                e.markets = consolidate_lines(e.markets)         # one line per bet, cached rows included
            from predbot.storage.identity import failure_record, resolve_events_detailed
            # Canonical identity is resolved once, here, so cross-provider merging keys on resolved IDs rather
            # than on re-fuzzy-matched names. Failures are kept per provider (attempts) AND as records so the
            # run can count unique fixtures.
            windowed = [e for e in events if start <= e.start < end]       # only fixtures in today's window are counted or resolved
            resolved, failed = resolve_events_detailed(windowed, chain.db, getattr(cfg, 'supported_competitions', ()))
            chain.last_report[name]['identity_failures'] = dict(Counter(r for _, r in failed))
            chain.last_report[name]['identity_failed_fixtures'] = [failure_record(e, r) for e, r in failed]
            pool = merge_events(pool, resolved,
                                getattr(cfg, 'price_mode', 'median'))
            chain.last_timings[name] = {'wall': round(p['spent'][0] + fetch_wall + time.perf_counter() - _w, 4),
                                        'cpu': round(p['spent'][1] + fetch_cpu + time.process_time() - _c, 4)}
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
