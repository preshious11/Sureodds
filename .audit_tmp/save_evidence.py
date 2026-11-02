exec(open('.audit_tmp/inspect_pr.py').read().split("p=get('pulls/4')")[0])
p=get('pulls/4');checks=get('commits/'+p['head']['sha']+'/check-runs')['check_runs']
print(json.dumps({'draft':p['draft'],'mergeable':p['mergeable'],'mergeable_state':p['mergeable_state'],'head':p['head']['sha'],'base':p['base']['sha']}))
print([(c['name'],c['status'],c['conclusion']) for c in checks])
for j in get('actions/runs/41309484981/jobs')['jobs']:
 if j['name']!='fresh-evaluation':continue
 a=get('check-runs/'+j['check_run_url'].rsplit('/',1)[-1]+'/annotations')
 for row in a:
  if row.get('title')=='Fresh evaluation':
   import pathlib
   pathlib.Path('artifacts/deployment-fresh-annotation.json').write_text(json.dumps(row,indent=2),encoding='utf-8')
   print('annotation_length',len(row['message'])); print('annotation_tail',row['message'][-150:])
