"""Read-only helper for querying the production D1 database through wrangler.

Only SELECT statements are allowed: anything else raises before wrangler is invoked.
"""
from __future__ import annotations

import json
import os
import re
import subprocess

WORKERS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "workers")
WRANGLER_JS = os.environ.get(
    "WRANGLER_JS",
    os.path.expanduser(r"~\AppData\Local\npm-cache\_npx\827da23dfd673aa2\node_modules\wrangler\bin\wrangler.js"))
_FORBIDDEN =re.compile(r"\b(insert|update|delete|alter|drop|create|replace|pragma|attach|detach|vacuum|reindex)\b",
                        re.IGNORECASE)


def select(sql: str):
    """Run one SELECT against sureodds_production (remote) and return the list of row dicts."""
    stripped = sql.strip().rstrip(";")
    if not stripped.lower().startswith("select") or ";" in stripped or _FORBIDDEN.search(stripped):
        raise ValueError("read-only helper: only a single SELECT statement is allowed")
    # wrangler 4.151.1 as cached by `npx -y wrangler@4.151.1`, invoked through node directly so the SQL is passed
    # as one argv element (no cmd.exe quoting of < > | characters).
    cmd = ["node", WRANGLER_JS, "d1", "execute", "sureodds_production", "--remote",
           "--config", "wrangler.toml", "--json", "--command", stripped]
    out = subprocess.run(cmd, cwd=WORKERS, capture_output=True, text=True, encoding="utf-8", timeout=300)
    text = out.stdout
    start = text.find("[")
    if out.returncode != 0 or start < 0:
        raise RuntimeError(f"wrangler failed ({out.returncode}): {out.stderr[-2000:]}")
    payload = json.loads(text[start:])
    if not payload or not payload[0].get("success"):
        raise RuntimeError(f"query failed: {text[-2000:]}")
    meta = payload[0].get("meta", {})
    if meta.get("changes") or meta.get("rows_written") or meta.get("changed_db"):
        raise RuntimeError("unexpected write reported by D1 - aborting")
    return payload[0]["results"]
