"""Builds the plan.json manifest the SSH relay (workers/bot/ssh-relay/) expects,
from a batch directory of .sql files already produced by d1_sync.py's existing
`plan`/`push` commands, plus (optionally) a rest/ directory of *.jsonl entries -
the SAME oversized-row output cmd_rest_apply applies to D1 over its REST API,
applied here to SQLite instead via parameterized statements (SQLite has no
100KB SQL-text cap, but the plan is generated with that split baked in before
the target is known, so both shapes must be accepted). Does not change
d1_sync.py itself - this is purely an adapter between its existing output
shapes and the relay's input shape.

The plan id is the sha256 of every file's own checksum, plus every jsonl
entry's own content hash, concatenated in a fixed order - deterministic from
the batch's actual content, so re-running this against the SAME batch (e.g. a
retried workflow step after a failed delivery) produces the SAME plan id,
which is exactly what makes the relay's apply-plan.ts idempotency (one
transaction, no-op on a repeat) work across a retry.

Usage: python3 build_plan_manifest.py <batch_dir> [--rest-dir DIR] [--expect-epoch N]
Writes <batch_dir>/plan.json. --expect-epoch is the sync_state.epoch the
source snapshot was pulled at (d1_sync.py epoch-get on the scratch db before
building the plan) - carried through so the server's apply bundles the
staleness check and the epoch bump into the same transaction as the data
(see docs/STAGE1_SERVER_MIGRATION_PLAN.md's epoch explanation). Omit it for
a manifest with no epoch guard (used by local tests).
"""
import argparse
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from d1_sync import unsafe_history_sql  # noqa: E402 - see sys.path insert above


def sha256_of_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def load_rest_statements(rest_dir: str) -> list[dict]:
    """Reads every *.jsonl file in rest_dir, validating each entry exactly like
    d1_sync.py's own cmd_rest_apply does (shape, INSERT-OR-verb allowlist,
    unsafe_history_sql) - these entries were already validated once when
    cmd_rest_apply's own code path runs, but build_plan_manifest.py is a
    separate, newer adapter and does not assume that validation already ran."""
    statements = []
    if not rest_dir or not os.path.isdir(rest_dir):
        return statements
    for name in sorted(os.listdir(rest_dir)):
        if not name.endswith(".jsonl"):
            continue
        with open(os.path.join(rest_dir, name), encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                entry = json.loads(line)
                if not isinstance(entry, dict) or not isinstance(entry.get("sql"), str) \
                        or not isinstance(entry.get("params"), list):
                    raise ValueError(f"bad rest entry shape {name}:{lineno}")
                if not entry["sql"].strip().upper().startswith("INSERT OR "):
                    raise ValueError(f"non-INSERT rest entry {name}:{lineno}")
                if unsafe_history_sql(entry["sql"]):
                    raise ValueError(f"unsafe historical SQL in rest entry {name}:{lineno}")
                statements.append({"sql": entry["sql"], "params": entry["params"]})
    return statements


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="build_plan_manifest.py")
    parser.add_argument("batch_dir")
    parser.add_argument("--rest-dir", default=None)
    parser.add_argument("--expect-epoch", type=int, default=None)
    try:
        args = parser.parse_args(argv[1:])
    except SystemExit:
        return 1

    files = sorted(f for f in os.listdir(args.batch_dir) if f.endswith(".sql"))
    try:
        statements = load_rest_statements(args.rest_dir) if args.rest_dir else []
    except (ValueError, OSError) as exc:
        print(f"rest entry validation failed: {exc}", file=sys.stderr)
        return 1

    if not files and not statements and args.expect_epoch is None:
        print(f"no .sql files found in {args.batch_dir} and no rest entries", file=sys.stderr)
        return 1

    checksums = {f: sha256_of_file(os.path.join(args.batch_dir, f)) for f in files}
    statement_hashes = [
        hashlib.sha256(json.dumps(s, sort_keys=True).encode("utf-8")).hexdigest() for s in statements
    ]
    plan_id_material = "".join(checksums[f] for f in files) + "".join(statement_hashes)
    if not files and not statements:
        # Nothing changed this run (a legitimate outcome - e.g. settlement
        # found nothing new to grade), but the production D1 apply path
        # bumps sync_state.epoch unconditionally regardless of content, so
        # this still has to be delivered as a real, epoch-bumping plan, not
        # skipped. Fold expect_epoch into the plan id so a DIFFERENT empty
        # run (a later day, a different epoch) never collides with an
        # earlier empty run's plan id and gets silently treated as
        # already-applied - which would wrongly skip ITS epoch bump, even
        # though a genuine retry of this SAME attempt (identical
        # expect_epoch) still correctly dedupes.
        plan_id_material += f"epoch-only:{args.expect_epoch}"
    plan_id = hashlib.sha256(plan_id_material.encode("utf-8")).hexdigest()

    manifest = {"plan_id": plan_id, "files": files, "checksums": checksums}
    if statements:
        manifest["statements"] = statements
    if args.expect_epoch is not None:
        manifest["expect_epoch"] = args.expect_epoch

    with open(os.path.join(args.batch_dir, "plan.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
    print(
        f"wrote plan.json: plan_id={plan_id}, {len(files)} file(s), "
        f"{len(statements)} rest statement(s), expect_epoch={args.expect_epoch}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
