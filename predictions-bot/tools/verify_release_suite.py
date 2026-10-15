"""Run offline tests in fresh scratch space; optional Windows sandbox ACL workaround.

The workaround affects only mode-0700 mkdir calls under --scratch, including
test subprocesses. It does not alter application logic or historical databases.
Use the standard pytest command in an unrestricted CI environment before release.
"""
import argparse
import os
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scratch', required=True)
    parser.add_argument('--windows-sandbox-acl', action='store_true')
    args, pytest_args = parser.parse_known_args()
    scratch = Path(args.scratch).resolve()
    if scratch.exists():
        parser.error('scratch must be a new directory')
    scratch.mkdir(parents=True)
    env = dict(os.environ, TEMP=str(scratch), TMP=str(scratch))
    # Explicitly disable delivery and remove provider credentials in child tests.
    for key in list(env):
        if any(marker in key for marker in ('API_KEY', 'API_TOKEN', 'TELEGRAM_TOKEN')):
            env.pop(key)
    if args.windows_sandbox_acl:
        bootstrap = scratch / 'bootstrap'; bootstrap.mkdir()
        (bootstrap / 'sitecustomize.py').write_text(
            'import os\n_real=os.mkdir\n_root=' + repr(str(scratch)) + '\n'
            'def _mkdir(path,mode=0o777,*args,**kwargs):\n'
            '    if mode==0o700 and os.path.commonpath([os.path.abspath(path),_root])==_root: mode=0o777\n'
            '    return _real(path,mode,*args,**kwargs)\n'
            'os.mkdir=_mkdir\n', encoding='utf-8')
        env['PYTHONPATH'] = str(bootstrap) + os.pathsep + env.get('PYTHONPATH', '')
    return subprocess.call([sys.executable, '-m', 'pytest', 'tests', '-q',
                            '-p', 'no:cacheprovider', '--basetemp', str(scratch / 'pytest'),
                            '--tb=short', *pytest_args], env=env,
                           cwd=Path(__file__).resolve().parents[1])


if __name__ == '__main__':
    raise SystemExit(main())
