"""Workflow shell-script hygiene: every bash heredoc closer must sit at its
run-block base indent so the generated script has the delimiter at column 0
(bash rejects indented closers with `<<'EOF'`, which failed stage5-acceptance
with "here-document delimited by end-of-file").

Pure-stdlib indentation scan (no PyYAML: CI installs only requests+pytest),
mirroring how YAML block scalars strip the block base indent.
"""
from __future__ import annotations

from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"


def _heredoc_errors(text: str):
    """Return a list of '<file>:<line> ...' problems (empty when clean)."""
    errors = []
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        if lines[i].strip().endswith("run: |"):
            base = None
            pending = []  # (opener line number, opener indent)
            i += 1
            while i < len(lines):
                stripped = lines[i].strip()
                if stripped == "" or stripped.startswith("#"):
                    i += 1
                    continue
                indent = len(lines[i]) - len(lines[i].lstrip())
                if base is None:
                    base = indent
                if indent < base and stripped:
                    break  # next YAML key: block ended
                if "<<'EOF'" in lines[i] or "<<EOF" in lines[i].split("#")[0]:
                    pending.append((i + 1, indent))
                if stripped == "EOF":
                    if not pending:
                        errors.append(f"line {i + 1}: closer without opener")
                    else:
                        opener = pending.pop()
                        if indent != base:
                            errors.append(
                                f"line {i + 1}: closer indent {indent} != block base {base} "
                                f"(opener line {opener[0]})")
                i += 1
            for opener in pending:
                errors.append(f"line {opener[0]}: opener without closer")
            continue
        i += 1
    return errors


def test_all_workflow_heredocs_close_at_block_base():
    files = sorted(WORKFLOWS.glob("*.yml"))
    assert files, "no workflows found"
    problems = []
    for path in files:
        for err in _heredoc_errors(path.read_text(encoding="utf-8")):
            problems.append(f"{path.name}:{err}")
    assert not problems, "broken heredoc delimiters:\n" + "\n".join(problems)


def _heredoc_bodies(text: str):
    """Yield (line number, opener line, body) for python heredocs."""
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        if lines[i].strip().endswith("run: |"):
            base = None
            i += 1
            body, opener_no, in_doc = [], None, False
            while i < len(lines):
                stripped = lines[i].strip()
                if stripped == "" or stripped.startswith("#"):
                    i += 1
                    continue
                indent = len(lines[i]) - len(lines[i].lstrip())
                if base is None:
                    base = indent
                if indent < base and stripped:
                    break
                if "<<'EOF'" in lines[i] or "<<EOF" in lines[i].split("#")[0]:
                    opener_no, in_doc = i + 1, True
                    i += 1
                    continue
                if stripped == "EOF" and in_doc:
                    yield opener_no, lines[opener_no - 1], "\n".join(body)
                    body, in_doc = [], False
                    i += 1
                    continue
                if in_doc:
                    body.append(lines[i][base:])
                i += 1
            continue
        i += 1


def test_all_workflow_python_heredocs_compile():
    problems = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        for opener_no, opener, body in _heredoc_bodies(path.read_text(encoding="utf-8")):
            if "python3" not in opener:
                continue
            try:
                compile(body, f"{path.name}:{opener_no}", "exec")
            except SyntaxError as exc:
                problems.append(f"{path.name}:{opener_no}: {exc}")
    assert not problems, "uncompilable embedded Python:\n" + "\n".join(problems)


def test_heredoc_checker_catches_misaligned_closer():
    bad = "jobs:\n  j:\n    steps:\n      - run: |\n        set -e\n        if true; then\n          cat <<'EOF'\n          hi\n          EOF\n        fi\n"
    errs = _heredoc_errors(bad)
    assert len(errs) == 1 and "closer indent 10 != block base 8" in errs[0]
    good = bad.replace("\n          EOF\n", "\n        EOF\n")
    assert _heredoc_errors(good) == []
