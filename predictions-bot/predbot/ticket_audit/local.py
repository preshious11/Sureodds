"""Direct SQLite SELECTs only. No Database constructor, fetcher, providers or models."""
from __future__ import annotations

from datetime import timedelta
import json
from pathlib import Path
import sqlite3
import threading
from zoneinfo import ZoneInfo

from .schemas import LocalEvidenceMatch, instant, normalized, number

METRICS = ('p_model', 'p_market', 'p_raw', 'p_fair', 'ev', 'edge', 'edge_ev', 'agreement',
           'liquidity', 'bookmaker_count', 'p_independent', 'fair_books', 'sources', 'notes',
           'price', 'book_quotes', 'book_prices', 'independent_status', 'p_model_basis')


def _json(value, default):
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return default


class LocalResolver:
    def __init__(self, path, timezone='Africa/Lagos'):
        self.path = Path(path).resolve()
        self.timezone = timezone
        self._lock = threading.Lock()
        self._last = None

    def _snapshot(self, now):
        """One read per audit instant: every leg of a ticket is resolved with the same `now`,
        so a 30-leg ticket reads (and JSON-parses) the local tables once, consistently."""
        with self._lock:
            if self._last is not None and self._last[0] == now:
                return self._last[1]
        records = self._records(now)
        with self._lock:
            self._last = (now, records)
        return records

    def _records(self, now):
        day = now.astimezone(ZoneInfo(self.timezone)).date().isoformat()
        with sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=2) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')  # Consistent read across sources. No migration/DDL/DML.
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            result = []
            def rows(table, where, args):
                return [dict(r) for r in db.execute(f'SELECT * FROM {table} WHERE {where}', args)] if table in tables else []
            for row in rows('custom_pick_candidates', 'prediction_date=? AND timezone=?', (day, self.timezone)):
                r = _json(row['payload_json'], {})
                if isinstance(r, dict) and r:
                    result.append((r, 'custom_pick_candidates', row['generated_at'], None))
            for row in rows('picks', 'pick_date=?', (day,)):
                r = dict(row)
                sources = _json(r.get('sources'), {})
                r.update(p_model=r.get('prob'), p_raw=r.get('prob_raw'), bookmaker_count=r.get('books'),
                         sources=sources, notes=sources.get('_notes') if isinstance(sources, dict) else None)
                if isinstance(r['notes'], dict):
                    r['edge'] = r['notes'].get('edge')
                result.append((r, 'picks', row.get('created_at'), None))
            for row in rows('prediction_cache', 'local_date=? AND timezone=?', (day, self.timezone)):
                payload = _json(row['payload_json'], {})
                if not isinstance(payload, dict):
                    continue
                for view in ('all_predictions', 'top_picks', 'daily_picks'):
                    items = payload.get(view, [])
                    if not isinstance(items, list):
                        continue
                    for item in items:
                        if not isinstance(item, dict):
                            continue
                        r = dict(item)
                        r.update(start_utc=r.get('kickoff_utc'), canonical_event_id=r.get('fixture_id'),
                                 p_model=r.get('prob'), bookmaker_count=r.get('n_books'))
                        result.append((r, 'prediction_cache', row['generated_at'], row['expires_at']))
            for row in rows('provider_cache', 'local_date=?', (day,)):
                payload = _json(row['payload_json'], [])
                if not isinstance(payload, list):
                    continue
                metadata = _json(row['metadata_json'], {})
                for e in payload:
                    if not isinstance(e, dict) or not isinstance(e.get('markets'), list):
                        continue  # Result/history payloads are not prediction evidence.
                    for market in e['markets']:
                        if isinstance(market, dict):
                            r = {**e, **market, 'start_utc': e.get('start')}
                            r.pop('markets', None)
                            result.append((r, 'provider_cache', metadata.get('generated_at') if isinstance(metadata, dict) else None,
                                           row['expires_at']))
            return result

    def resolve(self, selection, now):
        now = instant(now)
        coordinates = all((selection.sport, selection.market_family, selection.selection,
                           selection.period, selection.stat, selection.entity))
        line_valid = selection.line is None or number(selection.line) is not None
        needs_line = selection.market_family in ('total', 'handicap')
        if (now is None or not coordinates or not line_valid or (needs_line and selection.line is None)
                or (not selection.canonical_event_id and not selection.researchable())):
            return LocalEvidenceMatch('MISSING', limitations=('Incomplete fixture/market coordinates',))
        try:
            records = self._snapshot(now)
        except (sqlite3.Error, OSError, ValueError):
            return LocalEvidenceMatch('UNAVAILABLE', limitations=('Local read unavailable; no refresh attempted',))
        fixture_matches = []
        kickoff = instant(selection.kickoff)
        for record, source, at, expiry in records:
            start = instant(record.get('start_utc'))
            if normalized(record.get('sport')) != normalized(selection.sport) or start is None:
                continue
            if kickoff is not None and abs(start - kickoff) > timedelta(minutes=15):
                continue
            canonical = record.get('canonical_event_id')
            if selection.canonical_event_id:
                if canonical != selection.canonical_event_id:
                    continue
            if ((selection.home and normalized(record.get('home')) != normalized(selection.home))
                    or (selection.away and normalized(record.get('away')) != normalized(selection.away))):
                continue
            if selection.competition and normalized(record.get('league')) != normalized(selection.competition) and record.get('canonical_competition_id') != selection.competition:
                continue
            fixture_matches.append((record, source, at, expiry))
        canonical_ids = {r.get('canonical_event_id') for r, _, _, _ in fixture_matches if r.get('canonical_event_id')}
        kickoffs = {instant(r.get('start_utc')) for r, _, _, _ in fixture_matches}
        # Uncanonicalized raw-cache duplicates of one exact fixture are not a second identity.
        if len(canonical_ids) > 1 or len(kickoffs) > 1:
            return LocalEvidenceMatch('AMBIGUOUS', limitations=('Multiple fixture identities match; no guess made',))
        exact = [(r, src, at, ex) for r, src, at, ex in fixture_matches
                 if all(normalized(r.get(k)) == normalized(getattr(selection, attr)) for k, attr in
                        (('market', 'market_family'), ('selection', 'selection'), ('period', 'period'), ('stat', 'stat'), ('entity', 'entity')))
                 and number(r.get('line')) == number(selection.line)]
        if not exact:
            return LocalEvidenceMatch('MISSING', limitations=('No exact local selection; no refresh attempted',))
        # Prefer evaluated archive, then persisted picks, snapshot and raw quotes.
        priority = {'custom_pick_candidates': 0, 'picks': 1, 'prediction_cache': 2, 'provider_cache': 3}
        exact.sort(key=lambda v: (priority[v[1]], -(instant(v[2]).timestamp() if instant(v[2]) else 0)))
        r, source, at, expiry = exact[0]
        limitations = []
        age = now - instant(at) if instant(at) else None
        if age is None or age < timedelta(0) or age > timedelta(hours=6) or (instant(expiry) and instant(expiry) <= now):
            limitations.append('Local evidence stale or timestamp unavailable')
        if instant(r.get('start_utc')) <= now:
            limitations.append('Fixture already started; this is not live-result evidence')
        return LocalEvidenceMatch('EXACT', r, {k: r.get(k) for k in METRICS}, (source,), at, tuple(limitations))
