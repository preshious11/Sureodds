"""Robust reader for `wrangler d1 execute --json` output.

Wrangler prints human-readable progress lines (uploads, spinners) around the
JSON payload depending on version and subcommand (`--file` in particular),
so a bare ``json.load(sys.stdin)`` fails with ``JSONDecodeError: line 1
column 1`` while D1 may have applied everything just fine. That ambiguity
once failed a staging run AFTER its writes had committed.

This helper extracts the first JSON value (first line starting with ``[``
or ``{``), fails loudly with the captured tail when there is none, and can
assert success / drill into fields, so no step ever reports success on an
unparsed result.
"""
from __future__ import annotations

import argparse
import json
import sys


def extract(text: str):
    """Return (payload, preamble_lines). Raises ValueError with tail if none."""
    lines = text.splitlines()
    start = next((i for i, ln in enumerate(lines)
                  if ln.lstrip().startswith(("[", "{"))), None)
    if start is None:
        tail = "\n".join(lines[-25:])
        raise ValueError(f"no JSON in wrangler output; tail:\n{tail}")
    try:
        return json.loads("\n".join(lines[start:])), lines[:start]
    except ValueError:
        tail = "\n".join(lines[-25:])
        raise ValueError(f"unparseable wrangler JSON; tail:\n{tail}")


def _get(payload, path: str):
    node = payload
    for part in path.split("."):
        if isinstance(node, list):
            part = int(part)
        elif not isinstance(node, dict) or part not in node:
            raise KeyError(path)
        node = node[part]
    return node


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("file", nargs="?", default="-")
    ap.add_argument("--assert-success", action="store_true")
    ap.add_argument("--get", default="")
    ap.add_argument("--expect-changes", type=int, default=None)
    args = ap.parse_args(argv)
    text = sys.stdin.read() if args.file == "-" else open(args.file, encoding="utf-8").read()
    try:
        payload, _ = extract(text)
    except ValueError as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 2
    items = payload if isinstance(payload, list) else [payload]
    if args.assert_success:
        bad = [it for it in items
               if not (isinstance(it, dict) and it.get("success"))]
        if bad:
            print(f"FAIL D1 reported failure: {json.dumps(bad)[:1000]}", file=sys.stderr)
            return 1
    if args.expect_changes is not None:
        changes = [it.get("meta", {}).get("changes") for it in items
                   if isinstance(it, dict)]
        if changes != [args.expect_changes] * len(changes) or not changes:
            print(f"FAIL expected {args.expect_changes} changed row(s), got {changes}",
                  file=sys.stderr)
            return 1
    if args.get:
        try:
            value = _get(payload, args.get)
        except (KeyError, IndexError, ValueError, TypeError):
            print(f"FAIL path {args.get!r} not in payload", file=sys.stderr)
            return 1
        print(json.dumps(value) if not isinstance(value, str) else value)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
