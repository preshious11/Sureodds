"""CI guard: every column the Python engine writes must exist in the D1 schema.

Compares the live engine schema (PRAGMA table_info of a SQLite file built by
predbot.storage.db.Database — which runs all Python migrations) against the
columns declared in workers/migrations/*.sql for the tables the sync owns.

Exit 0 when D1 covers the engine; otherwise prints each missing
TABLE.COLUMN and exits 1 so the workflow fails before staging deploys.

Usage:
  python workers/scripts/check_schema_compat.py --sqlite /tmp/engine_schema.db
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys

# Tables the push/sync path replicates. telegram_updates/job_state are
# Worker-owned (created by the migrations, not by the engine).
# Every table the sync replicates (all app tables except the three
# Worker-owned runtime tables: telegram_updates, job_state, sync_state).
# Shared-mutable tables (posts, deliveries, subscribers) are included: their
# D1 columns must match even though replication is insert-only.
ENGINE_TABLES = (
    "api_budget", "calibration", "canonical_events", "canonical_participants",
    "custom_pick_candidates", "custom_pick_pools", "deliveries",
    "history_fetches", "kv", "live_fixture_state", "match_odds",
    "match_odds_ext", "match_results", "participant_aliases", "picks",
    "posts", "prediction_cache", "prediction_memberships", "provider_cache",
    "provider_identity", "ratings", "results_seen", "sgo_usage",
    "subscribers", "ticket_candidates",
)


def engine_columns(sqlite_path: str) -> dict:
    con = sqlite3.connect(sqlite_path)
    out = {}
    existing = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    for table in ENGINE_TABLES:
        if table not in existing:
            continue
        out[table] = [r[1] for r in con.execute(f"PRAGMA table_info({table})").fetchall()]
    return out


def split_columns(body: str) -> list:
    """Split a CREATE TABLE body on top-level commas (ignoring CHECK(...) lists)."""
    parts, depth, current = [], 0, []
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    tail = "".join(current).strip()
    if tail:
        parts.append(tail)
    return parts


def d1_columns(migrations_sql: str) -> dict:
    out: dict = {}
    # Strip line comments first so trailing "-- ..." never pollutes a column.
    code = re.sub(r"--[^\n]*", "", migrations_sql)
    for match in re.finditer(r"CREATE TABLE IF NOT EXISTS (\w+)\s*\((.*?)\);", code,
                             re.DOTALL | re.IGNORECASE):
        table, body = match.group(1), match.group(2)
        cols = []
        for segment in split_columns(body):
            tokens = segment.strip().split()
            if not tokens:
                continue
            head = tokens[0].strip('"`[]').upper()
            if head in ("PRIMARY", "FOREIGN", "UNIQUE", "CHECK", "CONSTRAINT", "CREATE", "INDEX"):
                continue
            cols.append(tokens[0].strip('"`[]'))
        out.setdefault(table, [])
        for col in cols:
            if col not in out[table]:
                out[table].append(col)
    # ALTER TABLE ... ADD COLUMN patches (0002-style).
    for match in re.finditer(r"ALTER TABLE (\w+)\s+ADD COLUMN (\w+)", migrations_sql, re.IGNORECASE):
        out.setdefault(match.group(1), [])
        if match.group(2) not in out[match.group(1)]:
            out[match.group(1)].append(match.group(2))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sqlite", required=True)
    ap.add_argument("--migrations", required=True, help="Concatenated migrations .sql text file")
    args = ap.parse_args(argv)
    with open(args.migrations, encoding="utf-8") as fh:
        d1 = d1_columns(fh.read())
    engine = engine_columns(args.sqlite)
    missing = []
    for table, cols in sorted(engine.items()):
        if table not in ("telegram_updates", "job_state") and table not in d1:
            missing.append(f"{table} (table absent from D1)")
            continue
        have = {c.lower() for c in d1.get(table, [])}
        for col in cols:
            if col.lower() not in have:
                missing.append(f"{table}.{col}")
    if missing:
        print("D1 schema is missing engine columns:")
        for item in missing:
            print(f"  - {item}")
        return 1
    print(f"schema compat OK: {len(engine)} engine tables covered by D1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
