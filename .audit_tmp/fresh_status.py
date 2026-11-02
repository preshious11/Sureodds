exec(open('.audit_tmp/inspect_pr.py').read().split("p=get('pulls/4')")[0])
runs=get('actions/workflows/stage5-acceptance-production.yml/runs?branch=fix%2Foracle-production-recovery-20261101&event=workflow_dispatch&per_page=3')['workflow_runs']
for r in runs:
 print(json.dumps({k:r[k] for k in ['id','status','conclusion','head_sha','created_at','html_url']}))
 for j in get('actions/runs/'+str(r['id'])+'/jobs')['jobs']:
  print(j['id'],j['name'],j['status'],j['conclusion'],[(s['name'],s['status'],s['conclusion']) for s in j['steps']])
 break
