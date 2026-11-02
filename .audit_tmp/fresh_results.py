exec(open('.audit_tmp/inspect_pr.py').read().split("p=get('pulls/4')")[0])
for j in get('actions/runs/41309484981/jobs')['jobs']:
 print(j['name'],j['status'],j['conclusion'])
 check_id=j.get('check_run_url','').rsplit('/',1)[-1]
 if check_id:
  for a in get('check-runs/'+check_id+'/annotations'):
   print(json.dumps({k:a.get(k) for k in ['title','message','path','start_line']})[:35000])
