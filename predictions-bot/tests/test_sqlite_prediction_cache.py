import json
import sys
import threading
from datetime import timedelta
from math import prod
from unittest.mock import Mock

import pytest

from predbot.cache.fetcher import UnifiedFetcher
from predbot.cache.models import DailySnapshot, SCHEMA_VERSION
from predbot.config import Config
from predbot.jobs import daily_picks
from predbot.providers.chain import ProviderChain
from predbot.providers.api_football import ApiFootballProvider
from predbot.providers.budget import Budget
from predbot.storage.db import Database
from predbot.util.http import ProviderError
from predbot.util.timeutil import parse_iso
from tests.conftest import dt
from tests.test_picker import ev, sc


class Provider:
    def __init__(self, sport, events):
        self.sports = [sport]
        self.fetch_events = Mock(return_value=events)

    def enabled(self):
        return True


@pytest.fixture
def clock(monkeypatch):
    now = [dt()]
    for module in ('predbot.cache.fetcher', 'predbot.cache.providers',
                   'predbot.jobs.daily_picks', 'predbot.providers.api_football'):
        monkeypatch.setattr(module + '.utcnow', lambda: now[0])
    return now


def service(db, monkeypatch, football_error=False):
    football = Provider('football', [ev(i) for i in range(1, 5)])
    hockey = Provider('hockey', [ev(i, sport='hockey') for i in range(5, 9)])
    if football_error:
        football.fetch_events.side_effect = ProviderError('temporary')
    chain = ProviderChain({'football': football, 'hockey': hockey}, ['football', 'hockey'], 1)
    # Exercise real selection and persistence; isolate only the probability model.
    def score(events, *args):
        return [sc(e, 'result3' if e.sport == 'football' else 'result2',
                   'home' if int(e.provider_event_id) % 2 else 'away', .76,
                   1.40 if int(e.provider_event_id) % 2 else 1.45) for e in events]
    monkeypatch.setattr(daily_picks, 'score_events', score)
    return UnifiedFetcher(chain, db, Config(timezone='Africa/Lagos', cache_retry_seconds=900)), football, hockey


def all_sports(snapshot):
    return {p.sport for p in snapshot.view_items('daily')}


def test_miss_hit_and_restart_roundtrip(tmp_path, monkeypatch, clock):
    path = str(tmp_path / 'bot.db')
    db = Database(path)
    app, football, hockey = service(db, monkeypatch)
    first = app.get_daily_snapshot()
    assert first.status == 'COMPLETE'
    assert all_sports(first) == {'football', 'hockey'}
    assert first.top_picks and first.daily_picks
    assert prod(p.price for p in first.top_picks) == pytest.approx(2.03)
    assert app.get_daily_snapshot().to_dict() == first.to_dict()
    assert football.fetch_events.call_count == hockey.fetch_events.call_count == 1
    assert db.get_prediction_cache(app._key(first.date))['status'] == 'COMPLETE'
    db.close()
    reopened = Database(path)
    fresh, f2, b2 = service(reopened, monkeypatch)
    f2.fetch_events.side_effect = b2.fetch_events.side_effect = AssertionError('API after restart')
    second = fresh.get_daily_snapshot()
    assert second.to_dict() == first.to_dict()
    assert all_sports(second) == {'football', 'hockey'}
    assert second.top_picks == first.top_picks
    assert second.daily_picks == first.daily_picks
    assert all(parse_iso(p.kickoff_utc).tzinfo for p in second.view_items('daily'))
    assert all(p.provider_event_id and p.provider and p.prob for p in second.view_items('daily'))
    assert prod(p.price for p in second.top_picks) == prod(p.price for p in first.top_picks)
    f2.fetch_events.assert_not_called()
    b2.fetch_events.assert_not_called()
    from predbot.bot.callbacks import handle_command
    from tests.test_subscribers_broadcast import FakeTG
    tg = FakeTG()
    handle_command('/top', '42', service=fresh, tg=tg, cfg=fresh.cfg)
    assert 'Top' in tg.sends[0]['text']
    assert '2.03' in tg.sends[0]['text']
    assert fresh.published_snapshot().to_dict() == second.to_dict()
    f2.fetch_events.assert_not_called()
    reopened.close()


def test_partial_football_recovers_without_repeating_hockey(tmp_path, monkeypatch, clock):
    path = str(tmp_path / 'bot.db')
    db = Database(path)
    app, football, hockey = service(db, monkeypatch, football_error=True)
    first = app.get_daily_snapshot()
    assert first.status == 'PARTIAL'
    assert all_sports(first) == {'hockey'}
    assert first.sport_status['football']['status'] == 'failed'
    assert first.sport_status['hockey']['status'] == 'success'
    assert db.get_prediction_cache(app._key(first.date))['status'] == 'PARTIAL'
    for _ in range(4):
        assert app.get_daily_snapshot().to_dict() == first.to_dict()
    assert football.fetch_events.call_count == hockey.fetch_events.call_count == 1
    # Restart while still partial: completed hockey portion must survive too.
    db.close()
    db = Database(path)
    app, football, hockey = service(db, monkeypatch)
    hockey.fetch_events.side_effect = AssertionError('hockey fetched again')
    clock[0] += timedelta(minutes=16)
    recovered = app.get_daily_snapshot()
    assert recovered.status == 'COMPLETE'
    assert recovered.sport_status['football']['status'] == 'success'
    assert all_sports(recovered) == {'football', 'hockey'}
    assert recovered.top_picks and recovered.daily_picks
    football.fetch_events.assert_called_once()
    hockey.fetch_events.assert_not_called()
    assert len(db.query('SELECT * FROM prediction_cache')) == 1
    db.close()
    db = Database(path)
    fresh, f, b = service(db, monkeypatch)
    assert fresh.get_daily_snapshot().to_dict() == recovered.to_dict()
    f.fetch_events.assert_not_called()
    b.fetch_events.assert_not_called()
    db.close()


def test_valid_empty_is_complete_and_not_retried(db, monkeypatch, clock):
    app, f, b = service(db, monkeypatch)
    f.fetch_events.return_value = []
    snapshot = app.get_daily_snapshot()
    assert snapshot.status == 'COMPLETE'
    assert snapshot.sport_status['football']['status'] == 'empty'
    clock[0] += timedelta(minutes=16)
    app.get_daily_snapshot()
    f.fetch_events.assert_called_once()


def test_empty_candidates_are_not_provider_failure(db, monkeypatch, clock):
    app, f, b = service(db, monkeypatch)
    monkeypatch.setattr(daily_picks, 'score_events', lambda *args: [])
    snapshot = app.get_daily_snapshot()
    assert snapshot.status == 'COMPLETE' and snapshot.total_picks() == 0
    assert snapshot.sport_status['football']['status'] == 'success'
    assert snapshot.generation['scored_by_sport'] == {}


def test_all_failures_stay_partial(db, monkeypatch, clock):
    app, f, b = service(db, monkeypatch, football_error=True)
    b.fetch_events.side_effect = ProviderError('temporary')
    snapshot = app.get_daily_snapshot()
    assert snapshot.status == 'PARTIAL' and snapshot.total_picks() == 0
    assert all(s['status'] == 'failed' for s in snapshot.sport_status.values())


@pytest.mark.parametrize('stale', ['version', 'expiry', 'json'])
def test_stale_snapshot_regenerated(db, monkeypatch, clock, stale):
    app, f, b = service(db, monkeypatch)
    first = app.get_daily_snapshot()
    row = db.get_prediction_cache(app._key(first.date))
    if stale == 'version': row['cache_version'] = 999
    if stale == 'expiry': row['expires_at'] = '2000-01-01T00:00:00Z'
    if stale == 'json': row['payload_json'] = '{invalid'
    db.upsert_prediction_cache(row)
    app.get_daily_snapshot()
    assert app.api_batches == 2
    assert db.get_prediction_cache(app._key(first.date))['cache_version'] == SCHEMA_VERSION
    # Rebuilding selection never burns already-completed provider requests.
    f.fetch_events.assert_called_once()


def test_lagos_day_rollover_expires_previous_snapshot(db, monkeypatch, clock):
    app, f, b = service(db, monkeypatch)
    first = app.get_daily_snapshot()
    row = db.get_prediction_cache(app._key(first.date))
    assert row['expires_at'] == '2030-06-01T23:00:00Z'
    clock[0] = dt(hh=23, mm=1)
    second = app.get_daily_snapshot()
    assert second.date == '2030-06-02'
    assert second.date != first.date
    assert f.fetch_events.call_count == 2


def test_force_refresh_rebuilds_selections_without_burning_completed_sources(db, monkeypatch, clock):
    app, f, b = service(db, monkeypatch)
    first = app.get_daily_snapshot()
    clock[0] += timedelta(minutes=1)
    second = app.get_daily_snapshot(force_refresh=True)
    assert app.api_batches == 2
    assert second.generated_at != first.generated_at
    assert len(db.query('SELECT * FROM prediction_cache')) == 1
    assert f.fetch_events.call_count == b.fetch_events.call_count == 1


def test_atomic_failure_keeps_previous_snapshot_and_picks(db, monkeypatch, clock):
    app, f, b = service(db, monkeypatch)
    original = app.get_daily_snapshot()
    saved = db.get_prediction_cache(app._key(original.date))['payload_json']
    count = len(db.picks_for_date(original.date))
    old_upsert = db.upsert_prediction_cache
    def broken(row):
        old_upsert(row)
        raise RuntimeError('failure after SQL write before outer commit')
    monkeypatch.setattr(db, 'upsert_prediction_cache', broken)
    clock[0] += timedelta(minutes=1)
    assert app.get_daily_snapshot(force_refresh=True).to_dict() == original.to_dict()
    assert db.get_prediction_cache(app._key(original.date))['payload_json'] == saved
    assert len(db.picks_for_date(original.date)) == count


def test_partial_refresh_exception_falls_back_and_throttles(db, monkeypatch, clock):
    app, f, b = service(db, monkeypatch, football_error=True)
    first = app.get_daily_snapshot()
    clock[0] += timedelta(minutes=16)
    build = Mock(side_effect=RuntimeError('temporary model failure'))
    monkeypatch.setattr(daily_picks, 'build_picks', build)
    assert app.get_daily_snapshot().to_dict() == first.to_dict()
    assert app.get_daily_snapshot().to_dict() == first.to_dict()
    build.assert_called_once()


def test_concurrent_services_share_single_generation(tmp_path, monkeypatch, clock):
    path = str(tmp_path / 'shared.db')
    db1, db2 = Database(path), Database(path)
    a, f1, b1 = service(db1, monkeypatch)
    b, f2, b2 = service(db2, monkeypatch)
    results, errors = [], []
    def worker(app):
        try: results.append(app.get_daily_snapshot().to_dict())
        except Exception as exc: errors.append(exc)
    threads = [threading.Thread(target=worker, args=(app,)) for app in (a,b,a,b)]
    for t in threads: t.start()
    for t in threads: t.join(10)
    assert not errors and len(results) == 4
    assert all(r == results[0] for r in results)
    assert f1.fetch_events.call_count + f2.fetch_events.call_count == 1
    assert b1.fetch_events.call_count + b2.fetch_events.call_count == 1
    db1.close(); db2.close()


def test_prune_retains_subscribers_and_history(db, monkeypatch, clock):
    app, f, b = service(db, monkeypatch)
    db.subscriber_add('42')
    first = app.get_daily_snapshot()
    clock[0] += timedelta(days=10)
    app.get_daily_snapshot()
    assert db.get_prediction_cache(app._key(first.date)) is None
    assert db.subscribers_active()
    assert db.picks_for_date(first.date)
    assert db.query('SELECT COUNT(*) n FROM provider_cache')[0]['n'] == 2


def test_no_redis_package_or_configuration_required(db, monkeypatch, clock):
    monkeypatch.setitem(sys.modules, 'redis', None)
    monkeypatch.setenv('REDIS_URL', 'intentionally-invalid-and-unused')
    app, f, b = service(db, monkeypatch)
    assert app.get_daily_snapshot().status == 'COMPLETE'
    assert not hasattr(app.cfg, 'redis_url')


def test_football_reuses_successful_raw_requests_after_restart(db, clock):
    p = ApiFootballProvider('fake-key', Budget(db))
    p.http.get = Mock(return_value={'response': [{'fixture': {'id': 1}}]})
    response = p._call('/fixtures', {'date': '2030-06-01'}, reserve=15)
    restarted = ApiFootballProvider('fake-key', Budget(db))
    restarted.http.get = Mock(side_effect=AssertionError('duplicate football request'))
    assert restarted._call('/fixtures', {'date': '2030-06-01'}, reserve=15) == response
    restarted.http.get.assert_not_called()
    assert Budget(db).used_today('api_football') == 1


def test_failed_football_request_can_recover_and_counts_attempts(db, clock):
    p = ApiFootballProvider('fake-key', Budget(db))
    p.http.get = Mock(side_effect=[ProviderError('network'), {'response': []}])
    with pytest.raises(ProviderError): p._call('/fixtures', {}, reserve=15)
    assert p._call('/fixtures', {}, reserve=15) == {'response': []}
    assert Budget(db).used_today('api_football') == 2
    assert p.http.retries == 0


def test_force_refresh_retries_failed_source_early(db, monkeypatch, clock):
    app, football, hockey = service(db, monkeypatch, football_error=True)
    assert app.get_daily_snapshot().status == 'PARTIAL'
    football.fetch_events.side_effect = None
    assert app.get_daily_snapshot(force_refresh=True).status == 'COMPLETE'
    assert football.fetch_events.call_count == 2
    hockey.fetch_events.assert_called_once()


def test_new_python_process_uses_sqlite_without_provider_calls(tmp_path, monkeypatch, clock):
    import subprocess
    path = str(tmp_path / 'restart.db')
    db = Database(path)
    app, f, b = service(db, monkeypatch)
    expected = app.get_daily_snapshot().to_dict()
    db.close()
    script = '''
import json, sys
from datetime import datetime, timezone
from predbot.storage.db import Database
from predbot.cache.fetcher import UnifiedFetcher
from predbot.config import Config
import predbot.cache.fetcher as module
module.utcnow = lambda: datetime(2030, 6, 1, 18, tzinfo=timezone.utc)
class Forbidden:
    def collect(self, *args):
        raise AssertionError('provider called after process restart')
db = Database(sys.argv[1])
app = UnifiedFetcher(Forbidden(), db, Config(timezone='Africa/Lagos'))
result = app.get_daily_snapshot()
assert app.api_batches == 0
assert result.top_picks and result.daily_picks
print(json.dumps(result.to_dict(), sort_keys=True))
db.close()
'''
    output = subprocess.check_output([sys.executable, '-c', script, path], text=True)
    assert json.loads(output) == expected


def test_additive_migration_preserves_old_database(tmp_path):
    import sqlite3
    path = str(tmp_path / 'old.db')
    old = sqlite3.connect(path)
    old.executescript('''
        CREATE TABLE subscribers (chat_id TEXT PRIMARY KEY, username TEXT,
            first_name TEXT, subscribed_at TEXT, active INTEGER DEFAULT 1);
        INSERT INTO subscribers VALUES ('42','reader','Reader','2030-01-01',1);
        CREATE TABLE kv (k TEXT PRIMARY KEY,v TEXT);
        INSERT INTO kv VALUES ('custom-setting','keep-me');
    ''')
    old.close()
    db = Database(path)
    assert db.get_kv('custom-setting') == 'keep-me'
    assert db.subscribers_active()[0]['chat_id'] == '42'
    assert db.query("SELECT name FROM sqlite_master WHERE name='prediction_cache'")
    assert db.query("SELECT name FROM sqlite_master WHERE name='provider_cache'")
    db.close()


def test_scheduled_publication_uses_same_snapshot(db, monkeypatch, clock):
    app, f, b = service(db, monkeypatch)
    snapshot = app.get_daily_snapshot()
    tg = Mock()
    tg.enabled.return_value = True
    tg.send.return_value = 'message-1'
    result = daily_picks.run(app.chain, db, app.cfg, tg, service=app)
    assert result.to_dict() == snapshot.to_dict()
    assert app.api_batches == 1
    f.fetch_events.assert_called_once()
    b.fetch_events.assert_called_once()
    daily_picks.run(app.chain, db, app.cfg, tg, service=app)
    tg.send.assert_called_once()


def _diagnose(db, monkeypatch, clock):
    import requests
    import diagnose_prediction_cache as diag
    monkeypatch.setattr(requests.Session, 'request',
                        Mock(side_effect=AssertionError('diagnostic must not call any API')))
    monkeypatch.setattr(diag.timeutil, 'utcnow', lambda: clock[0])
    # Only stdlib sqlite3 + config/time helpers: no provider, fetcher or HTTP modules loaded.
    assert not any(name in diag.__dict__ for name in ('requests', 'build_providers', 'UnifiedFetcher', 'Http'))
    return diag.main(Config(db_path=db.path, timezone='Africa/Lagos'))


def test_diagnostic_reports_complete_cache_without_api_calls(db, monkeypatch, clock, capsys):
    app, f, b = service(db, monkeypatch)
    snapshot = app.get_daily_snapshot()
    f.fetch_events.reset_mock(); b.fetch_events.reset_mock()
    info = _diagnose(db, monkeypatch, clock)
    out = capsys.readouterr().out
    f.fetch_events.assert_not_called(); b.fetch_events.assert_not_called()
    assert info['backend'] == 'sqlite' and info['today'] == 'HIT' and info['status'] == 'COMPLETE'
    assert info['prediction_table'] == 'prediction_cache' and info['provider_table'] == 'provider_cache'
    assert info['top_picks_count'] == len(snapshot.top_picks)
    assert info['daily_picks_count'] == len(snapshot.daily_picks)
    assert info['football_count'] + info['hockey_count'] == snapshot.total_picks()
    assert info['retry_after'] is None and info['cache_version'] == SCHEMA_VERSION
    for label in ('Prediction cache backend=sqlite', 'Database path:', 'Status: COMPLETE', 'Sport status:',
                  'Football count:', 'Hockey count:', 'Top Picks count:', 'Daily Picks count:',
                  'Generated time:', 'Updated time:', f'Cache version: {SCHEMA_VERSION}'):
        assert label in out
    assert 'Retry-after' not in out


def test_diagnostic_reports_partial_cache_with_retry_after(db, monkeypatch, clock, capsys):
    app, f, b = service(db, monkeypatch, football_error=True)
    assert app.get_daily_snapshot().status == 'PARTIAL'
    info = _diagnose(db, monkeypatch, clock)
    out = capsys.readouterr().out
    assert info['status'] == 'PARTIAL' and info['football_count'] == 0 and info['hockey_count'] > 0
    assert parse_iso(info['retry_after']) is not None
    assert 'Status: PARTIAL' in out
    assert 'Retry-after:' in out and info['retry_after'] not in out   # never the raw ISO-Z form
    import diagnose_prediction_cache as diag
    assert diag._display_ts(info['retry_after'], info['timezone']) in out


def test_diagnostic_reports_missing_database(tmp_path, capsys):
    import diagnose_prediction_cache as diag
    info = diag.main(Config(db_path=str(tmp_path / 'absent.db')))
    assert info['db_exists'] is False and info['today'] is None
    assert 'No database exists here' in capsys.readouterr().out


def test_runtime_source_has_no_redis_dependency():
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    files = list((root / 'predbot').rglob('*.py')) + [root / 'main.py', root / 'cli.py',
                                                       root / 'diagnose_prediction_cache.py']
    offenders = [str(f) for f in files if f.exists()
                 and re.search(r'\bimport redis\b|\bfrom redis\b|redis_url|REDIS_URL', f.read_text(encoding='utf-8'))]
    assert offenders == []
    assert 'redis' not in (root / 'requirements.txt').read_text(encoding='utf-8').lower()
