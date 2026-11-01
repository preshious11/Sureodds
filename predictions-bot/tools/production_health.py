"""Read-only Oracle subsystem report. No provider quota or Telegram sends."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess


def report(path):
    now = datetime.now(timezone.utc)
    db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    try:
        jobs = [dict(r) for r in db.execute('SELECT name,last_run_at,last_result FROM job_state')]
        live = db.execute('SELECT max(last_checked_at) FROM live_fixture_state').fetchone()[0]
        overdue = db.execute("SELECT count(*) FROM picks WHERE status='pending' AND julianday(start_utc)<julianday('now','-12 hours')").fetchone()[0]
        today = (now.astimezone(__import__('zoneinfo').ZoneInfo('Africa/Lagos'))).date().isoformat()
        cached = db.execute('SELECT status,payload_json FROM prediction_cache WHERE local_date=? ORDER BY generated_at DESC LIMIT 1',(today,)).fetchone()
        generation = json.loads(cached['payload_json']).get('generation', {}) if cached else {}
        output = {'checked_at': now.isoformat(), 'database': str(Path(path).resolve()),
                  'integrity': db.execute('PRAGMA quick_check').fetchone()[0],
                  'journal_mode': db.execute('PRAGMA journal_mode').fetchone()[0],
                  'disk_free_bytes': shutil.disk_usage(Path(path).parent).free,
                  'overdue_pending': overdue, 'latest_fixture_check': live,
                  'generation': {k:generation.get(k) for k in ('fixtures_discovered','fixtures_with_predictions','all_predictions','top','daily','provider_coverage','api_usage')},
                  'jobs':jobs, 'external_connectivity':'not probed; provider health requires recent successful execution evidence'}
        if shutil.which('systemctl'):
            for name,args in [('process',['is-active','sureodds-bot']),('timers',['list-timers','--all','--no-pager','sureodds-*'])]:
                output[name]=subprocess.run(['systemctl',*args],capture_output=True,text=True,timeout=10).stdout.strip()
        output['healthy'] = (output['integrity']=='ok' and overdue==0 and bool(live) and
                             (now-datetime.fromisoformat(live.replace('Z','+00:00'))).total_seconds()<1800 and bool(cached))
        return output
    finally:
        db.close()


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database')
    args=parser.parse_args()
    result=report(args.database)
    print(json.dumps(result,indent=2))
    raise SystemExit(0 if result['healthy'] else 1)
