"""Offline release guards: exercise generated SQL against disposable targets."""
import importlib.util
import json
import shutil
import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('release_sync', ROOT / 'workers/scripts/d1_sync.py')
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)


def seed(path):
    with sqlite3.connect(path) as con:
        con.executescript('''
            CREATE TABLE picks(id INTEGER PRIMARY KEY, price REAL, prob REAL,
                status TEXT, settled_at TEXT, result_json TEXT);
            CREATE TABLE prediction_memberships(prediction_id INTEGER, collection TEXT,
                rank INTEGER, PRIMARY KEY(prediction_id,collection));
            CREATE TABLE match_results(id INTEGER PRIMARY KEY, score TEXT);
            INSERT INTO picks VALUES(1,1.8,.7,'won','2026-10-01','{"score":"2-0"}');
            INSERT INTO picks VALUES(2,1.5,.8,'pending',NULL,NULL);
            INSERT INTO prediction_memberships VALUES(1,'daily',1);
            INSERT INTO match_results VALUES(1,'2-0');
        ''')


@pytest.mark.parametrize('mode', ['push', 'diff'])
def test_stale_source_cannot_replace_snapshot_settlement_or_membership(tmp_path, mode):
    base, src, target = [tmp_path / name for name in ('base.db', 'src.db', 'target.db')]
    seed(base)
    shutil.copyfile(base, src)
    shutil.copyfile(base, target)
    with sqlite3.connect(src) as con:
        con.execute("UPDATE picks SET price=9,prob=.99,status='lost' WHERE id=1")
        con.execute("UPDATE picks SET price=4,status='won' WHERE id=2")
        con.execute("UPDATE prediction_memberships SET rank=9")
        con.execute("UPDATE match_results SET score='0-5'")
    out = tmp_path / 'batches'
    rc = sync.cmd_push(str(src), str(out)) if mode == 'push' else sync.cmd_diff(str(base), str(src), str(out))
    assert rc == 0
    assert sync.cmd_apply(str(target), str(out)) == 0
    with sqlite3.connect(target) as con:
        assert con.execute('SELECT price,prob,status FROM picks ORDER BY id').fetchall() == [(1.8,.7,'won'), (1.5,.8,'pending')]
        assert con.execute('SELECT rank FROM prediction_memberships').fetchone() == (1,)
        assert con.execute('SELECT score FROM match_results').fetchone() == ('2-0',)


@pytest.mark.parametrize('status', ['won','lost','push','void','half_won','half_lost'])
def test_settlement_is_monotonic_and_preserves_quote(tmp_path, status):
    base, src = tmp_path / 'base.db', tmp_path / 'src.db'
    seed(base); shutil.copyfile(base, src)
    with sqlite3.connect(src) as con:
        con.execute("UPDATE picks SET status=?,settled_at='now',result_json='{}' WHERE id=2", (status,))
    out = tmp_path / 'batches'
    assert sync.cmd_diff(str(base), str(src), str(out)) == 0
    assert sync.cmd_apply(str(base), str(out)) == 0
    with sqlite3.connect(base) as con:
        assert con.execute('SELECT price,prob,status FROM picks WHERE id=2').fetchone() == (1.5,.8,status)


def test_partial_database_does_not_erase_history(tmp_path):
    base, src = tmp_path / 'base.db', tmp_path / 'src.db'
    seed(base); shutil.copyfile(base, src)
    with sqlite3.connect(src) as con:
        con.executescript('DELETE FROM picks; DELETE FROM prediction_memberships; DELETE FROM match_results;')
    out = tmp_path / 'batches'
    assert sync.cmd_diff(str(base), str(src), str(out)) == 0
    assert sync.cmd_apply(str(base), str(out)) == 0
    with sqlite3.connect(base) as con:
        assert con.execute('SELECT COUNT(*) FROM picks').fetchone() == (2,)
        assert con.execute('SELECT COUNT(*) FROM prediction_memberships').fetchone() == (1,)
    assert sync.cmd_diff(str(base), str(src), str(tmp_path / 'destructive'), allow_deletes=True) == 1


def test_stale_batches_and_unmigrated_base_fail_closed(tmp_path):
    base, src = tmp_path / 'base.db', tmp_path / 'src.db'
    seed(base); shutil.copyfile(base, src)
    out = tmp_path / 'batches'; out.mkdir(); (out / 'old.sql').write_text('DELETE FROM picks;')
    assert sync.cmd_diff(str(base), str(src), str(out)) == 1
    assert sync.cmd_push(str(src), str(out)) == 1
    with sqlite3.connect(src) as con:
        con.execute('CREATE TABLE ticket_candidates(pick_date TEXT,candidate_id TEXT,payload_json TEXT)')
    fresh = tmp_path / 'fresh'
    assert sync.cmd_diff(str(base), str(src), str(fresh)) == 1
    assert not list(fresh.glob('*.sql'))


def test_rest_overflow_retains_same_settlement_guards(tmp_path):
    src = tmp_path / 'src.db'; seed(src)
    with sqlite3.connect(src) as con:
        con.execute("UPDATE picks SET result_json=? WHERE id=1", (json.dumps({'large':'x'*110000}),))
    out = tmp_path / 'batches'
    assert sync.cmd_push(str(src), str(out)) == 0
    rows = [json.loads(line) for f in (out / 'rest').glob('*.jsonl') for line in f.read_text().splitlines()]
    assert rows and all("picks.status='pending'" in row['sql'] for row in rows)


def test_0005_preserves_existing_rows_and_rollback_keeps_alternates(tmp_path):
    from predbot.storage.db import Database
    from predbot.jobs.daily_picks import prediction_row
    from tests.test_picker import ev, sc
    path = tmp_path / 'history.db'
    db = Database(str(path))
    candidate = sc(ev(1), 'total', 'over', .8, 1.5, line=1.5)
    candidate.event.canonical_event_id = 'fixture'
    pid = db.store_prediction(prediction_row(candidate, '2030-06-01'))
    db.settle_pick(pid, 'half_won', {'score': '2-0'})
    before = dict(db.query('SELECT * FROM picks')[0]); before.pop('is_representative')
    db.close()
    with sqlite3.connect(path) as con:
        con.execute('DROP INDEX uq_prediction_event_family_day')
        con.execute('ALTER TABLE picks DROP COLUMN is_representative')
        # Apply the actual D1 migration to the disposable pre-0005 schema.
        con.executescript((ROOT / 'workers/migrations/0005_ticket_candidates.sql').read_text())
        con.row_factory = sqlite3.Row
        after = dict(con.execute('SELECT * FROM picks').fetchone())
        assert after.pop('is_representative') == 1
        assert after == before
        with pytest.raises(sqlite3.OperationalError, match='duplicate column'):
            con.executescript((ROOT / 'workers/migrations/0005_ticket_candidates.sql').read_text())
        assert con.execute('SELECT status FROM picks').fetchone()[0] == 'half_won'


def test_fresh_and_upgrade_migration_paths(tmp_path):
    migrations = ROOT / 'workers/migrations'
    for legacy in (False, True):
        with sqlite3.connect(tmp_path / f'{legacy}.db') as con:
            if legacy:
                con.executescript('CREATE TABLE telegram_updates(update_id INTEGER PRIMARY KEY, first_seen_at TEXT);')
                con.executescript((migrations / '0002_update_retry.sql').read_text())
            for name in ('0001_foundation.sql','0003_engine_state.sql','0004_sync_state.sql','0005_ticket_candidates.sql'):
                con.executescript((migrations / name).read_text())
            assert con.execute('PRAGMA integrity_check').fetchone() == ('ok',)
            assert not con.execute('PRAGMA foreign_key_check').fetchall()
            assert 'is_representative' in {row[1] for row in con.execute('PRAGMA table_info(picks)')}


def test_interrupted_apply_checkpoints_committed_batches(tmp_path):
    path = tmp_path / 'target.db'; seed(path)
    out = tmp_path / 'batches'; out.mkdir()
    (out / '001.sql').write_text('INSERT INTO match_results VALUES(2,\'1-0\');')
    (out / '002.sql').write_text('INSERT INTO missing_table VALUES(1);')
    with pytest.raises(sqlite3.OperationalError):
        sync.cmd_apply(str(path), str(out))
    assert json.loads((out / 'applied.json').read_text()) == ['001.sql']
    (out / '002.sql').write_text('INSERT INTO match_results VALUES(3,\'0-0\');')
    assert sync.cmd_apply(str(path), str(out)) == 0


def test_conflicting_stored_results_are_not_chosen_by_row_order(db):
    from predbot.jobs import settle
    from tests.test_live_results import pick
    first = pick(db, market='result3', selection='home')
    second = pick(db, market='total', selection='over', line=2.5)
    db.settle_pick(first, 'won', {'status':'final','scores':{'full':[2,0]},'source':'archive_a'})
    db.settle_pick(second, 'won', {'status':'final','scores':{'full':[0,3]},'source':'archive_b'})
    row = db.picks_where('p.id=?', (first,))[0]
    assert settle.stored_result(row, db) is None
    assert db.query('SELECT status FROM picks WHERE id=?', (first,))[0]['status'] == 'won'


@pytest.mark.parametrize('difference', ['canonical_competition_id', 'home_id', 'sport'])
def test_explicit_identity_conflicts_cannot_fall_back_to_matching_names(difference):
    from predbot.jobs.live_results import orient_result
    from predbot.providers.base import MatchResult
    reference = dict(sport='football', canonical_competition_id='epl',
                     home_id='chelsea', away_id='arsenal', home='Chelsea', away='Arsenal')
    row = dict(reference); row[difference] = 'different'
    assert orient_result(MatchResult('final', {'full':(2,0)}), reference, row).status == 'unknown'


@pytest.mark.parametrize('mode', ['push','diff'])
def test_cross_database_id_collision_cannot_attach_ticket_to_wrong_snapshot(tmp_path, mode):
    base, src = tmp_path / 'base.db', tmp_path / 'src.db'
    seed(base); shutil.copyfile(base, src)
    with sqlite3.connect(src) as con:
        con.execute('UPDATE picks SET price=4.0 WHERE id=2')
        con.execute("INSERT INTO prediction_memberships VALUES(2,'top',1)")
    out = tmp_path / 'batches'
    rc = sync.cmd_push(str(src), str(out)) if mode == 'push' else sync.cmd_diff(str(base), str(src), str(out))
    assert rc == 0
    assert sync.cmd_apply(str(base), str(out)) == 0
    with sqlite3.connect(base) as con:
        assert con.execute('SELECT * FROM prediction_memberships WHERE prediction_id=2').fetchall() == []


def test_destructive_legacy_batch_is_rejected_before_any_write(tmp_path):
    path = tmp_path / 'target.db'; seed(path)
    out = tmp_path / 'batches'; out.mkdir()
    (out / '001.sql').write_text("INSERT INTO match_results VALUES(9,'9-0');")
    (out / '002.sql').write_text('DELETE FROM picks;')
    assert sync.cmd_apply(str(path), str(out)) == 1
    with sqlite3.connect(path) as con:
        assert con.execute('SELECT COUNT(*) FROM match_results').fetchone() == (1,)


def test_rest_rejects_legacy_history_replace_without_network(tmp_path):
    rest = tmp_path / 'rest'; rest.mkdir()
    (rest / 'old.jsonl').write_text(json.dumps({'sql':'INSERT OR REPLACE INTO picks(id) VALUES(?)', 'params':[1]})+'\n')
    assert sync.cmd_rest_apply(str(rest), 'unused', 'unused', 'unused') == 1


def test_failed_batch_rolls_back_all_statements(tmp_path):
    path = tmp_path / 'target.db'; seed(path)
    out = tmp_path / 'batches'; out.mkdir()
    (out / '001.sql').write_text("INSERT INTO match_results VALUES(9,'9-0'); INSERT INTO nonexistent VALUES(1);")
    with pytest.raises(sqlite3.OperationalError):
        sync.cmd_apply(str(path), str(out))
    with sqlite3.connect(path) as con:
        assert con.execute('SELECT COUNT(*) FROM match_results').fetchone() == (1,)


def test_unicode_statement_limit_and_stale_quota_preservation(tmp_path):
    src, target = tmp_path / 'src.db', tmp_path / 'target.db'
    for path, used in ((src, 1), (target, 99)):
        with sqlite3.connect(path) as con:
            con.executescript('CREATE TABLE kv(k TEXT PRIMARY KEY,v TEXT); CREATE TABLE api_budget(provider TEXT,bucket TEXT,used INTEGER,PRIMARY KEY(provider,bucket));')
            con.execute("INSERT INTO api_budget VALUES('provider','month',?)", (used,))
    with sqlite3.connect(src) as con:
        con.execute('INSERT INTO kv VALUES(?,?)', ('unicode','界'*40000))
    out = tmp_path / 'batches'
    assert sync.cmd_push(str(src), str(out)) == 0
    for sql in out.glob('*.sql'):
        assert len(sql.read_bytes()) <= 90000
    assert list((out / 'rest').glob('*.jsonl'))
    assert sync.cmd_apply(str(target), str(out)) == 0
    with sqlite3.connect(target) as con:
        assert con.execute('SELECT used FROM api_budget').fetchone() == (99,)


def test_confirmed_live_result_is_not_replaced_by_stale_snapshot(tmp_path):
    src, target = tmp_path / 'src.db', tmp_path / 'target.db'
    for path, status, payload in ((src,'in_progress','{}'), (target,'final','{"score":"2-0"}')):
        with sqlite3.connect(path) as con:
            con.execute('CREATE TABLE live_fixture_state(pick_date TEXT,fixture_key TEXT,status TEXT,result_json TEXT,PRIMARY KEY(pick_date,fixture_key))')
            con.execute('INSERT INTO live_fixture_state VALUES(?,?,?,?)', ('2030-06-01','fixture',status,payload))
    out = tmp_path / 'batches'
    assert sync.cmd_push(str(src), str(out)) == 0
    assert sync.cmd_apply(str(target), str(out)) == 0
    with sqlite3.connect(target) as con:
        assert con.execute('SELECT status,result_json FROM live_fixture_state').fetchone() == ('final','{"score":"2-0"}')


def test_snapshot_and_rebase_include_committed_wal_rows(tmp_path):
    src = tmp_path / 'wal.db'
    con = sqlite3.connect(src)
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('CREATE TABLE kv(k TEXT PRIMARY KEY,v TEXT)')
    con.execute("INSERT INTO kv VALUES('uncheckpointed','committed')")
    con.commit()
    try:
        out = tmp_path / 'snapshots'
        assert sync.cmd_snapshot(str(src), str(out)) == 0
        snapshot = next(out.glob('*.db'))
        rebase = tmp_path / 'rebase.db'
        assert sync.cmd_rebase(str(src), str(rebase), []) == 0
        for copy in (snapshot,rebase):
            with sqlite3.connect(copy) as target:
                assert target.execute('SELECT * FROM kv').fetchall() == [('uncheckpointed','committed')]
        assert sync.cmd_rebase(str(src), str(src), []) == 1
    finally:
        con.close()


def test_coalesce_bounds_utf8_files_and_refuses_stale_output(tmp_path):
    batches, out = tmp_path / 'batches', tmp_path / 'out'
    batches.mkdir()
    statement = "INSERT OR IGNORE INTO kv(k,v) VALUES('key','" + '界'*100 + "');\n"
    (batches / '001.sql').write_text(statement*10, encoding='utf-8')
    assert sync.cmd_coalesce(str(batches), str(out), max_bytes=1000) == 0
    assert len(list(out.glob('*.sql'))) > 1
    assert all(path.stat().st_size<=1000 for path in out.glob('*.sql'))
    assert sync.cmd_coalesce(str(batches), str(out)) == 1


@pytest.mark.parametrize('published', [True, False])
def test_partial_cache_can_complete_only_before_publication(tmp_path, published):
    src, target = tmp_path / 'src.db', tmp_path / 'target.db'
    for path, status in ((src,'COMPLETE'), (target,'PARTIAL')):
        with sqlite3.connect(path) as con:
            con.executescript('CREATE TABLE prediction_cache(cache_key TEXT PRIMARY KEY,local_date TEXT,status TEXT,payload_json TEXT); CREATE TABLE posts(kind TEXT,post_date TEXT,PRIMARY KEY(kind,post_date));')
            con.execute('INSERT INTO prediction_cache VALUES(?,?,?,?)', ('key','2030-06-01',status,status))
            if published and path==target:
                con.execute("INSERT INTO posts VALUES('picks_daily','2030-06-01')")
    out = tmp_path / 'batches'
    assert sync.cmd_push(str(src), str(out)) == 0
    assert sync.cmd_apply(str(target), str(out)) == 0
    with sqlite3.connect(target) as con:
        expected = 'PARTIAL' if published else 'COMPLETE'
        assert con.execute('SELECT status,payload_json FROM prediction_cache').fetchone() == (expected,expected)
