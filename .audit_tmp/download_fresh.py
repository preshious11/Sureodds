exec(open('.audit_tmp/inspect_pr.py').read().split("p=get('pulls/4')")[0])
import zipfile,io,pathlib
art=get('actions/runs/41309484981/artifacts')['artifacts'];print([(x['id'],x['name'],x['size_in_bytes']) for x in art])
request=urllib.request.Request(art[0]['archive_download_url'],headers={'Authorization':'Bearer '+v['password'],'Accept':'application/vnd.github+json'})
try:
 with urllib.request.urlopen(request,timeout=60) as response: data=response.read()
 z=zipfile.ZipFile(io.BytesIO(data));report=json.loads(z.read('report.json'));pathlib.Path('artifacts/deployment-fresh-report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
 print(json.dumps(report,default=str)[-22000:])
except Exception as e: print('artifact_download_failed',type(e).__name__)
