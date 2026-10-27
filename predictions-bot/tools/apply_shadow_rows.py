"""Applies Pro value shadow rows delivered from a GitHub Actions selection
run to the SERVER's own shadow DB - the server-side half of the
selection/closing sync link (see docs/STAGE1_SERVER_MIGRATION_PLAN.md's note
on the gap found during the rehearsal: selection runs on Actions, closing
capture runs on the server, and until this script existed the two had no
connection at all).

Deliberately NOT routed through the main D1 relay (workers/bot/ssh-relay/) -
the shadow DB is a separate file with a different schema
(predbot/pro_value/store.py's ShadowStore explicitly refuses to touch the
production DB), so this reuses ShadowStore directly instead of teaching the
production-schema-specific plan/diff tooling a second, unrelated schema.

Idempotent by run_id, not by a plan-id ledger (there's no epoch/staleness
concept for this data - a selection run's rows are only ever written once,
by itself): if ANY row for the delivered run_id already exists in the
server's DB, the whole delivery is treated as already-applied and skipped,
so a retried delivery (e.g. after a dropped SSH connection) never
duplicates rows.

Usage: apply_shadow_rows.py <rows-json-path> <db-path>
rows-json-path: a JSON array of row dicts, as produced by
export_shadow_rows.py (every row from one run_id, straight from a
`SELECT * FROM pro_value_rows` - extra columns like `id` or `closing_*`
are present and harmless; only the columns ShadowStore.insert_rows() reads
are used).
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from predbot.pro_value.store import ShadowStore  # noqa: E402 - see sys.path insert above

REQUIRED_ROW_KEYS = {"match_key", "kickoff_ts", "outcome", "sporty_odds", "status"}


def fail(message: str) -> int:
    print(json.dumps({"event": "apply_shadow_rows_failed", "error": message}))
    return 1


def apply_rows(rows_path: str, db_path: str) -> int:
    try:
        with open(rows_path, encoding="utf-8") as fh:
            rows = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        return fail(f"could not read rows payload: {exc}")

    if not isinstance(rows, list):
        return fail("payload is not a JSON array")
    if not rows:
        print(json.dumps({"event": "apply_shadow_rows", "outcome": "empty", "run_id": None, "rows": 0}))
        return 0

    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            return fail(f"row {i} is not an object")
        missing = REQUIRED_ROW_KEYS - row.keys()
        if missing:
            return fail(f"row {i} missing required keys: {sorted(missing)}")

    run_ids = {row.get("run_id") for row in rows}
    if len(run_ids) != 1 or None in run_ids:
        return fail("every row in one delivery must share exactly one non-null run_id")
    run_id = run_ids.pop()

    store = ShadowStore(db_path)
    try:
        existing = store.conn.execute(
            "SELECT COUNT(*) FROM pro_value_rows WHERE run_id = ?", (run_id,)
        ).fetchone()[0]
        if existing > 0:
            print(json.dumps({"event": "apply_shadow_rows", "outcome": "already_applied", "run_id": run_id, "rows": 0}))
            return 0

        run_at = rows[0].get("run_at")
        n = store.insert_rows(run_id, run_at, rows)
        print(json.dumps({"event": "apply_shadow_rows", "outcome": "applied", "run_id": run_id, "rows": n}))
        return 0
    finally:
        store.close()


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: apply_shadow_rows.py <rows-json-path> <db-path>", file=sys.stderr)
        return 1
    return apply_rows(argv[1], argv[2])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
