"""Build and verify a source-only ZIP, excluding runtime data and active secrets."""
import argparse
import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import zipfile

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'predictions-bot'))
from predbot.config import _parse_env_file

EXCLUDED_PARTS={'.git','.venv','venv','node_modules','__pycache__','.pytest_cache',
    '.mypy_cache','.ruff_cache','.tool-cache','.wrangler','.codex','.agents','.aws','artifacts','dist','build'}
EXCLUDED_EXPORTS={'SPORTYBET_ARCHIVE_INVENTORY.json','SPORTYBET_APP_ARCHIVE_INVENTORY.json',
    'SPORTYBET_CURRENT_CANDIDATE_CHECK.json','SPORTYBET_CANDIDATE_SEARCH.json','SPORTYBET_FINAL_CANDIDATE_SEARCH.json',
    'MARKET_COVERAGE_EVIDENCE.json','MARKET_COVERAGE_EVIDENCE.csv'}


def excluded(name):
    p=Path(name);parts=set(p.parts);base=p.name.lower()
    if parts & EXCLUDED_PARTS or name.startswith(('data/','predictions-bot/data/')): return True
    if p.name in EXCLUDED_EXPORTS: return True
    if base.endswith(('.db','.db-wal','.db-shm','.sqlite','.sqlite3','.pyc','.log','.zip','.pem','.key','.pfx','.p12')): return True
    if base.endswith('.example'): return False
    return base.startswith(('.env','.dev.vars')) or base in {'.npmrc','.netrc','credentials','secrets','id_rsa','id_ed25519'}


def active_secrets():
    values=dict(os.environ)
    for name in ('predictions-bot/.env','.env','workers/.dev.vars'):
        path=ROOT/name
        if path.is_file(): values.update(_parse_env_file(path))
    return [v.encode() for k,v in values.items()
            if any(s in k.upper() for s in ('TOKEN','SECRET','PASSWORD','API_KEY','ACCESS_KEY'))
            and len(v)>=16 and not v.startswith(('YOUR_','<')) and '...' not in v]


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',required=True)
    args=p.parse_args()
    output=Path(args.output).resolve()
    if not output.is_relative_to(ROOT) or output.suffix!='.zip' or output.exists():
        p.error('output must be a new ZIP path inside this workspace')
    raw=subprocess.check_output(['git','ls-files','--cached','--others','--exclude-standard','-z',
        '--','.',':(exclude).tool-cache/**',':(exclude)artifacts/**'],cwd=ROOT)
    paths=sorted(set(n.decode().replace('\\','/') for n in raw.split(b'\0') if n))
    secrets=active_secrets();files=[]
    for name in paths:
        if excluded(name): continue
        path=ROOT/name
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(ROOT): continue
        content=path.read_bytes()
        if any(value in content for value in secrets) or re.search(br'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',content):
            raise SystemExit(f'Archive blocked: credential material detected in {name}; no secret value is printed.')
        files.append((name,content))
    output.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(output,'x',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as archive:
        for name,content in files: archive.writestr(name,content)
    with zipfile.ZipFile(output) as archive:
        assert archive.testzip() is None
        assert len(archive.namelist())==len(files)
        assert all(not excluded(n) and not Path(n).is_absolute() and '..' not in Path(n).parts for n in archive.namelist())
        for name,content in files: assert hashlib.sha256(archive.read(name)).digest()==hashlib.sha256(content).digest()
    metadata={'archive':str(output),'files':len(files),'bytes':output.stat().st_size,
              'sha256':hashlib.sha256(output.read_bytes()).hexdigest(),'crc_and_content_verified':True,
              'active_secret_values_checked':len(secrets),
              'exclusions':'Git/venvs/caches/local envs/credentials/runtime databases/data exports/build outputs excluded; example env templates retained.'}
    with output.with_suffix('.manifest.json').open('x',encoding='utf-8') as f:json.dump(metadata,f,indent=2);f.write('\n')
    print(json.dumps(metadata))


if __name__=='__main__': main()
