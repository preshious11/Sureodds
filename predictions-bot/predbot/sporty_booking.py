"""Isolated, experimental Nigeria share-code adapter. No betting or Telegram.

Undocumented routes evidenced by the public reference in SPORTYBET_BOOKING_PHASE1.md.
Only orders/share is writable, never auto-retried. Creation always re-reads live
quotes, then attempts readback; selected membership remains unverified until a
genuine response establishes its semantics. Owner confirmation is mandatory.
No synthetic code or source-capture response is accepted as live evidence.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import re
import time
from typing import Mapping

import requests

from predbot.markets.contracts import market_contract_reason, ambiguous_contract
from predbot.ticket_audit.booking_sources import SPORTY_MARKETS, SPORTY_SPORTS, SportyBetDirectResolver
from predbot.ticket_audit.resources import validate_tree, ResourceLimit
from predbot.util.timeutil import parse_iso

BASE = 'https://www.sportybet.com/api/ng'
MAX_BYTES = 2_000_000
CODE = re.compile(r'[A-Z0-9]{4,16}')


class AnonymousAuth(requests.auth.AuthBase):
    """No-op auth hook: suppress implicit netrc without adding credentials."""
    def __call__(self, request):
        return request


class BookingError(Exception):
    """Fixed status only: never log upstream text, credentials or cookies."""
    def __init__(self, status):
        self.status = status
        super().__init__(status)


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
        return (self.event_id, self.market_id, self.specifier or '', self.outcome_id)


@dataclass(frozen=True)
class BookingResult:
    status: str
    code: str
    selections: tuple[Resolved, ...]
    programmatic_verified: bool
    owner_verified: bool = False  # only owner confirmation can satisfy acceptance
    verification_reason: str | None = None


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
                                 'Current-Country':'NG', 'User-Agent':'Kovqi-Booking-Research/1.0'},
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

    def validate_selections(self, selections):
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
            if not isinstance(self.bindings.get(s.competition), str) or not self.bindings[s.competition]:
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

    def _events(self, selections):
        # The researched acquisition implementation is football-only. Existing
        # decoder IDs for other sports do not prove this endpoint accepts them.
        if any(s.sport != 'football' for s in selections):
            raise BookingError('live_sport_acquisition_unverified')
        events = []
        for sport in sorted({s.sport for s in selections}):
            sid = next(k for k,v in SPORTY_SPORTS.items() if v == sport)
            ids = sorted({m for s in selections if s.sport==sport for m in self.market_ids(s)})
            for page in range(1,self.max_pages+1):
                data = self.transport.request('GET','/factsCenter/pcUpcomingEvents', params={
                    'sportId':sid,'marketId':','.join(ids),'pageSize':100,'pageNum':page,
                    'todayGames':'false','timeline':72})
                body = envelope(data)
                tournaments = body.get('tournaments')
                if not isinstance(tournaments,list):
                    raise BookingError('malformed_catalogue')
                count = 0
                for t in tournaments:
                    if not isinstance(t,dict) or not isinstance(t.get('events'),list):
                        raise BookingError('malformed_catalogue')
                    for event in t['events']:
                        if not isinstance(event,dict):
                            raise BookingError('malformed_catalogue')
                        events.append((sport,str(t.get('id') or ''),event))
                        count += 1
                if count < 100:
                    break
        return events

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
                    spec = m.get('specifier') or None
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

    def create_booking_code(self, selections, *, accept_odds_changes=False):
        original = self.validate_selections(selections)
        resolved = self.resolve_selections(original,self._events(original))
        if any(r.current_odds != r.original.price for r in resolved) and not accept_odds_changes:
            raise BookingError('odds_changed')
        # Recheck time after catalogue requests. Never POST a partial slip.
        self.validate_selections(original)
        try:
            body = envelope(self.transport.request('POST','/orders/share',payload={
                'selections':[r.payload() for r in resolved]}))
        except BookingError as exc:
            if exc.status in ('timeout','network_unavailable','operation_timeout'):
                raise BookingError('creation_unknown_do_not_retry') from None
            raise
        code = body.get('shareCode')
        if not isinstance(code,str) or not CODE.fullmatch(code):
            raise BookingError('missing_or_invalid_returned_code')
        try:
            verified = self.verify_booking_code(code,resolved)
        except BookingError as exc:
            return BookingResult('created_manual_verification_required',code,resolved,False,
                                 verification_reason=exc.status)
        except (ValueError, TypeError, KeyError, AttributeError):
            return BookingResult('created_manual_verification_required',code,resolved,False,
                                 verification_reason='malformed_readback')
        return BookingResult('owner_confirmation_required',code,verified,True)

    def get_booking_status(self, code):
        if not isinstance(code,str) or not CODE.fullmatch(code):
            raise BookingError('invalid_booking_code')
        return envelope(self.transport.request('GET','/orders/share/'+code))

    def verify_booking_code(self, code, expected):
        if not expected or not all(isinstance(r,Resolved) for r in expected):
            raise BookingError('expected_manifest_required')
        body = self.get_booking_status(code)
        if body.get('shareCode') != code or body.get('unavailableOutcomes') not in (None,[]):
            raise BookingError('slip_unavailable_or_code_mismatch')
        deadline = body.get('deadline')
        if type(deadline) is not int or deadline <= int(self.clock().timestamp()*1000):
            raise BookingError('booking_expired_or_unknown')
        # Public references describe ticket as an opaque string. No real
        # captured response proves which nested outcomes are selected rather
        # than available. Do not invent a ticket schema or equate isActive with
        # membership. Full programmatic verification needs a genuine capture.
        raise BookingError('selected_membership_unverified')
