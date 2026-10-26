"""Builds the plan.json manifest the SSH relay (workers/bot/ssh-relay/) expects,
from a batch directory of .sql files already produced by d1_sync.py's existing
`plan`/`push` commands. Does not change d1_sync.py itself - this is purely an
adapter between its existing output shape and the relay's input shape.

The plan id is the sha256 of every file's own checksum concatenated in sorted
order - deterministic from the batch's actual content, so re-running this
against the SAME batch (e.g. a retried workflow step after a failed delivery)
produces the SAME plan id, which is exactly what makes the relay's
apply-plan.ts idempotency (one transaction, no-op on a repeat) work across a
retry.

Usage: python3 build_plan_manifest.py <batch_dir>
Writes <batch_dir>/plan.json.
"""
import hashlib
import json
import os
import sys


def sha256_of_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: build_plan_manifest.py <batch_dir>", file=sys.stderr)
        return 1
    batch_dir = argv[1]
    files = sorted(f for f in os.listdir(batch_dir) if f.endswith(".sql"))
    if not files:
        print(f"no .sql files found in {batch_dir}", file=sys.stderr)
        return 1

    checksums = {f: sha256_of_file(os.path.join(batch_dir, f)) for f in files}
    plan_id = hashlib.sha256("".join(checksums[f] for f in files).encode("utf-8")).hexdigest()

    manifest = {"plan_id": plan_id, "files": files, "checksums": checksums}
    with open(os.path.join(batch_dir, "plan.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
    print(f"wrote plan.json: plan_id={plan_id}, {len(files)} file(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
