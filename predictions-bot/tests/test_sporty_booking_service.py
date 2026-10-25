"""Private relay unit doubles; MOCK01 is not a provider-returned booking code."""
import hashlib
import io
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from predbot.config import Config
from predbot.sporty_booking import Selection, SportyBooking, BookingError
from predbot.sporty_booking_service import BookingService, BookingHTTPApplication


def manifest():
    rows=[dict(canonical_event_id='unit-event',canonical_competition_id='unit-comp',sport='football',
               home='Home',away='Away',start_utc='2099-01-01T12:00:00Z',market='total',period='full',
               stat='points',entity='all',selection='over',line=2.5,price=1.8)]
    raw=json.dumps(rows,separators=(',',':'))
    return {'manifest_json':raw,'fingerprint':hashlib.sha256(raw.encode()).hexdigest()}


def service(outcome='manual',enabled=True):
    calls=[]
    class Adapter(SportyBooking):
        def create_booking_code(self,selections):
            calls.append(selections)
            if outcome=='timeout': raise BookingError('creation_unknown_do_not_retry')
            if outcome=='exception': raise RuntimeError('sensitive upstream details')
            return SimpleNamespace(programmatic_verified=outcome=='verified',
                status='owner_confirmation_required' if outcome=='verified' else 'created_manual_verification_required',
                code='MOCK01',selections=[SimpleNamespace(original=s) for s in selections])
    db=sqlite3.connect(':memory:')
    return BookingService(db,lambda:Adapter({'unit-comp':'unit-tournament'}),enabled=enabled),calls


def test_default_flag_and_service_disabled(monkeypatch):
    monkeypatch.delenv('SPORTYBET_BOOKING_ENABLED',raising=False)
    assert Config().sportybet_booking_enabled is False
    svc,calls=service(enabled=False)
    assert svc.handle(manifest())=={'status':'disabled'}
    assert calls==[]


@pytest.mark.parametrize('outcome',['manual','timeout','exception'])
def test_unverified_and_ambiguous_results_never_disclose_code_or_retry(outcome):
    svc,calls=service(outcome)
    for _ in range(2): assert svc.handle(manifest())=={'status':'unverified'}
    assert len(calls)==1


def test_verified_adapter_contract_only_returns_exact_unchanged_selection():
    svc,calls=service('verified')
    req=manifest();result=svc.handle(req)
    assert result['status']=='verified'
    assert result['exact_membership_verified'] is True
    assert result['fingerprint']==req['fingerprint']
    assert calls[0]==[Selection.from_candidate(json.loads(req['manifest_json'])[0])]


def test_bad_hash_and_invalid_contract_prevent_adapter_write():
    svc,calls=service()
    req=manifest();req['fingerprint']='bad'
    assert svc.handle(req)=={'status':'unverified'}
    req=manifest();rows=json.loads(req['manifest_json']);rows[0]['sport']='baseball'
    req['manifest_json']=json.dumps(rows);req['fingerprint']=hashlib.sha256(req['manifest_json'].encode()).hexdigest()
    assert svc.handle(req)=={'status':'unverified'}
    assert calls==[]


def test_claim_survives_service_restart(tmp_path):
    path=tmp_path/'private-claims.db'
    req=manifest();db=sqlite3.connect(path)
    db.execute('CREATE TABLE booking_claims(fingerprint TEXT PRIMARY KEY)')
    db.execute('INSERT INTO booking_claims VALUES (?)',(req['fingerprint'],));db.commit();db.close()
    def factory():
        class A(SportyBooking):
            def create_booking_code(self,*a): pytest.fail('must not repeat a claimed booking')
        return A({'unit-comp':'unit-tournament'})
    svc=BookingService(sqlite3.connect(path),factory,enabled=True)
    assert svc.handle(req)=={'status':'unverified'}


@pytest.mark.parametrize('change,status',[
    ({'REQUEST_METHOD':'GET'},'404'),({'PATH_INFO':'/other'},'404'),
    ({'HTTP_AUTHORIZATION':'Bearer wrong'},'401'),
    ({'HTTP_AUTHORIZATION':'非ASCII'},'401'),
    ({'CONTENT_LENGTH':'999999'},'413'),({'CONTENT_TYPE':'text/plain'},'415'),
])
def test_private_http_authentication_and_limits(change,status):
    svc,calls=service('verified')
    token='unit-token-'+'x'*32
    app=BookingHTTPApplication(svc,token)
    body=json.dumps(manifest()).encode()
    env={'REQUEST_METHOD':'POST','PATH_INFO':'/v1/bookings','HTTP_AUTHORIZATION':'Bearer '+token,
         'CONTENT_TYPE':'application/json','CONTENT_LENGTH':str(len(body)),'wsgi.input':io.BytesIO(body)}
    env.update(change)
    statuses=[]
    response=b''.join(app(env,lambda s,h:statuses.append(s)))
    assert statuses[0].startswith(status)
    assert calls==[]
    assert b'MOCK01' not in response


def test_private_http_roundtrip_is_adapter_verified_not_http_status_only():
    svc,calls=service('manual')
    token='unit-token-'+'x'*32
    body=json.dumps(manifest()).encode()
    env={'REQUEST_METHOD':'POST','PATH_INFO':'/v1/bookings','HTTP_AUTHORIZATION':'Bearer '+token,
         'CONTENT_TYPE':'application/json','CONTENT_LENGTH':str(len(body)),'wsgi.input':io.BytesIO(body)}
    statuses=[]
    response=b''.join(BookingHTTPApplication(svc,token)(env,lambda s,h:statuses.append(s)))
    assert statuses==['200 OK']
    assert json.loads(response)=={'status':'unverified'}
    assert len(calls)==1


def test_worker_request_migration_claims_are_durable_and_unique():
    schema=Path(__file__).resolve().parents[2]/'workers/migrations/0017_sporty_booking.sql'
    with sqlite3.connect(':memory:') as db:
        db.executescript(schema.read_text(encoding='utf-8'))
        sql="INSERT OR IGNORE INTO sporty_booking_requests(request_key,status) VALUES (?,'processing')"
        assert db.execute(sql,('unit-hash',)).rowcount==1
        assert db.execute(sql,('unit-hash',)).rowcount==0
        db.execute("UPDATE sporty_booking_requests SET status='unverified' WHERE request_key='unit-hash'")
        assert db.execute(sql,('unit-hash',)).rowcount==0


def test_concurrent_duplicates_reach_adapter_only_once():
    entered=threading.Event();release=threading.Event();calls=[]
    class Adapter(SportyBooking):
        def create_booking_code(self,selections):
            calls.append(selections);entered.set()
            assert release.wait(5)
            raise BookingError('creation_unknown_do_not_retry')
    db=sqlite3.connect(':memory:',check_same_thread=False)
    svc=BookingService(db,lambda:Adapter({'unit-comp':'unit-tournament'}),enabled=True)
    with ThreadPoolExecutor(max_workers=8) as pool:
        first=pool.submit(svc.handle,manifest())
        assert entered.wait(3)
        try:
            others=[pool.submit(svc.handle,manifest()) for _ in range(7)]
            assert all(f.result(timeout=3)=={'status':'unverified'} for f in others)
        finally: release.set()
        assert first.result(timeout=3)=={'status':'unverified'}
    assert len(calls)==1
