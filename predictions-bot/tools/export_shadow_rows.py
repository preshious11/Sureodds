"""Exports one run's newly-inserted Pro value shadow rows as JSON, for
delivery to the server over the SSH relay (see
workers/bot/ssh-relay/receive-batch.sh's "pro-value-rows" job and
apply_shadow_rows.py, the server-side half of this sync).

Read-only against the Actions-side shadow DB (the same file the "Pro value
shadow (live only...)" step in stage5-acceptance-production.yml just wrote
to) - never touches it, just reads back the rows tagged with the given
run_id and writes them out.

Usage: export_shadow_rows.py <db-path> <run-id> <out-json-path>
"""
from __future__ import annotations

import json
import sqlite3
import sys


def export_rows(db_path: str, run_id: str) -> list[dict]:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(
            "SELECT * FROM pro_value_rows WHERE run_id = ? ORDER BY id", (run_id,)
        ).fetchall()]
    finally:
        con.close()


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print("usage: export_shadow_rows.py <db-path> <run-id> <out-json-path>", file=sys.stderr)
        return 1
    _, db_path, run_id, out_path = argv
    rows = export_rows(db_path, run_id)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(rows, fh)
    print(json.dumps({"event": "exported_shadow_rows", "run_id": run_id, "rows": len(rows)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
