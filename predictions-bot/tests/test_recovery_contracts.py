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


@pytest.mark.parametrize('price',[0.,-1.,1.,float('nan'),float('inf'),None,True])
def test_invalid_odds_cannot_enter_ticket_search(price):
    for selector in (select_top,select_daily):
        rows,diag=selector([candidate(price)],Config(),'2030-06-01T12:00:00Z')
        assert not rows
        assert diag['rejections']['invalid_candidate_fields']==1


def test_accumulator_uses_product_of_distinct_fixture_prices():
    rows=[dict(candidate(1.44),id=i,fixture_key=str(i)) for i in (1,2)]
    selected,diag=select_top(rows,Config(),'2030-06-01T12:00:00Z')
    assert len(selected)==2
    assert diag['combined_odds']==pytest.approx(1.44*1.44)


@pytest.mark.parametrize('selector,target,ceiling',[(select_top,2.,2.3),(select_daily,5.,7.)])
@pytest.mark.parametrize('price,valid',[(1.44,False),(2.,True),(2.2,True),(2.6,False)])
def test_combined_target_is_mandatory(selector,target,ceiling,price,valid):
    cfg=Config()
    cfg.top_odds_target=2.;cfg.top_odds_max=2.3
    if selector is select_daily:
        cfg.daily_min_odds=1.;cfg.daily_max_odds=20.;cfg.daily_odds_min=5.;cfg.daily_odds_max=7.;price*=3
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


@pytest.mark.parametrize('result_status,expected',[('final','won'),('scheduled','pending'),('in_progress','pending')])
def test_issued_ticket_scheduler_uses_final_evidence_and_is_idempotent(db,monkeypatch,result_status,expected):
    from predbot.jobs import ledger_settlement,settle
    from predbot.providers.base import MatchResult
    migration=Path(__file__).resolve().parents[2]/'workers/migrations/0014_issued_picks_ledger.sql'
    db.conn.executescript(migration.read_text())
    db.execute("INSERT INTO issued_tickets VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
               ('ticket','custom','owner','group','2020-01-01','Africa/Lagos','key','2020-01-01T00:00:00Z',2.,1,'issued','2020-01-01T00:00:00Z'))
    # Column order is explicit to make the issued price and contract immutable.
    db.execute("INSERT INTO issued_ticket_legs(ticket_id,leg_index,canonical_event_id,sport,home,away,market,period,stat,entity,selection,price,probability) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
               ('ticket',0,'event','football','Home','Away','result3','full','points','all','home',2.,.7))
    row=dict(candidate(1.44),canonical_event_id='event',sport='football',home='Home',away='Away',start_utc='2020-01-01T12:00:00Z')
    monkeypatch.setattr(db,'ticket_candidate_rows',lambda day:[row])
    monkeypatch.setattr(settle,'stored_result',lambda row,db:MatchResult(result_status,{'full':(2.,1.)}))
    result=ledger_settlement.run(None,db)
    leg=db.query('SELECT * FROM issued_ticket_legs')[0]
    assert leg['settlement_status']==expected
    assert leg['price']==2.
    if expected=='won':
        assert leg['payout_multiplier']==2.
        assert result['settled_legs']==1
        assert ledger_settlement.run(None,db)['settled_legs']==0
        assert db.query('SELECT status FROM issued_tickets')[0]['status']=='settled'


@pytest.mark.parametrize('authoritative,expected',[(True,'won'),(False,'pending')])
def test_overdue_prediction_requires_authoritative_result(db,monkeypatch,authoritative,expected):
    from datetime import datetime,timezone
    from predbot.jobs import settle
    from predbot.providers.base import MatchResult
    from tests.test_settle_parallel import _pick
    _pick(db,0,'football','epl','Home')
    monkeypatch.setattr(settle,'utcnow',lambda:datetime(2030,6,5,tzinfo=timezone.utc))
    class Resolver:
        providers={}
        def resolve(self,*args,**kwargs):
            return MatchResult('final',{'full':(2.,1.)},source='verified-test') if authoritative else None
    result=settle.run(Resolver(),db,Config())
    assert db.query('SELECT status FROM picks')[0]['status']==expected
    assert result['void']==0


def test_relay_ledger_update_requires_same_issued_contract(db):
    import sys
    scripts=Path(__file__).resolve().parents[2]/'workers/scripts'
    monkey_path=str(scripts)
    sys.path.insert(0,monkey_path)
    try:
        import d1_sync
    finally:
        sys.path.remove(monkey_path)
    db.execute('CREATE TABLE issued_ticket_legs(ticket_id TEXT,leg_index INTEGER,price REAL,settlement_status TEXT,payout_multiplier REAL,settled_at TEXT,PRIMARY KEY(ticket_id,leg_index))')
    db.execute("INSERT INTO issued_ticket_legs VALUES('ticket',0,2.,'pending',NULL,NULL)")
    cols=['ticket_id','leg_index','price','settlement_status','payout_multiplier','settled_at']
    head,suffix=d1_sync.write_policy('issued_ticket_legs',cols)
    sql=head+'('+','.join('?' for _ in cols)+')'+suffix
    db.execute(sql,('ticket',0,1.44,'won',1.44,'2030-06-01'))
    assert db.query('SELECT settlement_status FROM issued_ticket_legs')[0][0]=='pending'
    db.execute(sql,('ticket',0,2.,'won',2.,'2030-06-01'))
    assert db.query('SELECT settlement_status FROM issued_ticket_legs')[0][0]=='won'

    db.execute(sql,('ticket',0,2.,'lost',0.,'2030-06-02'))
    assert db.query('SELECT settlement_status FROM issued_ticket_legs')[0][0]=='won'


def test_recovered_final_queues_one_end_only_after_real_publication(db):
    from datetime import datetime,timezone
    from predbot.jobs import settle
    from predbot.providers.base import MatchResult
    from tests.test_settle_parallel import _pick
    _pick(db,0,'football','epl','Home')
    pid=db.query('SELECT id FROM picks')[0][0]
    db.settle_pick(pid,'won',{'status':'final','scores':{'full':[2.,1.]},'source':'verified-test'})
    db.execute('CREATE TABLE match_events(pick_date TEXT,fixture_key TEXT,event_type TEXT,text TEXT,created_at TEXT,PRIMARY KEY(pick_date,fixture_key,event_type))')
    now=datetime(2030,6,2,tzinfo=timezone.utc)
    assert settle.queue_final_events(db,now)==0
    db.execute("INSERT INTO posts(kind,post_date,message_id,created_at) VALUES('daily','2030-06-01','123','2030-06-01T08:30:00Z')")
    assert settle.queue_final_events(db,now)==1
    assert settle.queue_final_events(db,now)==0
    assert db.query('SELECT event_type FROM match_events')[0][0]=='end'
    assert db.query('SELECT settlement_done FROM live_fixture_state')[0][0]==1


def test_explicit_job_metadata_diff_preserves_coordinator_lease(tmp_path):
    import sys
    scripts=str(Path(__file__).resolve().parents[2]/'workers/scripts')
    sys.path.insert(0,scripts)
    try: import d1_sync
    finally: sys.path.remove(scripts)
    paths=[tmp_path/n for n in ('base.db','scratch.db','target.db')]
    for path in paths:
        c=sqlite3.connect(path)
        c.execute('CREATE TABLE job_state(name TEXT PRIMARY KEY,lease_owner TEXT,last_run_at TEXT,last_result TEXT,updated_at TEXT)')
        c.execute("INSERT INTO job_state VALUES('settle','original',NULL,NULL,'2030-06-01')")
        c.commit();c.close()
    c=sqlite3.connect(paths[1]);c.execute("UPDATE job_state SET last_run_at='2030-06-02',last_result='OK',updated_at='2030-06-02'");c.commit();c.close()
    target=sqlite3.connect(paths[2]);target.execute("UPDATE job_state SET lease_owner='new-worker'");target.commit()
    out=tmp_path/'plan'
    assert d1_sync.cmd_diff(str(paths[0]),str(paths[1]),str(out),only_tables=['job_state'])==0
    for file in sorted(out.glob('*.sql')): target.executescript(file.read_text())
    assert target.execute('SELECT lease_owner,last_result FROM job_state').fetchone()==('new-worker','OK')


def test_database_guards_reject_stale_ids_and_unverified_voids(db):
    from tests.test_settle_parallel import _pick
    migration=Path(__file__).resolve().parents[2]/'workers/migrations/0017_oracle_recovery_guards.sql'
    db.conn.executescript(migration.read_text())
    _pick(db,0,'football','epl','Home')
    row=dict(db.query('SELECT * FROM picks')[0]);row['pick_date']='2030-06-02'
    with pytest.raises(sqlite3.IntegrityError,match='different immutable contract'):
        db.execute('INSERT OR IGNORE INTO picks('+','.join(row)+') VALUES('+','.join('?' for _ in row)+')',tuple(row.values()))
    with pytest.raises(sqlite3.IntegrityError,match='unverified timeout'):
        db.execute("UPDATE picks SET status='void',result_json=?",('{"reason":"settlement deadline expired"}',))
    assert db.query('SELECT status FROM picks')[0][0]=='pending'
    db.settle_pick(row['id'],'void',{'status':'cancelled','source':'verified-test'})
    assert db.query('SELECT status FROM picks')[0][0]=='void'


@pytest.mark.parametrize('home,away,start,league,score',[
    ('Stade Brest','Nice','2026-10-23T18:45:00Z','france_-_ligue_1',(1.,2.)),
    ('Athletic Bilbao','Getafe','2026-10-25T13:00:00Z','spain_-_la_liga',(4.,0.)),
])
def test_recorded_final_results_resolve_curated_provider_names(home,away,start,league,score):
    from predbot.util.timeutil import parse_iso
    from tests.test_results_espn import _espn
    provider=_espn('espn_recovery_results.json')
    result=provider.find_result('football',home,away,parse_iso(start),league_id=league)
    assert result.status=='final'
    assert result.scores['full']==score
    assert provider.find_result('football',home+' B',away,parse_iso(start),league_id=league) is None


def test_provider_alias_cannot_match_a_different_team_id_or_league():
    from predbot.providers.espn import _matches
    assert _matches('Stade Brest',{'team':{'id':'6997','displayName':'Brest'}},'ligue_1')
    assert not _matches('Stade Brest',{'team':{'id':'9999','displayName':'Brest'}},'ligue_1')
    assert not _matches('Stade Brest',{'team':{'id':'6997','displayName':'Brest'}},'la_liga')


def test_rejection_recovery_remaps_integer_ids_without_duplicate_diagnostics(db,tmp_path):
    module=Path(__file__).resolve().parents[1]/'tools/repair_oracle_ids.py'
    spec=importlib.util.spec_from_file_location('repair_logs',module);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
    migration=Path(__file__).resolve().parents[2]/'workers/migrations/0015_selection_rejections.sql'
    db.conn.executescript(migration.read_text())
    db.execute("INSERT INTO selection_rejections(id,pick_date,product,reason,run_id,created_at) VALUES(1,'2026-10-31','top','below_probability','old','2026-10-31')")
    batch=tmp_path/'generation-new';batch.mkdir()
    (batch/'p0_1_selection_rejections_0000.sql').write_text("INSERT OR IGNORE INTO selection_rejections(id,pick_date,product,reason,run_id,created_at) VALUES(1,'2026-11-01','top','below_probability','new','2026-11-01');")
    assert m.restore_rejections(db.conn,tmp_path)==1
    assert m.restore_rejections(db.conn,tmp_path)==0
    assert len(db.query('SELECT * FROM selection_rejections'))==2


def test_health_requires_authenticated_telegram_and_never_changes_data(tmp_path,monkeypatch):
    import json
    from datetime import datetime,timezone
    from zoneinfo import ZoneInfo
    module=Path(__file__).resolve().parents[1]/'tools/production_health.py'
    spec=importlib.util.spec_from_file_location('health_test',module);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
    path=tmp_path/'health.db';c=sqlite3.connect(path)
    c.executescript('CREATE TABLE job_state(name TEXT,last_run_at TEXT,last_result TEXT); CREATE TABLE live_fixture_state(last_checked_at TEXT); CREATE TABLE picks(status TEXT,start_utc TEXT); CREATE TABLE prediction_cache(local_date TEXT,status TEXT,payload_json TEXT,generated_at TEXT);')
    now=datetime.now(timezone.utc)
    c.execute('INSERT INTO live_fixture_state VALUES(?)',(now.isoformat(),))
    generation={'fixtures_discovered':0,'top':{},'daily':{},'provider_coverage':{'complete':True}}
    c.execute('INSERT INTO prediction_cache VALUES(?,?,?,?)',(now.astimezone(ZoneInfo('Africa/Lagos')).date().isoformat(),'COMPLETE',json.dumps({'generation':generation}),now.isoformat()))
    c.commit();before=c.iterdump();before='\n'.join(before);c.close()
    monkeypatch.setattr(m.shutil,'which',lambda name:None)
    monkeypatch.setattr(m,'connectivity',lambda:{'telegram':{'reachable':True,'authenticated':False}})
    assert m.report(path)['healthy'] is False
    monkeypatch.setattr(m,'connectivity',lambda:{'telegram':{'reachable':True,'authenticated':True}})
    assert m.report(path)['healthy'] is True
    c=sqlite3.connect(path)
    assert '\n'.join(c.iterdump())==before
