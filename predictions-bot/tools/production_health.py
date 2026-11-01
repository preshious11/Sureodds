"""Read-only Oracle subsystem report. No provider quota or Telegram sends."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import os
import urllib.request
import urllib.error


def connectivity():
    endpoints={'results':'https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/scoreboard',
               'odds_transport':'https://api.the-odds-api.com/v4/sports/'}
    token=os.environ.get('TELEGRAM_BOT_TOKEN')
    if token: endpoints['telegram']='https://api.telegram.org/bot'+token+'/getMe'
    status={}
    for name,url in endpoints.items():
        try:
            with urllib.request.urlopen(url,timeout=8) as response:
                status[name]={'http_status':response.status,'reachable':True}
                if name=='telegram': status[name]['authenticated']=bool(json.load(response).get('ok'))
        except urllib.error.HTTPError as error:
            status[name]={'http_status':error.code,'reachable':True,'authenticated':False}
        except Exception as error:
            # Exception strings can contain the credential-bearing URL.
            status[name]={'reachable':False,'error_type':type(error).__name__}
    return status


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
        coverage=generation.get('provider_coverage') or {}
        generation['provider_coverage']={k:coverage.get(k) for k in ('complete','required','missing_required','optional_degraded','fallback_used','complete_providers')}
        output = {'checked_at': now.isoformat(), 'database': str(Path(path).resolve()),
                  'integrity': db.execute('PRAGMA quick_check').fetchone()[0],
                  'journal_mode': db.execute('PRAGMA journal_mode').fetchone()[0],
                  'disk_free_bytes': shutil.disk_usage(Path(path).parent).free,
                  'overdue_pending': overdue, 'latest_fixture_check': live,
                  'generation': {k:generation.get(k) for k in ('fixtures_discovered','fixtures_with_predictions','all_predictions','top','daily','provider_coverage','api_usage')},
                  'jobs':jobs, 'external_connectivity':connectivity(),
                  'quota_note':'usage is from the generation snapshot; odds transport probe does not authenticate or spend credits'}
        if shutil.which('systemctl'):
            for name,args in [('process',['is-active','sureodds-bot']),('timers',['list-timers','--all','--no-pager','sureodds-*'])]:
                output[name]=subprocess.run(['systemctl',*args],capture_output=True,text=True,timeout=10).stdout.strip()
        output['healthy'] = (output['integrity']=='ok' and output['disk_free_bytes']>100_000_000 and
                             output.get('process','active')=='active' and
                             all(v['reachable'] for v in output['external_connectivity'].values()) and
                             overdue==0 and bool(live) and
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
