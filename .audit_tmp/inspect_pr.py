import subprocess,os,json,urllib.request
r=subprocess.run(['git','credential','fill'],input='protocol=https\nhost=github.com\n\n',text=True,capture_output=True,env={**os.environ,'GIT_TERMINAL_PROMPT':'0'},timeout=30)
v=dict(l.split('=',1) for l in r.stdout.splitlines() if '=' in l)
def get(path):
 q=urllib.request.Request('https://api.github.com/repos/preshious11/Sureodds/'+path,headers={'Authorization':'Bearer '+v['password'],'Accept':'application/vnd.github+json'})
 with urllib.request.urlopen(q,timeout=30) as f:return json.load(f)
p=get('pulls/4');print(json.dumps({k:p.get(k) for k in ['state','draft','mergeable','mergeable_state','merged','node_id']}));print('head',p['head']['sha'],'base',p['base']['sha'])
for path in ['commits/'+p['head']['sha']+'/check-runs','commits/'+p['head']['sha']+'/status','pulls/4/reviews']:
 d=get(path)
 if isinstance(d,list): print('reviews',[(x['state'],x['body']) for x in d])
 else:print(json.dumps({k:d.get(k) for k in ['state','total_count'] if k in d}));print([(x['name'],x['status'],x['conclusion']) for x in d.get('check_runs',[])])
