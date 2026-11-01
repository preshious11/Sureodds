import importlib.util
import io
from pathlib import Path
import sqlite3

import pytest
from predbot.config import Config
from predbot.selection.top import select_top
from predbot.selection.daily import select_daily


def candidate(price):
    return dict(id=1,fixture_key='fixture',status='pending',start_utc='2030-06-02T12:00:00Z',
                sport='basketball',league='NBA',market='result2',selection='home',period='full',
                stat='points',entity='all',prob=.9,price=price,ev=.05,books=10,quality='main',sources='{}')


@pytest.mark.parametrize('selector,target,ceiling',[(select_top,2.,2.3),(select_daily,10.,12.)])
@pytest.mark.parametrize('price,valid',[(1.44,False),(2.,True),(2.2,True),(2.6,False)])
def test_combined_target_is_mandatory(selector,target,ceiling,price,valid):
    cfg=Config()
    cfg.top_odds_target=2.;cfg.top_odds_max=2.3
    if selector is select_daily:
        cfg.daily_min_odds=1.;cfg.daily_max_odds=20.;price*=5
    rows,diag=selector([candidate(price)],cfg,'2030-06-01T12:00:00Z')
    assert bool(rows)==valid
    if rows: assert target<=diag['combined_odds']<=ceiling


def test_snapshot_preserves_oracle_ids_epoch_and_sequence(tmp_path):
    module=Path(__file__).resolve().parents[2]/'workers/bot/ssh-relay/snapshot.py'
    spec=importlib.util.spec_from_file_location('snapshot',module); m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
    source=tmp_path/'source.db';c=sqlite3.connect(source)
    c.executescript('CREATE TABLE picks(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT); INSERT INTO picks VALUES(9000,"oracle-only"); CREATE TABLE sync_state(epoch INTEGER); INSERT INTO sync_state VALUES(42);');c.close()
    out=io.StringIO();m.export(source,out)
    restored=sqlite3.connect(':memory:');restored.executescript(out.getvalue())
    assert restored.execute('select * from picks').fetchall()==[(9000,'oracle-only')]
    assert restored.execute('select epoch from sync_state').fetchone()[0]==42
    restored.execute('insert into picks(name) values("next")')
    assert restored.execute('select max(id) from picks').fetchone()[0]==9001
    assert sqlite3.connect(source).execute('select count(*) from picks').fetchone()[0]==1


def test_repair_id_collision_preserves_history_and_is_idempotent(tmp_path):
    import json
    spec=importlib.util.spec_from_file_location('repair',Path(__file__).resolve().parents[1]/'tools/repair_oracle_ids.py')
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
    db=sqlite3.connect(':memory:')
    db.executescript('''CREATE TABLE picks(id INTEGER PRIMARY KEY AUTOINCREMENT,pick_date TEXT,provider TEXT,provider_event_id TEXT,
      fixture_key TEXT,canonical_event_id TEXT,is_representative INTEGER,home TEXT,away TEXT,market TEXT,selection TEXT,line REAL,
      price REAL,period TEXT,stat TEXT,entity TEXT,status TEXT);
      CREATE TABLE prediction_memberships(prediction_id INTEGER,collection TEXT,rank INTEGER);
      CREATE TABLE prediction_cache(cache_key TEXT,local_date TEXT,payload_json TEXT);''')
    row=dict(id=1,pick_date='2030-06-01',provider='p',provider_event_id='e',fixture_key='f',canonical_event_id='c',is_representative=1,
             home='Home',away='Away',market='result2',selection='home',line=None,price=2.,period='full',stat='points',entity='all',status='pending')
    cols=','.join(row);values=','.join('?' for _ in row)
    old=dict(row,pick_date='2030-05-31',status='won')
    db.execute('INSERT INTO picks('+cols+') VALUES('+values+')',tuple(old.values()))
    batch=tmp_path/'generation-20300601';batch.mkdir()
    scratch=sqlite3.connect(':memory:');scratch.execute(db.execute("select sql from sqlite_master where name='picks'").fetchone()[0])
    scratch.execute('INSERT INTO picks('+cols+') VALUES('+values+')',tuple(row.values()))
    sql=next(l for l in scratch.iterdump() if l.startswith('INSERT INTO "picks"'))
    (batch/'p0_1_picks_0000.sql').write_text(sql)
    pick=dict(row,pick_id=1)
    payload=dict(all_predictions=[pick],top_picks=[pick],daily_picks=[])
    db.execute('INSERT INTO prediction_cache VALUES(?,?,?)',('day','2030-06-01',json.dumps(payload)))
    first=m.repair(db,tmp_path)
    assert first['inserted']==1
    assert db.execute('SELECT status FROM picks WHERE id=1').fetchone()[0]=='won'
    restored=json.loads(db.execute('SELECT payload_json FROM prediction_cache').fetchone()[0])
    assert restored['top_picks'][0]['pick_id']==2
    assert m.repair(db,tmp_path)['inserted']==0
