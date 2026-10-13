"""Validate a prior run's plan artifact before a resume-only synchronization.

A resume applies EXACTLY the batches another run planned: no engine, no
providers, no regeneration. This check runs before any lease or write and
refuses when:
- plan.json is missing, malformed, or from a different target database,
- the estimated writes exceed the given budget,
- expected batch files are absent (plan.sql_batches vs files on disk),
- any batch touches a table outside the allowlist (picks,
  prediction_memberships, ticket_candidates, prediction_cache),
- any batch filename suggests worker-owned or roster data.

Prints a one-line summary; exit 0 = safe to resume, 1 = refuse with reason.
Reads nothing from the network and writes nothing anywhere.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

ALLOWED_TABLES = frozenset({
    "picks", "prediction_memberships", "ticket_candidates", "prediction_cache",
})

STATEMENT_TABLE = re.compile(r"^\s*INSERT\s+(?:OR\s+\w+\s+)?INTO\s+([A-Za-z_][\w]*)", re.IGNORECASE)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("artifact_dir")
    ap.add_argument("--target-db", required=True)
    ap.add_argument("--write-budget", type=int, default=5000)
    args = ap.parse_args(argv)

    plan_path = os.path.join(args.artifact_dir, "plan.json")
    try:
        with open(plan_path, encoding="utf-8") as fh:
            plan = json.load(fh)
    except (OSError, ValueError) as exc:
        print(f"FAIL plan.json unreadable: {exc}", file=sys.stderr)
        return 1
    target = plan.get("target_db", plan.get("target"))
    if target not in (None, args.target_db) and target != args.target_db:
        print(f"FAIL plan targets {target!r}, resume allows only {args.target_db!r}",
              file=sys.stderr)
        return 1
    if args.target_db != "sureodds_staging":
        print(f"FAIL resume allows only the staging database, got {args.target_db!r}",
              file=sys.stderr)
        return 1
    estimated = int(plan.get("estimated_writes", -1))
    if estimated < 0:
        print("FAIL plan has no estimated_writes", file=sys.stderr)
        return 1
    if estimated > args.write_budget:
        print(f"FAIL plan estimates {estimated} writes over budget {args.write_budget}",
              file=sys.stderr)
        return 1
    sql_files = sorted(f for f in os.listdir(args.artifact_dir) if f.endswith(".sql"))
    expected = int(plan.get("sql_batches", -1))
    if expected >= 0 and len(sql_files) != expected:
        print(f"FAIL artifact holds {len(sql_files)} batch files, plan lists {expected}",
              file=sys.stderr)
        return 1
    if expected < 0 and not sql_files:
        print("FAIL no batch files and no count to reconcile", file=sys.stderr)
        return 1
    for name in sql_files:
        lowered = name.lower()
        if "deliveries" in lowered or "subscribers" in lowered:
            print(f"FAIL refusing worker-owned batch {name}", file=sys.stderr)
            return 1
        with open(os.path.join(args.artifact_dir, name), encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                stripped = line.strip()
                if not stripped or stripped.startswith("--"):
                    continue
                m = STATEMENT_TABLE.match(stripped)
                if not m or m.group(1) not in ALLOWED_TABLES:
                    print(f"FAIL {name}:{lineno} touches unexpected table", file=sys.stderr)
                    return 1
    rest_dir = os.path.join(args.artifact_dir, "rest")
    rest_pending = int(plan.get("rest_pending", 0))
    rest_files = []
    if os.path.isdir(rest_dir):
        rest_files = [f for f in os.listdir(rest_dir) if f.endswith(".jsonl")]
    if rest_pending and not rest_files:
        print("FAIL plan lists rest batches but artifact has none", file=sys.stderr)
        return 1
    for name in rest_files:
        with open(os.path.join(rest_dir, name), encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                try:
                    entry = json.loads(line)
                except ValueError:
                    print(f"FAIL malformed rest entry {name}:{lineno}", file=sys.stderr)
                    return 1
                sql = entry.get("sql", "") if isinstance(entry, dict) else ""
                m = STATEMENT_TABLE.match(sql.strip())
                if not m or m.group(1) not in ALLOWED_TABLES:
                    print(f"FAIL rest entry {name}:{lineno} touches unexpected table",
                          file=sys.stderr)
                    return 1
    print(f"resume OK: target={args.target_db} estimated_writes={estimated} "
          f"batches={len(sql_files)} rest_files={len(rest_files)} "
          f"complete={bool(plan.get('complete'))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
