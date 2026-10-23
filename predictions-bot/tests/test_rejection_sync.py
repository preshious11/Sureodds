"""Two generation runs on one day must sync to D1 without duplicate rejection rows.

The D1 side is built from the real migration file (workers/migrations/0015_selection_rejections.sql), so
the unique index and column list are exactly what production will have. Each run follows the production
path: copy the D1 state, write that run's rejections locally, run ``d1_sync.py diff`` on base->scratch, and
apply the emitted SQL to the D1 stand-in.
"""
import os
import shutil
import sqlite3
import subprocess
import sys

from predbot.selection.rejections import collector
from predbot.storage.db import Database

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
SYNC = os.path.join(ROOT, "workers", "scripts", "d1_sync.py")
MIGRATION = os.path.join(ROOT, "workers", "migrations", "0015_selection_rejections.sql")
DAY = "2026-10-23"


def _d1(path):
    con = sqlite3.connect(path)
    with open(MIGRATION, encoding="utf-8") as fh:
        con.executescript(fh.read())
    con.commit()
    con.close()


def _run(base, scratch, out, run_id, n):
    shutil.copy(base, scratch)
    db = Database(scratch)
    sink, flush = collector(db, DAY, "daily", run_id, 20)
    for i in range(n):
        sink({"canonical_event_id": f"event:{i}", "market": "result3", "period": "full", "stat": "points",
              "entity": "all", "line": None, "selection": "home", "price": 1.6, "prob": 0.3, "ev": -0.1,
              "rank_score": 0.3, "quality": "main", "books": 4}, "below_probability")
        sink({"canonical_event_id": f"event:x{i}", "market": "result3", "period": "full", "stat": "points",
              "entity": "all", "line": None, "selection": "home", "price": 1.5, "prob": 0.7, "ev": 0.01,
              "rank_score": 0.7, "quality": "main", "books": 4}, "diversity_limit")
    flush([])
    db.close()
    proc = subprocess.run([sys.executable, SYNC, "diff", base, scratch, "--out", out, "--tables",
                           "selection_rejections"], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def _apply(d1, out):
    con = sqlite3.connect(d1)
    for name in sorted(n for n in os.listdir(out) if n.endswith(".sql")):
        with open(os.path.join(out, name), encoding="utf-8") as fh:
            con.executescript(fh.read())
    con.commit()
    con.close()


def test_two_runs_on_one_day_sync_without_duplicates_and_keep_the_first_run(tmp_path):
    d1 = str(tmp_path / "d1.db")
    _d1(d1)

    base1 = str(tmp_path / "base1.db")
    shutil.copy(d1, base1)
    _run(base1, str(tmp_path / "scratch1.db"), str(tmp_path / "out1"), "run-first", n=30)
    _apply(d1, str(tmp_path / "out1"))
    after_first = sqlite3.connect(d1).execute("SELECT COUNT(*) FROM selection_rejections").fetchone()[0]
    assert after_first > 0

    base2 = str(tmp_path / "base2.db")       # D1 as it stands when the second run starts
    shutil.copy(d1, base2)
    _run(base2, str(tmp_path / "scratch2.db"), str(tmp_path / "out2"), "run-second", n=30)
    emitted = "".join(open(os.path.join(tmp_path / "out2", n), encoding="utf-8").read()
                      for n in os.listdir(tmp_path / "out2") if n.endswith(".sql"))
    # Run 2's scratch copy already holds run 1's rows (same unique keys), so its own inserts are ignored
    # locally and the diff emits nothing: first write wins, end to end.
    assert emitted.count("INSERT OR IGNORE INTO selection_rejections") == 0
    _apply(d1, str(tmp_path / "out2"))

    con = sqlite3.connect(d1)
    total, distinct = con.execute(
        "SELECT COUNT(*), COUNT(DISTINCT pick_date || '|' || product || '|' || reason || '|' || "
        "COALESCE(candidate_id, '')) FROM selection_rejections").fetchone()
    runs = {r[0] for r in con.execute("SELECT DISTINCT run_id FROM selection_rejections")}
    con.close()
    assert total == distinct == after_first      # no duplicates; the second run added nothing
    assert runs == {"run-first"}                 # the first run's rows survive


def test_the_unique_index_rejects_a_second_insert_of_the_same_candidate(tmp_path):
    d1 = str(tmp_path / "d1.db")
    _d1(d1)
    con = sqlite3.connect(d1)
    row = ("2026-10-23", "daily", "cid-1", "event:1", "result3", "full", "home", None, 1.6, 0.3, -0.1, 0.3,
           "main", 4, "below_probability", None, 1, 0, "run-a", "2026-10-23T00:00:00Z")
    sql = ("INSERT OR IGNORE INTO selection_rejections (pick_date, product, candidate_id, canonical_event_id, "
           "market, period, selection, line, price, prob, ev, rank_score, quality, books, reason, "
           "duplicate_winner_candidate_id, count, sampled, run_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)")
    con.execute(sql, row)
    con.execute(sql, row[:-2] + ("run-b", row[-1]))
    con.commit()
    assert con.execute("SELECT COUNT(*), MIN(run_id) FROM selection_rejections").fetchone() == (1, "run-a")
    con.close()
