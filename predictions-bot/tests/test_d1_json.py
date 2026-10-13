"""Regression tests for workers/scripts/d1_json.py: wrangler output is never
bare JSON in practice (progress lines, upload notices), and a parse failure
must never read as success."""
import json
import os
import subprocess
import sys

HELPER = os.path.join(os.path.dirname(__file__), "..", "..", "workers", "scripts", "d1_json.py")

CLEAN = json.dumps([{"results": [{"epoch": 2}], "success": True,
                     "meta": {"changes": 1}}])
PREAMBLE = ("Checking if file needs uploading\n"
            "Uploading d1266e71.sql\n"
            "Uploading complete.\n\n")


def run(args, stdin_text):
    return subprocess.run([sys.executable, HELPER, *args], input=stdin_text,
                          capture_output=True, text=True)


def test_clean_json_get():
    proc = run(["--get", "0.results.0.epoch"], CLEAN)
    assert proc.returncode == 0 and proc.stdout.strip() == "2"


def test_preamble_is_skipped():
    proc = run(["--assert-success", "--get", "0.meta.changes"], PREAMBLE + CLEAN)
    assert proc.returncode == 0 and proc.stdout.strip() == "1"


def test_empty_output_fails_with_tail():
    proc = run([], "")
    assert proc.returncode == 2 and "no JSON" in proc.stderr


def test_garbage_output_fails_with_tail():
    proc = run([], "something went wrong\nno json here\n")
    assert proc.returncode == 2


def test_success_false_fails():
    payload = json.dumps([{"results": [], "success": False,
                           "error": {"text": "FOREIGN KEY constraint failed"}}])
    proc = run(["--assert-success"], PREAMBLE + payload)
    assert proc.returncode == 1 and "FOREIGN KEY" in proc.stderr


def test_expect_changes_enforced():
    proc = run(["--expect-changes", "1"], CLEAN)
    assert proc.returncode == 0
    proc = run(["--expect-changes", "2"], CLEAN)
    assert proc.returncode == 1


def test_missing_path_fails():
    proc = run(["--get", "0.results.9.epoch"], CLEAN)
    assert proc.returncode == 1


def test_file_input():
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        fh.write(PREAMBLE + CLEAN)
        path = fh.name
    try:
        proc = subprocess.run([sys.executable, HELPER, "--get", "0.results.0.epoch", path],
                              capture_output=True, text=True)
        assert proc.returncode == 0 and proc.stdout.strip() == "2"
    finally:
        os.remove(path)
