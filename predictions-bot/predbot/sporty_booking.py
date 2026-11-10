"""SportyBet booking-code generation for the production Oracle batch job.

Architecture C2: read canonical Top Picks from the production database
(read-only collection queries), resolve every selection against the live
Nigerian catalogue, create exactly ONE booking code, verify the complete
readback membership, and deliver the verified code only to the bot owner's
private Telegram chat (predbot.sporty_booking_delivery).

Restored from `wip/old-pro-gates` (0b0cd9d) with production adaptations:
odds changes are ALWAYS rejected (no acceptance parameter), readback is fully
verified instead of returning `selected_membership_unverified`, a durable
per-day quota ledger reserves capacity BEFORE the single POST and is never
refunded, and competition bindings are derived only from observed catalogue
tournament names.

Never auto-retried, never partial, never synthetic: a code is disclosed only
after exact readback verification. Owner confirmation stays mandatory before
any group publication workflow is considered.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import errno
import hashlib
import json
import math
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Mapping

import requests

from predbot.markets.contracts import market_contract_reason, ambiguous_contract
from predbot.ticket_audit.booking_sources import SPORTY_MARKETS, SPORTY_SPORTS, SportyBetDirectResolver
from predbot.ticket_audit.resources import validate_tree, ResourceLimit
from predbot.util.timeutil import parse_iso

BASE = 'https://www.sportybet.com/api/ng'
MAX_BYTES = 2_000_000
CODE = re.compile(r'[A-Z0-9]{4,16}')
#: Production identification (the archived research UA is not reused).
USER_AGENT = 'Sureodds-SportyBooking/1.0'
QUOTA_TABLE = 'sporty_booking_quota'


class AnonymousAuth(requests.auth.AuthBase):
    """No-op auth hook: suppress implicit netrc without adding credentials."""
    def __call__(self, request):
        return request


class BookingError(Exception):
    """Fixed status only: never log upstream text, credentials or cookies."""
    def __init__(self, status):
        self.status = status
        super().__init__(status)


class BookingRunLock:
    """Prevent overlapping generation commands with a non-blocking OS lock."""

    def __init__(self, path):
        self.path = Path(path)
        self.handle = None

    def __enter__(self):
        try:
            self.handle = self.path.open('a+b')
            if os.name == 'nt':
                import msvcrt
                if self.path.stat().st_size == 0:
                    self.handle.write(b'\0')
                    self.handle.flush()
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if self.handle is not None:
                self.handle.close()
                self.handle = None
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise BookingError('booking_job_already_running') from None
            raise BookingError('booking_lock_unavailable') from None
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if self.handle is not None:
            try:
                if os.name == 'nt':
                    import msvcrt
                    self.handle.seek(0)
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            finally:
                self.handle.close()
                self.handle = None
        return False


def text(value):
    return ' '.join(value.split()).casefold() if isinstance(value, str) else ''


def decimal(value):
    if isinstance(value, bool):
        raise BookingError('invalid_odds')
    try:
        value = float(value)
    except (ValueError, TypeError):
        raise BookingError('invalid_odds') from None
    if not math.isfinite(value) or not 1 < value <= 10000:
        raise BookingError('invalid_odds')
    return value


@dataclass(frozen=True)
class Selection:
    canonical_event_id: str
    competition: str  # canonical competition ID, not a guessed display name
    sport: str
    home: str
    away: str
    start_utc: str
    market: str
    period: str
    stat: str
    entity: str
    selection: str
    line: float | None
    price: float
    sporty_event_id: str | None = None

    @classmethod
    def from_candidate(cls, row):
        if not isinstance(row, Mapping) or ambiguous_contract(row.get('market_name')):
            raise BookingError('invalid_candidate')
        try:
            return cls(row['canonical_event_id'], row['canonical_competition_id'], row['sport'],
                       row['home'], row['away'], row['start_utc'], row['market'], row['period'],
                       row['stat'], row['entity'], row['selection'], row.get('line'), row['price'],
                       row.get('sporty_event_id'))
        except KeyError:
            raise BookingError('missing_candidate_fields') from None


@dataclass(frozen=True)
class Resolved:
    original: Selection
    event_id: str
    tournament_id: str
    market_id: str
    specifier: str | None
    outcome_id: str
    current_odds: float

    def payload(self):
        return dict(eventId=self.event_id, marketId=self.market_id,
                    specifier=self.specifier, outcomeId=self.outcome_id)

    def identity(self):
        return (self.event_id, self.market_id, self.specifier, self.outcome_id)


@dataclass(frozen=True)
class BookingResult:
    status: str
    code: str
    selections: tuple[Resolved, ...]
    programmatic_verified: bool
    owner_verified: bool = False  # only owner confirmation can satisfy acceptance
    verification_reason: str | None = None


def payload_fingerprint(payload):
    """Stable identity of the exact POST body for the quota ledger."""
    if not isinstance(payload, dict):
        raise BookingError('invalid_fingerprint')
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()


def default_quota_path(db_path):
    """Sidecar ledger next to the prediction database (never the DB itself)."""
    return str(Path(db_path).with_name(QUOTA_TABLE + '.sqlite3'))


def load_top_picks(db, day, *, max_legs=5):
    """Canonical Top Picks for one day: active revision only, never trimmed.

    `db` is the production Database opened read-only for collection queries.
    Fails closed on empty/oversized collections or mixed revisions; canonical
    identity and the observed league name are mandatory for every row.
    """
    rows = [dict(row) for row in db.collection_predictions('top', day)]
    if not rows:
        raise BookingError('collection_empty')
    if len(rows) > max_legs:
        raise BookingError('collection_too_large')
    revisions = [row.get('revision_id') for row in rows]
    if any(type(value) is not int or value < 1 for value in revisions) \
            or len(set(revisions)) != 1:
        raise BookingError('revision_mixed')
    revision = revisions[0]
    active = db.active_ticket_revision(day)
    if type(active) is not int or active != revision:
        raise BookingError('revision_mixed')
    for row in rows:
        if not str(row.get('canonical_event_id') or '').strip() or not str(
                row.get('canonical_competition_id') or '').strip():
            raise BookingError('missing_canonical_identity')
        if not str(row.get('league') or '').strip():
            raise BookingError('binding_missing')
    return rows, revision


def derive_competition_bindings(rows, tournaments):
    """Observed bindings only: exact (normalized) league-name equality with
    exactly one catalogue tournament. Never fuzzy, never guessed, never from
    stored guesses - ambiguous or missing names fail the whole batch.
    """
    observed = {}
    for tournament in tournaments or []:
        if not isinstance(tournament, dict):
            raise BookingError('malformed_catalogue')
        tournament_id = tournament.get('id')
        name = text(tournament.get('name'))
        if isinstance(tournament_id, str) and tournament_id and name:
            observed.setdefault(name, set()).add(tournament_id)
    bindings, owner = {}, {}
    for row in rows:
        mapping = dict(row) if not isinstance(row, Mapping) else row
        competition = mapping.get('canonical_competition_id')
        league = mapping.get('league')
        if not isinstance(competition, str) or not competition.strip() \
                or not isinstance(league, str) or not league.strip():
            raise BookingError('binding_missing')
        tournament_ids = observed.get(text(league), set())
        if not tournament_ids:
            raise BookingError('binding_missing')
        if len(tournament_ids) > 1:
            raise BookingError('binding_ambiguous')
        tournament_id = next(iter(tournament_ids))
        if (competition in bindings and bindings[competition] != tournament_id) \
                or (tournament_id in owner and owner[tournament_id] != competition):
            raise BookingError('binding_ambiguous')
        bindings[competition] = tournament_id
        owner[tournament_id] = competition
    if not bindings:
        raise BookingError('binding_missing')
    return bindings


class BookingQuota:
    """Durable per-day creation ledger: reserve BEFORE the POST, never refund.

    A sidecar SQLite file with exactly one table; an unrelated database (for
    example the prediction DB) is refused outright instead of modified.
    """
    TABLES = {QUOTA_TABLE}

    def __init__(self, path, max_per_day, *, prediction_db=None):
        if type(max_per_day) is not int or not 1 <= max_per_day <= 10:
            raise BookingError('invalid_quota_limit')
        self.path = Path(path)
        if prediction_db is not None and self.path.resolve() == Path(prediction_db).resolve():
            raise BookingError('quota_db_is_prediction_db')
        self.max_per_day = max_per_day
        db = sqlite3.connect(self.path, timeout=5.0)
        try:
            with db:
                tables = {row[0] for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
                if tables - self.TABLES:
                    raise BookingError('quota_db_unrelated_tables')
                db.execute(f'CREATE TABLE IF NOT EXISTS {QUOTA_TABLE} ('
                           'claimed_at TEXT NOT NULL, day TEXT NOT NULL, '
                           'fingerprint TEXT NOT NULL)')
        except BookingError:
            raise
        except sqlite3.Error:
            raise BookingError('quota_db_unavailable') from None
        finally:
            db.close()

    def used(self, day):
        db = sqlite3.connect(self.path, timeout=5.0)
        try:
            row = db.execute(
                f'SELECT COUNT(*) FROM {QUOTA_TABLE} WHERE day=?', (day,)).fetchone()
            return int(row[0]) if row else 0
        except sqlite3.Error:
            raise BookingError('quota_db_unavailable') from None
        finally:
            db.close()

    def reserve(self, fingerprint, *, now):
        """Atomically claim a slot, rejecting exhausted and repeated payloads."""
        if not isinstance(fingerprint, str) or not fingerprint:
            raise BookingError('invalid_fingerprint')
        day = now.strftime('%Y-%m-%d')  # UTC day, same convention as ticket_audit
        db = sqlite3.connect(self.path, timeout=5.0)
        try:
            with db:
                cursor = db.execute(
                    f'INSERT INTO {QUOTA_TABLE}(claimed_at, day, fingerprint) '
                    f'SELECT ?, ?, ? WHERE (SELECT COUNT(*) FROM {QUOTA_TABLE} '
                    'WHERE day = ?) < ? AND NOT EXISTS (SELECT 1 FROM '
                    f'{QUOTA_TABLE} WHERE day = ? AND fingerprint = ?)',
                    (now.isoformat(), day, fingerprint, day, self.max_per_day,
                     day, fingerprint))
                if cursor.rowcount != 1:
                    duplicate = db.execute(
                        f'SELECT 1 FROM {QUOTA_TABLE} WHERE day=? AND fingerprint=? LIMIT 1',
                        (day, fingerprint)).fetchone()
                    raise BookingError(
                        'duplicate_payload_do_not_retry' if duplicate else 'quota_exhausted')
        except BookingError:
            raise
        except sqlite3.Error:
            raise BookingError('quota_db_unavailable') from None
        finally:
            db.close()


class PublicTransport:
    """Bounded anonymous calls; fixed routes, no cookies/auth, no redirects/retries."""
    def __init__(self, limit=12, total_timeout=30):
        if (isinstance(total_timeout,bool) or not isinstance(total_timeout,(int,float))
                or not math.isfinite(total_timeout) or not 0<total_timeout<=120):
            raise BookingError('invalid_timeout')
        self.limit, self.used, self.last_call = limit, 0, 0.
        self.deadline = time.monotonic() + total_timeout

    def request(self, method, path, *, params=None, payload=None):
        if not ((method == 'GET' and path == '/factsCenter/pcUpcomingEvents') or
                (method == 'GET' and re.fullmatch(r'/orders/share/[A-Z0-9]{4,16}', path)) or
                (method == 'POST' and path == '/orders/share')):
            raise BookingError('forbidden_route')
        if self.used >= self.limit:
            raise BookingError('request_budget_exhausted')
        if time.monotonic() >= self.deadline:
            raise BookingError('operation_timeout')
        wait = .5 - (time.monotonic() - self.last_call)
        if wait > 0:
            time.sleep(wait)
        self.used += 1
        self.last_call = time.monotonic()
        started = time.monotonic()
        remaining = self.deadline - started
        if remaining <= 0:
            raise BookingError('operation_timeout')
        try:
            # Fresh session per call: no session credentials or persistent jar.
            with requests.Session() as session:
                session.auth = AnonymousAuth()  # prevent implicit .netrc credentials
                with session.request(method, BASE + path, params=params, json=payload,
                        headers={'Accept':'application/json', 'Content-Type':'application/json',
                                 'Current-Country':'NG', 'User-Agent':USER_AGENT},
                        timeout=(min(4,remaining), min(8,remaining)), stream=True, allow_redirects=False) as response:
                    if response.status_code in (401, 403):
                        raise BookingError('access_blocked')
                    if response.status_code == 429:
                        raise BookingError('rate_limited')
                    if response.status_code != 200:
                        raise BookingError('http_error')
                    if 'json' not in response.headers.get('Content-Type','').lower():
                        raise BookingError('non_json_response')
                    body = bytearray()
                    for part in response.iter_content(8192):
                        body.extend(part)
                        if len(body) > MAX_BYTES:
                            raise BookingError('response_too_large')
                        if time.monotonic() - started > 15:
                            raise BookingError('timeout')
                        if time.monotonic() >= self.deadline:
                            raise BookingError('operation_timeout')
                    data = json.loads(body)
                    validate_tree(data)
                    return data
        except BookingError:
            raise
        except requests.Timeout:
            raise BookingError('timeout') from None
        except (requests.RequestException, OSError):
            raise BookingError('network_unavailable') from None
        except (ValueError, TypeError, RecursionError, ResourceLimit):
            raise BookingError('malformed_response') from None


def envelope(data):
    if not isinstance(data, dict) or data.get('bizCode') != 10000 or not isinstance(data.get('data'), dict):
        raise BookingError('provider_rejected')
    if '_provenance' in data:  # repository reconstruction is NOT an observed live response
        raise BookingError('reconstructed_response_not_live_evidence')
    return data['data']


class SportyBooking:
    """Separate adapter; competition_bindings are observed canonical -> tournament IDs.

    No fuzzy fixture/team/competition matching. Existing Sporty event IDs are
    authoritative constraints, not hints; a mismatch never falls back to names.
    """
    def __init__(self, competition_bindings, *, transport=None, clock=None, max_pages=2):
        self.bindings = dict(competition_bindings)
        self.transport = transport or PublicTransport()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.max_pages = max_pages
        if type(max_pages) is not int or not 1 <= max_pages <= 5:
            raise BookingError('invalid_page_budget')

    def validate_selections(self, selections, *, require_bindings=True):
        if not isinstance(selections, (list, tuple)) or not 1 <= len(selections) <= 5:
            raise BookingError('invalid_slip_size')
        seen = set()
        for s in selections:
            if not isinstance(s, Selection):
                raise BookingError('invalid_candidate')
            if any(not isinstance(v,str) or not v.strip() for v in (
                    s.canonical_event_id,s.competition,s.home,s.away,s.start_utc)):
                raise BookingError('missing_fixture_identity')
            if text(s.home) == text(s.away):
                raise BookingError('invalid_participants')
            start = parse_iso(s.start_utc)
            if start is None or start <= self.clock():
                raise BookingError('expired_fixture')
            decimal(s.price)
            why = market_contract_reason(s.sport,s.market,s.selection,s.period,s.stat,s.entity,s.line)
            if why:
                raise BookingError('invalid_contract_' + why)
            if not self.market_ids(s):
                raise BookingError('unsupported_sporty_market')
            if require_bindings and (not isinstance(self.bindings.get(s.competition), str)
                                     or not self.bindings[s.competition]):
                raise BookingError('unverified_competition_binding')
            if s.sporty_event_id is not None and (not isinstance(s.sporty_event_id,str) or not re.fullmatch(r'sr:match:\d+',s.sporty_event_id)):
                raise BookingError('invalid_sporty_event_id')
            key = (s.sport,s.competition,text(s.home),text(s.away),start)
            if key in seen or s.canonical_event_id in seen:
                raise BookingError('duplicate_or_correlated_fixture')
            seen.add(key); seen.add(s.canonical_event_id)
        return tuple(selections)

    @staticmethod
    def market_ids(s):
        # Reuse existing sport-aware decoder IDs, never label/fuzzy guesses.
        if s.entity != 'all' or s.stat != 'points':
            return []
        return [mid for mid,(family,period,_,_,sports) in SPORTY_MARKETS.items()
                if s.sport in sports and s.market == family and s.period == period]

    def fetch_catalogue(self, selections):
        """(tournaments, events) from the Nigerian catalogue; football only,
        bounded pages. `tournaments` feeds observed competition bindings."""
        if any(s.sport != 'football' for s in selections):
            raise BookingError('live_sport_acquisition_unverified')
        tournaments, events = [], []
        for sport in sorted({s.sport for s in selections}):
            sid = next(k for k,v in SPORTY_SPORTS.items() if v == sport)
            ids = sorted({m for s in selections if s.sport==sport for m in self.market_ids(s)})
            for page in range(1,self.max_pages+1):
                data = self.transport.request('GET','/factsCenter/pcUpcomingEvents', params={
                    'sportId':sid,'marketId':','.join(ids),'pageSize':100,'pageNum':page,
                    'todayGames':'false','timeline':72})
                body = envelope(data)
                page_tournaments = body.get('tournaments')
                if not isinstance(page_tournaments,list):
                    raise BookingError('malformed_catalogue')
                count = 0
                for t in page_tournaments:
                    if not isinstance(t,dict) or not isinstance(t.get('events'),list):
                        raise BookingError('malformed_catalogue')
                    tournaments.append(t)
                    for event in t['events']:
                        if not isinstance(event,dict):
                            raise BookingError('malformed_catalogue')
                        events.append((sport,str(t.get('id') or ''),event))
                        count += 1
                if count < 100:
                    break
        return tournaments, events

    def _events(self, selections):
        # The researched acquisition implementation is football-only. Existing
        # decoder IDs for other sports do not prove this endpoint accepts them.
        return self.fetch_catalogue(selections)[1]

    def resolve_selections(self, selections, events):
        selections = self.validate_selections(selections)
        resolved = []
        for s in selections:
            matches = []
            for sport,tournament,event in events:
                if sport != s.sport or tournament != self.bindings[s.competition]:
                    continue
                eid = event.get('eventId')
                if s.sporty_event_id and eid != s.sporty_event_id:
                    continue
                # IDs never override contradictory participants/kickoff.
                if text(event.get('homeTeamName')) != text(s.home) or text(event.get('awayTeamName')) != text(s.away):
                    continue
                start = event.get('estimateStartTime')
                if type(start) is not int or start != int(parse_iso(s.start_utc).timestamp()*1000):
                    continue
                if not isinstance(eid,str) or not re.fullmatch(r'sr:match:\d+',eid):
                    continue
                matches.append(event)
            if len(matches) != 1:
                raise BookingError('fixture_missing_or_ambiguous')
            event = matches[0]
            if event.get('matchStatus') != 'Not start':
                # Exact observed status vocabulary must be verified. Missing or
                # unknown statuses cannot prove a market remains pregame.
                raise BookingError('fixture_status_unverified')
            choices = []
            markets = event.get('markets')
            if not isinstance(markets,list):
                raise BookingError('missing_market_identifiers')
            for m in markets:
                if not isinstance(m,dict) or str(m.get('id')) not in self.market_ids(s) or type(m.get('status')) is not int or m['status'] != 0:
                    continue
                outcomes = m.get('outcomes')
                if not isinstance(outcomes,list):
                    continue
                for o in outcomes:
                    if not isinstance(o,dict) or type(o.get('isActive')) is not int or o['isActive'] != 1 or not str(o.get('id') or ''):
                        continue
                    mid = str(m['id'])
                    _, _, spec_key, sides, _ = SPORTY_MARKETS[mid]
                    # Decoder label fallbacks are useful for audits, but cannot
                    # authorize a write with missing or extra specifier semantics.
                    observed_specifier = m.get('specifier')
                    if observed_specifier is not None and not isinstance(observed_specifier, str):
                        continue
                    spec = observed_specifier or None
                    if spec_key:
                        if not isinstance(spec,str) or not re.fullmatch(
                                re.escape(spec_key)+r'=[+-]?\d+(?:\.\d+)?',spec):
                            continue
                    elif spec is not None:
                        continue
                    if str(o['id']) not in sides:
                        continue
                    leg = SportyBetDirectResolver._leg(dict(sport=s.sport,home=s.home,away=s.away),m,o)
                    side = {'1x':'home_draw','x2':'away_draw','12':'home_away'}.get(leg.outcome,leg.outcome)
                    if (leg.market,leg.period,leg.stat,leg.entity,side,leg.line) != (s.market,s.period,s.stat,s.entity,s.selection,s.line):
                        continue
                    choices.append(Resolved(s,event['eventId'],self.bindings[s.competition],str(m['id']),
                                            spec,str(o['id']),decimal(o.get('odds'))))
            if len(choices) != 1:
                raise BookingError('market_missing_or_ambiguous')
            resolved.append(choices[0])
        if len({r.event_id for r in resolved}) != len(resolved):
            raise BookingError('duplicate_or_correlated_fixture')
        return tuple(resolved)

    def prepare_creation(self, selections, *, events=None):
        """Validate, resolve and gate odds BEFORE any quota/POST side effect."""
        original = self.validate_selections(selections)
        resolved = self.resolve_selections(
            original, self._events(original) if events is None else events)
        # Strict odds gate: engine price must equal the live quote exactly.
        # There is deliberately no acceptance path.
        if any(r.current_odds != r.original.price for r in resolved):
            raise BookingError('odds_changed')
        # Recheck time after catalogue requests. Never POST a partial slip.
        self.validate_selections(original)
        return tuple(resolved)

    def create_booking_code(self, selections, *, quota, events=None):
        """The only writable call. `quota` is REQUIRED and reserved BEFORE the
        POST; a reservation is never refunded on any later failure. Full
        readback verification must pass or a fixed status is raised - the
        code is never disclosed unverified and never retried."""
        if not isinstance(quota, BookingQuota):
            raise BookingError('quota_required')
        resolved = self.prepare_creation(selections, events=events)
        payload = {'selections': [r.payload() for r in resolved]}
        quota.reserve(payload_fingerprint(payload), now=self.clock())
        try:
            body = envelope(self.transport.request('POST','/orders/share',payload=payload))
        except BookingError:
            raise BookingError('creation_outcome_unverified_do_not_retry') from None
        code = body.get('shareCode')
        if not isinstance(code,str) or not CODE.fullmatch(code):
            raise BookingError('missing_or_invalid_returned_code')
        try:
            verified = self.verify_booking_code(code, resolved)
        except BookingError as exc:
            if exc.status.startswith('readback_'):
                raise
            raise BookingError('readback_failed') from None
        except (ValueError, TypeError, KeyError, AttributeError, IndexError):
            raise BookingError('readback_verification_failed') from None
        return BookingResult('verified', code, verified, True,
                             verification_reason='exact_membership_verified')

    def get_booking_status(self, code):
        if not isinstance(code,str) or not CODE.fullmatch(code):
            raise BookingError('invalid_booking_code')
        return envelope(self.transport.request('GET','/orders/share/'+code))

    def verify_booking_code(self, code, expected):
        """Full readback verification: the booked code must contain EXACTLY the
        expected selections with matching fixtures, competition, kickoff,
        pregame status, market/specifier, active outcome, odds and sport - and
        re-resolving the readback event must reproduce the identical payload.
        Any mismatch raises a fixed readback_* status; nothing is disclosed."""
        if (not expected or not all(isinstance(r, Resolved) for r in expected)
                or len({r.identity() for r in expected}) != len(expected)):
            raise BookingError('expected_manifest_required')
        body = self.get_booking_status(code)
        if body.get('shareCode') != code:
            raise BookingError('readback_code_mismatch')
        if body.get('unavailableOutcomes') != []:
            raise BookingError('readback_selection_unavailable')
        deadline = body.get('deadline')
        if type(deadline) is not int or deadline <= int(self.clock().timestamp()*1000):
            raise BookingError('readback_expired')
        ticket = body.get('ticket')
        selections = ticket.get('selections') if isinstance(ticket, dict) else None
        if not isinstance(selections, list):
            raise BookingError('readback_membership_mismatch')

        def coord(s):
            if not isinstance(s, dict):
                raise BookingError('readback_membership_mismatch')
            event_id, market_id, specifier, outcome_id = (
                s.get('eventId'), s.get('marketId'), s.get('specifier'), s.get('outcomeId'))
            if (not all(isinstance(value, str) and value for value in
                        (event_id, market_id, outcome_id))
                    or (specifier is not None and not isinstance(specifier, str))):
                raise BookingError('readback_membership_mismatch')
            return (event_id, market_id, specifier, outcome_id)

        got = [coord(s) for s in selections]
        want = [r.identity() for r in expected]
        if Counter(got) != Counter(want):
            raise BookingError('readback_membership_mismatch')
        by_coord = {r.identity(): r for r in expected}
        sport_ids = {name: sid for sid, name in SPORTY_SPORTS.items()}
        for s in selections:
            if s.get('parentBetBuilderMarketId'):
                raise BookingError('readback_parent_market')
            resolved_leg = by_coord[coord(s)]
            want_sid = sport_ids.get(resolved_leg.original.sport)
            if want_sid is None or s.get('sportId') != want_sid:
                raise BookingError('readback_sport_mismatch')
        outcomes = body.get('outcomes')
        if not isinstance(outcomes, list):
            raise BookingError('readback_leg_mismatch')
        for r in expected:
            events = [e for e in outcomes if isinstance(e, dict) and e.get('eventId') == r.event_id]
            if len(events) != 1:
                raise BookingError('readback_leg_mismatch')
            event = events[0]
            if (text(event.get('homeTeamName')) != text(r.original.home)
                    or text(event.get('awayTeamName')) != text(r.original.away)):
                raise BookingError('readback_teams_mismatch')
            start = parse_iso(r.original.start_utc)
            seen_start = event.get('estimateStartTime')
            if start is None or type(seen_start) is not int or seen_start != int(start.timestamp()*1000):
                raise BookingError('readback_kickoff_mismatch')
            if event.get('matchStatus') != 'Not start':
                raise BookingError('readback_status_unverified')
            sport = event.get('sport')
            category = sport.get('category') if isinstance(sport, dict) else None
            tournament = category.get('tournament') if isinstance(category, dict) else None
            if not isinstance(sport, dict) or sport.get('id') != sport_ids.get(r.original.sport) \
                    or not isinstance(tournament, dict) or tournament.get('id') != r.tournament_id:
                raise BookingError('readback_tournament_mismatch')
            markets = event.get('markets')
            matched = [m for m in markets if isinstance(m, dict)
                       and m.get('id') == r.market_id
                       and (m.get('specifier') or None) == r.specifier] \
                if isinstance(markets, list) else []
            if len(matched) != 1:
                raise BookingError('readback_market_mismatch')
            market = matched[0]
            if type(market.get('status')) is not int or market['status'] != 0:
                raise BookingError('readback_market_mismatch')
            outs = market.get('outcomes')
            matched_outs = [o for o in outs if isinstance(o, dict) and o.get('id') == r.outcome_id] \
                if isinstance(outs, list) else []
            if len(matched_outs) != 1:
                raise BookingError('readback_outcome_mismatch')
            outcome = matched_outs[0]
            if type(outcome.get('isActive')) is not int or outcome['isActive'] != 1:
                raise BookingError('readback_outcome_mismatch')
            try:
                odds = decimal(outcome.get('odds'))
            except BookingError:
                raise BookingError('readback_outcome_mismatch') from None
            if odds != r.current_odds:
                raise BookingError('readback_odds_mismatch')
            # Re-run the exact resolver against the readback event: the
            # reconstructed payload must be identical to what we booked.
            row = asdict(r.original)
            row['canonical_competition_id'] = row.pop('competition')
            resolver = SportyBooking({r.original.competition: r.tournament_id},
                                     transport=self.transport, clock=self.clock)
            again, = resolver.resolve_selections(
                [Selection.from_candidate(row)], [('football', r.tournament_id, event)])
            if again.payload() != r.payload() or again.current_odds != r.current_odds:
                raise BookingError('readback_resolver_mismatch')
        return tuple(expected)
