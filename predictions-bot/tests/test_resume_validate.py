"""Regression tests for workers/scripts/resume_validate.py: a resumed plan
must prove target, budget, inventory, and table scope before any D1 write."""
import json
import os
import shutil
import sqlite3
import subprocess
import sys

VALIDATOR = os.path.join(os.path.dirname(__file__), "..", "..", "workers", "scripts",
                          "resume_validate.py")
SYNC = os.path.join(os.path.dirname(__file__), "..", "..", "workers", "scripts", "d1_sync.py")


def validate(*args):
    return subprocess.run([sys.executable, VALIDATOR, *args],
                          capture_output=True, text=True)


def _seed(path):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE picks (id INTEGER PRIMARY KEY, v TEXT)")
    con.execute("INSERT INTO picks VALUES (1, 'a')")
    con.execute("CREATE TABLE prediction_memberships (prediction_id INTEGER NOT NULL,"
                " collection TEXT NOT NULL, PRIMARY KEY (prediction_id, collection))")
    con.commit()
    con.close()


def _artifact(tmp_path):
    base = str(tmp_path / "base.db")
    src = str(tmp_path / "src.db")
    _seed(base)
    shutil.copy(base, src)
    con = sqlite3.connect(src)
    con.execute("INSERT INTO picks VALUES (2, 'b')")
    con.commit()
    con.close()
    out = str(tmp_path / "plan")
    assert subprocess.run(
        [sys.executable, SYNC, "plan", base, src, "--out", out,
         "--tables", "picks,prediction_memberships", "--write-budget", "100"],
        capture_output=True, text=True).returncode == 0
    return out


def test_valid_artifact_passes(tmp_path):
    out = _artifact(tmp_path)
    proc = validate(out, "--target-db", "sureodds_staging", "--write-budget", "100")
    assert proc.returncode == 0, proc.stderr
    assert "resume OK" in proc.stdout


def test_wrong_target_refused(tmp_path):
    out = _artifact(tmp_path)
    assert validate(out, "--target-db", "sureodds_prod").returncode == 1
    assert validate(out, "--target-db", "anything").returncode == 1


def test_over_budget_refused(tmp_path):
    out = _artifact(tmp_path)
    proc = validate(out, "--target-db", "sureodds_staging", "--write-budget", "0")
    assert proc.returncode == 1 and "budget" in proc.stderr


def test_missing_batches_refused(tmp_path):
    out = _artifact(tmp_path)
    for name in os.listdir(out):
        if name.endswith(".sql"):
            os.remove(os.path.join(out, name))
    proc = validate(out, "--target-db", "sureodds_staging")
    assert proc.returncode == 1


def test_worker_owned_batch_refused(tmp_path):
    out = _artifact(tmp_path)
    with open(os.path.join(out, "p9_deliveries_0000.sql"), "w", encoding="utf-8") as fh:
        fh.write("INSERT OR IGNORE INTO deliveries (kind, post_date, chat_id, created_at)"
                 " VALUES ('picks','2026-10-13','1','t');\n")
    with open(os.path.join(out, "plan.json"), encoding="utf-8") as fh:
        plan = json.load(fh)
    plan["sql_batches"] += 1
    plan["estimated_writes"] += 1
    with open(os.path.join(out, "plan.json"), "w", encoding="utf-8") as fh:
        json.dump(plan, fh)
    proc = validate(out, "--target-db", "sureodds_staging", "--write-budget", "100")
    assert proc.returncode == 1 and "worker-owned" in proc.stderr


def test_unexpected_table_statement_refused(tmp_path):
    out = _artifact(tmp_path)
    with open(os.path.join(out, "p9_evil_0000.sql"), "w", encoding="utf-8") as fh:
        fh.write("DROP TABLE picks;\n")
    with open(os.path.join(out, "plan.json"), encoding="utf-8") as fh:
        plan = json.load(fh)
    plan["sql_batches"] += 1
    plan["estimated_writes"] += 1
    with open(os.path.join(out, "plan.json"), "w", encoding="utf-8") as fh:
        json.dump(plan, fh)
    proc = validate(out, "--target-db", "sureodds_staging", "--write-budget", "100")
    assert proc.returncode == 1


def test_rest_malformed_refused(tmp_path):
    out = _artifact(tmp_path)
    os.makedirs(os.path.join(out, "rest"), exist_ok=True)
    with open(os.path.join(out, "rest", "p9_x.jsonl"), "w", encoding="utf-8") as fh:
        fh.write("NOT JSON\n")
    with open(os.path.join(out, "plan.json"), encoding="utf-8") as fh:
        plan = json.load(fh)
    plan["rest_pending"] = 1
    with open(os.path.join(out, "plan.json"), "w", encoding="utf-8") as fh:
        json.dump(plan, fh)
    proc = validate(out, "--target-db", "sureodds_staging", "--write-budget", "100")
    assert proc.returncode == 1


def test_missing_plan_refused(tmp_path):
    proc = validate(str(tmp_path), "--target-db", "sureodds_staging")
    assert proc.returncode == 1
