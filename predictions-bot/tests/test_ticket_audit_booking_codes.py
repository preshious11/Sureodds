"""Booking-code intake: mocked transports only. Network, sports providers and settlement forbidden."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import json
import logging
import socket
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from predbot.bot import audit_format
from predbot.bot.ticket_audit import (TicketAuditController, BOOKING, CHOOSE_BOOKMAKER, CODE_MESSAGES,
                                      INTRO, INTRO_CODES, UNREADABLE)
from predbot.config import Config
from predbot.ticket_audit import booking as bk
from predbot.ticket_audit.booking_sources import MyBetCodeResolver, SportyBetDirectResolver
from predbot.ticket_audit.gemini import Settings
from predbot.ticket_audit.local import LocalResolver
from predbot.ticket_audit.service import AuditService
from tests.test_community_menu import MenuTelegram, COMMUNITY
from tests.test_ticket_audit_foundation import no_providers, ticket, client, response, NOW, db_dump, forbidden  # noqa: F401
from tests.test_ticket_audit_telegram import input_message, KEY

KICK = int(datetime(2030, 6, 1, 19, 0, tzinfo=timezone.utc).timestamp() * 1000)
KICK_ISO = '2030-06-01T19:00:00+00:00'


@pytest.fixture(autouse=True)
def firewall(monkeypatch):
    from predbot.jobs import settle, live_results
    for obj, name in ((settle, 'run'), (settle, '_prefetch'), (live_results, 'run'), (live_results, '_ask')):
        monkeypatch.setattr(obj, name, forbidden)
    monkeypatch.setattr(socket.socket, 'connect', forbidden)


# ---------------------------------------------------------------- transports
class Resp:
    def __init__(self, status=200, body=None, headers=None, raw=None):
        self.status_code = status
        self.body = raw if raw is not None else (json.dumps(body).encode() if body is not None else b'')
        self.headers = {'Content-Type': 'application/json', **(headers or {})}
        self.closed = False

    def iter_content(self, chunk_size=1):
        for i in range(0, len(self.body), chunk_size):
            yield self.body[i:i + chunk_size]

    def close(self):
        self.closed = True


class Transport:
    def __init__(self, *responses, gate=None):
        self.responses = list(responses)
        self.calls = []
        self.gate = gate
        self.lock = threading.Lock()

    def get(self, url, **kw):
        with self.lock:
            self.calls.append((url, kw))
        if self.gate:
            self.gate.wait(5)
        r = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(r, BaseException):
            raise r
        return r

    def request(self, *a, **k):  # any non-GET verb is a test failure
        pytest.fail('non-GET request')

    post = put = delete = request


def event(home, away, sport, tournament, market_id, outcome_id, odds, specifier=None, mdesc='M', odesc='O'):
    return {'eventId': 'sr:match:1', 'estimateStartTime': KICK, 'homeTeamName': home, 'awayTeamName': away,
            'sport': {'id': sport, 'name': 'x', 'category': {'name': 'Cat', 'tournament': {'name': tournament}}},
            'markets': [{'id': market_id, 'specifier': specifier, 'desc': mdesc,
                         'outcomes': [{'id': outcome_id, 'odds': odds, 'desc': odesc, 'isActive': 1}]}]}


FOOTBALL_1X2 = event('Arsenal', 'Chelsea', 'sr:sport:1', 'Premier League', '1', '1', '1.85', mdesc='1X2', odesc='Home')
FOOTBALL_TOTAL = event('Real Madrid', 'Barcelona', 'sr:sport:1', 'LaLiga', '18', '12', '1.72', 'total=2.5', 'Over/Under', 'Over 2.5')
FOOTBALL_AH = event('Inter', 'Milan', 'sr:sport:1', 'Serie A', '16', '1715', '1.95', 'hcp=-1.5', 'Handicap', 'Milan (+1.5)')
TENNIS = event('Sinner J.', 'Alcaraz C.', 'sr:sport:5', 'ATP Finals', '186', '5', '2.10', mdesc='Winner', odesc='Alcaraz C.')
BASKETBALL = event('Lakers', 'Celtics', 'sr:sport:2', 'NBA', '219', '4', '1.60', mdesc='Winner (incl. overtime)', odesc='Lakers')


def sporty(*events, biz=10000, message='0#0'):
    return {'bizCode': biz, 'isAvailable': True, 'message': message,
            'data': {'shareCode': 'IGNORED', 'outcomes': list(events)} if biz == 10000 else None}


MBC_OK = {'message': 'success', 'data': {'bookie': 'bet9ja', 'code': 'V08G7Y', 'count': 2, 'total_odds': 3.42,
    'selections': [
        {'home_team': 'Arsenal', 'away_team': 'Chelsea', 'kickoff_utc': '2030-06-01T19:00:00Z',
         'competition': 'Premier League', 'market': '_1x2', 'specifier': None, 'pick': 'home', 'odds': 1.85},
        {'home_team': 'Real Madrid', 'away_team': 'Barcelona', 'kickoff_utc': '2030-06-01T19:00:00Z',
         'competition': 'La Liga', 'market': 'over_under', 'specifier': '2.5', 'pick': 'over', 'odds': 1.85}]}}


@pytest.fixture
def store(tmp_path):
    return bk.BookingStore(tmp_path / 'booking.sqlite3')


def direct(store, *responses, daily=50, gate=None):
    t = Transport(*responses, gate=gate)
    return SportyBetDirectResolver(store, daily, t), t


def mbc(store, *responses, daily=10, monthly=100):
    t = Transport(*responses)
    return MyBetCodeResolver(store, 'MBC-SECRET-KEY-123', daily, monthly, t), t


def service(store, sporty_resolver=None, mbc_resolver=None):
    routes = {'sportybet': [r for r in (sporty_resolver, mbc_resolver) if r]}
    if mbc_resolver:
        routes.update(bet9ja=[mbc_resolver], nairabet=[mbc_resolver])
    return bk.BookingCodeService(routes, store)


# ---------------------------------------------------------------- SportyBet direct
def test_sportybet_multi_leg_decode_all_market_shapes(store):
    r, t = direct(store, Resp(body=sporty(FOOTBALL_1X2, FOOTBALL_TOTAL, FOOTBALL_AH, TENNIS, BASKETBALL)))
    result = service(store, r).resolve('sportybet', ' ab12cd ', NOW)
    assert result.status == bk.OK and len(result.selections) == 5 and result.valid
    assert len(t.calls) == 1
    url, kw = t.calls[0]
    assert url == 'https://www.sportybet.com/api/ng/orders/share/AB12CD'
    assert kw['allow_redirects'] is False and kw['timeout'] == (4, 8) and kw['stream'] is True
    assert 'Cookie' not in kw['headers'] and 'Authorization' not in kw['headers']
    x12, total, ah, tennis, nba = result.selections
    assert (x12.sport, x12.competition, x12.home, x12.away, x12.kickoff, x12.market, x12.outcome, x12.odds) == \
        ('football', 'Premier League', 'Arsenal', 'Chelsea', KICK_ISO, 'result3', 'home', 1.85)
    assert (total.market, total.outcome, total.line, total.specifier) == ('total', 'over', 2.5, 'total=2.5')
    # Home-perspective specifier -> selected side's signed line.
    assert (ah.market, ah.outcome, ah.line) == ('handicap', 'away', 1.5)
    assert (tennis.sport, tennis.market, tennis.outcome, tennis.participants) == ('tennis', 'result2', 'away', ('Sinner J.', 'Alcaraz C.'))
    assert (nba.sport, nba.market, nba.outcome, nba.period) == ('basketball', 'result2', 'home', 'full')
    assert all(l.unresolved == () and (l.period, l.stat, l.entity) == ('full', 'points', 'all') for l in result.selections)
    assert result.provider == 'sportybet-direct-experimental' and result.source_host == 'www.sportybet.com'
    assert result.booking_code == 'AB••CD' and 'AB12CD' not in json.dumps(asdict(result))
    assert result.retrieved_at and result.total_odds is None  # never computed from legs


def test_handicap_home_line_kept_and_first_half_period(store):
    home_ah = event('A', 'B', 'sr:sport:1', 'L', '16', '1714', '1.9', 'hcp=-0.25')
    ht = event('C', 'D', 'sr:sport:1', 'L', '68', '13', '1.5', 'total=1.5')
    r, _ = direct(store, Resp(body=sporty(home_ah, ht)))
    a, h = service(store, r).resolve('sportybet', 'HC1234', NOW).selections
    assert (a.outcome, a.line) == ('home', -0.25)
    assert (h.market, h.outcome, h.line, h.period) == ('total', 'under', 1.5, '1h')


def test_unknown_market_and_sport_partial_preserved_not_substituted(store):
    odd = event('Team A', 'Team B', 'sr:sport:999', 'Weird Cup', '999', '77', '3.3', mdesc='Exotic', odesc='Something')
    r, _ = direct(store, Resp(body=sporty(FOOTBALL_1X2, odd)))
    result = service(store, r).resolve('sportybet', 'PART12', NOW)
    assert result.status == bk.PARTIAL and len(result.selections) == 2
    good, bad = result.selections
    assert good.unresolved == ()
    assert bad.sport is None and bad.market is None and bad.outcome is None and bad.odds == 3.3
    assert {'sport', 'market', 'outcome', 'period', 'stat', 'entity'} <= set(bad.unresolved)
    t = bk.to_ticket(result)
    assert t.selections[1].market_family is None and t.selections[1].raw_text == 'Exotic — Something'
    assert any('Selection 2 not fully decoded' in w for w in t.extraction_warnings)


@pytest.mark.parametrize('resp,status', [
    (Resp(body=sporty(biz=4200, message='Invalid booking code')), bk.NOT_FOUND),
    (Resp(body=sporty(biz=4201, message='The code has expired')), bk.EXPIRED),
    (Resp(body={'bizCode': 10000, 'message': 'ok', 'data': {'outcomes': []}}), bk.EMPTY),
    (Resp(raw=b'x' * 512001), bk.RESPONSE_TOO_LARGE),
    (Resp(raw=b'<html>challenge</html>', headers={'Content-Type': 'text/html'}), bk.PROVIDER_CHANGED),
    (Resp(body={'unexpected': True}), bk.PROVIDER_CHANGED),
    (Resp(403, raw=b'<html>blocked</html>'), bk.PROVIDER_UNAVAILABLE),
    (Resp(302, headers={'Location': 'https://evil.example/x'}), bk.PROVIDER_UNAVAILABLE),
    (Resp(429), bk.QUOTA_EXHAUSTED),
    (Resp(500), bk.PROVIDER_UNAVAILABLE),
    (Resp(503, body={'bizCode': 1}), bk.PROVIDER_UNAVAILABLE),
    (Resp(raw=b'{' + b' ' * 600_000 + b'}'), bk.RESPONSE_TOO_LARGE),
    (requests.Timeout('secret detail'), bk.TIMEOUT),
    (requests.ConnectionError('secret detail'), bk.PROVIDER_UNAVAILABLE),
])
def test_sportybet_failures_fail_closed_one_request_no_retry(store, resp, status, caplog):
    r, t = direct(store, resp)
    result = service(store, r).resolve('sportybet', 'FAIL12', NOW)
    assert result.status == status and not result.valid and result.selections == ()
    assert len(t.calls) == 1
    assert 'secret detail' not in caplog.text and 'FAIL12' not in caplog.text


MALICIOUS = ['../../admin', 'AB/CD12', 'AB12?x=1', 'AB12#frag', 'AB%2F12', 'AB 12CD', 'AB12\nCD', 'AB12&code=X',
             'ABC_123', 'ABC-123', '١٢٣٤٥٦', 'ＡＢ１２３４', 'A' * 17, 'AB1', '', 'https://evil.example/AB12',
             'AB12;DROP', 'AB12\x00', 'AB12' * 20]


@pytest.mark.parametrize('code', MALICIOUS)
def test_malicious_codes_rejected_before_any_request(store, code):
    s, ts = direct(store, Resp(body=sporty(FOOTBALL_1X2)))
    m, tm = mbc(store, Resp(body=MBC_OK))
    for bookmaker in ('sportybet', 'bet9ja'):
        assert service(store, s, m).resolve(bookmaker, code, NOW).status == bk.MALFORMED_CODE
    assert ts.calls == [] and tm.calls == []
    assert store.used('sportybet-direct-experimental', NOW) == 0


@pytest.mark.parametrize('code', MALICIOUS)
def test_adapter_url_builders_cannot_leave_fixed_route(store, code):
    s, _ = direct(store, Resp())
    m, _ = mbc(store, Resp())
    with pytest.raises(bk.BookingCodeFailure):
        s.url(code)
    with pytest.raises(bk.BookingCodeFailure):
        m.url('bet9ja', code)
    with pytest.raises(bk.BookingCodeFailure):
        m.url('../bet9ja', 'AB12CD')


def test_fetch_reverifies_final_url_host_and_path(store):
    from predbot.ticket_audit.booking_sources import fetch
    t = Transport(Resp(body={}))
    for url in ('https://www.sportybet.com.evil.example/api/ng/orders/share/AB12',
                'http://www.sportybet.com/api/ng/orders/share/AB12',
                'https://www.sportybet.com/api/ng/orders/share/AB12/../../x',
                'https://user@www.sportybet.com/api/ng/orders/share/AB12'):
        with pytest.raises(bk.BookingCodeFailure):
            fetch(t, url, SportyBetDirectResolver.HOST, SportyBetDirectResolver.PATH)
    assert t.calls == []


# ---------------------------------------------------------------- MyBetCode
def test_mybetcode_documented_retrieve_one_request_key_header_only(store, caplog):
    caplog.set_level(logging.DEBUG)
    m, t = mbc(store, Resp(body=MBC_OK))
    result = service(store, mbc_resolver=m).resolve('bet9ja', 'v08g7y', NOW)
    assert len(t.calls) == 1
    url, kw = t.calls[0]
    assert url == 'https://api.mybetcode.com/api/v1/retrieve?bookie=bet9ja&code=V08G7Y'
    assert kw['headers']['X-API-Key'] == 'MBC-SECRET-KEY-123' and kw['allow_redirects'] is False
    for word in ('convert', 'merge', 'lookup', 'picks', 'fixtures', 'predict'):
        assert word not in url
    assert result.provider == 'mybetcode-v1' and result.total_odds == 3.42 and result.declared_count == 2
    a, b = result.selections
    assert (a.home, a.away, a.kickoff, a.competition, a.market, a.outcome, a.odds) == \
        ('Arsenal', 'Chelsea', KICK_ISO, 'Premier League', 'result3', 'home', 1.85)
    assert (b.market, b.outcome, b.line) == ('total', 'over', 2.5)
    # The documented schema has no sport: kept null and flagged, never inferred.
    assert a.sport is None and 'sport' in a.unresolved and result.status == bk.PARTIAL
    assert 'MBC-SECRET-KEY-123' not in repr(m) and 'MBC-SECRET-KEY-123' not in caplog.text
    assert 'MBC-SECRET-KEY-123' not in json.dumps(asdict(result))


def test_mybetcode_sportybet_uses_documented_bookie_and_supplied_sport(store):
    body = json.loads(json.dumps(MBC_OK))
    body['data']['bookie'] = 'sportybet:ng'
    for s in body['data']['selections']:
        s['sport'] = 'Football'
    m, t = mbc(store, Resp(body=body))
    result = service(store, mbc_resolver=m).resolve('sportybet', 'V08G7Y', NOW)
    assert 'bookie=sportybet%3Ang' in t.calls[0][0]
    assert result.status == bk.OK and all(l.sport == 'football' for l in result.selections)


@pytest.mark.parametrize('resp,status', [
    (Resp(422, {'message': 'error', 'error': 'x', 'reason_code': 'code_not_found'}), bk.NOT_FOUND),
    (Resp(422, {'message': 'error', 'reason_code': 'invalid_code'}), bk.MALFORMED_CODE),
    (Resp(422, {'message': 'error', 'reason_code': 'empty_slip'}), bk.EMPTY),
    (Resp(422, {'message': 'error', 'reason_code': 'unsupported_bookmaker'}), bk.UNSUPPORTED_BOOKMAKER),
    (Resp(422, {'message': 'error', 'reason_code': 'source_unreachable'}), bk.PROVIDER_UNAVAILABLE),
    (Resp(422, {'message': 'error', 'reason_code': 'something_new'}), bk.PROVIDER_UNAVAILABLE),
    (Resp(401, {'message': 'error', 'error': 'Invalid API key'}), bk.PROVIDER_UNAVAILABLE),
    (Resp(403, {'message': 'error', 'error': 'Live keys are not available on your current plan'}), bk.PROVIDER_UNAVAILABLE),
    (Resp(429, {'message': 'error', 'limits': {'daily_remaining': 0}}), bk.QUOTA_EXHAUSTED),
    (Resp(500), bk.PROVIDER_UNAVAILABLE),
    (Resp(body={**MBC_OK, 'data': {**MBC_OK['data'], 'bookie': 'nairabet'}}), bk.PROVIDER_CHANGED),
    (Resp(body={'message': 'success', 'data': {'selections': 'x'}}), bk.PROVIDER_CHANGED),
    (requests.Timeout(), bk.TIMEOUT),
])
def test_mybetcode_failures_mapped(store, resp, status):
    m, t = mbc(store, resp)
    assert service(store, mbc_resolver=m).resolve('bet9ja', 'V08G7Y', NOW).status == status
    assert len(t.calls) == 1


def test_mybetcode_count_mismatch_is_partial_with_warning(store):
    body = json.loads(json.dumps(MBC_OK))
    body['data']['count'] = 3
    m, _ = mbc(store, Resp(body=body))
    result = service(store, mbc_resolver=m).resolve('bet9ja', 'V08G7Y', NOW)
    assert result.status == bk.PARTIAL and result.unresolved == ('Bookmaker reported 3 selections; 2 were returned',)


# ---------------------------------------------------------------- cache, quota, routing
def test_repeat_and_restart_hit_cache_zero_requests_and_raw_code_not_stored(store, tmp_path):
    r, t = direct(store, Resp(body=sporty(FOOTBALL_1X2)))
    first = service(store, r).resolve('sportybet', 'CACHE1', NOW)
    second = service(store, r).resolve('sportybet', 'cache1', NOW + timedelta(minutes=5))
    restarted_store = bk.BookingStore(tmp_path / 'booking.sqlite3')
    r2, t2 = direct(restarted_store, Resp(body=sporty(FOOTBALL_1X2)))
    third = service(restarted_store, r2).resolve('sportybet', 'CACHE1', NOW + timedelta(minutes=10))
    assert len(t.calls) == 1 and t2.calls == []
    assert not first.cached and second.cached and third.cached
    assert second.selections == first.selections == third.selections
    assert b'CACHE1' not in (tmp_path / 'booking.sqlite3').read_bytes()
    # Same code, different bookmaker: separate identity.
    assert store.key('sportybet', 'CACHE1') != store.key('bet9ja', 'CACHE1')


def test_cache_expires_at_kickoff_and_negative_cache_ten_minutes(store):
    r, t = direct(store, Resp(body=sporty(FOOTBALL_1X2)))
    svc = service(store, r)
    svc.resolve('sportybet', 'EXPIR1', NOW)
    svc.resolve('sportybet', 'EXPIR1', datetime(2030, 6, 1, 19, 1, tzinfo=timezone.utc))
    assert len(t.calls) == 2
    n, tn = direct(store, Resp(body=sporty(biz=4200)))
    svc = service(store, n)
    assert svc.resolve('sportybet', 'NOPE12', NOW).status == bk.NOT_FOUND
    assert svc.resolve('sportybet', 'NOPE12', NOW + timedelta(minutes=9)).status == bk.NOT_FOUND
    assert len(tn.calls) == 1
    svc.resolve('sportybet', 'NOPE12', NOW + timedelta(minutes=11))
    assert len(tn.calls) == 2


def test_transient_failures_are_not_cached(store):
    r, t = direct(store, Resp(500), Resp(body=sporty(FOOTBALL_1X2)))
    svc = service(store, r)
    assert svc.resolve('sportybet', 'RETRY1', NOW).status == bk.PROVIDER_UNAVAILABLE
    assert svc.resolve('sportybet', 'RETRY1', NOW).status == bk.OK
    assert len(t.calls) == 2


def test_concurrent_identical_requests_single_flight(store):
    gate = threading.Event()
    r, t = direct(store, Resp(body=sporty(FOOTBALL_1X2)), gate=gate)
    svc = service(store, r)
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(svc.resolve, 'sportybet', 'FLIGHT', NOW) for _ in range(8)]
        threading.Timer(0.3, gate.set).start()
        results = [f.result(10) for f in futures]
    assert len(t.calls) == 1 and all(x.status == bk.OK for x in results)


def test_daily_ledger_blocks_before_http_and_failures_consume(store):
    r, t = direct(store, Resp(500), daily=2)
    svc = service(store, r)
    for code in ('QUOTA1', 'QUOTA2', 'QUOTA3'):
        last = svc.resolve('sportybet', code, NOW)
    assert last.status == bk.QUOTA_EXHAUSTED and len(t.calls) == 2


def test_mybetcode_monthly_ledger(store):
    m, t = mbc(store, Resp(500), daily=10, monthly=1)
    svc = service(store, mbc_resolver=m)
    svc.resolve('bet9ja', 'MONTH1', NOW)
    assert svc.resolve('bet9ja', 'MONTH2', NOW + timedelta(days=1)).status == bk.QUOTA_EXHAUSTED
    assert len(t.calls) == 1


def test_direct_transient_failure_falls_back_once_definitive_does_not(store):
    s, ts = direct(store, Resp(raw=b'<html/>'))
    m, tm = mbc(store, Resp(body={**MBC_OK, 'data': {**MBC_OK['data'], 'bookie': 'sportybet:ng'}}))
    result = service(store, s, m).resolve('sportybet', 'FALL12', NOW)
    assert result.provider == 'mybetcode-v1' and len(ts.calls) == len(tm.calls) == 1
    s2, ts2 = direct(store, Resp(body=sporty(biz=4200)))
    m2, tm2 = mbc(store, Resp(body=MBC_OK))
    assert service(store, s2, m2).resolve('sportybet', 'NOTF12', NOW).status == bk.NOT_FOUND
    assert tm2.calls == []


def test_unknown_and_unsupported_bookmakers(store):
    r, t = direct(store, Resp(body=sporty(FOOTBALL_1X2)))
    svc = service(store, r)
    assert svc.bookmakers() == ('sportybet',)
    assert svc.resolve('betking', 'AB12CD', NOW).status == bk.UNKNOWN_BOOKMAKER
    assert svc.resolve('bet9ja', 'AB12CD', NOW).status == bk.UNSUPPORTED_BOOKMAKER
    assert t.calls == []


def test_adapter_bug_fails_closed_without_details(store, caplog):
    r = Mock(source_id='broken')
    r.retrieve.side_effect = KeyError('provider internals')
    result = bk.BookingCodeService({'sportybet': [r]}, store).resolve('sportybet', 'BUG123', NOW)
    assert result.status == bk.PROVIDER_CHANGED and 'provider internals' not in caplog.text


# ---------------------------------------------------------------- settings / construction
def test_settings_default_disabled_and_no_key_means_no_route(tmp_path, monkeypatch, db):
    for k in ('AUDIT_BOOKING_CODES_ENABLED', 'SPORTYBET_DIRECT_ENABLED', 'MYBETCODE_ENABLED', 'MYBETCODE_API_KEY'):
        monkeypatch.delenv(k, raising=False)
    env = tmp_path / '.env'
    env.write_text('X=1\n')
    cfg = SimpleNamespace(db_path=db.path, env_file=str(env))
    s = bk.BookingSettings.from_environment(str(env))
    assert not s.enabled and not s.sportybet_direct and not s.mybetcode and s.mybetcode_key == ''
    assert bk.build_booking_service(cfg) is None
    env.write_text('AUDIT_BOOKING_CODES_ENABLED=true\nMYBETCODE_ENABLED=true\nMYBETCODE_API_KEY=\n')
    assert bk.build_booking_service(cfg) is None  # NOT_TESTED_NO_KEY: disabled, not an error
    env.write_text('AUDIT_BOOKING_CODES_ENABLED=true\nMYBETCODE_ENABLED=true\nMYBETCODE_API_KEY=k-SECRET\n')
    svc = bk.build_booking_service(cfg)
    assert svc.bookmakers() == ('sportybet', 'bet9ja', 'nairabet')
    assert 'k-SECRET' not in repr(bk.BookingSettings.from_environment(str(env)))
    env.write_text('AUDIT_BOOKING_CODES_ENABLED=true\nSPORTYBET_DIRECT_ENABLED=true\n')
    assert bk.build_booking_service(cfg).bookmakers() == ('sportybet',)


def test_store_refuses_prediction_db_and_unrelated_files(db, tmp_path):
    with pytest.raises(ValueError):
        bk.BookingStore(db.path, prediction_db=db.path)
    with pytest.raises(ValueError):
        bk.BookingStore(db.path)


def test_to_ticket_core_schema_unchanged_no_raw_code(store):
    r, _ = direct(store, Resp(body=sporty(FOOTBALL_1X2, FOOTBALL_TOTAL)))
    result = service(store, r).resolve('sportybet', 'TICK12', NOW)
    t = bk.to_ticket(result)
    assert t.input_type == 'normalized' and t.source_bookmaker == 'SportyBet' and t.source_code is None
    assert t.selections[0].source_event_id is None and t.selections[1].line == 2.5
    assert 'TICK12' not in json.dumps(asdict(t))
    with pytest.raises(bk.BookingCodeFailure):
        bk.to_ticket(replace(result, status=bk.NOT_FOUND))


# ---------------------------------------------------------------- Telegram flow
@pytest.fixture
def ui(db, tmp_path):
    cfg = Config(db_path=db.path, timezone='UTC', telegram_chat_id=COMMUNITY, telegram_admin_ids=['42'])
    tg = MenuTelegram()
    c = client(tmp_path, response(json.dumps(asdict(replace(ticket(), input_type='text')))),
               settings=Settings('MOCK-KEY', 'gemini-3.5-flash-lite', True, True))
    audit = AuditService(LocalResolver(db.path, 'UTC'))
    backend = SimpleNamespace(client=c, service=audit)
    store = bk.BookingStore(tmp_path / 'booking.sqlite3')
    transport = Transport(Resp(body=sporty(FOOTBALL_1X2, FOOTBALL_TOTAL, TENNIS)))
    svc = service(store, SportyBetDirectResolver(store, 50, transport))
    booking_factory = Mock(return_value=svc)
    controller = TicketAuditController(tg, cfg, Mock(return_value=backend), clock=lambda: NOW,
                                       runner=lambda job: job(), booking_factory=booking_factory)
    return SimpleNamespace(tg=tg, cfg=cfg, client=c, audit=audit, controller=controller, transport=transport,
                           booking_factory=booking_factory, store=store, svc=svc)


def tap(controller, action, user='42', token=None, mid=None):
    s = controller.sessions.get(KEY)
    return {'id': 'tap', 'from': {'id': int(user)}, 'data': f'ta|{token or s.token}|{action}',
            'message': {'message_id': mid or s.review_id, 'chat': {'id': 42, 'type': 'private'}}}


def code_prompt(ui, code='AB12CD'):
    ui.controller.start(input_message())
    ui.controller.message(input_message(code, mid=2))
    return ui.controller.sessions[KEY]


def texts(ui):
    return '\n\n'.join(s['text'] for s in ui.tg.sends)


def test_intro_mentions_codes_only_when_enabled(ui, db):
    ui.controller.start(input_message())
    assert ui.tg.sends[-1]['text'] == INTRO_CODES and 'booking code' in INTRO_CODES
    off = TicketAuditController(ui.tg, ui.cfg, Mock(), clock=lambda: NOW, booking_factory=lambda: None)
    off.start(input_message())
    assert ui.tg.sends[-1]['text'] == INTRO
    off.message(input_message('AB12CD', mid=5))
    assert ui.tg.sends[-1]['text'] == BOOKING


def test_code_asks_only_bookmaker_then_decodes_into_same_review_without_gemini(ui, db):
    before = db_dump(db)
    s = code_prompt(ui)
    assert s.state == 'CHOOSING_BOOKMAKER' and ui.tg.sends[-1]['text'] == CHOOSE_BOOKMAKER
    buttons = [b['text'] for row in ui.tg.sends[-1]['reply_markup']['inline_keyboard'] for b in row]
    assert buttons == ['SportyBet', 'More', '❌ Cancel']
    assert all(len(b['callback_data'].encode()) <= 64 for row in ui.tg.sends[-1]['reply_markup']['inline_keyboard'] for b in row)
    assert ui.transport.calls == []  # no retrieval until the bookmaker is known
    ui.controller.callback(tap(ui.controller, 'bk:sportybet'))
    assert len(ui.transport.calls) == 1 and s.pending_code is None
    assert s.state == 'PENDING_REVIEW' and s.booking.status == bk.OK
    out = texts(ui)
    assert 'SportyBet code AB••CD — 3 selections loaded' in out
    assert 'Arsenal vs Chelsea' in out and 'over 2.5' in out and 'Alcaraz C. to Win' in out and '@ 1.85' in out
    assert 'AB12CD' not in out
    assert [r[0]['text'] for r in ui.tg.sends[-1]['reply_markup']['inline_keyboard']] == ['✅ Audit Ticket', '✏️ Send Again', '❌ Cancel']
    ui.client.transport.request.assert_not_called()  # resolver supplied fields: no Gemini extraction
    spy = Mock(wraps=ui.audit.audit)
    ui.audit.audit = spy
    ui.controller.callback(tap(ui.controller, 'confirm'))
    assert spy.call_count == 1 and s.state == 'COMPLETE'
    audited = spy.call_args.args[0]
    assert len(audited.selections) == 3 and audited.source_code is None
    assert 'Ticket Audit\n\n3 selections' in texts(ui)
    assert len(ui.transport.calls) == 1 and db_dump(db) == before


def test_partial_decode_review_flags_unresolved_legs_and_skips_clarification(ui):
    ui.transport.responses = [Resp(body=sporty(FOOTBALL_1X2, event('X', 'Y', 'sr:sport:999', 'Cup', '999', '1', '2.0',
                                                                  mdesc='Exotic', odesc='Pick')))]
    s = code_prompt(ui)
    ui.controller.callback(tap(ui.controller, 'bk:sportybet'))
    assert s.state == 'PENDING_REVIEW'
    out = texts(ui)
    assert 'only partly decode' in out and '⚠️ Not decoded: sport, market, outcome' in out
    assert 'I need a few details' not in out
    assert s.ticket.selections[1].market_family is None


@pytest.mark.parametrize('resp,status', [
    (Resp(body=sporty(biz=4200)), bk.NOT_FOUND), (Resp(body=sporty(biz=4201, message='code expired')), bk.EXPIRED),
    (Resp(429), bk.QUOTA_EXHAUSTED), (Resp(503), bk.PROVIDER_UNAVAILABLE), (requests.Timeout(), bk.TIMEOUT),
    (Resp(raw=b'<html/>'), bk.PROVIDER_CHANGED), (Resp(raw=b'x' * 512001), bk.RESPONSE_TOO_LARGE),
])
def test_failure_states_have_distinct_user_messages(ui, resp, status):
    ui.transport.responses = [resp]
    s = code_prompt(ui)
    ui.controller.callback(tap(ui.controller, 'bk:sportybet'))
    assert ui.tg.sends[-1]['text'] == CODE_MESSAGES[status]
    assert s.state == 'WAITING' and s.ticket is None and s.pending_code is None
    ui.client.transport.request.assert_not_called()


def test_distinct_failure_copy():
    assert CODE_MESSAGES[bk.NOT_FOUND] == 'I couldn’t find that booking code. Check the code and bookmaker and try again.'
    assert CODE_MESSAGES[bk.UNSUPPORTED_BOOKMAKER] == 'That bookmaker isn’t supported for code import yet. You can still send a screenshot.'
    assert CODE_MESSAGES[bk.PROVIDER_UNAVAILABLE] == 'Code import is temporarily unavailable. You can send a screenshot instead.'
    assert len({CODE_MESSAGES[k] for k in (bk.MALFORMED_CODE, bk.UNKNOWN_BOOKMAKER, bk.UNSUPPORTED_BOOKMAKER, bk.NOT_FOUND,
                bk.EXPIRED, bk.EMPTY, bk.QUOTA_EXHAUSTED, bk.TIMEOUT, bk.PROVIDER_UNAVAILABLE)}) == 9


def test_more_bookmaker_is_unsupported_without_request(ui):
    s = code_prompt(ui)
    ui.controller.callback(tap(ui.controller, 'bk:other'))
    assert ui.tg.sends[-1]['text'] == CODE_MESSAGES[bk.UNSUPPORTED_BOOKMAKER]
    assert ui.transport.calls == [] and s.state == 'WAITING'


def test_forged_bookmaker_callback_values_rejected(ui):
    code_prompt(ui)
    ui.controller.callback(tap(ui.controller, 'bk:../../evil'))
    assert ui.tg.sends[-1]['text'] == CODE_MESSAGES[bk.UNSUPPORTED_BOOKMAKER] and ui.transport.calls == []


@pytest.mark.parametrize('code', ['AB_12CD', 'ABCDEFGH12345678X'])
def test_malformed_code_in_chat_friendly_no_prompt(ui, code):
    ui.controller.start(input_message())
    ui.controller.message(input_message(code, mid=2))
    assert ui.tg.sends[-1]['text'] == CODE_MESSAGES[bk.MALFORMED_CODE]
    assert ui.controller.sessions[KEY].state == 'WAITING'


def test_callback_ownership_and_stale_prompt(ui):
    s = code_prompt(ui)
    ui.controller.callback(tap(ui.controller, 'bk:sportybet', user='99'))
    ui.controller.callback(tap(ui.controller, 'bk:sportybet', mid='999'))
    ui.controller.callback(tap(ui.controller, 'bk:sportybet', token='forged-token'))
    assert s.state == 'CHOOSING_BOOKMAKER' and ui.transport.calls == []


def test_duplicate_concurrent_bookmaker_taps_decode_once(ui):
    s = code_prompt(ui)
    jobs = []
    ui.controller.runner = jobs.append
    cb = tap(ui.controller, 'bk:sportybet')
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: ui.controller.callback(cb), range(10)))
    assert len(jobs) == 1 and s.state == 'DECODING'
    jobs.pop()()
    assert len(ui.transport.calls) == 1 and s.state == 'PENDING_REVIEW'


def test_restart_invalidates_prompt_but_cache_saves_quota(ui):
    code_prompt(ui)
    cb = tap(ui.controller, 'bk:sportybet')
    restarted = TicketAuditController(ui.tg, ui.cfg, Mock(), clock=lambda: NOW, runner=lambda j: j(),
                                      booking_factory=lambda: ui.svc)
    restarted.callback(cb)
    assert 'expired' in ui.tg.answers[-1]['text'] and ui.transport.calls == []
    ui.controller.callback(cb)
    assert len(ui.transport.calls) == 1
    restarted.start(input_message())
    restarted.message(input_message('ab12cd', mid=3))
    restarted.callback(tap(restarted, 'bk:sportybet'))
    assert restarted.sessions[KEY].state == 'PENDING_REVIEW' and len(ui.transport.calls) == 1


def test_expired_session_drops_pending_code(ui):
    s = code_prompt(ui)
    ui.controller.clock = lambda: NOW + timedelta(minutes=16)
    ui.controller.callback(tap(ui.controller, 'bk:sportybet', token=s.token))
    assert s.pending_code is None and ui.transport.calls == []


def test_screenshot_or_text_ticket_still_works_after_code_prompt(ui):
    s = code_prompt(ui)
    ui.controller.message(input_message('Arsenal vs Chelsea: over 2.5, football, EPL, June 1 2030 at 15:00 UTC', mid=3))
    assert s.pending_code is None and s.state == 'PENDING_REVIEW'
    assert ui.client.transport.request.call_count == 1 and ui.transport.calls == []


def test_existing_text_audit_path_unchanged_with_codes_enabled(ui):
    ui.client.transport.request.return_value.json.return_value = response('not json')
    ui.controller.start(input_message())
    ui.controller.message(input_message('Not a readable ticket at all', mid=2))
    assert ui.tg.sends[-1]['text'] == UNREADABLE
    assert ui.client.transport.request.call_count == 1 and ui.transport.calls == []


def test_cancel_from_bookmaker_prompt(ui):
    s = code_prompt(ui)
    ui.controller.callback(tap(ui.controller, 'cancel'))
    assert not ui.controller.active(KEY) and s.pending_code is None and ui.transport.calls == []


def test_rate_limit_applies_to_code_retrieval(ui):
    for i in range(6):
        ui.controller.start(input_message())
        ui.controller.message(input_message(f'RATE{i:02d}', mid=10 + i))
        ui.controller.callback(tap(ui.controller, 'bk:sportybet'))
    assert ui.tg.sends[-1]['text'] == 'Please wait before sending another ticket. Try again later.'
    assert len(ui.transport.calls) == 5


def test_review_rendering_escapes_provider_strings(store):
    evil = event('<b>Evil</b>', 'A&B', 'sr:sport:1', '<script>', '1', '1', '1.5')
    r, _ = direct(store, Resp(body=sporty(evil)))
    result = service(store, r).resolve('sportybet', 'ESC123', NOW)
    page = '\n'.join(audit_format.booking_review(bk.to_ticket(result), result))
    assert '&lt;b&gt;Evil&lt;/b&gt;' in page and '<script>' not in page and 'A&amp;B' in page


@pytest.mark.parametrize('env,argv,result', [
    ({}, ['--source', 'sportybet-direct', '--live'], 'NOT_TESTED_NO_CODE'),
    ({'BOOKING_DIAG_CODE': 'AB12CD'}, ['--source', 'sportybet-direct'], 'NOT_RUN_PASS_--live'),
    ({'BOOKING_DIAG_CODE': '../x'}, ['--source', 'sportybet-direct', '--live'], bk.MALFORMED_CODE),
    ({'BOOKING_DIAG_CODE': 'AB12CD'}, ['--source', 'mybetcode', '--live', '--env-file', 'missing.env'], 'NOT_TESTED_NO_KEY'),
])
def test_diagnostic_makes_no_request_without_code_live_flag_or_key(monkeypatch, capsys, env, argv, result, tmp_path):
    import diagnose_booking_code as diag
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'data').mkdir()
    for k in ('BOOKING_DIAG_CODE', 'MYBETCODE_API_KEY'):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    diag.main(argv)
    out = json.loads(capsys.readouterr().out)
    assert out['result'] == result and out['live_requests'] == 0
    assert 'AB12CD' not in json.dumps(out)


def test_diagnostic_live_path_sanitized_with_mocked_transport(monkeypatch, capsys, tmp_path):
    import diagnose_booking_code as diag
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'data').mkdir()
    monkeypatch.setenv('BOOKING_DIAG_CODE', 'AB12CD')
    t = Transport(Resp(body=sporty(FOOTBALL_1X2, FOOTBALL_TOTAL)))
    monkeypatch.setattr(diag, 'SportyBetDirectResolver', lambda store, daily: SportyBetDirectResolver(store, daily, t))
    diag.main(['--source', 'sportybet-direct', '--live'])
    out = json.loads(capsys.readouterr().out)
    assert out['result'] == bk.OK and out['live_requests'] == 1 and len(t.calls) == 1
    assert out['http']['http_status'] == 200 and out['http']['json'] is True and out['all_legs_normalized']
    assert out['field_coverage']['home'] == 2 and 'AB12CD' not in json.dumps(out) and 'IGNORED' not in json.dumps(out)


def test_booking_modules_import_no_sports_provider_or_prediction_code():
    import ast
    import inspect
    from predbot.ticket_audit import booking, booking_sources
    for module in (booking, booking_sources):
        tree = ast.parse(inspect.getsource(module))
        names = {n.module or '' for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        names |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        assert not any(x.startswith(('predbot.providers', 'predbot.model', 'predbot.jobs', 'predbot.cache',
                                     'predbot.storage', 'predbot.markets')) for x in names)
