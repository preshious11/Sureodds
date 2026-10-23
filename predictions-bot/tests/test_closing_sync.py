"""D1 sync rules for the closing-price columns (workers/scripts/d1_sync.py write_policy()).

Each test builds three SQLite files: ``base`` (the D1 export a workflow started from), ``scratch``
(what the job wrote locally) and ``d1`` (production as it is at apply time, which may have moved on
since the export). It runs the real ``d1_sync.py diff`` on base->scratch and applies the emitted SQL
to ``d1``, exactly as a workflow would.
"""
import os
import shutil
import sqlite3
import subprocess
import sys

SYNC = os.path.join(os.path.dirname(__file__), "..", "..", "workers", "scripts", "d1_sync.py")

COLS = ("id INTEGER PRIMARY KEY, status TEXT, result_json TEXT, created_at TEXT, settled_at TEXT, "
        "price REAL, sources TEXT, closing_price REAL, clv REAL, closing_fair_prob REAL, closing_captured_at TEXT")
PENDING = dict(id=1, status="pending", result_json=None, created_at="2026-10-23T07:00:00Z", settled_at=None,
               price=1.9, sources='{"_notes": {}}', closing_price=None, clv=None, closing_fair_prob=None,
               closing_captured_at=None)
CAPTURE = dict(closing_price=1.8, clv=0.0556, closing_fair_prob=0.55, closing_captured_at="2026-10-23T18:52:00Z")
EARLIER_CAPTURE = dict(closing_price=1.85, clv=0.027, closing_fair_prob=0.54, closing_captured_at="2026-10-23T18:50:00Z")
SETTLED = dict(status="won", result_json='{"s": "2-0"}', settled_at="2026-10-23T21:00:00Z")


def _db(path, **row):
    con = sqlite3.connect(path)
    con.execute(f"CREATE TABLE picks ({COLS})")
    values = {**PENDING, **row}
    con.execute(f"INSERT INTO picks ({', '.join(values)}) VALUES ({', '.join('?' for _ in values)})",
                tuple(values.values()))
    con.commit()
    con.close()


def _sync(tmp_path, scratch_row, d1_row):
    base, scratch, d1 = (str(tmp_path / n) for n in ("base.db", "scratch.db", "d1.db"))
    _db(base)
    _db(scratch, **scratch_row)
    _db(d1, **d1_row)
    out = str(tmp_path / "delta")
    proc = subprocess.run([sys.executable, SYNC, "diff", base, scratch, "--out", out, "--tables", "picks"],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    con = sqlite3.connect(d1)
    for name in sorted(n for n in os.listdir(out) if n.endswith(".sql")):
        with open(os.path.join(out, name), encoding="utf-8") as fh:
            con.executescript(fh.read())
    con.row_factory = sqlite3.Row
    row = dict(con.execute("SELECT * FROM picks WHERE id=1").fetchone())
    con.close()
    return row


def _closing(row):
    return {k: row[k] for k in CAPTURE}


def test_a_pending_pick_capture_syncs_the_closing_columns(tmp_path):
    row = _sync(tmp_path, scratch_row=CAPTURE, d1_row={})
    assert _closing(row) == CAPTURE
    assert row["status"] == "pending"


def test_b_a_second_capture_never_overwrites_existing_closing_values(tmp_path):
    row = _sync(tmp_path, scratch_row=CAPTURE, d1_row=EARLIER_CAPTURE)
    assert _closing(row) == EARLIER_CAPTURE


def test_c_settlement_succeeds_when_scratch_has_closing_values_and_d1_does_not(tmp_path):
    row = _sync(tmp_path, scratch_row={**CAPTURE, **SETTLED}, d1_row={})
    assert row["status"] == "won" and row["result_json"] == SETTLED["result_json"]
    assert _closing(row) == CAPTURE


def test_c_settlement_succeeds_when_d1_has_closing_values_and_scratch_does_not(tmp_path):
    row = _sync(tmp_path, scratch_row=SETTLED, d1_row=CAPTURE)
    assert row["status"] == "won"
    assert _closing(row) == CAPTURE                     # D1's capture kept, not nulled


def test_d_settlement_still_fails_when_sources_differs(tmp_path):
    row = _sync(tmp_path, scratch_row=SETTLED, d1_row={"sources": '{"_notes": {"edited": true}}'})
    assert row["status"] == "pending"


def test_d_settlement_still_fails_when_price_differs(tmp_path):
    row = _sync(tmp_path, scratch_row={**CAPTURE, **SETTLED}, d1_row={"price": 2.0})
    assert row["status"] == "pending" and row["closing_price"] is None


def test_d_a_capture_is_also_refused_when_another_column_differs(tmp_path):
    row = _sync(tmp_path, scratch_row=CAPTURE, d1_row={"price": 2.0})
    assert row["closing_price"] is None


def test_e_a_capture_arriving_after_settlement_fills_closing_and_keeps_the_result(tmp_path):
    """The snapshot was taken before kickoff, so it is still that pick's closing price; it is
    recorded, and the settled status/result in D1 are left exactly as they are."""
    row = _sync(tmp_path, scratch_row=CAPTURE, d1_row=SETTLED)
    assert row["status"] == "won" and row["result_json"] == SETTLED["result_json"]
    assert _closing(row) == CAPTURE


def test_settlement_without_any_closing_change_behaves_as_before(tmp_path):
    row = _sync(tmp_path, scratch_row=SETTLED, d1_row={})
    assert row["status"] == "won" and row["closing_price"] is None
