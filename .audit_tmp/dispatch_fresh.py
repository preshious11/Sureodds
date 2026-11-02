exec(open('.audit_tmp/inspect_pr.py').read().split("p=get('pulls/4')")[0])
payload={'ref':'fix/oracle-production-recovery-20261101','inputs':{'operation':'fresh-evaluate','dry_run':True,'live_confirm':'RUN-LIVE','allow_telegram_posts':False,'write_target':'server'}}
req=urllib.request.Request('https://api.github.com/repos/preshious11/Sureodds/actions/workflows/stage5-acceptance-production.yml/dispatches',data=json.dumps(payload).encode(),headers={'Authorization':'Bearer '+v['password'],'Content-Type':'application/json','Accept':'application/vnd.github+json'},method='POST')
with urllib.request.urlopen(req,timeout=30) as response:print('dispatch_http',response.status)
