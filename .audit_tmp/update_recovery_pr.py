import subprocess,os,json,urllib.request,pathlib
r=subprocess.run(['git','credential','fill'],input='protocol=https\nhost=github.com\n\n',text=True,capture_output=True,env={**os.environ,'GIT_TERMINAL_PROMPT':'0'},timeout=30)
values=dict(l.split('=',1) for l in r.stdout.splitlines() if '=' in l)
if not values.get('password'): raise SystemExit('No repository credential available for draft PR')
payload={'title':'Recover Oracle prediction persistence, notifications, and settlement','body':pathlib.Path('.audit_tmp/recovery-pr-body.md').read_text()}
request=urllib.request.Request('https://api.github.com/repos/preshious11/Sureodds/pulls/4',data=json.dumps(payload).encode(),headers={'Authorization':'Bearer '+values['password'],'Accept':'application/vnd.github+json','Content-Type':'application/json'},method='PATCH')
try:
 with urllib.request.urlopen(request,timeout=30) as response:
  result=json.load(response); print(json.dumps({'url':result['html_url'],'number':result['number'],'draft':result['draft']}))
except urllib.error.HTTPError as error: print('draft_pr_http_status',error.code)
