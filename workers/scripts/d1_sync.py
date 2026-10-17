"""Stage 2/3 engine-state synchronization between the Python runner and D1.

Single source of truth: D1 is the SERVING truth (Worker reads only).
The Actions runner works on a scratch SQLite copy: pull -> compute -> push.

Table ownership (Stage 3 audit — see report for the full table):
  ENGINE_OWNED  : computed by the engine; full-row replace allowed, but ONLY
                  when the push proves freshness (--expect-epoch).
  RUNNER_SCRATCH: provider_cache. Ephemeral API cache (rows exceed D1's 100KB
                  statement cap); rebuilt by the runner, never served, never
                  replicated. No history is lost (match_odds_ext/results keep it).
  SHARED_MERGE  : posts, deliveries. Insert-only receipts; push emits
                  INSERT OR IGNORE so a newer D1 row is never overwritten.
                  (The roster is Worker-owned; see --include-roster.)
  WORKER_OWNED  : telegram_updates, job_state, sync_state. NEVER in a push.

Staleness guard: sync_state.epoch advances on every accepted push. A push
records the epoch seen at pull time (--expect-epoch); apply/verify refuse
when the target has moved on. Concurrent generation is fenced by the
job_state lease AND the epoch check together.

Commands:
  manifest SRC --out DIR | push SRC --out DIR [--expect-epoch N]
  coalesce BATCH_DIR --out DIR [--max-bytes 90000]
  apply TARGET DIR | verify TARGET --manifest DIR/manifest.json
  lease TARGET JOB OWNER [--ttl-min 30] | release TARGET JOB OWNER
  epoch-get TARGET | epoch-bump TARGET [--expect N]
  snapshot SRC --out DIR   (timestamped backup copy + manifest; never the live DB)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

#: Commit phasing (atomic-fencing limitation, Stage 4A audit): D1 file
#: imports apply statement-by-statement with no cross-file transaction, so a
#: concurrent writer could interleave. Two mechanisms close the hole:
#:  1. The `generate` lease + epoch CAS serialize writers (only one holder).
#:  2. Phase-ordered commit: engine fact tables (p0_*) land before the
#:     serving gate and receipts (p1_*: prediction_cache COMPLETE marker,
#:     posts, deliveries). Readers gate on the COMPLETE row for the date, so
#:     a partial apply is INVISIBLE until the final phase lands; verify then
#:     proves completeness before cutover. Coalesce preserves lexical order.
PHASE_GATE_TABLES = {"prediction_cache", "posts", "deliveries"}

#: Parent tables that must sort before their FOREIGN KEY children within a
#: phase (D1 enforces FKs; local SQLite leaves them off by default, which is
#: why unordered imports pass locally and fail on D1). Children:
#: match_odds/match_odds_ext -> match_results, prediction_memberships -> picks.
#: picks already sorts before prediction_memberships alphabetically; only the
#: match_* family needs an explicit rank.
PARENT_FIRST = {"match_results"}

#: Max bytes per emitted SQL statement. D1's per-statement cap is 100,000
#: bytes; 90,000 leaves headroom for multi-byte characters and the import
#: wrapper. Statements are built row-by-row and flushed before crossing it,
#: so no statement can exceed the D1 limit no matter how wide the rows are.
STATEMENT_MAX_BYTES = 90000


def emit_statements(out: str, prefix: str, head: str, rows: list, render, rest_dir: str = "", suffix: str = "") -> int:
    """Write head + VALUES statements, each <= STATEMENT_MAX_BYTES. Returns file count.

    A single row that cannot fit D1's per-statement cap in ANY batching is
    diverted to rest/<prefix>.jsonl as {sql, params} for the D1 REST query
    API (params don't count toward the SQL-text cap; row cap is 2MB) instead
    of aborting the whole diff. Applying rest/ needs CLOUDFLARE_API_TOKEN and
    is a separately audited step; the manifest records rest_pending.
    """
    import json as _json
    files, current, index, rested = 0, [], 0, 0
    overhead = len((head + suffix).encode('utf-8'))
    size = overhead
    for r in rows:
        piece = render(r)
        piece_size = len(piece.encode('utf-8'))
        if overhead + piece_size + 1 > STATEMENT_MAX_BYTES:
            if not rest_dir:
                print(f"FAIL {prefix}: single row exceeds D1's 100KB statement cap", file=sys.stderr)
                raise ValueError(f"{prefix}: oversize row")
            os.makedirs(rest_dir, exist_ok=True)
            cols = head[head.index("(") + 1:head.index(")")].split(",")
            placeholders = ",".join("?" * len(cols))
            stmt = f"{head[:head.index('VALUES')]}VALUES({placeholders}){suffix}"
            with open(os.path.join(rest_dir, f"{prefix}.jsonl"), "a", encoding="utf-8") as fh:
                fh.write(_json.dumps({"sql": stmt, "params": list(r)}, default=str) + "\n")
            rested += 1
            continue
        if current and size + 1 + piece_size + 1 > STATEMENT_MAX_BYTES:
            with open(os.path.join(out, f"{prefix}_{index:04d}.sql"), "w", encoding="utf-8") as fh:
                fh.write(head + ",".join(current) + suffix + ";\n")
            files += 1
            index += 1
            current, size = [], overhead
        current.append(piece)
        size += (1 if len(current) > 1 else 0) + piece_size
    if current:
        with open(os.path.join(out, f"{prefix}_{index:04d}.sql"), "w", encoding="utf-8") as fh:
            fh.write(head + ",".join(current) + suffix + ";\n")
        files += 1
    return files

#: Never replicated from an Actions snapshot (Worker-owned runtime state).
PUSH_EXCLUDE = {"telegram_updates", "job_state", "sync_state"}

#: Ephemeral compute scratch: provider API-response cache (payloads routinely
#: exceed D1's 100KB per-statement cap — the drill found a 103,889-byte row —
#: and carry expires_at semantics). The runner rebuilds it from providers;
#: the Worker never serves from it (it reads prediction_cache/picks).
#: Excluding it loses no history: match_odds_ext/match_results carry the record.
SCRATCH_EXCLUDE = {"provider_cache"}

#: The subscriber roster is Worker-owned once live: every /start, timezone
#: change and unsubscribe lands in D1 first, and engine snapshots can never
#: be fresher (the drill proved a stale snapshot's non-empty username would
#: regress a newer D1 value under the engine's own merge rule). Ongoing
#: pushes therefore EXCLUDE subscribers; the roster is seeded once via
#: --include-roster (initial staging import) and thereafter lives in D1.
ROSTER_TABLE = "subscribers"

#: Shared mutable receipts: insert-only replication. A conflict means D1
#: already has the row, which is newer-or-equal by construction (receipts are
#: never updated), so IGNORE is safe. The roster is NOT here (see above).
MERGE_ONLY = {"posts", "deliveries"}

# Synchronization is not an administrative history editor. Corrections require
# a separately reviewed recovery procedure, never a stale engine snapshot.
PRESERVE_ONLY = {"prediction_memberships", "match_results", "match_odds",
                 "match_odds_ext", "results_seen", "canonical_events",
                 "canonical_participants", "provider_identity", "participant_aliases",
                 "custom_pick_candidates", "custom_pick_pools"}


def unsafe_history_sql(sql):
    """Reject legacy destructive batches before any local/REST batch is applied."""
    protected = '|'.join(sorted(PRESERVE_ONLY | MERGE_ONLY | {'picks', 'ticket_candidates', 'prediction_cache'}))
    return bool(re.search(
        rf'\b(?:DELETE\s+FROM|DROP\s+TABLE|INSERT\s+OR\s+REPLACE\s+INTO)\s+["`\[]?(?:{protected})\b',
        sql, re.IGNORECASE))


def write_policy(table, cols, con=None):
    """Target-side guards apply equally to SQL batches and REST overflow rows."""
    columns = ", ".join(cols)
    if table in MERGE_ONLY | PRESERVE_ONLY:
        return f"INSERT OR IGNORE INTO {table} ({columns}) VALUES ", ""
    if table == "picks":
        mutable = [c for c in ("status", "settled_at", "result_json") if c in cols]
        if "id" not in cols or "status" not in cols:
            return f"INSERT OR IGNORE INTO {table} ({columns}) VALUES ", ""
        immutable = [c for c in cols if c not in mutable and c != "created_at"]
        same = " AND ".join(f"picks.{c} IS excluded.{c}" for c in immutable)
        assignments = ", ".join(f"{c}=excluded.{c}" for c in mutable)
        suffix = (f" ON CONFLICT(id) DO UPDATE SET {assignments} WHERE "
                  f"picks.status='pending' AND excluded.status IN "
                  f"('won','lost','push','void','half_won','half_lost') AND {same}")
        return f"INSERT OR IGNORE INTO picks ({columns}) VALUES ", suffix
    # Candidate archives and serving snapshots may be refreshed before issue;
    # once synced, retain them rather than replace historical evaluation data.
    if table == "prediction_cache" and {"cache_key", "status"} <= set(cols):
        assignments = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "cache_key")
        publication_guard = ''
        if con is not None and 'local_date' in cols:
            for receipt in ('posts', 'deliveries'):
                if receipt in app_tables(con):
                    publication_guard += (f" AND NOT EXISTS(SELECT 1 FROM {receipt} WHERE "
                                          f"post_date=prediction_cache.local_date AND kind IN "
                                          "('picks','picks_top','picks_daily'))")
        return (f"INSERT OR IGNORE INTO {table} ({columns}) VALUES ",
                f" ON CONFLICT(cache_key) DO UPDATE SET {assignments} "
                "WHERE prediction_cache.status!='COMPLETE' AND excluded.status='COMPLETE'" + publication_guard)
    if table in {"ticket_candidates", "prediction_cache"}:
        return f"INSERT OR IGNORE INTO {table} ({columns}) VALUES ", ""
    if table == 'api_budget' and {'provider','bucket','used'} <= set(cols):
        return (f"INSERT OR IGNORE INTO {table} ({columns}) VALUES ",
                " ON CONFLICT(provider,bucket) DO UPDATE SET used=MAX(api_budget.used,excluded.used)")
    if table == 'live_fixture_state' and {'pick_date','fixture_key','status'} <= set(cols):
        # settlement_done/notifications_done must update monotonically (MAX) even once
        # status is already final: the old single row-level WHERE guarded the WHOLE
        # UPDATE, so a later settlement/notification flip (exactly the transition the
        # Match Ended alert waits on) could never reach D1 once a fixture went final.
        # Every other column keeps the previous guard, now expressed per-column via
        # CASE instead of one WHERE, since the monotonic columns must bypass it.
        guard = ("live_fixture_state.status NOT IN ('final','postponed','cancelled') "
                 "AND NOT (live_fixture_state.status='in_progress' AND excluded.status='scheduled')")
        monotonic = {'settlement_done', 'notifications_done'}
        parts = []
        for c in cols:
            if c in {'pick_date', 'fixture_key'}:
                continue
            if c in monotonic:
                parts.append(f"{c}=MAX(live_fixture_state.{c}, excluded.{c})")
            else:
                parts.append(f"{c}=CASE WHEN {guard} THEN excluded.{c} ELSE live_fixture_state.{c} END")
        assignments = ', '.join(parts)
        return (f"INSERT OR IGNORE INTO {table} ({columns}) VALUES ",
                f" ON CONFLICT(pick_date,fixture_key) DO UPDATE SET {assignments}")
    return f"INSERT OR REPLACE INTO {table} ({columns}) VALUES ", ""


def emit_linked_rows(con, table, cols, rows, out, prefix, rest):
    """Prevent local integer-ID collisions from attaching history to another fixture.

    Compare the source parent's immutable snapshot with the actual target parent,
    not just its ID. This condition survives coalescing and REST parameterization.
    """
    parents = [(r[3], r[2], r[4]) for r in con.execute(f"PRAGMA foreign_key_list({table})")]
    if table == 'prediction_memberships' and not parents:
        parents = [('prediction_id', 'picks', 'id')]
    statements = []
    for index, row in enumerate(rows):
        values = dict(zip(cols, row))
        clauses, parameters = [], list(row)
        for child_key, parent, parent_key in parents:
            cur = con.execute(f"SELECT * FROM {parent} WHERE {parent_key}=?", (values[child_key],))
            record = cur.fetchone()
            if record is None:
                raise ValueError(f"{table}: missing source parent {parent}")
            names = [d[0] for d in cur.description]
            immutable = [(name, value) for name, value in zip(names, record)
                         if not (parent == 'picks' and name in
                                 {'status', 'settled_at', 'result_json', 'created_at'})]
            clauses.append(f"EXISTS(SELECT 1 FROM {parent} WHERE " +
                           " AND ".join(f"{name} IS ?" for name, _ in immutable) + ")")
            parameters.extend(value for _, value in immutable)
        sql = (f"INSERT OR IGNORE INTO {table} ({', '.join(cols)}) SELECT " +
               ', '.join('?' for _ in cols) + ' WHERE ' + ' AND '.join(clauses))
        pieces = sql.split('?')
        rendered = ''.join(piece + (quote(parameters[i]) if i < len(parameters) else '')
                           for i, piece in enumerate(pieces)) + ';\n'
        if len(rendered.encode('utf-8')) > STATEMENT_MAX_BYTES:
            os.makedirs(rest, exist_ok=True)
            with open(os.path.join(rest, prefix + '.jsonl'), 'a', encoding='utf-8') as fh:
                fh.write(json.dumps({'sql': sql, 'params': parameters}) + '\n')
        else:
            statements.append(rendered)
    if statements:
        # Individual statements obey D1 limits; coalesce supplies bounded files.
        with open(os.path.join(out, prefix + '_0000.sql'), 'w', encoding='utf-8') as fh:
            fh.writelines(statements)
    return int(bool(statements))


def app_tables(con: sqlite3.Connection) -> list:
    rows = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [r[0] for r in rows]


def table_fingerprint(con: sqlite3.Connection, table: str) -> tuple:
    # Order-insensitive: rows hash as sorted (column, value) pairs, so a
    # target whose DDL declares the same columns in a different physical
    # order (e.g. picks after Python ALTER migrations) still verifies.
    # Volatile write-stamps and timing telemetry are excluded from the hash
    # (see VOLATILE_COLUMNS / VOLATILE_JSON_PATHS): identical predictions
    # re-run minutes apart must compare equal. Full rows still replicate.
    cols = [r[1] for r in con.execute(f"PRAGMA table_info({table})").fetchall()]
    count = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    h = hashlib.sha256()
    cur = con.execute(f"SELECT * FROM {table}")
    names = [d[0] for d in cur.description] if cur.description else []
    for row in cur:
        h.update(_row_hash(table, names, row).encode("utf-8"))
    return count, h.hexdigest(), cols


#: Wall-clock write stamps excluded from row-hash comparison. Evidence: two
#: identical replay runs 88s apart differ ONLY here (plus JSON timings
#: below); every probability, price, status, selection, odd and ID is
#: bit-identical. Safety per column:
#: - picks.created_at: write default (db.py setdefault); never read by
#:   selection, pricing or display logic (verified by grep: only writes,
#:   ticket-audit display, and an unrelated recovery dataclass field).
#: - picks.settled_at: always written atomically WITH status+result_json by
#:   settle_pick, so excluding it cannot mask a grading change (the status
#:   and result still trigger the update, carrying the fresh timestamp).
#: Policy: extend ONLY with run-vs-run diff evidence, never speculatively.
VOLATILE_COLUMNS = {
    "picks": {"created_at", "settled_at"},
}

#: Timing-telemetry subtrees inside JSON payload columns, excluded from the
#: hash by explicit path (never by key name, so a market literally called
#: "timings" could not be ignored). Evidence: identical runs differ only in
#: these cpu/wall duration leaves; the replay harness itself already strips
#: "timings" from golden diagnostics (tools/replay.py _VOLATILE precedent).
#: - prediction_cache.payload_json @ generation.timings
#: - custom_pick_pools.diagnostics_json @ timings
VOLATILE_JSON_PATHS = {
    "prediction_cache": {"payload_json": ["generation", "timings"]},
    "custom_pick_pools": {"diagnostics_json": ["timings"]},
}


def _without_volatile_subpath(value, path: list):
    """Return value with the nested path removed (lists traversed by index
    only when the path element is an int; dicts by key). Missing paths and
    unparsable JSON are returned unchanged (fail-visible, never fail-silent).
    """
    if not path or not isinstance(value, (dict, list)):
        return value
    head, rest = path[0], path[1:]
    if isinstance(value, dict):
        if head not in value:
            return value
        if not rest:
            return {k: v for k, v in value.items() if k != head}
        return {k: (_without_volatile_subpath(v, rest) if k == head else v)
                for k, v in value.items()}
    if isinstance(head, int) and 0 <= head < len(value):
        return [(_without_volatile_subpath(v, rest) if i == head else v)
                for i, v in enumerate(value)]
    return value


def _row_hash(table: str, names: list, row: tuple) -> str:
    skip = VOLATILE_COLUMNS.get(table, set())
    json_paths = VOLATILE_JSON_PATHS.get(table, {})
    pairs = []
    by_name = dict(zip(names, row))
    for name in names:
        if name in skip:
            continue
        value = by_name[name]
        if name in json_paths and isinstance(value, str):
            try:
                parsed = json.loads(value)
            except ValueError:
                pairs.append((name, value))
                continue
            cleaned = _without_volatile_subpath(parsed, json_paths[name])
            pairs.append((name, json.dumps(cleaned, sort_keys=True)))
            continue
        pairs.append((name, value))
    return repr(sorted(pairs, key=lambda kv: kv[0]))


def key_columns(con: sqlite3.Connection, table: str) -> list | None:
    """Stable row key for diffing: PRIMARY KEY columns, else UNIQUE index
    columns, else None (add-only by full-row hash; never deleted)."""
    info = con.execute(f"PRAGMA table_info({table})").fetchall()
    pks = [r[1] for r in sorted([r for r in info if r[5] > 0], key=lambda r: r[5])]
    if pks:
        return pks
    for idx in con.execute(f"PRAGMA index_list({table})").fetchall():
        if idx[2]:  # unique
            cols = [r[2] for r in con.execute(f"PRAGMA index_info({idx[1]})").fetchall()]
            if cols:
                return cols
    return None


def row_map(con: sqlite3.Connection, table: str, keys: list | None) -> dict:
    """key-tuple -> row-hash for every row. Streams; ~70k rows fit in memory.
    Hashing honors VOLATILE_COLUMNS / VOLATILE_JSON_PATHS (see table_fingerprint)."""
    out = {}
    cur = con.execute(f"SELECT * FROM {table}")
    names = [d[0] for d in cur.description] if cur.description else []
    key_idx = [names.index(k) for k in keys] if keys else None
    for row in cur:
        h = hashlib.sha256(_row_hash(table, names, row).encode("utf-8")).hexdigest()
        if key_idx is None:
            out.setdefault("__hash__:" + h, h)
        else:
            out[tuple(row[i] for i in key_idx)] = h
    return out


#: Tables whose rows the engine legitimately DELETES (reset_prediction_day
#: clears today's unpublished pending rows). All other tables are
#: append-or-update: diffs never emit DELETEs for them, so history and
#: settlement records cannot be removed by a sync.
DELETE_ALLOWED = {"picks", "prediction_memberships", "prediction_cache",
                  "custom_pick_candidates", "custom_pick_pools", "ticket_candidates"}

#: Delete order is children-before-parents (mirror of the FK-ordered commit).
DELETE_ORDER = ["ticket_candidates", "prediction_memberships", "custom_pick_candidates", "custom_pick_pools",
                "prediction_cache", "picks"]


def cmd_diff(base: str, src: str, out: str, only_tables=None, allow_deletes: bool = False,
             exclude_keys=None) -> int:
    """Emit ONLY the delta between the pulled base (D1 export) and the fresh
    compute snapshot: changed/added rows as phased batches + deletes.sql for
    DELETE_ALLOWED tables + delta-manifest.json.

    A no-change sync emits zero SQL files (manifest only): ordinary daily
    operations cost table-fingerprint READS, not 76k row WRITES.

    only_tables restricts the diff to named tables (a compute scratch DB that
    lacks history must never be diffed whole: every absent history row would
    look deleted). allow_deletes=False suppresses all DELETE emission for
    additive-only runs (e.g. publishing a backdated fixture slate).
    exclude_keys maps table -> set of single-column PK values (as strings) to
    skip entirely, recorded in the manifest under "excluded". Used ONLY for
    explicitly approved rows (e.g. a historical PARTIAL cache that must ride
    the REST path later): skipping is auditable, never silent.
    """
    if allow_deletes:
        print("FAIL destructive synchronization requires a separately validated recovery plan", file=sys.stderr)
        return 1
    os.makedirs(out, exist_ok=True)
    if os.listdir(out):
        print("FAIL output directory must be empty (stale batches are unsafe)", file=sys.stderr)
        return 1
    bcon = sqlite3.connect(Path(base).resolve().as_uri() + "?mode=ro", uri=True)
    scon = sqlite3.connect(Path(src).resolve().as_uri() + "?mode=ro", uri=True)
    scon.row_factory = sqlite3.Row
    delta = {"base": os.path.basename(base), "source": os.path.basename(src),
             "tables": {}, "deletes": {}}
    n_files = 0
    tables = push_tables(scon)
    if only_tables:
        unknown = set(only_tables) - set(tables)
        if unknown:
            print(f"FAIL unknown tables: {sorted(unknown)}", file=sys.stderr)
            return 1
        tables = [t for t in tables if t in set(only_tables)]
    for table in tables:
        if table not in [r[0] for r in bcon.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]:
            print(f"FAIL base lacks {table}; migrate then pull a fresh complete base", file=sys.stderr)
            return 1
        if {r[1] for r in bcon.execute(f"PRAGMA table_info({table})")} != {
                r[1] for r in scon.execute(f"PRAGMA table_info({table})")}:
            print(f"FAIL schema mismatch for {table}; migrate before synchronization", file=sys.stderr)
            return 1
    for table in tables:
        keys = key_columns(scon, table)
        bmap = row_map(bcon, table, keys)
        smap = row_map(scon, table, keys)
        skip_vals = (exclude_keys or {}).get(table, set())
        if skip_vals and (keys is None or len(keys) != 1):
            print(f"FAIL --exclude-keys needs a single-column PK for {table}", file=sys.stderr)
            return 1
        if table in MERGE_ONLY:
            # Insert-only: rows D1 lacks, never touch existing receipts.
            wanted = [k for k in smap if k not in bmap]
            added = len(wanted)
            updated = 0
        else:
            wanted = [k for k, h in smap.items() if bmap.get(k) != h]
            added = sum(1 for k in wanted if k not in bmap)
            updated = len(wanted) - added
        excluded = sorted(str(k[0]) for k in wanted if str(k[0]) in skip_vals)
        if excluded:
            drop = {k for k in wanted if str(k[0]) in skip_vals}
            wanted = [k for k in wanted if k not in drop]
            added = sum(1 for k in wanted if k not in bmap)
            updated = len(wanted) - added
        gone = []
        if allow_deletes and keys and table in DELETE_ALLOWED:
            gone = [k for k in bmap if k not in smap]
        delta["tables"][table] = {"added_or_changed": len(wanted), "added": added,
                                  "updated": updated, "deleted": len(gone),
                                  "excluded": excluded}
        delta["deletes"][table] = [list(k) for k in gone]
        if not wanted and not gone:
            continue
        cols = [r[1] for r in scon.execute(f"PRAGMA table_info({table})").fetchall()]
        col_list = ", ".join(cols)
        rest = os.path.join(out, "rest")
        if table in {'prediction_memberships', 'match_odds', 'match_odds_ext'}:
            rows = _rows_by_key(scon, table, cols, keys, wanted)
            n_files += emit_linked_rows(scon, table, cols, rows, out,
                                       'p0_1_' + table, rest)
            continue
        if table in MERGE_ONLY:
            head = f"INSERT OR IGNORE INTO {table} ({col_list}) VALUES "
            phase = "p1"
            getrows = _rows_by_key(scon, table, cols, keys, wanted)
            n_files += emit_statements(out, f"{phase}_{table}", head, getrows,
                                       lambda r: "(" + ", ".join(quote(v) for v in r) + ")",
                                       rest)
        else:
            head, suffix = write_policy(table, cols, scon)
            phase = "p1" if table in PHASE_GATE_TABLES else (
                f"p0_{'0' if table in PARENT_FIRST else '1'}")
            getrows = _rows_by_key(scon, table, cols, keys, wanted)
            n_files += emit_statements(out, f"{phase}_{table}", head, getrows,
                                       lambda r: "(" + ", ".join(quote(v) for v in r) + ")",
                                       rest, suffix)
    # Deletes last-writer note: children before parents, one file.
    ordered = [t for t in DELETE_ORDER if delta["deletes"].get(t)]
    if any(delta["deletes"].get(t) for t in ordered):
        bcon_info = scon
        with open(os.path.join(out, "p1_deletes.sql"), "w", encoding="utf-8") as fh:
            for table in ordered:
                keys = key_columns(bcon_info, table)
                for key in delta["deletes"][table]:
                    cond = " AND ".join(f"{c}={quote(v)}" for c, v in zip(keys, key))
                    fh.write(f"DELETE FROM {table} WHERE {cond};\n")
        n_files += 1
    with open(os.path.join(out, "delta-manifest.json"), "w", encoding="utf-8") as fh:
        rest_pending = 0
        rest_dir = os.path.join(out, "rest")
        if os.path.isdir(rest_dir):
            for name in os.listdir(rest_dir):
                if name.endswith(".jsonl"):
                    with open(os.path.join(rest_dir, name), encoding="utf-8") as rf:
                        rest_pending += sum(1 for _ in rf)
        delta["rest_pending"] = rest_pending
        json.dump(delta, fh, indent=2, sort_keys=True, default=str)
    changed = sum(v["added_or_changed"] for v in delta["tables"].values())
    removed = sum(v["deleted"] for v in delta["tables"].values())
    print(f"delta: {changed} upserted, {removed} deleted, {n_files} file(s), "
          f"{delta['rest_pending']} rest-pending")
    return 0


def _rows_by_key(con: sqlite3.Connection, table: str, cols: list, keys: list | None, wanted: list) -> list:
    """Fetch full rows for wanted keys (PK/UNIQUE lookup; full scan fallback)."""
    if not wanted:
        return []
    cur = con.execute(f"SELECT * FROM {table}")
    names = [d[0] for d in cur.description] if cur.description else []
    if keys is None:
        want_hashes = {k.split("__hash__:", 1)[1] for k in wanted}
        out = []
        for row in cur:
            rec = dict(zip(names, row))
            h = hashlib.sha256(repr(sorted(rec.items(), key=lambda kv: kv[0])).encode("utf-8")).hexdigest()
            if h in want_hashes:
                out.append(tuple(rec.get(c) for c in cols))
        return out
    want = set(wanted)
    out = []
    for row in cur:
        rec = dict(zip(names, row))
        if tuple(rec.get(k) for k in keys) in want:
            out.append(tuple(rec.get(c) for c in cols))
    return out


def cmd_rebase(src: str, out: str, specs: list) -> int:
    """Shift autoincrement id ranges so a fixture partition can land in D1
    without colliding with existing rows: --rebase
    picks:id:100000:prediction_memberships:prediction_id (repeatable).
    Copies src->out first; src is never modified. Offsets must exceed the
    target's current MAX(id); the plan step prints both for the operator to
    confirm before any write."""
    if os.path.exists(out):
        print('FAIL rebase output must not already exist', file=sys.stderr)
        return 1
    with sqlite3.connect(Path(src).resolve().as_uri() + '?mode=ro', uri=True) as source:
        with sqlite3.connect(out) as target:
            source.backup(target)  # file copying omits committed WAL records
    con = sqlite3.connect(out)
    for spec in specs:
        try:
            table, column, offset, ref_table, ref_column = spec.split(":")
            offset = int(offset)
        except ValueError:
            print(f"FAIL bad --rebase spec {spec!r} (want table:column:offset:reftable:refcolumn)",
                  file=sys.stderr)
            return 1
        existing = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        if table not in existing or ref_table not in existing:
            print(f"FAIL rebase table missing: {spec!r}", file=sys.stderr)
            return 1
        mx = con.execute(f"SELECT COALESCE(MAX({column}),0) FROM {table}").fetchone()[0]
        con.execute(f"UPDATE {ref_table} SET {ref_column}={ref_column}+{offset}")
        con.execute(f"UPDATE {table} SET {column}={column}+{offset}")
        con.commit()
        lo = con.execute(f"SELECT COALESCE(MIN({column}),0) FROM {table}").fetchone()[0]
        hi = con.execute(f"SELECT COALESCE(MAX({column}),0) FROM {table}").fetchone()[0]
        print(f"rebase {table}.{column}: max was {mx}, now {lo}..{hi}")
    return 0


def cmd_manifest(src: str, out: str, only=None) -> int:
    os.makedirs(out, exist_ok=True)
    con = sqlite3.connect(src)
    manifest = {"source": os.path.basename(src), "tables": {}}
    tables = only if only is not None else app_tables(con)
    for table in tables:
        count, digest, cols = table_fingerprint(con, table)
        manifest["tables"][table] = {"rows": count, "sha256": digest, "columns": cols}
    with open(os.path.join(out, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, sort_keys=True)
    print(f"{len(manifest['tables'])} tables fingerprinted")
    return 0


def quote(value):
    if value is None:
        return "NULL"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (bytes, bytearray)):
        return "X'" + bytes(value).hex() + "'"
    return "'" + str(value).replace("'", "''") + "'"


def push_tables(con: sqlite3.Connection, include_roster: bool = False) -> list:
    """Tables a snapshot may contribute. Worker-owned runtime state is never
    replicated; the roster joins only for the one-time staging seed."""
    skip = set(PUSH_EXCLUDE) | SCRATCH_EXCLUDE
    if not include_roster:
        skip.add(ROSTER_TABLE)
    return [t for t in app_tables(con) if t not in skip]


def _parse_exclude_keys(value: str) -> dict:
    """Comma-separated TABLE:key list -> {table: {keys}}. Split on the FIRST
    colon only: keys themselves (e.g. cache_key values) may contain colons.
    Malformed entries are a hard error (an exclusion must never be silently
    misparsed)."""
    out: dict = {}
    for spec in (value or "").split(","):
        spec = spec.strip()
        if not spec:
            continue
        if ":" not in spec:
            raise ValueError(f"--exclude-keys entries look like TABLE:key, got {spec!r}")
        table, key = spec.split(":", 1)
        if not table.strip() or not key.strip():
            raise ValueError(f"--exclude-keys entries look like TABLE:key, got {spec!r}")
        out.setdefault(table.strip(), set()).add(key.strip())
    return out


def cmd_require_complete(db_path: str, local_date: str, timezone: str = "Africa/Lagos") -> int:
    """Acceptance gate: fail unless prediction_cache holds a COMPLETE row for
    the given Lagos date. Prints the row; never writes. The epoch may advance
    for a date only after this passes (checked against the WORKER's serving
    gate: getDayCache requires status COMPLETE)."""
    con = sqlite3.connect(db_path)
    row = con.execute(
        "SELECT cache_key, local_date, timezone, status FROM prediction_cache"
        " WHERE local_date=? AND timezone=? AND status='COMPLETE' LIMIT 1",
        (local_date, timezone)).fetchone()
    if not row:
        have = con.execute(
            "SELECT local_date, timezone, status FROM prediction_cache ORDER BY local_date DESC LIMIT 5"
        ).fetchall()
        print(f"FAIL no COMPLETE cache for {local_date} [{timezone}]; newest rows: {have}",
              file=sys.stderr)
        # Self-diagnosing failure: extract the generation verdict recorded in
        # the newest PARTIAL row (provider coverage + error type), so the run
        # log alone identifies whether a required provider failed, quotas were
        # hit, or generation raised — no extra provider calls, no secrets
        # (allowlisted scalar fields only, payload never dumped).
        try:
            prow = con.execute(
                "SELECT payload_json FROM prediction_cache WHERE local_date=? AND timezone=?"
                " ORDER BY local_date DESC LIMIT 1",
                (local_date, timezone)).fetchone()
            if prow and prow[0]:
                gen = (json.loads(prow[0]).get("generation") or {})
                cov = gen.get("provider_coverage") or {}
                print(f"diagnosis date={local_date}: complete={gen.get('complete')}, "
                      f"error_type={gen.get('error_type')}, "
                      f"error_traceback={gen.get('error_traceback')}, "
                      f"required={cov.get('required')}, "
                      f"missing_required={cov.get('missing_required')}, "
                      f"optional_degraded={cov.get('optional_degraded')}", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001 - diagnosis is best-effort only
            print(f"diagnosis unavailable: {type(exc).__name__}", file=sys.stderr)
        return 1
    print(f"COMPLETE cache: {row[0]} date={row[1]} tz={row[2]}")
    return 0


def cmd_plan(base: str, src: str, out: str, only_tables=None, allow_deletes: bool = False,
             write_budget: int = 5000, exclude_keys=None) -> int:
    """Build a diff, then audit it into plan.json and enforce the write budget.

    Estimated D1 row writes = upserts + deletes + rest_pending (each REST row
    costs one write). Refuses (exit 2) when the estimate exceeds the budget,
    so a mis-scoped run can never repeat the 76K-row quota incident. A plan
    with rest_pending>0 is marked "complete": false until rest-apply succeeds.
    Explicitly excluded rows are listed under "excluded" (auditable, and they
    do not count toward the budget since nothing is emitted for them).
    """
    rc = cmd_diff(base, src, out, only_tables, allow_deletes, exclude_keys)
    if rc != 0:
        return rc
    with open(os.path.join(out, "delta-manifest.json"), encoding="utf-8") as fh:
        delta = json.load(fh)
    upserts = sum(v["added_or_changed"] for v in delta["tables"].values())
    deletes = sum(v["deleted"] for v in delta["tables"].values())
    rest = int(delta.get("rest_pending", 0))
    files = sorted(f for f in os.listdir(out) if f.endswith(".sql"))
    excluded = {t: sorted(v.get("excluded", [])) for t, v in delta["tables"].items()
                if v.get("excluded")}
    plan = {
        "tables": {t: v for t, v in delta["tables"].items()
                   if v["added_or_changed"] or v["deleted"] or v.get("excluded")},
        "inserts": sum(v.get("added", 0) for v in delta["tables"].values()),
        "updates": sum(v.get("updated", 0) for v in delta["tables"].values()),
        "deletes": deletes,
        "excluded": excluded,
        "rest_pending": rest,
        "sql_batches": len(files),
        "estimated_writes": upserts + deletes + rest,
        "write_budget": write_budget,
        "complete": rest == 0,
    }
    with open(os.path.join(out, "plan.json"), "w", encoding="utf-8") as fh:
        json.dump(plan, fh, indent=2, sort_keys=True)
    print(f"plan: +{plan['inserts']}/~{plan['updates']}/-{plan['deletes']} "
          f"rest={rest} batches={len(files)} estimated_writes={plan['estimated_writes']} "
          f"budget={write_budget} excluded={sum(len(v) for v in excluded.values())}")
    if plan["estimated_writes"] > write_budget:
        import shutil
        print(f"FAIL plan exceeds write budget ({plan['estimated_writes']} > {write_budget}); "
              f"narrow --tables or raise --write-budget explicitly", file=sys.stderr)
        # Remove emitted batches so a later step cannot apply a refused plan.
        for name in files:
            try:
                os.remove(os.path.join(out, name))
            except OSError:
                pass
        for name in ("delta-manifest.json",):
            try:
                os.remove(os.path.join(out, name))
            except OSError:
                pass
        return 2
    if not plan["complete"]:
        print("NOTE plan incomplete until rest-apply succeeds (do not bump epoch yet)")
    return 0


def cmd_push(src: str, out: str, expect_epoch=None, include_roster: bool = False,
             max_full_rows: int = 20000, full_ok: bool = False) -> int:
    os.makedirs(out, exist_ok=True)
    if os.listdir(out):
        print("FAIL output directory must be empty (stale batches are unsafe)", file=sys.stderr)
        return 1
    con = sqlite3.connect(Path(src).resolve().as_uri() + "?mode=ro", uri=True)
    if expect_epoch is not None:
        got = sync_epoch(con)
        if got != expect_epoch:
            print(f"FAIL stale snapshot: expected epoch {expect_epoch}, source is {got} "
                  f"(pull a fresh base first)", file=sys.stderr)
            return 1
    total = sum(con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in push_tables(con, include_roster))
    if total > max_full_rows and not full_ok:
        print(f"FAIL unscoped full push of {total} rows exceeds --max-full-rows {max_full_rows}; "
              f"use scoped diff/plan instead, or pass --full-ok explicitly", file=sys.stderr)
        return 2
    con.row_factory = sqlite3.Row
    # Filename order IS commit order (coalesce/apply sort lexically):
    # p0 facts first (parents before children), p1 gate+receipts last.
    for table in sorted(push_tables(con, include_roster)):
        dep = "0" if table in PARENT_FIRST else "1"
        phase = "p1" if table in PHASE_GATE_TABLES else f"p0_{dep}"
        rows = con.execute(f"SELECT * FROM {table}").fetchall()
        if not rows:
            continue
        cols = rows[0].keys()
        col_list = ", ".join(cols)
        if table in {'prediction_memberships', 'match_odds', 'match_odds_ext'}:
            emit_linked_rows(con, table, list(cols), rows, out, f'{phase}_{table}',
                             os.path.join(out, 'rest'))
            continue
        if table == "subscribers":
            # Engine snapshots never carry fresher roster data than D1 (all
            # /start activity lands in D1 first); replicate as field merges.
            stmts = 0
            for i, r in enumerate(rows):
                vals = {"chat_id": quote(r["chat_id"]),
                        "username": quote(r["username"]),
                        "first_name": quote(r["first_name"]),
                        "timezone": quote(r["timezone"]),
                        "subscribed_at": quote(r["subscribed_at"])}
                text = (
                    "INSERT INTO subscribers(chat_id,username,first_name,timezone,subscribed_at,active) "
                    f"VALUES({vals['chat_id']},{vals['username']},{vals['first_name']},"
                    f"{vals['timezone']},{vals['subscribed_at']},1) "
                    "ON CONFLICT(chat_id) DO UPDATE SET active=1, "
                    "username=CASE WHEN excluded.username!='' THEN excluded.username "
                    "ELSE subscribers.username END, "
                    "first_name=CASE WHEN excluded.first_name!='' THEN excluded.first_name "
                    "ELSE subscribers.first_name END, "
                    "subscribed_at=CASE WHEN subscribers.active=0 THEN excluded.subscribed_at "
                    "ELSE subscribers.subscribed_at END, "
                    "timezone=CASE WHEN excluded.timezone!='' THEN excluded.timezone "
                    "ELSE subscribers.timezone END;\n")
                if len(text) > 100000:
                    print(f"WARN subscribers row {i} exceeds D1's statement cap; skipped",
                          file=sys.stderr)
                    continue
                with open(os.path.join(out, f"p1_{table}_{i:04d}.sql"), "w", encoding="utf-8") as fh:
                    fh.write(text)
                stmts += 1
            continue
        head, suffix = write_policy(table, list(cols), con)
        emit_statements(out, f"{phase}_{table}", head, rows,
                        lambda r: "(" + ", ".join(quote(r[c]) for c in cols) + ")",
                        os.path.join(out, "rest"), suffix)
    rc = cmd_manifest(src, out, push_tables(sqlite3.connect(src), include_roster))
    if rc == 0 and expect_epoch is not None:
        with open(os.path.join(out, "manifest.json"), "r+", encoding="utf-8") as fh:
            manifest = json.load(fh)
            manifest["base_epoch"] = expect_epoch
            fh.seek(0)
            json.dump(manifest, fh, indent=2, sort_keys=True)
            fh.truncate()
    return rc


def cmd_apply(target: str, batch_dir: str, expect_epoch=None) -> int:
    # Epoch CAS DURING the write, not just before it: a concurrent push that
    # lands mid-apply is detected here (fail closed; applied.json preserves
    # resume position, verify then proves completeness before any cutover).
    # Prevention remains the lease's job; this is the tripwire.
    con = sqlite3.connect(target)
    con.execute('PRAGMA foreign_keys=ON')
    if expect_epoch is not None and sync_epoch(con) != expect_epoch:
        print(f"FAIL target epoch moved before apply (expected {expect_epoch})", file=sys.stderr)
        return 1
    state_path = os.path.join(batch_dir, "applied.json")
    try:
        with open(state_path, encoding="utf-8") as fh:
            applied = set(json.load(fh))
    except (OSError, ValueError):
        applied = set()
    files = sorted(f for f in os.listdir(batch_dir) if f.endswith(".sql"))
    for name in files:
        with open(os.path.join(batch_dir, name), encoding='utf-8') as fh:
            if unsafe_history_sql(fh.read()):
                print(f"FAIL unsafe historical SQL in {name}; nothing applied", file=sys.stderr)
                return 1
    done = 0
    for name in files:
        if name in applied:
            continue
        with open(os.path.join(batch_dir, name), encoding="utf-8") as fh:
            try:
                con.executescript('BEGIN IMMEDIATE;\n' + fh.read() + '\nCOMMIT;')
            except sqlite3.Error:
                con.rollback()
                raise
        con.commit()
        applied.add(name)
        done += 1
        # Each committed batch is replay-safe; checkpoint after EACH commit so
        # interruption does not lose the position of earlier successful files.
        with open(state_path + ".tmp", "w", encoding="utf-8") as fh:
            json.dump(sorted(applied), fh, indent=2)
        os.replace(state_path + ".tmp", state_path)
    with open(state_path, "w", encoding="utf-8") as fh:
        json.dump(sorted(applied), fh, indent=2)
    print(f"applied {done} new batch(es), {len(applied)} total")
    if expect_epoch is not None and sync_epoch(con) != expect_epoch:
        print(f"FAIL target epoch moved DURING apply (expected {expect_epoch}): "
              f"do not cut over; re-pull, re-push, re-verify", file=sys.stderr)
        return 1
    return 0


def sync_epoch(con: sqlite3.Connection) -> int:
    try:
        row = con.execute("SELECT epoch FROM sync_state WHERE id=1").fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(row[0]) if row else 0


def cmd_rest_apply(batches: str, account_id: str, database_id: str, api_token: str,
                   batch_size: int = 25, retries: int = 3, endpoint: str = "") -> int:
    """Apply rest/*.jsonl oversize rows via the D1 REST query API (params do
    not count toward the 100KB SQL-text cap; row cap is 2MB).

    Safety: every line is validated FIRST (shape {sql, params}, allowlisted
    INSERT verbs only); any malformed line aborts before anything is sent.
    Sends one {sql, params} object per HTTP call (the /query endpoint
    rejects a batch array) with retries, and records progress in
    applied-rest.json every batch_size entries so a retry resumes without
    rewriting rows (statements are idempotent INSERT OR REPLACE / IGNORE).
    Any entry that doesn't succeed fails the run: rest_pending rows are
    never silently dropped, and callers must not bump the epoch until this
    exits 0. Response bodies are summarized (counts only); params are never
    logged.
    """
    import time
    import urllib.error
    import urllib.request
    entries = []
    for name in sorted(os.listdir(batches)):
        if not name.endswith(".jsonl"):
            continue
        with open(os.path.join(batches, name), encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    print(f"FAIL malformed JSON {name}:{lineno}; nothing sent", file=sys.stderr)
                    return 1
                if not isinstance(entry, dict) or not isinstance(entry.get("sql"), str) \
                        or not isinstance(entry.get("params"), list):
                    print(f"FAIL bad entry shape {name}:{lineno}; nothing sent", file=sys.stderr)
                    return 1
                verb = entry["sql"].lstrip().split(None, 1)[0].upper() if entry["sql"].strip() else ""
                if not entry["sql"].strip().upper().startswith("INSERT OR "):
                    print(f"FAIL non-INSERT statement {name}:{lineno} ({verb}); nothing sent",
                          file=sys.stderr)
                    return 1
                if unsafe_history_sql(entry['sql']):
                    print(f"FAIL unsafe historical SQL {name}:{lineno}; nothing sent", file=sys.stderr)
                    return 1
                entries.append((name, entry))
    if not entries:
        print("rest-apply: nothing pending")
        return 0
    state_path = os.path.join(batches, "applied-rest.json")
    try:
        with open(state_path, encoding="utf-8") as fh:
            applied = set(json.load(fh))
    except (OSError, ValueError):
        applied = set()
    url = endpoint or (f"https://api.cloudflare.com/client/v4/accounts/{account_id}"
                         f"/d1/database/{database_id}/query")
    pending = [(i, n, e) for i, (n, e) in enumerate(entries) if f"{n}#{i}" not in applied]
    print(f"rest-apply: {len(entries)} total, {len(pending)} pending")
    # D1's /query endpoint takes exactly one {sql, params} object per request
    # (error 7400 "Expected object, received array" if sent a batch array),
    # so each entry is its own HTTP call; batch_size only paces how often
    # applied-rest.json is flushed, not what's sent on the wire.
    for idx, (i, n, e) in enumerate(pending):
        payload = json.dumps({"sql": e["sql"], "params": e["params"]}).encode()
        ok = False
        for attempt in range(retries + 1):
            req = urllib.request.Request(url, data=payload,
                                         headers={"Content-Type": "application/json",
                                                  "Authorization": f"Bearer {api_token}"},
                                         method="POST")
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    body = json.load(resp)
            except urllib.error.HTTPError as exc:
                # The response body is Cloudflare's own error JSON (codes/
                # messages only, no secrets) and is the only way to tell a
                # bad token/account id (4xx, won't clear on retry) from a
                # transient rate limit or outage (429/5xx, retried below).
                detail = exc.read().decode("utf-8", "replace")[:500]
                last = f"HTTPError {exc.code}: {detail}"
                time.sleep(min(2 ** attempt, 8))
                continue
            except Exception as exc:  # noqa: BLE001 - transient or quota; retried below
                last = f"transport: {type(exc).__name__}: {exc}"
                time.sleep(min(2 ** attempt, 8))
                continue
            results = body if isinstance(body, list) else body.get("result", [])
            failures = [r for r in results
                        if not (isinstance(r, dict) and r.get("success", True))]
            if isinstance(results, list) and results and not failures:
                ok = True
                break
            last = f"{len(failures)}/{max(len(results), 1)} failed"
            if any("row write limit" in json.dumps(r) for r in failures):
                print("FAIL D1 write quota exhausted during rest-apply; progress saved, "
                      "retry after reset", file=sys.stderr)
                break
            time.sleep(min(2 ** attempt, 8))
        if not ok:
            print(f"FAIL rest entry {n}#{i} ({last}); applied so far kept in "
                  f"applied-rest.json; epoch must NOT advance", file=sys.stderr)
            return 1
        applied.add(f"{n}#{i}")
        if (idx + 1) % batch_size == 0 or idx == len(pending) - 1:
            with open(state_path, "w", encoding="utf-8") as fh:
                json.dump(sorted(applied), fh, indent=2)
            print(f"rest-apply: {len(applied)}/{len(entries)} applied")
    print(f"rest-apply complete: {len(entries)} rows via {len(entries)} params calls")
    return 0


def cmd_epoch_get(target: str) -> int:
    print(sync_epoch(sqlite3.connect(target)))
    return 0


def cmd_epoch_bump(target: str, expect=None) -> int:
    con = sqlite3.connect(target)
    got = sync_epoch(con)
    if expect is not None and got != expect:
        print(f"FAIL stale epoch: expected {expect}, target is {got}", file=sys.stderr)
        return 1
    con.execute("UPDATE sync_state SET epoch=epoch+1 WHERE id=1")
    if con.total_changes == 0:
        con.execute("INSERT INTO sync_state(id, epoch) VALUES(1, 1)")
    con.commit()
    print(sync_epoch(con))
    return 0


def cmd_coalesce(batch_dir: str, out: str, max_bytes: int = 90000) -> int:
    """Merge per-table batch files into import chunks of at most max_bytes.

    D1 limits per STATEMENT are 100KB / 100 parameters; our statements are
    byte-capped at push time, so chunking only reduces file count for
    `d1 execute --file` / import polling. Chunk boundaries never split a
    statement, and output names sort in the same order as inputs (p0_ facts
    before p1_ gate/receipts), so apply+resume and commit-phase semantics
    are unchanged. A manifest.json is copied alongside for verify.
    """
    os.makedirs(out, exist_ok=True)
    if os.listdir(out):
        print('FAIL coalesce output must be empty', file=sys.stderr)
        return 1
    files = sorted(f for f in os.listdir(batch_dir) if f.endswith(".sql"))
    chunk, size, index = [], 0, 0
    chunks = 0

    def flush():
        nonlocal chunk, size, index, chunks
        if not chunk:
            return
        with open(os.path.join(out, f"chunk_{index:04d}.sql"), "w", encoding="utf-8") as fh:
            fh.writelines(chunk)
        chunks += 1
        index += 1
        chunk, size = [], 0

    for name in files:
        with open(os.path.join(batch_dir, name), encoding="utf-8") as fh:
            text = fh.read()
        if unsafe_history_sql(text):
            print(f'FAIL unsafe historical SQL in {name}', file=sys.stderr)
            return 1
        statement = ''
        for line in text.splitlines(keepends=True):
            statement += line
            if not sqlite3.complete_statement(statement):
                continue
            byte_count = len(statement.encode('utf-8'))
            if byte_count > max_bytes:
                print(f'FAIL statement exceeds coalesce byte limit in {name}', file=sys.stderr)
                return 1
            if size + byte_count > max_bytes and chunk:
                flush()
            chunk.append(statement)
            size += byte_count
            statement = ''
        if statement.strip():
            print(f'FAIL incomplete SQL in {name}', file=sys.stderr)
            return 1
    flush()
    for extra in ("manifest.json",):
        src = os.path.join(batch_dir, extra)
        if os.path.exists(src):
            with open(src, encoding="utf-8") as fh_in, open(
                    os.path.join(out, extra), "w", encoding="utf-8") as fh_out:
                fh_out.write(fh_in.read())
    total = sum(os.path.getsize(os.path.join(out, f)) for f in os.listdir(out) if f.endswith(".sql"))
    print(f"{len(files)} batches -> {chunks} chunks, {total} bytes total")
    return 0


def cmd_snapshot(src: str, out: str) -> int:
    """Timestamped production-backup drill: file copy + manifest + verify of
    the copy. The live DB is only ever read (never written) by this command."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    os.makedirs(out, exist_ok=True)
    dest = os.path.join(out, f"predbot-backup-{stamp}.db")
    if os.path.exists(dest):
        print('FAIL snapshot output already exists', file=sys.stderr)
        return 1
    with sqlite3.connect(Path(src).resolve().as_uri() + '?mode=ro', uri=True) as source:
        with sqlite3.connect(dest) as target:
            source.backup(target)
    manifest_dir = os.path.join(out, f"predbot-backup-{stamp}.manifest")
    rc = cmd_manifest(dest, manifest_dir)
    if rc != 0:
        return rc
    print(f"backup {dest}")
    return 0


def cmd_verify(target: str, manifest_path: str) -> int:
    with open(manifest_path, encoding="utf-8") as fh:
        manifest = json.load(fh)
    if "base_epoch" in manifest:
        epoch = sync_epoch(sqlite3.connect(target))
        if epoch != manifest["base_epoch"]:
            print(f"WARN target epoch {epoch} != push base epoch {manifest['base_epoch']} "
                  f"(concurrent push landed; re-verify against the newer manifest)")
    con = sqlite3.connect(target)
    ok = True
    for table, want in sorted(manifest["tables"].items()):
        try:
            count, digest, _ = table_fingerprint(con, table)
        except sqlite3.OperationalError as exc:
            print(f"FAIL {table}: missing ({exc})")
            ok = False
            continue
        if count != want["rows"] or digest != want["sha256"]:
            print(f"FAIL {table}: source={want['rows']}/{want['sha256'][:12]} "
                  f"target={count}/{digest[:12]}")
            ok = False
        else:
            print(f"OK   {table}: {count} rows")
    return 0 if ok else 1


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def cmd_lease(target: str, job: str, owner: str, ttl_min: int) -> int:
    # Atomic single-statement acquire: the lease is granted only when free,
    # expired, or already ours. No SELECT-then-UPDATE gap, so two concurrent
    # acquirers cannot both succeed — on D1 this is one statement, which is
    # the atomicity unit. Same SQL is used verbatim against D1 via wrangler.
    con = sqlite3.connect(target)
    if con.execute("SELECT 1 FROM job_state WHERE name=?", (job,)).fetchone() is None:
        print(f"FAIL unknown job {job!r}", file=sys.stderr)
        return 1
    until_new = (datetime.now(timezone.utc) + timedelta(minutes=ttl_min)).strftime("%Y-%m-%dT%H:%M:%SZ")
    cur = con.execute(
        "UPDATE job_state SET lease_owner=?, lease_until=?, attempts=attempts+1,"
        " updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE name=?"
        " AND (lease_owner IS NULL OR lease_until IS NULL OR lease_until < strftime('%Y-%m-%dT%H:%M:%SZ','now')"
        " OR lease_owner=?)",
        (owner, until_new, job, owner))
    con.commit()
    if cur.rowcount == 0:
        holder = con.execute("SELECT lease_owner, lease_until FROM job_state WHERE name=?",
                             (job,)).fetchone()
        print(f"FAIL lease held by {holder[0]} until {holder[1]}", file=sys.stderr)
        return 1
    print(f"lease {job} -> {owner} until {until_new}")
    return 0


def cmd_release(target: str, job: str, owner: str) -> int:
    con = sqlite3.connect(target)
    cur = con.execute("UPDATE job_state SET lease_owner=NULL, lease_until=NULL WHERE name=? AND lease_owner=?",
                      (job, owner))
    con.commit()
    if cur.rowcount == 0:
        print(f"FAIL lease not held by {owner}", file=sys.stderr)
        return 1
    print(f"released {job}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("manifest"); p.add_argument("src"); p.add_argument("--out", required=True)
    p.add_argument("--tables", default="",
                   help="Comma-separated subset (default: all app tables).")
    p = sub.add_parser("push"); p.add_argument("src"); p.add_argument("--out", required=True)
    p.add_argument("--expect-epoch", type=int, default=None)
    p.add_argument("--include-roster", action="store_true",
                   help="One-time staging seed only: include the subscriber roster.")
    p.add_argument("--max-full-rows", type=int, default=20000)
    p.add_argument("--full-ok", action="store_true",
                   help="Acknowledge an unscoped full-table push explicitly.")
    p = sub.add_parser("plan"); p.add_argument("base"); p.add_argument("src")
    p.add_argument("--out", required=True)
    p.add_argument("--tables", default="")
    p.add_argument("--no-deletes", action="store_true")
    p.add_argument("--write-budget", type=int, default=5000)
    p.add_argument("--exclude-keys", default="",
                   help="Comma-separated TABLE:key skips, recorded in the manifest.")
    p = sub.add_parser("rest-apply"); p.add_argument("batches")
    p.add_argument("--account-id", required=True)
    p.add_argument("--database-id", required=True)
    p.add_argument("--api-token", required=True)
    p.add_argument("--batch-size", type=int, default=25)
    p.add_argument("--retries", type=int, default=3)
    p.add_argument("--endpoint", default="",
                   help="Override D1 query endpoint (tests only; default is the real API).")
    p = sub.add_parser("diff"); p.add_argument("base"); p.add_argument("src")
    p.add_argument("--out", required=True)
    p.add_argument("--tables", default="",
                   help="Comma-separated subset to diff (default: all pushable).")
    p.add_argument("--no-deletes", action="store_true",
                   help="Additive-only run: never emit DELETEs.")
    p.add_argument("--exclude-keys", default="",
                   help="Comma-separated TABLE:key skips, recorded in the manifest.")
    p = sub.add_parser("require-complete"); p.add_argument("db"); p.add_argument("date")
    p.add_argument("--timezone", default="Africa/Lagos")
    p = sub.add_parser("coalesce"); p.add_argument("batches"); p.add_argument("--out", required=True)
    p.add_argument("--max-bytes", type=int, default=90000)
    p = sub.add_parser("snapshot"); p.add_argument("src"); p.add_argument("--out", required=True)
    p = sub.add_parser("rebase"); p.add_argument("src"); p.add_argument("--out", required=True)
    p.add_argument("--rebase", action="append", default=[],
                   help="table:column:offset:reftable:refcolumn (repeatable)")
    p = sub.add_parser("apply"); p.add_argument("target"); p.add_argument("batches")
    p.add_argument("--expect-epoch", type=int, default=None,
                   help="Abort when the target epoch differs before AND after the write.")
    p = sub.add_parser("verify"); p.add_argument("target"); p.add_argument("--manifest", required=True)
    p = sub.add_parser("lease"); p.add_argument("target"); p.add_argument("job"); p.add_argument("owner")
    p.add_argument("--ttl-min", type=int, default=30)
    p = sub.add_parser("release"); p.add_argument("target"); p.add_argument("job"); p.add_argument("owner")
    p = sub.add_parser("epoch-get"); p.add_argument("target")
    p = sub.add_parser("epoch-bump"); p.add_argument("target")
    p.add_argument("--expect", type=int, default=None)
    args = ap.parse_args(argv)
    if args.cmd == "manifest":
        only = [t.strip() for t in args.tables.split(",") if t.strip()] or None
        return cmd_manifest(args.src, args.out, only)
    if args.cmd == "push":
        return cmd_push(args.src, args.out, args.expect_epoch, args.include_roster,
                        args.max_full_rows, args.full_ok)
    if args.cmd == "plan":
        only = [t.strip() for t in args.tables.split(",") if t.strip()] or None
        try:
            excluded = _parse_exclude_keys(args.exclude_keys)
        except ValueError as exc:
            print(f"FAIL {exc}", file=sys.stderr)
            return 2
        return cmd_plan(args.base, args.src, args.out, only, False,
                        args.write_budget, excluded)
    if args.cmd == "rest-apply":
        return cmd_rest_apply(args.batches, args.account_id, args.database_id,
                              args.api_token, args.batch_size, args.retries,
                              args.endpoint)
    if args.cmd == "diff":
        only = [t.strip() for t in args.tables.split(",") if t.strip()] or None
        try:
            excluded = _parse_exclude_keys(args.exclude_keys)
        except ValueError as exc:
            print(f"FAIL {exc}", file=sys.stderr)
            return 2
        return cmd_diff(args.base, args.src, args.out, only, False, excluded)
    if args.cmd == "require-complete":
        return cmd_require_complete(args.db, args.date, args.timezone)
    if args.cmd == "coalesce":
        return cmd_coalesce(args.batches, args.out, args.max_bytes)
    if args.cmd == "snapshot":
        return cmd_snapshot(args.src, args.out)
    if args.cmd == "rebase":
        return cmd_rebase(args.src, args.out, args.rebase)
    if args.cmd == "apply":
        return cmd_apply(args.target, args.batches, args.expect_epoch)
    if args.cmd == "verify":
        return cmd_verify(args.target, args.manifest)
    if args.cmd == "lease":
        return cmd_lease(args.target, args.job, args.owner, args.ttl_min)
    if args.cmd == "release":
        return cmd_release(args.target, args.job, args.owner)
    if args.cmd == "epoch-get":
        return cmd_epoch_get(args.target)
    if args.cmd == "epoch-bump":
        return cmd_epoch_bump(args.target, args.expect)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
