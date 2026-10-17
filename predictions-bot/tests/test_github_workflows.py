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


def _run_blocks(text: str):
    """Yield (step name or '', block text) for each run: block."""
    blocks, current, name = [], None, ""
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("- name:"):
            if current is not None:
                blocks.append((name, "\n".join(current)))
            name, current = stripped[len("- name:"):].strip(), None
        elif stripped.endswith("run: |") and current is None:
            current = []
        elif current is not None:
            indent = len(line) - len(line.lstrip())
            if stripped and indent < 10 and not stripped.startswith("#"):
                blocks.append((name, "\n".join(current)))
                current = None
                if stripped.startswith("- name:"):
                    name = stripped[len("- name:"):].strip()
            else:
                current.append(line)
    if current is not None:
        blocks.append((name, "\n".join(current)))
    return blocks


def test_stage5_single_mode_decision():
    """One resolver owns the offline/live decision; every DB-selecting step
    branches on its output, never re-derives mode from raw inputs (a past bug
    generated the fixture DB but then expected the live DB file)."""
    text = (WORKFLOWS / "stage5-acceptance.yml").read_text(encoding="utf-8")
    assert "id: mode" in text
    resolver = [b for n, b in _run_blocks(text) if "mode=" in b]
    assert resolver, "no mode resolver step found"
    body = resolver[0]
    for token in ("inputs.operation", "inputs.dry_run", "inputs.live_confirm",
                  "mode=live", "mode=offline"):
        assert token in body, f"resolver must test all three inputs, missing {token}"
    for name, block in _run_blocks(text):
        selects_db = ("acc_live.db" in block or "acc_scratch.db" in block
                      or "no-deletes" in block or "--rebase" in block)
        if not selects_db:
            continue
        assert "steps.mode.outputs.mode" in block, \
            f"step {name!r} selects DB files without consulting the resolved mode"
        assert "inputs.operation" not in block and "inputs.dry_run" not in block, \
            f"step {name!r} re-derives mode from raw inputs instead of steps.mode"


def test_heredoc_checker_catches_misaligned_closer():
    bad = "jobs:\n  j:\n    steps:\n      - run: |\n        set -e\n        if true; then\n          cat <<'EOF'\n          hi\n          EOF\n        fi\n"
    errs = _heredoc_errors(bad)
    assert len(errs) == 1 and "closer indent 10 != block base 8" in errs[0]
    good = bad.replace("\n          EOF\n", "\n        EOF\n")
    assert _heredoc_errors(good) == []


def test_stage5_live_pushes_need_explicit_opt_in():
    """The live engine branch must silence Telegram pushes unless
    allow_telegram_posts is true: acceptance verifies pull (commands/views),
    and an unapproved channel/recap post would violate the no-broadcast rule."""
    text = (WORKFLOWS / "stage5-acceptance.yml").read_text(encoding="utf-8")
    assert "allow_telegram_posts" in text
    live_branch = [b for n, b in _run_blocks(text) if "PREDBOT_ALLOW_LIVE=1" in b]
    assert live_branch, "no live engine branch found"
    body = live_branch[0]
    assert 'TELEGRAM_CHAT_ID=""' in body
    assert "SUBSCRIBER_DELIVERY_ENABLED=false" in body
    assert "allow_telegram_posts" in body


def test_stage8_has_no_schedule_and_uses_the_livemon_group():
    """A3: the live monitor is dispatched on demand by the Worker
    (maybeDispatchLiveMonitor) instead of running on its own schedule
    288x/day. It also must never share stage5/stage7/stage9's
    sureodds-production-sync concurrency group (A1/A2's root cause: that
    sharing let a live-monitor tick cancel an in-progress generation run)."""
    text = (WORKFLOWS / "stage8-live-monitor-production.yml").read_text(encoding="utf-8")
    assert "schedule:" not in text, "stage8 must have no schedule trigger - dispatch is on-demand only"
    assert "workflow_dispatch:" in text
    assert "group: sureodds-production-livemon" in text
    assert "group: sureodds-production-sync" not in text


def test_stage9_exists_with_the_shared_lease():
    """A5/A2: the new settlement workflow must exist, use the shared
    sync_write lease (retry-then-fail, not stage8's skip-this-tick), and
    release it unconditionally afterward."""
    path = WORKFLOWS / "stage9-settle-production.yml"
    assert path.exists(), "stage9-settle-production.yml must exist"
    text = path.read_text(encoding="utf-8")
    assert "STAGE9_LIVE_ENABLED" in text
    assert "name='sync_write'" in text
    lease_branch = [b for n, b in _run_blocks(text) if "name='sync_write'" in b and "seq 1 10" in b]
    assert lease_branch, "stage9 must retry the sync_write lease acquire (up to 10 times), not just skip"
    assert "lease_owner=NULL, lease_until=NULL WHERE name='sync_write'" in text, "must release the lease it acquired"
    assert "run_once.py settle --live" in text


def test_stage7_has_both_the_primary_and_backup_schedule():
    """A4: off the top-of-hour slot (09:03 Lagos primary), with a 09:45
    backup fire that is a safe cache-HIT no-op if the primary already
    landed (predbot/cache/fetcher.py _snapshot_locked)."""
    text = (WORKFLOWS / "stage7-scheduled-production.yml").read_text(encoding="utf-8")
    assert '"3 8 * * *"' in text, "primary schedule (09:03 Lagos) missing"
    assert '"45 8 * * *"' in text, "backup schedule (09:45 Lagos) missing"
