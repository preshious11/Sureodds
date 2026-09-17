from __future__ import annotations
import pathlib, re, sys

PAT = re.compile(r"ASSERT FAIL|discover-and-fix|provider-line")
hits: list[str] = []
for f in pathlib.Path(".").rglob("*.py"):
    try:
        t = f.read_text(errors="ignore")
    except Exception as e:
        print("ERR", f, e, file=sys.stderr)
        continue
    if PAT.search(t):
        hits.append(str(f))
if hits:
    print("\n".join(hits))
else:
    print("NO_HITS")
sys.exit(0)
