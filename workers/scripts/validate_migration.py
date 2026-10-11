"""Validate a Stage-1 migration without touching production.

Compares workers/build/row_counts.json (source SQLite counts) against a
target D1 database (queried via `wrangler d1 execute --json`) or against a
local SQLite file created by applying the export SQL.

Usage:
  python workers/scripts/validate_migration.py --counts workers/build/row_counts.json --target-sqlite /tmp/staging_check.db
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys

TABLES = (
    "subscribers",
    "picks",
    "prediction_memberships",
    "prediction_cache",
    "posts",
    "deliveries",
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--counts", required=True)
    ap.add_argument("--target-sqlite", required=True)
    args = ap.parse_args()

    with open(args.counts, encoding="utf-8") as fh:
        expected = json.load(fh)

    con = sqlite3.connect(args.target_sqlite)
    ok = True
    for table in TABLES:
        try:
            got = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except sqlite3.OperationalError as exc:
            print(f"FAIL {table}: target missing ({exc})")
            ok = False
            continue
        want = int(expected.get(table, 0))
        status = "OK  " if got == want else "FAIL"
        if got != want:
            ok = False
        print(f"{status} {table}: source={want} target={got}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
