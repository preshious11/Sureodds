import json,pathlib,subprocess,shlex
values={}
for line in pathlib.Path('predictions-bot/.env').read_text(encoding='utf-8-sig').splitlines():
    if '=' in line and not line.lstrip().startswith('#'):
        k,v=line.split('=',1); values[k.strip()]=v.strip().strip('"').strip("'")
key=values.get('ODDS_API_KEY','')
if not key: raise SystemExit('local Odds API credential absent')
remote="""import sys,json,urllib.request,urllib.parse
key=json.load(sys.stdin)['key']
try:
 req=urllib.request.Request('https://api.the-odds-api.com/v4/sports/?'+urllib.parse.urlencode({'apiKey':key}))
 with urllib.request.urlopen(req,timeout=25) as r:
  data=json.load(r)
  print(json.dumps({'origin':'Oracle','http_status':r.status,'sports':len(data),'quota_headers':{k:v for k,v in r.headers.items() if k.lower().startswith('x-requests') or 'reset' in k.lower()}}))
except urllib.error.HTTPError as e:
 print(json.dumps({'origin':'Oracle','http_status':e.code,'quota_headers':{k:v for k,v in e.headers.items() if k.lower().startswith('x-requests') or 'reset' in k.lower()}}))
except Exception as e: print(json.dumps({'error_type':type(e).__name__}))
"""
r=subprocess.run(['ssh','-i',str(pathlib.Path.home()/'.ssh/sureodds_oracle'),'sureodds@84.12.123.136','/var/lib/sureodds/runtime/bin/python3 -c '+shlex.quote(remote)],input=json.dumps({'key':key}),text=True,capture_output=True,timeout=40)
print(r.stdout)
if r.returncode: print('ssh_failure',r.returncode)
