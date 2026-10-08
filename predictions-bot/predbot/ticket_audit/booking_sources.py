"""Retrieval-only booking-code adapters. Fixed hosts, GET only, no cookies/credentials.

Provider JSON never leaves this module: adapters return DecodedLeg/BookingCodeResult.
No retries, redirects, conversion, booking creation or bet placement. The SportyBet
route is an UNDOCUMENTED web endpoint: experimental and disabled by default.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import re
import time
from urllib.parse import quote, urlencode, urlsplit

import requests
from predbot import ticket_audit_switch

from .booking import (BookingCodeFailure, DecodedLeg, EMPTY, EXPIRED, NOT_FOUND, PROVIDER_CHANGED,
                      PROVIDER_UNAVAILABLE, QUOTA_EXHAUSTED, TIMEOUT, RESPONSE_TOO_LARGE, PARSER_LIMIT, UNSUPPORTED_BOOKMAKER,
                      MALFORMED_CODE, DISABLED, CODE, finalize, masked, note_booking)
from .schemas import instant
from .resources import ResourceLimit, validate_tree

MAX_BODY = 512_000
WALL_SECONDS = 15
TIMEOUT_PAIR = (4, 8)


def _text(value, limit=150):
    """Bounded provider string or None. Data only; never interpreted."""
    if not isinstance(value, str):
        return None
    value = ' '.join(value.split())
    return value[:limit] if value else None


def _odds(value):
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n if math.isfinite(n) and 1 < n <= 10000 else None


def bookmaker_total_odds(value):
    """Bookmaker-supplied combined odds, or None. Never computed from legs.

    Accepts 12.5, '12.50' or '12,345.67' (comma grouping only with a decimal part, so a
    European decimal comma such as '1,850' is rejected rather than misread).
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        n = float(value)
    elif isinstance(value, str) and re.fullmatch(r'\d{1,3}(?:,\d{3})+\.\d{1,4}|\d{1,12}(?:\.\d{1,4})?',
                                                 value.strip()):
        n = float(value.strip().replace(',', ''))
    else:
        return None
    return n if math.isfinite(n) and 1 < n <= 1e12 else None


def _line(value):
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n if math.isfinite(n) and abs(n) <= 1000 else None


def fetch(transport, url, host, path, headers=None, diagnostic=None):
    """One bounded GET to an allowlisted host/path. Returns (status, parsed JSON or None).

    The caller supplies an already-validated code; this re-verifies the final URL so
    no input can alter scheme, host or route. Redirects are never followed.
    """
    if not ticket_audit_switch.ENABLED:
        raise BookingCodeFailure(DISABLED)
    parts = urlsplit(url)
    if parts.scheme != 'https' or parts.netloc != host or not re.fullmatch(path, parts.path) or parts.fragment:
        raise BookingCodeFailure(MALFORMED_CODE)
    response = None
    started = time.monotonic()
    try:
        note_booking(stage='provider_http', http_attempted=True)
        response = transport.get(url, timeout=TIMEOUT_PAIR, allow_redirects=False, stream=True,
                                 headers={'Accept': 'application/json', 'User-Agent': 'Sureodds-TicketAudit/1.0',
                                          **(headers or {})})
        status = response.status_code
        note_booking(http_status=status)
        declared = response.headers.get('Content-Length') if hasattr(response, 'headers') else None
        if declared is not None and str(declared).isdigit() and int(declared) > MAX_BODY:
            raise BookingCodeFailure(RESPONSE_TOO_LARGE)
        body = bytearray()
        for chunk in response.iter_content(chunk_size=4096):
            if time.monotonic() - started > WALL_SECONDS:
                raise BookingCodeFailure(TIMEOUT)
            body.extend(chunk or b'')
            if len(body) > MAX_BODY:
                raise BookingCodeFailure(RESPONSE_TOO_LARGE)
        ctype = str((response.headers or {}).get('Content-Type', '')) if hasattr(response, 'headers') else ''
        try:
            data = json.loads(bytes(body)) if body else None
        except ValueError:
            data = None
        except RecursionError:
            raise BookingCodeFailure(PARSER_LIMIT) from None
        try:
            validate_tree(data)
        except ResourceLimit:
            raise BookingCodeFailure(PARSER_LIMIT) from None
        if diagnostic is not None:
            diagnostic.update(http_status=status, json=data is not None, content_type=ctype.split(';')[0][:60],
                              bytes=len(body), redirect=300 <= status < 400)
        return status, data
    except BookingCodeFailure:
        raise
    except requests.Timeout:
        raise BookingCodeFailure(TIMEOUT) from None
    except (requests.RequestException, OSError, ValueError, TypeError, AttributeError):
        raise BookingCodeFailure(PROVIDER_UNAVAILABLE) from None
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:
                pass


def _odds_paths(value, path='', depth=0, out=None):
    out = [] if out is None else out
    if depth > 4 or len(out) >= 20:
        return out
    if isinstance(value, dict):
        for k, v in list(value.items())[:50]:
            name = f'{path}.{str(k)[:40]}' if path else str(k)[:40]
            if 'odd' in str(k).casefold() and not isinstance(v, (dict, list)):
                out.append(name)
            _odds_paths(v, name, depth + 1, out)
    elif isinstance(value, list) and value:
        _odds_paths(value[0], path + '[0]', depth + 1, out)
    return out


def _common_status(status):
    if 300 <= status < 400:
        return PROVIDER_UNAVAILABLE  # never follow; a redirect is a change or a block
    if status == 429:
        return QUOTA_EXHAUSTED
    if status in (401, 403) or status >= 500:
        return PROVIDER_UNAVAILABLE
    return None


def _spec(raw):
    """'total=2.5|variant=x' -> {'total': '2.5', ...}; malformed -> {}."""
    out = {}
    if isinstance(raw, str) and len(raw) <= 200:
        for part in raw.split('|'):
            k, sep, v = part.partition('=')
            if sep:
                out[k.strip()] = v.strip()
    return out


# Sportradar-style ids used by SportyBet. Unknown ids stay unresolved; never guessed.
SPORTY_SPORTS = {'sr:sport:1': 'football', 'sr:sport:2': 'basketball', 'sr:sport:5': 'tennis',
                 'sr:sport:3': 'baseball', 'sr:sport:4': 'hockey', 'sr:sport:16': 'american_football',
                 'sr:sport:117': 'mma', 'sr:sport:23': 'volleyball', 'sr:sport:6': 'handball'}
# Canonical vocabulary is the project's own (MarketLine/LocalResolver): the main game
# score is stat='points' (football goals included), the whole match is entity='all',
# halves are period '1h'/'2h'; football regulation time is 'full'. Local evidence is
# matched EXACTLY on these values, so synonyms such as 'goals'/'match' would break it.
#
# market id -> (family, period, specifier key or None, outcome id -> canonical side, sports)
_R3 = {'1': 'home', '2': 'draw', '3': 'away'}
_R2 = {'4': 'home', '5': 'away'}
_OU = {'12': 'over', '13': 'under'}
_AH = {'1714': 'home', '1715': 'away'}
FOOTBALL, TENNIS, BASKETBALL = frozenset({'football'}), frozenset({'tennis'}), frozenset({'basketball'})
SPORTY_MARKETS = {
    '1': ('result3', 'full', None, _R3, FOOTBALL),
    '60': ('result3', '1h', None, _R3, FOOTBALL), '83': ('result3', '2h', None, _R3, FOOTBALL),
    '10': ('dc', 'full', None, {'9': '1x', '10': '12', '11': 'x2'}, FOOTBALL),
    '11': ('dnb', 'full', None, _R2, FOOTBALL),
    '18': ('total', 'full', 'total', _OU, FOOTBALL),
    '68': ('total', '1h', 'total', _OU, FOOTBALL), '90': ('total', '2h', 'total', _OU, FOOTBALL),
    '16': ('handicap', 'full', 'hcp', _AH, FOOTBALL),
    '29': ('btts', 'full', None, {'74': 'yes', '76': 'no'}, FOOTBALL),
    '186': ('result2', 'full', None, _R2, TENNIS),               # Winner
    '219': ('result2', 'full', None, _R2, BASKETBALL),           # Winner incl. overtime
    '225': ('total', 'full', 'total', _OU, BASKETBALL),          # Over/Under incl. overtime
    '223': ('handicap', 'full', 'hcp', _AH, BASKETBALL),         # Handicap incl. overtime
}
# Label fallback for an unknown/absent id, general for any fixture: '[1st|2nd Half - ]Over/Under',
# '[1st|2nd Half - ]1X2', tennis 'Winner'. An id and a label that disagree are a conflict.
_HALF = r'(?:(?P<half>1st|2nd|first|second)\s+half\s*[-–:]?\s*)?'
LABEL_RULES = (
    (re.compile(r'^' + _HALF + r'(?:over\s*/\s*under|total goals|total)$', re.I), 'total', FOOTBALL),
    (re.compile(r'^' + _HALF + r'1\s*x\s*2$', re.I), 'result3', FOOTBALL),
    (re.compile(r'^(?:match\s+)?winner$', re.I), 'result2', TENNIS),
)
HALVES = {None: 'full', '1st': '1h', 'first': '1h', '2nd': '2h', 'second': '2h'}
SIDES = {'result3': {'home', 'draw', 'away'}, 'result2': {'home', 'away'}, 'total': {'over', 'under'}}
OU_OUTCOME = re.compile(r'^(over|under)(?:\s+([0-9]{1,3}(?:\.[0-9]{1,2})?))?$', re.I)


def _label_rule(desc):
    for pattern, family, sports in LABEL_RULES:
        m = pattern.match(desc or '')
        if m:
            half = m.groupdict().get('half')
            if family == 'result2' and half:
                return None
            return family, HALVES[half.casefold() if half else None], sports
    return None


def _side_from_label(family, label, home, away):
    """Outcome label -> canonical side, or None if it does not unambiguously name one."""
    text = ' '.join((label or '').split()).casefold()
    if not text:
        return None
    if family == 'total':
        m = OU_OUTCOME.match(text)
        return m.group(1).casefold() if m else None
    names = {'home': {'home', '1'}, 'away': {'away', '2'}, 'draw': {'draw', 'x'}}
    if home and away and home.casefold() != away.casefold():
        names['home'].add(home.casefold())
        names['away'].add(away.casefold())
    hits = [side for side, words in names.items() if text in words and side in SIDES.get(family, ())]
    return hits[0] if len(hits) == 1 else None


def selected_participant(family, side, home, away):
    """Deterministic name of the selected participant for two-sided markets; else None."""
    if family in ('result3', 'result2', 'dnb', 'handicap') and side in ('home', 'away'):
        return home if side == 'home' else away
    return None


def _sportybet_structure_counts(body):
    """Structural diagnostics only, NOT evidence of ticket membership.

    Inspect fixed fields without logging their values or interpreting activity as
    selection. The bounded HTTP body also bounds this linear traversal. Neither
    ticket reference counts nor selected flags are used to filter actual legs:
    their relationship to the shared ticket has not been verified.
    """
    events = body['outcomes']
    market_count = outcome_count = selected_flags = 0
    for event in events:
        markets = event.get('markets') if isinstance(event, dict) else None
        for market in markets if isinstance(markets, list) else ():
            market_count += 1
            outcomes = market.get('outcomes') if isinstance(market, dict) else None
            if isinstance(outcomes, list):
                outcome_count += len(outcomes)
                selected_flags += sum(isinstance(o, dict) and o.get('isSelected') is True
                                      for o in outcomes)
    ticket = body.get('ticket')
    ticket = ticket if isinstance(ticket, dict) else {}

    def count(field):
        rows = ticket.get(field)
        return len(rows) if isinstance(rows, list) else None

    def references(field):
        rows = ticket.get(field)
        if not isinstance(rows, list):
            return None
        return sum(isinstance(row, dict) and all(
            type(row.get(key)) in (str, int) and row[key] != ''
            for key in ('eventId', 'marketId', 'outcomeId')) for row in rows)

    unavailable = body.get('unavailableOutcomes', [])
    return dict(raw_outcome_count=len(events), raw_event_count=len(events),
                raw_market_count=market_count, raw_nested_outcome_count=outcome_count,
                ticket_bet_count=count('bets'), ticket_selection_count=count('selections'),
                ticket_complete_reference_count=references('selections'),
                ticket_bet_complete_reference_count=references('bets'),
                unavailable_outcome_count=len(unavailable) if isinstance(unavailable, list) else None,
                explicit_selected_flag_count=selected_flags,
                selected_selection_count=None, unavailable_selected_count=None,
                raw_selection_count=outcome_count)


class SportyBetDirectResolver:
    """EXPERIMENTAL: SportyBet's undocumented share-code GET. Anonymous, no cookies.

    Never a hard dependency: any block, redirect, HTML or unexpected envelope fails
    closed to PROVIDER_UNAVAILABLE/PROVIDER_CHANGED, letting routing fall back.
    """
    source_id = 'sportybet-direct-experimental'
    HOST = 'www.sportybet.com'
    PATH = r'/api/ng/orders/share/[A-Z0-9]{4,16}'

    def __init__(self, store, daily, transport=None):
        self.store, self.daily = store, daily
        # A fresh Session per request: no cookie jar persists between lookups.
        self.transport = transport
        self.last_diagnostic = {}

    def url(self, code):
        if not CODE.fullmatch(code):
            raise BookingCodeFailure(MALFORMED_CODE)
        return f'https://{self.HOST}/api/ng/orders/share/{quote(code, safe="")}'

    def retrieve(self, bookmaker, code, code_hash, now):
        if not ticket_audit_switch.ENABLED:
            raise BookingCodeFailure(DISABLED)
        if bookmaker != 'sportybet':
            raise BookingCodeFailure(UNSUPPORTED_BOOKMAKER)
        url = self.url(code)
        self.store.reserve(self.source_id, now, self.daily)
        self.last_diagnostic = diagnostic = {'source': self.source_id}
        transport = self.transport or requests.Session()
        try:
            status, data = fetch(transport, url, self.HOST, self.PATH, diagnostic=diagnostic)
        finally:
            if self.transport is None:
                transport.close()
        common = _common_status(status)
        if common:
            raise BookingCodeFailure(common)
        if not isinstance(data, dict):
            raise BookingCodeFailure(PROVIDER_CHANGED)  # HTML/challenge/empty body: never parsed further
        diagnostic['top_level_keys'] = sorted(str(k)[:40] for k in data)[:20]
        biz = data.get('bizCode')
        message = str(data.get('message') or '').casefold()
        if biz != 10000:
            if 'expire' in message:
                raise BookingCodeFailure(EXPIRED)
            if status in (200, 400, 404) and isinstance(biz, int):
                raise BookingCodeFailure(NOT_FOUND)
            raise BookingCodeFailure(PROVIDER_CHANGED)
        body = data.get('data')
        if not isinstance(body, dict) or not isinstance(body.get('outcomes'), list):
            raise BookingCodeFailure(NOT_FOUND if body is None else PROVIDER_CHANGED)
        diagnostic['data_keys'] = sorted(str(k)[:40] for k in body)[:30]
        events = body['outcomes']
        # raw_outcome_count is the legacy name for the EVENT/container count.
        # Keep it compatible; report nested outcomes separately, before rejection.
        counts = _sportybet_structure_counts(body)
        diagnostic.update(counts)
        note_booking(stage='sportybet_event_count', **counts)
        if not events:
            raise BookingCodeFailure(EMPTY)
        legs = []
        for event in events:
            legs.extend(self._legs(event))
        diagnostic['legs'] = len(legs)
        note_booking(stage='sportybet_decode', decoded_selection_count=len(legs))
        diagnostic['market_ids'] = [l.provider_market_id for l in legs]
        ticket = body.get('ticket')
        diagnostic['ticket_keys'] = sorted(str(k)[:40] for k in ticket)[:30] if isinstance(ticket, dict) else None
        # Names only: where (if anywhere) a combined/total odds value could live. Never mapped by guess.
        diagnostic['odds_like_paths'] = _odds_paths({k: v for k, v in body.items() if k != 'outcomes'})
        # Confirmed by the 2nd real run: ticket.displayTotalOdds. Stake/winnings are never read.
        raw_total = ticket.get('displayTotalOdds') if isinstance(ticket, dict) else None
        total = bookmaker_total_odds(raw_total)
        notes = ('Combined odds supplied by the bookmaker could not be read',) if raw_total not in (None, '') and total is None else ()
        diagnostic['combined_odds'] = 'parsed' if total else 'malformed' if notes else 'absent'
        return finalize('sportybet', code_hash, masked(code), self.source_id, self.HOST, legs, now,
                        total_odds=total, notes=notes)

    @staticmethod
    def _legs(event):
        if not isinstance(event, dict):
            return [DecodedLeg(unresolved=('selection',))]
        sport = event.get('sport') if isinstance(event.get('sport'), dict) else {}
        category = sport.get('category') if isinstance(sport.get('category'), dict) else {}
        tournament = category.get('tournament') if isinstance(category.get('tournament'), dict) else {}
        home, away = _text(event.get('homeTeamName')), _text(event.get('awayTeamName'))
        start = event.get('estimateStartTime')
        kickoff = None
        if type(start) is int and 0 < start < 10**14:
            kickoff = datetime.fromtimestamp(start / 1000, timezone.utc).isoformat()
        base = dict(sport=SPORTY_SPORTS.get(sport.get('id')), competition=_text(tournament.get('name')),
                    home=home, away=away, participants=tuple(p for p in (home, away) if p), kickoff=kickoff)
        markets = event.get('markets')
        legs = []
        for market in markets if isinstance(markets, list) else []:
            if not isinstance(market, dict):
                continue
            outcomes = market.get('outcomes') if isinstance(market.get('outcomes'), list) else []
            for outcome in outcomes:
                if isinstance(outcome, dict):
                    legs.append(SportyBetDirectResolver._leg(base, market, outcome))
        return legs or [DecodedLeg(**base, unresolved=('market', 'outcome'))]

    @staticmethod
    def _leg(base, market, outcome):
        """Map one outcome. Every canonical value must be supported by the id or the label and
        contradicted by neither; a non-null value alone does not count as normalized."""
        mid, oid = str(market.get('id') or ''), str(outcome.get('id') or '')
        specifier = _text(market.get('specifier'), 100)
        market_label, outcome_label = _text(market.get('desc'), 80), _text(outcome.get('desc'), 80)
        sport, home, away = base['sport'], base['home'], base['away']
        by_id, by_label = SPORTY_MARKETS.get(mid), _label_rule(market_label)
        family = period = side = line = None
        sides = spec_key = None
        sports = frozenset()
        if by_id:
            family, period, spec_key, sides, sports = by_id
            if by_label and (by_label[0], by_label[1]) != (family, period):
                family = period = None  # id says one market, label another: never pick one
        elif by_label:
            family, period, sports = by_label
            spec_key = 'total' if family == 'total' else None
        if family and sport not in sports:
            family = period = None      # e.g. a 1X2 rule applied to an unreviewed sport
        if family:
            id_side = sides.get(oid) if sides else None
            label_side = _side_from_label(family, outcome_label, home, away)
            if id_side and label_side and id_side != label_side:
                side = None
            else:
                side = id_side or label_side
            if spec_key:
                line = _line(_spec(specifier).get(spec_key))
                # Sportradar handicap specifier is from the HOME side; our line is the selected side's.
                if family == 'handicap' and line is not None and side == 'away':
                    line = -line
            if family == 'total':
                m = OU_OUTCOME.match(' '.join((outcome_label or '').split()))
                label_line = _line(m.group(2)) if m and m.group(2) else None
                if line is not None and label_line is not None and line != label_line:
                    line = None             # specifier and label disagree: unresolved, not chosen
                elif line is None:
                    line = label_line
        known = family is not None
        return DecodedLeg(**base, market=family, market_label=market_label, line=line,
                          specifier=specifier, outcome=side, outcome_label=outcome_label,
                          odds=_odds(outcome.get('odds')), period=period,
                          stat='points' if known else None, entity='all' if known else None,
                          selected_participant=selected_participant(family, side, home, away),
                          provider_market_id=mid[:20] or None)


MBC_MARKETS = {'_1x2': ('result3', {'home', 'draw', 'away'}),
               'over_under': ('total', {'over', 'under'})}


class MyBetCodeResolver:
    """Documented GET /api/v1/retrieve only (X-API-Key). Disabled by default.

    Never calls convert, merge, lookup-for-conversion, AI picks or fixtures.
    Market vocabulary beyond the documented examples stays unresolved.
    """
    source_id = 'mybetcode-v1'
    HOST = 'api.mybetcode.com'
    PATH = r'/api/v1/retrieve'
    BOOKIES = {'sportybet': 'sportybet:ng', 'bet9ja': 'bet9ja', 'nairabet': 'nairabet'}
    REASONS = {'invalid_code': MALFORMED_CODE, 'code_not_found': NOT_FOUND, 'empty_slip': EMPTY,
               'unsupported_bookmaker': UNSUPPORTED_BOOKMAKER, 'source_unreachable': PROVIDER_UNAVAILABLE}

    def __init__(self, store, api_key, daily, monthly, transport=None):
        if not api_key:
            raise ValueError('MyBetCode key not configured')
        self.store, self.daily, self.monthly = store, daily, monthly
        self._key = api_key
        self.transport = transport
        self.last_diagnostic = {}

    def __repr__(self):
        return 'MyBetCodeResolver(key=***)'

    def url(self, bookmaker, code):
        if bookmaker not in self.BOOKIES or not CODE.fullmatch(code):
            raise BookingCodeFailure(MALFORMED_CODE)
        return f'https://{self.HOST}/api/v1/retrieve?' + urlencode({'bookie': self.BOOKIES[bookmaker], 'code': code})

    def retrieve(self, bookmaker, code, code_hash, now):
        if not ticket_audit_switch.ENABLED:
            raise BookingCodeFailure(DISABLED)
        if bookmaker not in self.BOOKIES:
            raise BookingCodeFailure(UNSUPPORTED_BOOKMAKER)
        url = self.url(bookmaker, code)
        self.store.reserve(self.source_id, now, self.daily, self.monthly)
        self.last_diagnostic = diagnostic = {'source': self.source_id}
        transport = self.transport or requests.Session()
        try:
            status, data = fetch(transport, url, self.HOST, self.PATH, {'X-API-Key': self._key}, diagnostic)
        finally:
            if self.transport is None:
                transport.close()
        if isinstance(data, dict):
            diagnostic['top_level_keys'] = sorted(str(k)[:40] for k in data)[:20]
            if status == 422:
                diagnostic['reason_code'] = _text(data.get('reason_code'), 60)
        if status == 422 and isinstance(data, dict):
            raise BookingCodeFailure(self.REASONS.get(data.get('reason_code'), PROVIDER_UNAVAILABLE))
        if status == 404:
            raise BookingCodeFailure(NOT_FOUND if isinstance(data, dict) else PROVIDER_CHANGED)
        common = _common_status(status)
        if common:
            raise BookingCodeFailure(common)
        if status != 200 or not isinstance(data, dict) or not isinstance(data.get('data'), dict):
            raise BookingCodeFailure(PROVIDER_CHANGED)
        body = data['data']
        rows = body.get('selections')
        if not isinstance(rows, list):
            raise BookingCodeFailure(PROVIDER_CHANGED)
        if body.get('bookie') not in (None, self.BOOKIES[bookmaker]):
            raise BookingCodeFailure(PROVIDER_CHANGED)  # answer for another bookmaker: never accept
        declared = body.get('count') if type(body.get('count')) is int and 0 <= body['count'] <= 100 else None
        note_booking(stage='mybetcode_row_count', raw_outcome_count=len(rows))
        legs = [self._leg(r) for r in rows]
        note_booking(decoded_selection_count=len(legs))
        diagnostic['legs'] = len(legs)
        return finalize(bookmaker, code_hash, masked(code), self.source_id, self.HOST, legs, now,
                        declared, bookmaker_total_odds(body.get('total_odds')))

    @staticmethod
    def _leg(row):
        if not isinstance(row, dict):
            return DecodedLeg(unresolved=('selection',))
        home, away = _text(row.get('home_team')), _text(row.get('away_team'))
        kickoff = instant(row.get('kickoff_utc')) if isinstance(row.get('kickoff_utc'), str) else None
        market_raw = _text(row.get('market'), 60)
        pick = _text(row.get('pick'), 60)
        rule = MBC_MARKETS.get(market_raw)
        family = side = line = None
        if rule and pick and pick.casefold() in rule[1]:
            family, side = rule[0], pick.casefold()
            if family == 'total':
                line = _line(row.get('specifier'))
        sport = _text(row.get('sport'), 40)  # Not in the documented schema; used only if supplied.
        sport = sport.casefold() if sport and sport.casefold() in (
            'football', 'basketball', 'tennis', 'baseball', 'hockey', 'american_football') else None
        known = family is not None
        return DecodedLeg(selected_participant=selected_participant(family, side, home, away), sport=sport, competition=_text(row.get('competition')), home=home, away=away,
                          participants=tuple(p for p in (home, away) if p),
                          kickoff=kickoff.isoformat() if kickoff else None, market=family,
                          market_label=market_raw, line=line,
                          specifier=_text(row.get('specifier'), 60), outcome=side, outcome_label=pick,
                          odds=_odds(row.get('odds')), period='full' if known else None,
                          stat='points' if known else None, entity='all' if known else None)
