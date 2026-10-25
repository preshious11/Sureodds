"""Offline contract/failure tests, NOT successful provider response fixtures.

Catalogue dictionaries below are synthetic inputs to the pure resolver. No
generated booking code or successful HTTP response is fabricated. Live success
tests require genuine captures and owner verification and are not claimed here.
"""
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path

import pytest
import requests

from predbot.sporty_booking import (
    AnonymousAuth, BookingError, Selection, SportyBooking, PublicTransport, envelope,
)

NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)


def selection(**changes):
    s = Selection('canonical-1','competition-1','football','Home','Away',
                  '2030-01-02T12:00:00Z','total','full','points','all','over',2.5,1.8)
    return replace(s,**changes)


def adapter():
    return SportyBooking({'competition-1':'synthetic-tournament'},clock=lambda: NOW)


def catalogue(s=None):
    s = s or selection()
    return [('football','synthetic-tournament',dict(
        eventId='sr:match:1', homeTeamName=s.home,awayTeamName=s.away,
        estimateStartTime=1893585600000,matchStatus='Not start',markets=[dict(
            id='18',desc='Total',specifier='total=2.5',status=0,outcomes=[dict(
                id='12',desc='Over 2.5',odds='1.8',isActive=1)])]))]


def assert_status(status,call):
    with pytest.raises(BookingError) as exc:
        call()
    assert exc.value.status == status


def test_valid_single_preserves_contract_and_exact_post_identifiers():
    s = selection()
    r, = adapter().resolve_selections([s],catalogue())
    assert r.original == s
    assert r.current_odds == s.price
    assert r.payload() == dict(eventId='sr:match:1',marketId='18',
                               specifier='total=2.5',outcomeId='12')


def test_valid_two_leg_resolution_preserves_order():
    a,b = selection(),selection(canonical_event_id='canonical-2',home='Other',away='Visitors')
    events=catalogue(a)+catalogue(b)
    events[1][2]['eventId']='sr:match:2'
    resolved=adapter().resolve_selections([a,b],events)
    assert [r.original for r in resolved] == [a,b]
    assert [r.event_id for r in resolved] == ['sr:match:1','sr:match:2']


@pytest.mark.parametrize('changes',[
    {'sport':'baseball'}, {'sport':'mma'}, {'stat':'corners'}, {'entity':'home'},
    {'market':'btts','sport':'basketball','selection':'yes','line':None},
    {'market':'result3','sport':'tennis','selection':'draw','line':None},
    {'period':'1i'}, {'line':float('nan')}, {'line':None},
])
def test_invalid_or_unsupported_contract_rejected(changes):
    with pytest.raises(BookingError):
        adapter().validate_selections([selection(**changes)])


@pytest.mark.parametrize('changes',[
    {'canonical_event_id':''},{'home':''},{'away':'Home'},
    {'start_utc':'2020-01-01T00:00:00Z'}, {'start_utc':'bad'},
    {'price':float('inf')},{'price':True}, {'sporty_event_id':'not-an-event'},
])
def test_invalid_fixture_and_odds(changes):
    with pytest.raises(BookingError):
        adapter().validate_selections([selection(**changes)])


@pytest.mark.parametrize('items',[[],[selection()]*2,[selection()]*6])
def test_slip_size_and_duplicates(items):
    with pytest.raises(BookingError):
        adapter().validate_selections(items)


def test_binding_must_be_explicit():
    assert_status('unverified_competition_binding',
                  lambda: SportyBooking({},clock=lambda:NOW).validate_selections([selection()]))


@pytest.mark.parametrize('change',[
    {'eventId':None},{'homeTeamName':'Away','awayTeamName':'Home'},
    {'estimateStartTime':1893585600001},{'matchStatus':'Live'}, {'markets':[]},
])
def test_fixture_and_market_mismatches(change):
    events=catalogue()
    events[0][2].update(change)
    with pytest.raises(BookingError):
        adapter().resolve_selections([selection()],events)


@pytest.mark.parametrize('change',[
    {'id':None},{'status':1},{'status':False},{'specifier':None},
    {'specifier':'total=2.5|variant=promo'}, {'specifier':'total=3.5'},
    {'desc':'First Half Total'}, {'outcomes':[]},
])
def test_exact_market_required(change):
    events=catalogue()
    events[0][2]['markets'][0].update(change)
    assert_status('market_missing_or_ambiguous',lambda:adapter().resolve_selections([selection()],events))


@pytest.mark.parametrize('change',[
    {'id':None},{'id':'unknown'},{'isActive':0},{'isActive':True},{'desc':'Under 2.5'},
])
def test_exact_active_outcome_required(change):
    events=catalogue()
    events[0][2]['markets'][0]['outcomes'][0].update(change)
    assert_status('market_missing_or_ambiguous',lambda:adapter().resolve_selections([selection()],events))


def test_wrong_authoritative_event_id_never_falls_back_to_names():
    assert_status('fixture_missing_or_ambiguous',lambda:adapter().resolve_selections(
        [selection(sporty_event_id='sr:match:999')],catalogue()))


def test_wrong_competition_and_ambiguous_fixture():
    assert_status('fixture_missing_or_ambiguous',lambda:adapter().resolve_selections(
        [selection()],[('football','other',catalogue()[0][2])]))
    assert_status('fixture_missing_or_ambiguous',lambda:adapter().resolve_selections(
        [selection()],catalogue()*2))


def test_away_handicap_preserves_selected_side_signed_line():
    s=selection(market='handicap',selection='away',line=1.5)
    events=catalogue()
    events[0][2]['markets']=[dict(id='16',desc='Asian Handicap',specifier='hcp=-1.5',status=0,
                                 outcomes=[dict(id='1715',desc='Away',odds='1.8',isActive=1)])]
    r,=adapter().resolve_selections([s],events)
    assert r.original.line == 1.5
    assert r.specifier == 'hcp=-1.5'


def test_basketball_decoder_is_not_proof_of_live_acquisition():
    s=selection(sport='basketball',market='result2',selection='home',line=None)
    assert adapter().validate_selections([s]) == (s,)
    assert_status('live_sport_acquisition_unverified',lambda:adapter()._events([s]))


class FailureTransport:
    def __init__(self,status='provider_rejected'):
        self.calls=[]
        self.status=status

    def request(self,method,path,**kwargs):
        self.calls.append((method,path,kwargs))
        raise BookingError(self.status)


def prepared(monkeypatch,status='provider_rejected',events=None):
    a=adapter()
    a.transport=FailureTransport(status)
    monkeypatch.setattr(a,'_events',lambda _:catalogue() if events is None else events)
    return a


def test_changed_odds_prevents_write(monkeypatch):
    a=prepared(monkeypatch)
    assert_status('odds_changed',lambda:a.create_booking_code([selection(price=1.9)]))
    assert a.transport.calls == []


def test_explicit_odds_change_acceptance_still_preserves_exact_selection(monkeypatch):
    a=prepared(monkeypatch)
    assert_status('provider_rejected',lambda:a.create_booking_code([selection(price=1.9)],accept_odds_changes=True))
    assert len(a.transport.calls)==1
    assert a.transport.calls[0][2]['payload']['selections'][0]['specifier']=='total=2.5'


def test_multi_failure_never_posts_partial_slip(monkeypatch):
    a=prepared(monkeypatch)
    s2=selection(canonical_event_id='other',home='Missing')
    assert_status('fixture_missing_or_ambiguous',lambda:a.create_booking_code([selection(),s2]))
    assert a.transport.calls == []


@pytest.mark.parametrize('failure,expected',[
    ('timeout','creation_unknown_do_not_retry'),
    ('network_unavailable','creation_unknown_do_not_retry'),
    ('rate_limited','rate_limited'), ('access_blocked','access_blocked'),
    ('provider_rejected','provider_rejected'), ('malformed_response','malformed_response'),
])
def test_post_failure_never_retries(monkeypatch,failure,expected):
    a=prepared(monkeypatch,failure)
    assert_status(expected,lambda:a.create_booking_code([selection()]))
    assert len(a.transport.calls)==1


@pytest.mark.parametrize('code',['','../escape','abc!','A'*17,None])
def test_invalid_codes_do_not_request(code):
    a=adapter();a.transport=FailureTransport()
    assert_status('invalid_booking_code',lambda:a.get_booking_status(code))
    assert a.transport.calls==[]


@pytest.mark.parametrize('body',[None,[],{}, {'bizCode':40000,'message':'private session info'},
                                       {'bizCode':10000,'data':[]}])
def test_malformed_and_rejected_envelopes(body):
    assert_status('provider_rejected',lambda:envelope(body))


def test_existing_reconstruction_cannot_count_as_live_evidence():
    fixture=Path(__file__).parent/'fixtures/sportybet_share_4leg_sanitized.json'
    data=json.loads(fixture.read_text(encoding='utf-8'))
    with pytest.raises(BookingError):
        envelope(data)


def test_genuine_catalogue_capture_proves_only_observed_market_shape():
    fixture=Path(__file__).parent/'fixtures/sportybet_catalogue_observed_20261025.json'
    body=envelope(json.loads(fixture.read_text(encoding='utf-8')))
    events=[e for t in body['tournaments'] for e in t['events']]
    assert len(events)==4
    assert all(e['eventId'].startswith('sr:match:') and type(e['estimateStartTime']) is int for e in events)
    first=events[0]
    assert first['matchStatus']=='Not start'
    for mid in ('1','18'):
        markets=[m for m in first['markets'] if m['id']==mid]
        assert markets
        assert all(m['status']==0 and m['outcomes'] for m in markets)
    # No canonical Kovqi identity is invented from this public catalogue.
    assert 'canonical_event_id' not in first


@pytest.mark.parametrize('method,path',[
    ('POST','/orders/bet'),('GET','https://elsewhere/'),('DELETE','/orders/share'),
])
def test_transport_fixed_routes_only(method,path):
    assert_status('forbidden_route',lambda:PublicTransport().request(method,path))


def test_transport_budget_prevents_network():
    assert_status('request_budget_exhausted',lambda:PublicTransport(limit=0).request('GET','/factsCenter/pcUpcomingEvents'))


def test_transport_operation_deadline_prevents_next_request():
    import time
    transport=PublicTransport()
    transport.deadline=time.monotonic()-1
    assert_status('operation_timeout',lambda:transport.request('GET','/factsCenter/pcUpcomingEvents'))
    assert transport.used==0


@pytest.mark.parametrize('value',[float('nan'),float('inf'),True,0,-1,121])
def test_transport_rejects_invalid_operation_timeout(value):
    assert_status('invalid_timeout',lambda:PublicTransport(total_timeout=value))


@pytest.mark.parametrize('error,status',[
    (requests.Timeout(),'timeout'),(requests.ConnectionError(),'network_unavailable'),
])
def test_transport_network_failures(monkeypatch,error,status):
    def fail(*args,**kwargs):
        raise error
    monkeypatch.setattr(requests.Session,'request',fail)
    assert_status(status,lambda:PublicTransport().request('GET','/factsCenter/pcUpcomingEvents'))


def test_validation_has_no_network_dependency(monkeypatch):
    def forbidden(*args,**kwargs):
        pytest.fail('Validation must not perform network IO')
    monkeypatch.setattr(requests.Session,'request',forbidden)
    assert adapter().validate_selections([selection()]) == (selection(),)


def test_anonymous_auth_prepares_real_request_without_netrc_or_credentials(monkeypatch):
    def forbidden(*args,**kwargs):
        pytest.fail('Anonymous session must not consult netrc')
    monkeypatch.setattr(requests.sessions,'get_netrc_auth',forbidden)
    with requests.Session() as session:
        session.auth=AnonymousAuth()
        prepared=session.prepare_request(requests.Request(
            'GET','https://www.sportybet.com/api/ng/factsCenter/pcUpcomingEvents'))
    assert 'Authorization' not in prepared.headers
    assert 'Cookie' not in prepared.headers


@pytest.mark.parametrize('status,kind,body,expected',[
    (403,'text/html',b'','access_blocked'),
    (429,'application/json',b'','rate_limited'),
    (500,'application/json',b'','http_error'),
    (302,'text/html',b'','http_error'),
    (200,'text/html',b'<html>','non_json_response'),
    (200,'application/json',b'{broken','malformed_response'),
])
def test_http_failures_are_fixed_statuses(monkeypatch,status,kind,body,expected):
    class Refusal:
        status_code=status
        headers={'Content-Type':kind}
        def __enter__(self):
            return self
        def __exit__(self,*args):
            return False
        def iter_content(self,*args):
            yield body
    def request(session,*args,**kwargs):
        assert isinstance(session.auth,requests.auth.AuthBase)
        assert kwargs['allow_redirects'] is False
        return Refusal()
    monkeypatch.setattr(requests.Session,'request',request)
    assert_status(expected,lambda:PublicTransport().request('GET','/factsCenter/pcUpcomingEvents'))


def test_observed_access_block_is_not_booking_success():
    fixture=Path(__file__).parent/'fixtures/sportybet_booking_access_observation.json'
    observation=json.loads(fixture.read_text(encoding='utf-8'))
    assert observation['status_code']==403
    assert observation['booking_codes']==[]
    assert observation['booking_post_attempted'] is False


def test_archive_inventory_is_read_only_and_excludes_raw_extras(tmp_path):
    import hashlib
    import sqlite3
    from tools.sporty_booking import inventory
    source=tmp_path/'archive.db'
    with sqlite3.connect(source) as db:
        db.execute('CREATE TABLE custom_pick_candidates(candidate_id TEXT,payload_json TEXT)')
        from dataclasses import asdict
        row=asdict(selection())
        row['canonical_competition_id']=row.pop('competition')
        row['private_extra']='must-not-escape'
        db.execute('INSERT INTO custom_pick_candidates VALUES (?,?)',('archived',json.dumps(row)))
    before=hashlib.sha256(source.read_bytes()).hexdigest()
    report=inventory(source,now=NOW)
    assert report['summary']['football']['future']==1
    assert report['booking_codes']==[]
    assert 'must-not-escape' not in json.dumps(report)
    assert hashlib.sha256(source.read_bytes()).hexdigest()==before
