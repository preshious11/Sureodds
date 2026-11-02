exec(open('.audit_tmp/inspect_pr.py').read().split("p=get('pulls/4')")[0])
p=get('pulls/4')
for c in get('commits/'+p['head']['sha']+'/check-runs')['check_runs']:
 print(c['name'],c['conclusion'],c['details_url'])
 if c['conclusion']=='failure':
  print(c.get('output'))
  for a in get('check-runs/'+str(c['id'])+'/annotations'): print(json.dumps({k:a.get(k) for k in ['path','start_line','message','title']})[:1500])
