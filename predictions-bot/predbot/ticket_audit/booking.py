"""Booking-code intake: bookmaker + code -> provider-neutral result -> NormalizedTicket.

Retrieval only. No conversion between bookmakers, booking creation, bet placement
or sports-provider clients. Adapters (booking_sources.py) are the ONLY code that
sees provider JSON; the rest of Ticket Audit depends on ``BookingCodeResult``.
Raw codes are never persisted: the cache and ledger key on a salted one-way hash.
"""
from __future__ import annotations

from concurrent.futures import Future
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import secrets
import sqlite3
import sys
import threading
from predbot import ticket_audit_switch

from .schemas import NormalizedSelection, NormalizedTicket, instant, ticket_from_json
from .resources import validate_payload

log = logging.getLogger(__name__)

# Fixed, user-facing outcome states. Never carry provider text or exceptions.
OK = 'OK'
PARTIAL = 'PARTIAL'
MALFORMED_CODE = 'MALFORMED_CODE'
UNKNOWN_BOOKMAKER = 'UNKNOWN_BOOKMAKER'
UNSUPPORTED_BOOKMAKER = 'UNSUPPORTED_BOOKMAKER'
NOT_FOUND = 'NOT_FOUND'
EXPIRED = 'EXPIRED'
EMPTY = 'EMPTY'
RESPONSE_TOO_LARGE = 'RESPONSE_TOO_LARGE'
PARSER_LIMIT = 'PARSER_LIMIT'
PROVIDER_UNAVAILABLE = 'PROVIDER_UNAVAILABLE'
PROVIDER_CHANGED = 'PROVIDER_CHANGED'
QUOTA_EXHAUSTED = 'QUOTA_EXHAUSTED'
TIMEOUT = 'TIMEOUT'
DISABLED = 'DISABLED'
STATUSES = (OK, PARTIAL, MALFORMED_CODE, UNKNOWN_BOOKMAKER, UNSUPPORTED_BOOKMAKER, NOT_FOUND, EXPIRED,
            EMPTY, RESPONSE_TOO_LARGE, PARSER_LIMIT, PROVIDER_UNAVAILABLE, PROVIDER_CHANGED, QUOTA_EXHAUSTED, TIMEOUT, DISABLED)
SUCCESS = (OK, PARTIAL)
# A definitive provider answer stops routing; anything transient may try the next source.
DEFINITIVE = SUCCESS + (NOT_FOUND, EXPIRED, EMPTY, RESPONSE_TOO_LARGE, PARSER_LIMIT)
NEGATIVE_CACHE = (NOT_FOUND, EXPIRED, EMPTY, RESPONSE_TOO_LARGE, PARSER_LIMIT)

# Bookmakers we can ASK about. Codes do not reliably reveal their bookmaker.
BOOKMAKERS = {'sportybet': 'SportyBet', 'bet9ja': 'Bet9ja', 'nairabet': 'Nairabet'}
CODE = re.compile(r'[A-Z0-9]{4,16}')

# Per retrieval, not resolver.last_diagnostic: that attribute can be overwritten
# by a different code resolving concurrently. Never include codes or payloads.
_DIAGNOSTIC = ContextVar('booking_retrieval_diagnostic', default=None)
_COUNT_FIELDS = frozenset({'raw_outcome_count', 'ticket_bet_count', 'ticket_selection_count',
                          'decoded_selection_count', 'normalized_selection_count',
                          'raw_event_count', 'raw_market_count', 'raw_nested_outcome_count',
                          'unavailable_outcome_count', 'ticket_complete_reference_count',
                          'ticket_bet_complete_reference_count', 'explicit_selected_flag_count',
                          'selected_selection_count', 'unavailable_selected_count', 'raw_selection_count'})
_STAGES = frozenset({'resolver', 'provider_http', 'sportybet_event_count', 'sportybet_decode',
                     'mybetcode_row_count', 'finalize', 'finalized'})


def note_booking(stage=None, http_attempted=None, http_status=None, **counts):
    """Allowlisted counts/fixed stages only; never provider strings or ticket data."""
    diagnostic = _DIAGNOSTIC.get()
    if diagnostic is None:
        return
    if stage in _STAGES:
        diagnostic['stage'] = stage
    if type(http_attempted) is bool:
        diagnostic['http_attempted'] = http_attempted
    if type(http_status) is int and 100 <= http_status <= 599:
        diagnostic['http_status'] = http_status
    diagnostic.update({key: value if type(value) is int and value >= 0 else None
                       for key, value in counts.items() if key in _COUNT_FIELDS})


def _retrieval_diagnostic(resolver=None, cached=False):
    schema = sys.modules[NormalizedTicket.__module__]
    module = sys.modules.get(type(resolver).__module__) if resolver is not None else None
    return {'booking_module': __file__, 'schemas_module': schema.__file__,
            'resolver_module': getattr(module, '__file__', None),
            'leg_collection': {'SportyBetDirectResolver': 'data.outcomes[*].markets[*].outcomes[*]',
                               'MyBetCodeResolver': 'data.selections'}.get(type(resolver).__name__),
            'cached': cached,
            'stage': 'cache' if cached else 'resolver', 'http_attempted': False, 'http_status': None,
            **dict.fromkeys(_COUNT_FIELDS)}


class BookingCodeFailure(Exception):
    """Carries one fixed status from STATUSES; never wraps transport/provider text."""
    def __init__(self, status):
        if status not in STATUSES:
            status = PROVIDER_CHANGED
        super().__init__(status)
        self.status = status


def normalize_code(value):
    """Strict untrusted-input gate. Returns the canonical code or raises MALFORMED_CODE."""
    if not isinstance(value, str) or len(value) > 64:
        raise BookingCodeFailure(MALFORMED_CODE)
    code = value.strip().upper()
    if not CODE.fullmatch(code):
        raise BookingCodeFailure(MALFORMED_CODE)
    return code


def masked(code):
    return code[:2] + '•' * max(1, len(code) - 4) + code[-2:] if len(code) > 4 else '•' * len(code)


@dataclass(frozen=True)
class DecodedLeg:
    """One provider-decoded selection. Missing values stay None; never inferred."""
    sport: str | None = None
    competition: str | None = None
    home: str | None = None
    away: str | None = None
    participants: tuple[str, ...] = ()
    kickoff: str | None = None
    market: str | None = None          # canonical market_family, or None when unmapped
    market_label: str | None = None    # provider's own label, display only
    line: float | None = None
    specifier: str | None = None       # provider's raw line/specifier, display/provenance only
    outcome: str | None = None         # canonical selection, or None when unmapped
    outcome_label: str | None = None
    odds: float | None = None
    period: str | None = None
    stat: str | None = None
    entity: str | None = None
    unresolved: tuple[str, ...] = ()
    selected_participant: str | None = None  # name behind outcome home/away, never inferred otherwise
    provider_market_id: str | None = None    # provenance/diagnostics only


@dataclass(frozen=True)
class BookingCodeResult:
    bookmaker: str
    booking_code: str                  # masked for display; the raw code is never kept
    code_hash: str
    status: str
    selections: tuple[DecodedLeg, ...] = ()
    provider: str | None = None        # e.g. sportybet-direct-experimental / mybetcode-v1
    source_host: str | None = None
    retrieved_at: str | None = None
    declared_count: int | None = None
    total_odds: float | None = None
    unresolved: tuple[str, ...] = ()   # ticket-level gaps, e.g. undecoded legs
    cached: bool = False

    @property
    def valid(self):
        return self.status in SUCCESS


def selected_name(selection):
    """Participant behind a NormalizedSelection's home/away side: deterministic, no lookup.

    The core schema keeps canonical sides (needed for exact local evidence matching);
    home/away names travel with it, so this is all Audit/research needs.
    """
    if selection.market_family in ('result3', 'result2', 'dnb', 'handicap') and selection.selection in ('home', 'away'):
        return selection.home if selection.selection == 'home' else selection.away
    return None


def leg_unresolved(leg):
    """Fields Audit needs that the provider did not supply (or we could not map)."""
    gaps = [k for k in ('sport', 'competition', 'home', 'away', 'kickoff', 'market', 'outcome',
                        'period', 'stat', 'entity') if getattr(leg, k) is None]
    if leg.market in ('total', 'handicap') and leg.line is None:
        gaps.append('line')
    if (leg.market in ('result3', 'result2', 'dnb', 'handicap') and leg.outcome in ('home', 'away')
            and not leg.selected_participant):
        gaps.append('selected_participant')
    return tuple(dict.fromkeys(tuple(leg.unresolved) + tuple(gaps)))


def finalize(bookmaker, code_hash, code_masked, provider, host, legs, now, declared=None, total_odds=None,
             notes=()):
    """Shared adapter tail: bounds, partial detection, provenance. No substitution of legs.

    ``notes`` are ticket-level remarks (e.g. unreadable combined odds) that never make the
    selections PARTIAL."""
    legs = tuple(replace(leg, unresolved=leg_unresolved(leg)) for leg in legs)
    note_booking(stage='finalize', normalized_selection_count=len(legs))
    if not legs:
        raise BookingCodeFailure(EMPTY)
    gaps = []
    if declared is not None and declared != len(legs):
        gaps.append(f'Bookmaker reported {declared} selections; {len(legs)} were returned')
    status = PARTIAL if gaps or any(l.unresolved for l in legs) else OK
    note_booking(stage='finalized')
    return BookingCodeResult(bookmaker, code_masked, code_hash, status, legs, provider, host,
                             now.astimezone(timezone.utc).isoformat(), declared, total_odds,
                             tuple(gaps) + tuple(notes))


def to_ticket(result):
    """Detached NormalizedTicket for the existing review/audit pipeline.

    Provenance stays on BookingCodeResult; the core schema only receives the
    bookmaker name it already had a field for. Raw codes never enter the ticket.
    """
    if not result.valid:
        raise BookingCodeFailure(result.status)
    legs = []
    for leg in result.selections:
        # A generic 'Home'/'1' label is replaced by the selected participant's own name.
        outcome = leg.outcome_label
        if leg.selected_participant and (outcome or '').strip().casefold() in ('home', 'away', '1', '2', ''):
            outcome = leg.selected_participant
        label = ' — '.join(v for v in (leg.market_label, outcome) if v) or None
        legs.append(NormalizedSelection(
            sport=leg.sport, competition=leg.competition, home=leg.home, away=leg.away, kickoff=leg.kickoff,
            market_family=leg.market, selection=leg.outcome, line=leg.line, submitted_odds=leg.odds,
            source_event_id=None, raw_text=label[:300] if label else None, canonical_event_id=None,
            period=leg.period, stat=leg.stat, entity=leg.entity))
    warnings = list(result.unresolved)
    warnings += [f'Selection {i} not fully decoded: {", ".join(l.unresolved)}'
                 for i, l in enumerate(result.selections, 1) if l.unresolved]
    ticket = NormalizedTicket('normalized', BOOKMAKERS.get(result.bookmaker, result.bookmaker), None,
                              result.total_odds, tuple(legs), 'HIGH' if result.status == OK else 'MEDIUM',
                              tuple(warnings))
    # Same strict validator as Gemini extraction: one schema gate for every intake path.
    return ticket_from_json(json.dumps(asdict(ticket)))


@dataclass(frozen=True)
class BookingSettings:
    enabled: bool = False
    sportybet_direct: bool = False
    sportybet_direct_daily: int = 50
    mybetcode: bool = False
    mybetcode_key: str = field(default='', repr=False)
    mybetcode_daily: int = 10
    mybetcode_monthly: int = 100

    @classmethod
    def from_environment(cls, env_file=None):
        # Same isolated reader as Gemini settings: never loads Config or touches os.environ.
        values = {}
        if env_file and Path(env_file).is_file():
            for line in Path(env_file).read_text(encoding='utf-8-sig').splitlines():
                if '=' not in line or line.lstrip().startswith('#'):
                    continue
                k, v = line.split('=', 1)
                values[k.strip()] = re.split(r'\s+#', v, maxsplit=1)[0].strip().strip('"\'')
        get = lambda k, d='': os.environ.get(k, values.get(k, d))
        flag = lambda k: get(k).strip().lower() == 'true'
        def bounded(k, default, top):
            try:
                n = int(get(k, str(default)))
            except ValueError:
                return 0
            return n if 1 <= n <= top else 0
        return cls(flag('AUDIT_BOOKING_CODES_ENABLED'), flag('SPORTYBET_DIRECT_ENABLED'),
                   bounded('SPORTYBET_DIRECT_MAX_REQUESTS_PER_DAY', 50, 500),
                   flag('MYBETCODE_ENABLED'), get('MYBETCODE_API_KEY').strip(),
                   bounded('MYBETCODE_MAX_REQUESTS_PER_DAY', 10, 1000),
                   bounded('MYBETCODE_MAX_REQUESTS_PER_MONTH', 100, 30000))


class BookingStore:
    """Dedicated sidecar: salted-hash result cache + per-provider request ledger.

    Never the prediction DB; refuses to attach to a file with unrelated tables.
    Survives restarts so a repeated code costs zero provider quota.
    """
    TABLES = {'booking_meta', 'booking_cache', 'booking_usage'}

    def __init__(self, path, prediction_db=None):
        self.path = Path(path).resolve()
        if prediction_db and self.path == Path(prediction_db).resolve():
            raise ValueError('booking store must be separate from the prediction database')
        with self._db() as db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables - self.TABLES:
                raise ValueError('unrelated database')
            db.execute('CREATE TABLE IF NOT EXISTS booking_meta(k TEXT PRIMARY KEY, v TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS booking_cache(key TEXT PRIMARY KEY, status TEXT NOT NULL, '
                       'payload TEXT, expires TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS booking_usage(provider TEXT, period TEXT, requests INTEGER NOT NULL, '
                       'PRIMARY KEY(provider, period))')
            db.execute('INSERT OR IGNORE INTO booking_meta VALUES(?,?)', ('salt', secrets.token_hex(16)))
            self.salt = db.execute("SELECT v FROM booking_meta WHERE k='salt'").fetchone()[0]

    def _db(self):
        return sqlite3.connect(self.path, timeout=5)

    def key(self, bookmaker, code):
        return hashlib.sha256(f'{self.salt}|{bookmaker}|{code}'.encode()).hexdigest()

    def get(self, key, now):
        try:
            with self._db() as db:
                row = db.execute('SELECT status, payload, expires FROM booking_cache WHERE key=?', (key,)).fetchone()
                if not row:
                    return None
                if instant(row[2]) is None or instant(row[2]) <= now:
                    db.execute('DELETE FROM booking_cache WHERE key=?', (key,))
                    return None
        except sqlite3.Error:
            return None
        if row[0] not in SUCCESS:
            # Retired count-based rejections must not survive an application restart.
            return row[0] if row[0] in STATUSES else None
        try:
            data = validate_payload(row[1])
            data['selections'] = tuple(DecodedLeg(**{**l, 'participants': tuple(l['participants']),
                                                      'unresolved': tuple(l['unresolved'])}) for l in data['selections'])
            data['unresolved'] = tuple(data['unresolved'])
            return replace(BookingCodeResult(**data), cached=True)
        except (ValueError, TypeError, KeyError):
            return None

    def put(self, key, result_or_status, expires):
        status = result_or_status if isinstance(result_or_status, str) else result_or_status.status
        payload = None if isinstance(result_or_status, str) else json.dumps(asdict(replace(result_or_status, cached=False)))
        try:
            with self._db() as db:
                db.execute('INSERT OR REPLACE INTO booking_cache VALUES(?,?,?,?)', (key, status, payload, expires.isoformat()))
                db.execute('DELETE FROM booking_cache WHERE expires < ?', (datetime.now(timezone.utc).isoformat(),))
        except sqlite3.Error:
            log.warning('Booking-code cache write failed')

    def reserve(self, provider, now, daily, monthly=None):
        """Reserve BEFORE HTTP; failed requests consume quota. UTC day/month."""
        day, month = now.strftime('%Y-%m-%d'), now.strftime('%Y-%m')
        try:
            with self._db() as db:
                db.execute('BEGIN IMMEDIATE')
                used_day = db.execute('SELECT COALESCE(SUM(requests),0) FROM booking_usage WHERE provider=? AND period=?',
                                      (provider, day)).fetchone()[0]
                used_month = db.execute('SELECT COALESCE(SUM(requests),0) FROM booking_usage WHERE provider=? AND period LIKE ?',
                                        (provider, month + '-%')).fetchone()[0]
                if daily <= 0 or used_day >= daily or (monthly is not None and (monthly <= 0 or used_month >= monthly)):
                    raise BookingCodeFailure(QUOTA_EXHAUSTED)
                db.execute('INSERT INTO booking_usage VALUES(?,?,1) ON CONFLICT(provider,period) '
                           'DO UPDATE SET requests=requests+1', (provider, day))
        except sqlite3.Error:
            raise BookingCodeFailure(PROVIDER_UNAVAILABLE) from None

    def used(self, provider, now):
        with self._db() as db:
            return db.execute('SELECT COALESCE(SUM(requests),0) FROM booking_usage WHERE provider=? AND period=?',
                              (provider, now.strftime('%Y-%m-%d'))).fetchone()[0]


def cache_until(result, now):
    """Positive results live until the earliest kickoff (max 6h); negatives 10 minutes."""
    if isinstance(result, str):
        return now + timedelta(minutes=10)
    kickoffs = [instant(l.kickoff) for l in result.selections if instant(l.kickoff)]
    upper = now + timedelta(hours=6)
    soonest = min(kickoffs) if kickoffs else upper
    return min(upper, soonest) if soonest > now else now + timedelta(minutes=10)


class BookingCodeService:
    """Routes (bookmaker, code) to configured resolvers with cache and single-flight.

    ``routes`` maps a bookmaker key to an ordered tuple of resolvers. Each resolver
    exposes ``source_id`` and ``retrieve(bookmaker, code, code_hash, now)`` returning
    a BookingCodeResult or raising BookingCodeFailure. One provider call per
    successful decode; the next route is tried only after a transient failure.
    """
    def __init__(self, routes, store, wait_seconds=30):
        self.routes = {k: tuple(v) for k, v in routes.items() if k in BOOKMAKERS and v}
        self.store, self.wait_seconds = store, wait_seconds
        self.flights, self.lock = {}, threading.Lock()

    def bookmakers(self):
        return tuple(k for k in BOOKMAKERS if k in self.routes)

    def resolve(self, bookmaker, code, now):
        """Never raises: always a BookingCodeResult with one fixed status."""
        now = instant(now)
        if bookmaker not in BOOKMAKERS:
            return BookingCodeResult(str(bookmaker)[:20], '', '', UNKNOWN_BOOKMAKER)
        try:
            code = normalize_code(code)
        except BookingCodeFailure as exc:
            return BookingCodeResult(bookmaker, '', '', exc.status)
        if bookmaker not in self.routes:
            return BookingCodeResult(bookmaker, masked(code), '', UNSUPPORTED_BOOKMAKER)
        key = self.store.key(bookmaker, code)
        cached = self.store.get(key, now)
        if cached is not None:
            log.info('Booking-code cache hit | bookmaker=%s | diagnostics=%s',
                     bookmaker, json.dumps(_retrieval_diagnostic(cached=True), sort_keys=True))
            return cached if not isinstance(cached, str) else BookingCodeResult(bookmaker, masked(code), key, cached, cached=True)
        with self.lock:
            future = self.flights.get(key)
            owner = future is None
            if owner:
                future = self.flights[key] = Future()
        if not owner:
            try:
                return future.result(timeout=self.wait_seconds)
            except Exception:
                return BookingCodeResult(bookmaker, masked(code), key, TIMEOUT)
        result = BookingCodeResult(bookmaker, masked(code), key, PROVIDER_UNAVAILABLE)
        try:
            # Re-check: a flight may have completed between the cache read and ownership.
            cached = self.store.get(key, now)
            if cached is not None:
                log.info('Booking-code cache hit | bookmaker=%s | diagnostics=%s',
                         bookmaker, json.dumps(_retrieval_diagnostic(cached=True), sort_keys=True))
                result = cached if not isinstance(cached, str) else BookingCodeResult(bookmaker, masked(code), key, cached, cached=True)
                return result
            for resolver in self.routes[bookmaker]:
                diagnostic = _retrieval_diagnostic(resolver)
                diagnostic_token = _DIAGNOSTIC.set(diagnostic)
                try:
                    result = resolver.retrieve(bookmaker, code, key, now)
                except BookingCodeFailure as exc:
                    if exc.status in (RESPONSE_TOO_LARGE, PARSER_LIMIT):
                        trace = exc.__traceback__
                        while trace.tb_next is not None:
                            trace = trace.tb_next
                        diagnostic.update(rejection_file=trace.tb_frame.f_code.co_filename,
                                          rejection_function=trace.tb_frame.f_code.co_name,
                                          rejection_line=trace.tb_lineno)
                    result = BookingCodeResult(bookmaker, masked(code), key, exc.status, provider=resolver.source_id)
                except Exception as exc:  # adapter bug: fail closed, never surface details
                    log.warning('Booking-code resolver failed | source=%s | error_type=%s',
                                resolver.source_id, type(exc).__name__)
                    result = BookingCodeResult(bookmaker, masked(code), key, PROVIDER_CHANGED, provider=resolver.source_id)
                finally:
                    _DIAGNOSTIC.reset(diagnostic_token)
                log.info('Booking-code retrieval | bookmaker=%s | source=%s | status=%s | diagnostics=%s',
                         bookmaker, resolver.source_id, result.status, json.dumps(diagnostic, sort_keys=True))
                if result.status in DEFINITIVE:
                    break
            if result.status in SUCCESS:
                self.store.put(key, result, cache_until(result, now))
            elif result.status in NEGATIVE_CACHE:
                self.store.put(key, result.status, cache_until(result.status, now))
            return result
        finally:
            future.set_result(result)
            with self.lock:
                self.flights.pop(key, None)


def build_booking_service(cfg, transport=None):
    """None when disabled/unconfigured. No HTTP; only opens the owned sidecar."""
    if not ticket_audit_switch.ENABLED:
        return None
    from .booking_sources import MyBetCodeResolver, SportyBetDirectResolver
    settings = BookingSettings.from_environment(getattr(cfg, 'env_file', None))
    if not settings.enabled:
        return None
    prediction = Path(cfg.db_path).resolve()
    store = BookingStore(prediction.parent / 'ticket_audit_booking.sqlite3', prediction_db=prediction)
    routes = {k: [] for k in BOOKMAKERS}
    if settings.sportybet_direct and settings.sportybet_direct_daily:
        routes['sportybet'].append(SportyBetDirectResolver(store, settings.sportybet_direct_daily, transport))
    if settings.mybetcode and settings.mybetcode_key and settings.mybetcode_daily and settings.mybetcode_monthly:
        mbc = MyBetCodeResolver(store, settings.mybetcode_key, settings.mybetcode_daily,
                                settings.mybetcode_monthly, transport)
        for bookmaker in mbc.BOOKIES:
            routes[bookmaker].append(mbc)
    service = BookingCodeService(routes, store)
    return service if service.bookmakers() else None
