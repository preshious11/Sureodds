import subprocess,os,json,urllib.request,urllib.error,re
credential=subprocess.run(['git','credential','fill'],input='protocol=https\nhost=github.com\n\n',text=True,capture_output=True,env={**os.environ,'GIT_TERMINAL_PROMPT':'0'},timeout=20)
values=dict(l.split('=',1) for l in credential.stdout.splitlines() if '=' in l)
class Redirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self,req,fp,code,msg,headers,newurl):
  result=super().redirect_request(req,fp,code,msg,headers,newurl)
  if result: result.remove_header('Authorization')
  return result
request=urllib.request.Request('https://api.github.com/repos/preshious11/Sureodds/actions/jobs/124843085611/logs',headers={'Authorization':'Bearer '+values['password'],'Accept':'application/vnd.github+json'})
try:
 raw=urllib.request.build_opener(Redirect()).open(request,timeout=30).read().decode()
 lines=[l for l in raw.splitlines() if any(s in l for s in ['result lookup via','lookup failed','quota','rate limit','unresolved'])]
 safe=[re.sub(r'https?://\S+','[redacted-url]',l) for l in lines]
 pathlib.Path('artifacts/recovery-provider-warnings.log').write_text('\n'.join(safe),encoding='utf-8')
 print('warning_lines',len(safe))
 print('\n'.join(safe[:12]))
except urllib.error.HTTPError as e: print('logs_http_status',e.code)
