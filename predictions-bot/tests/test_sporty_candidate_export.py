"""Synthetic local archive inputs only; never submitted to a service."""
import hashlib
import json
import sqlite3
from datetime import datetime,timezone

from tools.export_sporty_candidates import inspect
from predbot.sporty_booking import Selection

NOW=datetime(2030,1,1,tzinfo=timezone.utc)


def archive(path,**changes):
    row=dict(canonical_event_id='test-event',canonical_competition_id='test-comp',
             sport='football',home='Test Home',away='Test Away',start_utc='2030-01-02T12:00:00Z',
             market='total',period='full',stat='points',entity='all',selection='over',line=2.5,price=1.8,
             unrelated_private_data='excluded')
    row.update(changes)
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE custom_pick_candidates(payload_json TEXT)')
        db.execute('INSERT INTO custom_pick_candidates VALUES (?)',(json.dumps(row),))
    return row


def test_read_only_export_preserves_contract_and_excludes_private_data(tmp_path):
    path=tmp_path/'source.db';row=archive(path)
    before=hashlib.sha256(path.read_bytes()).hexdigest()
    result=inspect([path],NOW)
    assert Selection.from_candidate(result['selections'][0])==Selection.from_candidate(row)
    assert 'unrelated_private_data' not in json.dumps(result)
    assert result['competition_bindings']=={}
    assert result['status']=='pending_sporty_resolution'
    assert hashlib.sha256(path.read_bytes()).hexdigest()==before


def test_expired_rows_are_not_shifted_or_exported(tmp_path):
    path=tmp_path/'source.db';archive(path,start_utc='2020-01-02T12:00:00Z')
    result=inspect([path],NOW)
    assert result['selections']==[]
    assert result['future_valid_unique_by_sport_market']==[]
    assert result['status']=='blocked_no_upcoming_bookable_candidates'


def test_missing_canonical_identity_is_rejected(tmp_path):
    path=tmp_path/'source.db';archive(path,canonical_event_id='')
    result=inspect([path],NOW)
    assert result['selections']==[]
    assert result['sources'][0]['tables']['custom_pick_candidates']['rejections']=={'missing_fixture_identity':1}


def test_overlapping_archives_not_counted_twice(tmp_path):
    a,b=tmp_path/'a.db',tmp_path/'b.db'
    archive(a);archive(b)
    result=inspect([a,b],NOW)
    assert len(result['selections'])==1
    assert result['future_valid_unique_by_sport_market']==[{'sport':'football','market':'total','count':1}]
