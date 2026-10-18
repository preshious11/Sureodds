"""Stage 3 sync-safety tests. All on throwaway DBs; never the live database.

Proves: stale snapshots are refused, newer D1 receipts survive pushes, the
roster is excluded from ongoing pushes, import chunks respect D1's 100KB
per-statement cap, interrupted applies resume, and the compat guard covers
all 24 synced tables.
"""
import json
import os
import sqlite3
import subprocess
import sys

SYNC = os.path.join(os.path.dirname(__file__), "..", "..", "workers", "scripts", "d1_sync.py")
COMPAT = os.path.join(os.path.dirname(__file__), "..", "..", "workers", "scripts",
                      "check_schema_compat.py")
MIGRATIONS = os.path.join(os.path.dirname(__file__), "..", "..", "workers", "migrations")


def sync(*args):
    return subprocess.run([sys.executable, SYNC, *args], capture_output=True, text=True)


def make_db(path, jobs=True):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE kv (k TEXT PRIMARY KEY, v TEXT)")
    con.execute("CREATE TABLE posts (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,"
                " post_date TEXT NOT NULL, message_id TEXT, created_at TEXT NOT NULL,"
                " UNIQUE(kind, post_date))")
    con.execute("CREATE TABLE deliveries (kind TEXT NOT NULL, post_date TEXT NOT NULL,"
                " chat_id TEXT NOT NULL, created_at TEXT NOT NULL,"
                " UNIQUE(kind, post_date, chat_id))")
    con.execute("CREATE TABLE subscribers (chat_id TEXT PRIMARY KEY, username TEXT,"
                " first_name TEXT, timezone TEXT NOT NULL DEFAULT '',"
                " subscribed_at TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1)")
    con.execute("CREATE TABLE sync_state (id INTEGER PRIMARY KEY CHECK(id = 1),"
                " epoch INTEGER NOT NULL DEFAULT 0,"
                " updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')))")
    con.execute("INSERT INTO sync_state (id, epoch) VALUES (1, 0)")
    if jobs:
        con.execute("CREATE TABLE job_state (name TEXT PRIMARY KEY, due_at TEXT,"
                    " lease_owner TEXT, lease_until TEXT, attempts INTEGER NOT NULL DEFAULT 0,"
                    " last_run_at TEXT, last_result TEXT,"
                    " updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')))")
        con.execute("INSERT INTO job_state (name) VALUES ('generate')")
    con.commit()
    con.close()


def test_stale_push_refused(tmp_path):
    src = str(tmp_path / "src.db")
    make_db(src)
    out = str(tmp_path / "push")
    proc = sync("push", src, "--out", out, "--expect-epoch", "7")
    assert proc.returncode == 1
    assert "stale snapshot" in proc.stderr


def test_worker_tables_never_pushed(tmp_path):
    src = str(tmp_path / "src.db")
    make_db(src)
    con = sqlite3.connect(src)
    con.execute("CREATE TABLE telegram_updates (update_id INTEGER PRIMARY KEY)")
    con.execute("INSERT INTO telegram_updates VALUES (1)")
    con.commit()
    con.close()
    out = str(tmp_path / "push")
    assert sync("push", src, "--out", out).returncode == 0
    names = os.listdir(out)
    assert not [n for n in names if n.startswith("telegram_updates")]
    assert not [n for n in names if n.startswith("job_state")]
    assert not [n for n in names if n.startswith("sync_state")]


def test_newer_d1_receipts_survive_push(tmp_path):
    src = str(tmp_path / "src.db")
    tgt = str(tmp_path / "tgt.db")
    make_db(src)
    make_db(tgt)
    stale = sqlite3.connect(src)
    stale.execute("INSERT INTO deliveries VALUES ('picks','2026-10-11','1','t')")
    stale.commit()
    fresh = sqlite3.connect(tgt)
    fresh.execute("INSERT INTO deliveries VALUES ('picks','2026-10-11','1','t')")
    fresh.execute("INSERT INTO deliveries VALUES ('picks','2026-10-11','2','t')")
    fresh.execute("INSERT INTO posts VALUES (1,'picks_top','2026-10-11','m1','t')")
    fresh.commit()
    out = str(tmp_path / "push")
    assert sync("push", src, "--out", out).returncode == 0
    assert sync("apply", tgt, out).returncode == 0
    rows = fresh.execute("SELECT chat_id FROM deliveries ORDER BY chat_id").fetchall()
    assert [r[0] for r in rows] == ["1", "2"]
    assert fresh.execute("SELECT message_id FROM posts").fetchone()[0] == "m1"


def test_roster_excluded_unless_seed(tmp_path):
    src = str(tmp_path / "src.db")
    make_db(src)
    con = sqlite3.connect(src)
    con.execute("INSERT INTO subscribers VALUES ('9','u','U','','t',1)")
    con.commit()
    con.close()
    plain = str(tmp_path / "plain")
    assert sync("push", src, "--out", plain).returncode == 0
    assert not [n for n in os.listdir(plain) if "subscribers" in n]
    seed = str(tmp_path / "seed")
    assert sync("push", src, "--out", seed, "--include-roster").returncode == 0
    assert [n for n in os.listdir(seed) if "subscribers" in n]


def test_oversize_row_diverted_to_rest_not_dropped(tmp_path):
    base = str(tmp_path / "base.db")
    _seed_engine_tables(base)
    src = str(tmp_path / "src.db")
    import shutil
    shutil.copy(base, src)
    con = sqlite3.connect(src)
    con.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)")
    con.execute("INSERT INTO kv VALUES ('big', ?)", ("x" * 120000,))
    con.commit()
    con.close()
    out = str(tmp_path / "delta")
    assert sync("diff", base, src, "--out", out, "--tables", "kv").returncode == 0
    rest = [n for n in os.listdir(os.path.join(out, "rest")) if n.endswith(".jsonl")]
    assert len(rest) == 1
    import json as _json
    with open(os.path.join(out, "rest", rest[0]), encoding="utf-8") as fh:
        entry = _json.loads(fh.readline())
    assert "?" in entry["sql"] and entry["params"][1] == "x" * 120000
    with open(os.path.join(out, "delta-manifest.json"), encoding="utf-8") as fh:
        assert _json.load(fh)["rest_pending"] == 1


def test_statements_respect_d1_cap(tmp_path):
    src = str(tmp_path / "src.db")
    make_db(src)
    con = sqlite3.connect(src)
    con.execute("CREATE TABLE big (id INTEGER PRIMARY KEY, blob TEXT)")
    con.executemany("INSERT INTO big VALUES (?, ?)",
                    [(i, "x" * 20000) for i in range(10)])
    con.commit()
    con.close()
    out = str(tmp_path / "push")
    assert sync("push", src, "--out", out).returncode == 0
    for name in os.listdir(out):
        if not name.endswith(".sql"):
            continue
        with open(os.path.join(out, name), encoding="utf-8") as fh:
            for line in fh:
                assert len(line) <= 100000, f"{name}: statement exceeds D1 cap"


def test_interrupted_apply_resumes(tmp_path):
    src = str(tmp_path / "src.db")
    tgt = str(tmp_path / "tgt.db")
    make_db(src)
    make_db(tgt)
    con = sqlite3.connect(src)
    con.executemany("INSERT INTO kv VALUES (?, ?)", [(f"k{i}", "v") for i in range(60)])
    con.commit()
    con.close()
    out = str(tmp_path / "push")
    assert sync("push", src, "--out", out).returncode == 0
    assert sync("apply", tgt, out).returncode == 0
    again = sync("apply", tgt, out)
    assert again.returncode == 0 and "0 new" in again.stdout
    proc = sync("verify", tgt, "--manifest", os.path.join(out, "manifest.json"))
    assert proc.returncode == 0, proc.stdout


def test_compat_covers_all_synced_tables(tmp_path):
    from predbot.storage.db import Database
    engine = str(tmp_path / "engine.db")
    Database(engine).close()
    merged = str(tmp_path / "all.sql")
    with open(merged, "w", encoding="utf-8") as fh_out:
        for name in ("0001_foundation.sql", "0002_update_retry.sql",
                     "0003_engine_state.sql", "0004_sync_state.sql", "0005_ticket_candidates.sql"):
            with open(os.path.join(MIGRATIONS, name), encoding="utf-8") as fh_in:
                fh_out.write(fh_in.read() + "\n")
    proc = subprocess.run([sys.executable, COMPAT, "--sqlite", engine, "--migrations", merged],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout
    assert "24 engine tables" in proc.stdout or "engine tables covered" in proc.stdout


def test_0005_migration_preserves_rows_and_unblocks_sync(tmp_path):
    """Pre-migration staging shape + rows -> apply ONLY 0005 ->
    all rows intact, is_representative defaults to 1, ticket_candidates
    exists, and the engine/D1 schemas reconcile for sync."""
    pre = str(tmp_path / "pre.db")
    con = sqlite3.connect(pre)
    for name in ("0001_foundation.sql", "0003_engine_state.sql", "0004_sync_state.sql"):
        with open(os.path.join(MIGRATIONS, name), encoding="utf-8") as fh:
            con.executescript(fh.read())
    cols = [r[1] for r in con.execute("PRAGMA table_info(picks)").fetchall()]
    assert "is_representative" not in cols
    assert "ticket_candidates" not in {
        r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    seed = [
        ("2026-10-08", "c1", "result3", "pending"),
        ("2026-10-08", "c1", "total", "pending"),
        ("2026-10-08", None, "result3", "won"),
        ("2026-10-08", "", "result3", "lost"),
    ]
    for day, canon, market, status in seed:
        col_list = ", ".join(c for c in cols if c not in ("id",))
        placeholders = ", ".join("?" for c in cols if c not in ("id",))
        row = {c: None for c in cols if c not in ("id",)}
        row.update({"pick_date": day, "tier": "all", "rank": 1, "provider": "sharpapi",
                    "provider_event_id": "e", "sport": "football", "home": "A", "away": "B",
                    "start_utc": "2026-10-08T18:00:00Z", "market": market, "period": "game",
                    "stat": "goals", "entity": "all", "selection": "home", "label": "L",
                    "price": 2.0, "prob": 0.6, "ev": 0.0, "status": status,
                    "created_at": "t", "canonical_event_id": canon})
        con.execute(f"INSERT INTO picks ({col_list}) VALUES ({placeholders})",
                    [row[c] for c in cols if c not in ("id",)])
    con.commit()
    before = con.execute("SELECT COUNT(*) FROM picks").fetchone()[0]
    # Applying ONLY the 0005 file, exactly as staging D1 must.
    with open(os.path.join(MIGRATIONS, "0005_ticket_candidates.sql"), encoding="utf-8") as fh:
        con.executescript(fh.read())
    con.commit()
    assert con.execute("SELECT COUNT(*) FROM picks").fetchone()[0] == before
    assert all(r[0] == 1 for r in con.execute("SELECT DISTINCT is_representative FROM picks").fetchall())
    assert con.execute("SELECT COUNT(*) FROM ticket_candidates").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM picks WHERE canonical_event_id IS NULL").fetchone()[0] == 1
    con.close()
    # Engine schema and migrated schema now reconcile for every synced table.
    from predbot.storage.db import Database
    engine = str(tmp_path / "engine.db")
    Database(engine).close()
    merged = str(tmp_path / "all.sql")
    with open(merged, "w", encoding="utf-8") as fh_out:
        for name in ("0001_foundation.sql", "0002_update_retry.sql",
                     "0003_engine_state.sql", "0004_sync_state.sql", "0005_ticket_candidates.sql"):
            with open(os.path.join(MIGRATIONS, name), encoding="utf-8") as fh_in:
                fh_out.write(fh_in.read() + "\n")
    proc = subprocess.run([sys.executable, COMPAT, "--sqlite", engine, "--migrations", merged],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout


def test_gate_tables_commit_last(tmp_path):
    src = str(tmp_path / "src.db")
    make_db(src)
    con = sqlite3.connect(src)
    con.execute("CREATE TABLE prediction_cache (cache_key TEXT PRIMARY KEY, status TEXT)")
    con.execute("INSERT INTO prediction_cache VALUES ('k','COMPLETE')")
    con.execute("INSERT INTO kv VALUES ('a','b')")
    con.commit()
    con.close()
    out = str(tmp_path / "push")
    assert sync("push", src, "--out", out).returncode == 0
    names = sorted(n for n in os.listdir(out) if n.endswith(".sql"))
    gate = [n for n in names if n.startswith("p1_")]
    facts = [n for n in names if n.startswith("p0_")]
    assert gate and facts
    assert max(facts) < min(gate), "serving gate must sort after all fact tables"
    assert any("prediction_cache" in n for n in gate)


def test_apply_epoch_fence_before_and_after(tmp_path):
    src = str(tmp_path / "src.db")
    tgt = str(tmp_path / "tgt.db")
    make_db(src)
    make_db(tgt)
    con = sqlite3.connect(src)
    con.executemany("INSERT INTO kv VALUES (?, ?)", [("k1", "v1")])
    con.commit()
    con.close()
    out = str(tmp_path / "push")
    assert sync("push", src, "--out", out).returncode == 0
    # Pre-check: moved epoch aborts before writing anything.
    tgt_con = sqlite3.connect(tgt)
    tgt_con.execute("UPDATE sync_state SET epoch=5 WHERE id=1")
    tgt_con.commit()
    tgt_con.close()
    assert sync("apply", tgt, out, "--expect-epoch", "0").returncode == 1
    # Post-check: epoch moved DURING the write is detected. Simulate with a
    # trigger that bumps the epoch on the applied table's first insert.
    tgt2 = str(tmp_path / "tgt2.db")
    make_db(tgt2)
    trip = sqlite3.connect(tgt2)
    trip.execute("CREATE TRIGGER trip_epoch AFTER INSERT ON kv BEGIN "
                 "UPDATE sync_state SET epoch=epoch+1 WHERE id=1; END")
    trip.commit()
    trip.close()
    rc = sync("apply", tgt2, out, "--expect-epoch", "0")
    assert rc.returncode == 1
    assert "DURING apply" in (rc.stdout + rc.stderr)


def test_diff_scoping_tables_and_no_deletes(tmp_path):
    base = str(tmp_path / "base.db")
    src = str(tmp_path / "src.db")
    _seed_engine_tables(base)
    import shutil
    shutil.copy(base, src)
    con = sqlite3.connect(src)
    con.execute("UPDATE kv SET v='changed'")
    con.execute("INSERT INTO picks VALUES (9, '2026-10-11', 'pending')")
    con.execute("DELETE FROM prediction_memberships WHERE prediction_id=1")
    con.commit()
    con.close()
    # --tables restricts to kv: picks change invisible, membership delete invisible.
    scoped = str(tmp_path / "scoped")
    assert sync("diff", base, src, "--out", scoped, "--tables", "kv").returncode == 0
    names = os.listdir(scoped)
    assert any("kv" in n for n in names if n.endswith(".sql"))
    assert not any("picks" in n or "membership" in n for n in names if n.endswith(".sql"))
    # --no-deletes suppresses the membership DELETE while keeping upserts.
    nodelete = str(tmp_path / "nodelete")
    assert sync("diff", base, src, "--out", nodelete, "--no-deletes").returncode == 0
    assert not os.path.exists(os.path.join(nodelete, "p1_deletes.sql"))
    assert any(n.endswith(".sql") for n in os.listdir(nodelete))
    # Unknown table is a hard error, not a silent skip.
    bad = sync("diff", base, src, "--out", str(tmp_path / "bad"), "--tables", "nope")
    assert bad.returncode == 1


def test_plan_enforces_write_budget_and_cleans_up(tmp_path):
    base = str(tmp_path / "base.db")
    src = str(tmp_path / "src.db")
    _seed_engine_tables(base)
    import shutil
    shutil.copy(base, src)
    con = sqlite3.connect(src)
    con.executemany("INSERT INTO picks VALUES (?, ?, ?)",
                    [(10 + i, "2026-10-11", "pending") for i in range(5)])
    con.commit()
    con.close()
    over = str(tmp_path / "over")
    proc = sync("plan", base, src, "--out", over, "--tables", "picks", "--write-budget", "2")
    assert proc.returncode == 2
    left = os.listdir(over)
    assert "plan.json" in left  # numbers kept for the report
    assert not [n for n in left if n.endswith(".sql")], "refused plan must leave no appliable SQL"
    ok = str(tmp_path / "ok")
    assert sync("plan", base, src, "--out", ok, "--tables", "picks",
                "--write-budget", "50").returncode == 0
    import json as _json
    with open(os.path.join(ok, "plan.json"), encoding="utf-8") as fh:
        plan = _json.load(fh)
    assert plan["inserts"] == 5 and plan["complete"] is True
    assert plan["estimated_writes"] <= 50


def test_push_refuses_unscoped_full_replacement(tmp_path):
    base = str(tmp_path / "base.db")
    _seed_engine_tables(base)
    con = sqlite3.connect(base)
    con.executemany("INSERT INTO kv VALUES (?, ?)", [(f"k{i}", "v") for i in range(30)])
    con.commit()
    con.close()
    out = str(tmp_path / "push")
    proc = sync("push", base, "--out", out, "--max-full-rows", "10")
    assert proc.returncode == 2
    out2 = str(tmp_path / "push2")
    assert sync("push", base, "--out", out2, "--max-full-rows", "10", "--full-ok").returncode == 0


def _mock_d1_server(handler):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            code, payload = handler(self.path, dict(self.headers), body)
            data = __import__("json").dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    return server, url


def _rest_dir(tmp_path, rows):
    import json as _json
    d = str(tmp_path / "delta")
    os.makedirs(os.path.join(d, "rest"), exist_ok=True)
    with open(os.path.join(d, "rest", "p0_1_kv_0000.jsonl"), "w", encoding="utf-8") as fh:
        for k, v in rows:
            fh.write(_json.dumps({"sql": "INSERT OR REPLACE INTO kv (k, v) VALUES (?,?)",
                                  "params": [k, v]}) + "\n")
    return d


def test_rest_apply_validates_before_sending(tmp_path):
    import json as _json
    d = str(tmp_path / "delta")
    os.makedirs(os.path.join(d, "rest"), exist_ok=True)
    with open(os.path.join(d, "rest", "p0_1_kv_0000.jsonl"), "w", encoding="utf-8") as fh:
        fh.write('{"sql": "INSERT OR REPLACE INTO kv (k,v) VALUES (?,?)", "params": ["a","b"]}\n')
        fh.write("NOT JSON AT ALL\n")
    calls = []
    server, url = _mock_d1_server(lambda p, h, b: (calls.append(b), (200, [{"success": True}]))[1])
    try:
        proc = sync("rest-apply", d + "/rest", "--account-id", "a", "--database-id", "b",
                    "--api-token", "t", "--endpoint", url)
    finally:
        server.shutdown()
    assert proc.returncode == 1
    assert calls == [], "malformed payload must send nothing"


def test_rest_apply_rejects_non_insert(tmp_path):
    d = _rest_dir(tmp_path, [])
    import json as _json
    with open(os.path.join(d, "rest", "evil.jsonl"), "w", encoding="utf-8") as fh:
        fh.write(_json.dumps({"sql": "DELETE FROM kv", "params": []}) + "\n")
    calls = []
    server, url = _mock_d1_server(lambda p, h, b: (calls.append(b), (200, [{"success": True}]))[1])
    try:
        proc = sync("rest-apply", d + "/rest", "--account-id", "a", "--database-id", "b",
                    "--api-token", "t", "--endpoint", url)
    finally:
        server.shutdown()
    assert proc.returncode == 1
    assert calls == []


def test_rest_apply_retries_resumes_and_verifies(tmp_path):
    import json as _json
    d = _rest_dir(tmp_path, [(f"k{i}", "v") for i in range(3)])
    calls = []
    failures = {"n": 1}  # first request fails transiently, then heals
    seen_auth = {}

    def handler(path, headers, body):
        calls.append(_json.loads(body))
        seen_auth["auth"] = headers.get("Authorization", "")
        if failures["n"] > 0:
            failures["n"] -= 1
            return 500, {"error": "boom"}
        batch = _json.loads(body)
        return 200, [{"success": True} for _ in batch]

    server, url = _mock_d1_server(handler)
    try:
        proc = sync("rest-apply", d + "/rest", "--account-id", "acct", "--database-id", "db",
                    "--api-token", "SECRET", "--batch-size", "2", "--endpoint", url)
    finally:
        server.shutdown()
    assert proc.returncode == 0, proc.stderr
    assert seen_auth["auth"] == "Bearer SECRET"
    # All three rows went in exactly one batch each (retry re-sent batch 1, no dupes applied server-side here).
    assert os.path.exists(os.path.join(d, "rest", "applied-rest.json"))
    # Resume is a no-op success.
    proc2 = sync("rest-apply", d + "/rest", "--account-id", "acct", "--database-id", "db",
                 "--api-token", "SECRET", "--endpoint", url)
    assert proc2.returncode == 0


def test_rest_apply_quota_failure_is_explicit(tmp_path):
    d = _rest_dir(tmp_path, [("k1", "v")])
    server, url = _mock_d1_server(
        lambda p, h, b: (200, [{"success": False, "error": "row write limit exceeded [code: 7500]"}]))
    try:
        proc = sync("rest-apply", d + "/rest", "--account-id", "a", "--database-id", "b",
                    "--api-token", "t", "--retries", "0", "--endpoint", url)
    finally:
        server.shutdown()
    assert proc.returncode == 1


def test_rebase_avoids_id_collision_and_preserves_refs(tmp_path):
    base = str(tmp_path / "base.db")
    _seed_engine_tables(base)
    src = str(tmp_path / "src.db")
    import shutil
    shutil.copy(base, src)
    con = sqlite3.connect(src)
    con.execute("INSERT INTO picks VALUES (3, '2026-09-19', 'pending')")
    con.execute("INSERT INTO prediction_memberships VALUES (3, 'daily', 1)")
    con.commit()
    con.close()
    rebased = str(tmp_path / "rebased.db")
    assert sync("rebase", src, "--out", rebased, "--rebase",
                "picks:id:100000:prediction_memberships:prediction_id").returncode == 0
    # Source untouched; rebased ids shifted with refs intact.
    assert sqlite3.connect(src).execute("SELECT MAX(id) FROM picks").fetchone()[0] == 3
    got = sqlite3.connect(rebased)
    assert got.execute("SELECT MIN(id) FROM picks").fetchone()[0] == 100001
    refs = {r[0] for r in got.execute("SELECT prediction_id FROM prediction_memberships"
                                      " WHERE collection='daily' AND rank=1").fetchall()}
    assert refs == {100001, 100003}
    assert got.execute("SELECT COUNT(*) FROM prediction_memberships m JOIN picks p"
                       " ON p.id=m.prediction_id").fetchone()[0] == 3
    bad = sync("rebase", src, "--out", str(tmp_path / "x.db"), "--rebase", "bogus")
    assert bad.returncode == 1


def test_rebase_is_deterministic_across_runs(tmp_path):
    """Same source + same fixed offset must yield byte-identical output, so a
    repeated fixture sync matches existing D1 rows instead of re-inserting."""
    import hashlib
    import shutil
    base = str(tmp_path / "base.db")
    _seed_engine_tables(base)
    src = str(tmp_path / "src.db")
    shutil.copy(base, src)
    con = sqlite3.connect(src)
    con.execute("INSERT INTO picks VALUES (3, '2026-09-19', 'pending')")
    con.execute("INSERT INTO prediction_memberships VALUES (3, 'daily', 1)")
    con.commit()
    con.close()
    outs = []
    for i in (1, 2):
        out = str(tmp_path / f"rebased{i}.db")
        assert sync("rebase", src, "--out", out, "--rebase",
                    "picks:id:100000:prediction_memberships:prediction_id").returncode == 0
        outs.append(out)
    hashes = []
    for out in outs:
        con = sqlite3.connect(out)
        h = hashlib.sha256()
        for tbl in ("picks", "prediction_memberships"):
            for row in con.execute(f"SELECT * FROM {tbl} ORDER BY 1"):
                h.update(repr(row).encode())
        hashes.append(h.hexdigest())
    assert hashes[0] == hashes[1]


def test_repeated_fixture_sync_is_zero_write(tmp_path):
    """A fresh engine scratch holds ONLY new rows (like replay scratch); after
    import, diffing the same rebased source again emits nothing and adds nothing."""
    base = str(tmp_path / "base.db")
    _seed_engine_tables(base)  # picks 1,2 already in "D1"
    src = str(tmp_path / "src.db")
    fresh = sqlite3.connect(src)
    fresh.execute("CREATE TABLE picks (id INTEGER PRIMARY KEY, pick_date TEXT, status TEXT)")
    fresh.execute("CREATE TABLE prediction_memberships (prediction_id INTEGER NOT NULL"
                  " REFERENCES picks(id), collection TEXT NOT NULL, rank INTEGER NOT NULL DEFAULT 0,"
                  " PRIMARY KEY (prediction_id, collection))")
    fresh.execute("INSERT INTO picks VALUES (7, '2026-09-19', 'pending')")
    fresh.execute("INSERT INTO prediction_memberships VALUES (7, 'daily', 1)")
    fresh.commit()
    fresh.close()
    rebased = str(tmp_path / "rebased.db")
    assert sync("rebase", src, "--out", rebased, "--rebase",
                "picks:id:100000:prediction_memberships:prediction_id").returncode == 0
    first = str(tmp_path / "first")
    assert sync("diff", base, rebased, "--out", first,
                "--tables", "picks,prediction_memberships", "--no-deletes").returncode == 0
    assert [n for n in os.listdir(first) if n.endswith(".sql")]
    # Import (as the workflow would), then diff the same source again.
    assert sync("apply", base, first).returncode == 0
    assert sqlite3.connect(base).execute("SELECT COUNT(*) FROM picks").fetchone()[0] == 3
    second = str(tmp_path / "second")
    assert sync("diff", base, rebased, "--out", second,
                "--tables", "picks,prediction_memberships", "--no-deletes").returncode == 0
    assert [n for n in os.listdir(second) if n.endswith(".sql")] == []
    n = sqlite3.connect(base).execute("SELECT COUNT(*) FROM picks").fetchone()[0]
    assert n == 3, "no duplicates may appear"


def test_volatile_timestamps_ignored_but_grading_syncs(tmp_path):
    """created_at/settled_at alone never trigger a rewrite; a status flip does
    (carrying the fresh timestamp with the full row)."""
    import shutil
    base = str(tmp_path / "base.db")
    con = sqlite3.connect(base)
    con.execute("CREATE TABLE picks (id INTEGER PRIMARY KEY, status TEXT,"
                " result_json TEXT, created_at TEXT, settled_at TEXT)")
    con.execute("INSERT INTO picks VALUES (1, 'pending', NULL, '2026-10-11T10:00:00Z', NULL)")
    con.commit()
    con.close()
    # Touch only the write stamps: zero files.
    src = str(tmp_path / "src.db")
    shutil.copy(base, src)
    con = sqlite3.connect(src)
    con.execute("UPDATE picks SET created_at='2026-10-11T11:00:00Z', settled_at='2026-10-11T11:00:00Z'")
    con.commit()
    con.close()
    out = str(tmp_path / "delta")
    assert sync("diff", base, src, "--out", out, "--tables", "picks").returncode == 0
    assert [n for n in os.listdir(out) if n.endswith(".sql")] == []
    # Genuine grading flip: exactly one row emitted, with its new timestamp.
    con = sqlite3.connect(src)
    con.execute("UPDATE picks SET status='won', result_json='{\"s\":\"2-0\"}',"
                " settled_at='2026-10-11T12:00:00Z' WHERE id=1")
    con.commit()
    con.close()
    out2 = str(tmp_path / "delta2")
    assert sync("diff", base, src, "--out", out2, "--tables", "picks").returncode == 0
    files = [n for n in os.listdir(out2) if n.endswith(".sql")]
    assert len(files) == 1
    with open(os.path.join(out2, files[0]), encoding="utf-8") as fh:
        text = fh.read()
    assert "'won'" in text and "2026-10-11T12:00:00Z" in text


def test_volatile_json_timings_ignored_but_content_syncs(tmp_path):
    """generation.timings drift alone emits nothing; a status change emits."""
    import json as _json
    import shutil
    base = str(tmp_path / "base.db")
    con = sqlite3.connect(base)
    con.execute("CREATE TABLE prediction_cache (cache_key TEXT PRIMARY KEY,"
                " status TEXT, payload_json TEXT)")
    payload = {"generation": {"timings": {"wall": 0.5}, "complete": True}, "n": 3}
    con.execute("INSERT INTO prediction_cache VALUES ('k', 'COMPLETE', ?)",
                (_json.dumps(payload),))
    con.commit()
    con.close()
    src = str(tmp_path / "src.db")
    shutil.copy(base, src)
    con = sqlite3.connect(src)
    payload["generation"]["timings"]["wall"] = 0.9
    con.execute("UPDATE prediction_cache SET payload_json=? WHERE cache_key='k'",
                (_json.dumps(payload),))
    con.commit()
    con.close()
    out = str(tmp_path / "delta")
    assert sync("diff", base, src, "--out", out, "--tables", "prediction_cache").returncode == 0
    assert [n for n in os.listdir(out) if n.endswith(".sql")] == []
    con = sqlite3.connect(src)
    con.execute("UPDATE prediction_cache SET status='PARTIAL' WHERE cache_key='k'")
    con.commit()
    con.close()
    out2 = str(tmp_path / "delta2")
    assert sync("diff", base, src, "--out", out2, "--tables", "prediction_cache").returncode == 0
    assert len([n for n in os.listdir(out2) if n.endswith(".sql")]) == 1


def test_identical_replay_runs_diff_to_zero(tmp_path):
    """End-to-end guard for the observed failure: two identical engine runs
    must produce zero SQL (timestamps and timing telemetry excluded)."""
    import sys as _sys
    _sys.path.insert(0, str(__import__("pathlib").Path("tools").resolve()))
    import replay
    first = str(tmp_path / "first")
    second = str(tmp_path / "second")
    import os as _os
    _os.makedirs(first, exist_ok=True)
    _os.makedirs(second, exist_ok=True)
    replay.run(send_delay=0.0, workdir=first)
    replay.run(send_delay=0.0, workdir=second)
    out = str(tmp_path / "delta")
    assert sync("diff", os.path.join(first, "replay.db"), os.path.join(second, "replay.db"),
                "--out", out).returncode == 0
    assert [n for n in os.listdir(out) if n.endswith(".sql")] == []


def test_exclude_keys_skips_auditable_rows_only(tmp_path):
    import shutil
    base = str(tmp_path / "base.db")
    _seed_engine_tables(base)
    src = str(tmp_path / "src.db")
    shutil.copy(base, src)
    con = sqlite3.connect(src)
    con.execute("CREATE TABLE IF NOT EXISTS prediction_cache (cache_key TEXT PRIMARY KEY,"
                " status TEXT, payload_json TEXT)")
    con.execute("INSERT INTO prediction_cache VALUES ('skip-me', 'PARTIAL', '{}')")
    con.execute("INSERT INTO prediction_cache VALUES ('keep-me', 'COMPLETE', '{}')")
    con.commit()
    con.close()
    # Base lacks the table rows entirely? Seed base identically minus the rows.
    bcon = sqlite3.connect(base)
    bcon.execute("CREATE TABLE IF NOT EXISTS prediction_cache (cache_key TEXT PRIMARY KEY,"
                 " status TEXT, payload_json TEXT)")
    bcon.commit()
    bcon.close()
    out = str(tmp_path / "delta")
    assert sync("diff", base, src, "--out", out, "--tables", "prediction_cache",
                "--exclude-keys", "prediction_cache:skip-me").returncode == 0
    import json as _json
    with open(os.path.join(out, "delta-manifest.json"), encoding="utf-8") as fh:
        delta = _json.load(fh)
    assert delta["tables"]["prediction_cache"]["excluded"] == ["skip-me"]
    assert delta["tables"]["prediction_cache"]["added"] == 1
    files = [n for n in os.listdir(out) if n.endswith(".sql")]
    assert len(files) == 1
    with open(os.path.join(out, files[0]), encoding="utf-8") as fh:
        text = fh.read()
    assert "keep-me" in text and "skip-me" not in text
    # Malformed specs fail loudly.
    bad = sync("diff", base, src, "--out", str(tmp_path / "bad"),
               "--tables", "prediction_cache", "--exclude-keys", "no-colon-here")
    assert bad.returncode == 2


def test_require_complete_gate(tmp_path):
    base = str(tmp_path / "base.db")
    con = sqlite3.connect(base)
    con.execute("CREATE TABLE prediction_cache (cache_key TEXT PRIMARY KEY, local_date TEXT,"
                " timezone TEXT, status TEXT, payload_json TEXT)")
    con.execute("INSERT INTO prediction_cache VALUES ('k', '2026-10-11', 'Africa/Lagos',"
                " 'PARTIAL', '{}')")
    con.commit()
    con.close()
    assert sync("require-complete", base, "2026-10-11").returncode == 1
    con = sqlite3.connect(base)
    con.execute("UPDATE prediction_cache SET status='COMPLETE' WHERE cache_key='k'")
    con.commit()
    con.close()
    assert sync("require-complete", base, "2026-10-11").returncode == 0
    assert sync("require-complete", base, "2026-10-12").returncode == 1


def test_require_complete_failure_prints_provider_verdict(tmp_path):
    """The gate failure must name the cause (required vs optional vs error)
    from the PARTIAL row itself, so a bare log line diagnoses the run."""
    import json as _json
    base = str(tmp_path / "base.db")
    con = sqlite3.connect(base)
    con.execute("CREATE TABLE prediction_cache (cache_key TEXT PRIMARY KEY, local_date TEXT,"
                " timezone TEXT, status TEXT, payload_json TEXT)")
    payload = {"generation": {"complete": False,
                              "provider_coverage": {"required": ["sharpapi"],
                                                    "missing_required": [],
                                                    "optional_degraded": {"sportsgameodds": "partial"},
                                                    "complete": False}}}
    con.execute("INSERT INTO prediction_cache VALUES ('k', '2026-10-13', 'Africa/Lagos',"
                " 'PARTIAL', ?)", (_json.dumps(payload),))
    con.commit()
    con.close()
    proc = sync("require-complete", base, "2026-10-13")
    assert proc.returncode == 1
    assert "missing_required=[]" in proc.stderr
    assert "optional_degraded={'sportsgameodds': 'partial'}" in proc.stderr
    assert "payload_json" not in proc.stderr  # never dump the payload itself


def test_end_to_end_offline_pipeline_with_mid_apply_failure(tmp_path):
    """Full lifecycle with the real scripts on throwaway DBs: plan (budgeted)
    -> lease -> apply (fails halfway) -> resume -> epoch bump (fenced) ->
    verify -> readback. Mirrors the GitHub Actions order of operations."""
    import shutil
    base = str(tmp_path / "base.db")
    _seed_engine_tables(base)
    con = sqlite3.connect(base)
    con.execute("CREATE TABLE prediction_cache (cache_key TEXT PRIMARY KEY,"
                " local_date TEXT, timezone TEXT, status TEXT, payload_json TEXT)")
    con.execute("INSERT INTO prediction_cache VALUES ('day', '2026-10-13', 'Africa/Lagos',"
                " 'COMPLETE', '{}')")
    con.execute("CREATE TABLE sync_state (id INTEGER PRIMARY KEY, epoch INTEGER)")
    con.execute("INSERT INTO sync_state VALUES (1, 7)")
    con.execute("CREATE TABLE job_state (name TEXT PRIMARY KEY, lease_owner TEXT,"
                " lease_until TEXT, attempts INTEGER DEFAULT 0,"
                " last_run_at TEXT, last_result TEXT,"
                " updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')))")
    con.execute("INSERT INTO job_state (name) VALUES ('generate')")
    con.commit()
    con.close()
    # Engine output: one settled pick, one new pick, one new cache row.
    src = str(tmp_path / "src.db")
    shutil.copy(base, src)
    con = sqlite3.connect(src)
    con.execute("UPDATE picks SET status='won' WHERE id=1")
    con.execute("INSERT INTO picks VALUES (9, '2026-10-13', 'pending')")
    con.execute("INSERT INTO prediction_memberships VALUES (9, 'daily', 1)")
    con.execute("INSERT INTO prediction_cache VALUES ('day2', '2026-10-13', 'Africa/Lagos',"
                " 'COMPLETE', '{}')")
    con.commit()
    con.close()
    out = str(tmp_path / "plan")
    assert sync("plan", base, src, "--out", out, "--tables",
                "picks,prediction_memberships,prediction_cache",
                "--write-budget", "100").returncode == 0
    # Lease, then apply with a poisoned second batch to simulate interruption.
    assert sync("lease", base, "generate", "e2e").returncode == 0
    batches = sorted(n for n in os.listdir(out) if n.endswith(".sql"))
    assert len(batches) >= 2
    poisoned = os.path.join(out, batches[1])
    with open(poisoned, "a", encoding="utf-8") as fh:
        fh.write("THIS IS NOT SQL;\n")
    assert sync("apply", base, out, "--expect-epoch", "7").returncode != 0
    # Epoch must be untouched by the failed apply.
    assert sqlite3.connect(base).execute("SELECT epoch FROM sync_state WHERE id=1"
                                         ).fetchone()[0] == 7
    # Remove the poison (operator fix), resume: completes without duplicates.
    with open(poisoned, encoding="utf-8") as fh:
        lines = [ln for ln in fh if "THIS IS NOT SQL" not in ln]
    with open(poisoned, "w", encoding="utf-8") as fh:
        fh.writelines(lines)
    assert sync("apply", base, out, "--expect-epoch", "7").returncode == 0
    assert sync("epoch-bump", base, "--expect", "7").returncode == 0
    assert sqlite3.connect(base).execute("SELECT epoch FROM sync_state WHERE id=1"
                                         ).fetchone()[0] == 8
    # Full-manifest verify against the source passes after resume. The epoch
    # itself is target-side fencing state (converged by the fenced bump, not
    # by row replication), so align it before comparing.
    con = sqlite3.connect(src)
    con.execute("UPDATE sync_state SET epoch=8 WHERE id=1")
    con.commit()
    con.close()
    man = str(tmp_path / "man")
    # Scoped to synced tables: the epoch is target-side fencing state that
    # legitimately differs (converged by the fenced bump, not replication).
    assert sync("manifest", src, "--out", man, "--tables",
                "picks,prediction_memberships,prediction_cache").returncode == 0
    assert sync("verify", base, "--manifest", os.path.join(man, "manifest.json")).returncode == 0
    # Readback: settled flip present exactly once, new pick present once.
    got = sqlite3.connect(base)
    assert got.execute("SELECT status FROM picks WHERE id=1").fetchone()[0] == "won"
    assert got.execute("SELECT COUNT(*) FROM picks WHERE id=9").fetchone()[0] == 1
    assert got.execute("SELECT COUNT(*) FROM picks").fetchone()[0] == 3
    assert sync("release", base, "generate", "e2e").returncode == 0


def test_concurrent_worker_receipt_survives_engine_apply(tmp_path):
    # A delivery DM recorded in D1 while an engine apply runs must survive:
    # receipts replicate INSERT OR IGNORE, so either order converges.
    src = str(tmp_path / "src.db")
    tgt = str(tmp_path / "tgt.db")
    make_db(src)
    make_db(tgt)
    tgt_con = sqlite3.connect(tgt)
    tgt_con.execute("INSERT INTO deliveries VALUES ('picks','2026-10-11','STAGING_1','t')")
    tgt_con.commit()
    tgt_con.close()
    out = str(tmp_path / "push")
    assert sync("push", src, "--out", out).returncode == 0
    assert sync("apply", tgt, out).returncode == 0
    got = sqlite3.connect(tgt).execute("SELECT chat_id FROM deliveries").fetchall()
    assert got == [("STAGING_1",)]


def _seed_engine_tables(path):
    """Minimal engine-shaped DB: PK tables with history + a settlement flip."""
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE picks (id INTEGER PRIMARY KEY, pick_date TEXT, status TEXT)")
    con.execute("CREATE TABLE prediction_memberships (prediction_id INTEGER NOT NULL"
                " REFERENCES picks(id), collection TEXT NOT NULL, rank INTEGER NOT NULL DEFAULT 0,"
                " PRIMARY KEY (prediction_id, collection))")
    con.execute("CREATE TABLE match_results (id INTEGER PRIMARY KEY, score TEXT)")
    con.execute("CREATE TABLE kv (k TEXT PRIMARY KEY, v TEXT)")
    con.executemany("INSERT INTO picks VALUES (?, ?, ?)",
                    [(1, "2026-10-08", "pending"), (2, "2026-10-08", "pending")])
    con.executemany("INSERT INTO prediction_memberships VALUES (?, ?, ?)",
                    [(1, "daily", 1), (2, "daily", 2)])
    con.execute("INSERT INTO match_results VALUES (1, '1-0')")
    con.execute("INSERT INTO kv VALUES ('a', 'b')")
    con.commit()
    con.close()


def test_no_change_diff_emits_zero_writes(tmp_path):
    base = str(tmp_path / "base.db")
    _seed_engine_tables(base)
    out = str(tmp_path / "delta")
    assert sync("diff", base, base, "--out", out).returncode == 0
    sql_files = [n for n in os.listdir(out) if n.endswith(".sql")]
    assert sql_files == [], f"no-change sync must emit no SQL, got {sql_files}"
    # Applying the empty delta executes zero statements (D1 would see no writes).
    import shutil
    tgt = str(tmp_path / "tgt.db")
    shutil.copy(base, tgt)
    before = sqlite3.connect(tgt).execute("SELECT COUNT(*) FROM picks").fetchone()[0]
    assert sync("apply", tgt, out).returncode == 0
    after = sqlite3.connect(tgt).execute("SELECT COUNT(*) FROM picks").fetchone()[0]
    assert before == after


def test_changed_records_converge_and_history_is_never_deleted(tmp_path):
    base = str(tmp_path / "base.db")
    src = str(tmp_path / "src.db")
    _seed_engine_tables(base)
    import shutil
    shutil.copy(base, src)
    con = sqlite3.connect(src)
    con.execute("UPDATE picks SET status='won' WHERE id=1")          # settlement flip
    con.execute("INSERT INTO picks VALUES (3, '2026-10-11', 'pending')")  # new day
    con.execute("INSERT INTO prediction_memberships VALUES (3, 'daily', 1)")
    con.execute("DELETE FROM prediction_memberships WHERE prediction_id=2")  # reset-day
    con.execute("DELETE FROM picks WHERE id=2")
    con.execute("DELETE FROM match_results WHERE id=1")  # NOT allowed: must be ignored
    con.commit()
    con.close()
    out = str(tmp_path / "delta")
    assert sync("diff", base, src, "--out", out).returncode == 0
    tgt = str(tmp_path / "tgt.db")
    shutil.copy(base, tgt)
    assert sync("apply", tgt, out).returncode == 0
    got = sqlite3.connect(tgt)
    assert got.execute("SELECT status FROM picks WHERE id=1").fetchone()[0] == "won"
    assert got.execute("SELECT COUNT(*) FROM picks WHERE id=3").fetchone()[0] == 1
    assert got.execute("SELECT COUNT(*) FROM picks WHERE id=2").fetchone()[0] == 1
    assert got.execute("SELECT COUNT(*) FROM prediction_memberships WHERE prediction_id=2").fetchone()[0] == 1
    # match_results is append-or-update: the illegitimate delete did NOT replicate.
    assert got.execute("SELECT COUNT(*) FROM match_results WHERE id=1").fetchone()[0] == 1
    # A partial/reset source must never erase historical tickets in D1.
    deletes = os.path.join(out, "p1_deletes.sql")
    assert not os.path.exists(deletes)


def _seed_live_fixture_state(path, status="in_progress", settlement_done=0, notifications_done=0):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE kv (k TEXT PRIMARY KEY, v TEXT)")
    con.execute("CREATE TABLE live_fixture_state (pick_date TEXT NOT NULL, fixture_key TEXT NOT NULL,"
                " status TEXT NOT NULL, started_at TEXT, final_at TEXT, last_checked_at TEXT,"
                " result_source TEXT, result_json TEXT,"
                " settlement_done INTEGER NOT NULL DEFAULT 0, notifications_done INTEGER NOT NULL DEFAULT 0,"
                " updated_at TEXT NOT NULL, PRIMARY KEY (pick_date, fixture_key))")
    con.execute(
        "INSERT INTO live_fixture_state (pick_date, fixture_key, status, settlement_done,"
        " notifications_done, updated_at) VALUES (?, ?, ?, ?, ?, '2026-10-11T00:00:00Z')",
        ("2026-10-11", "f1", status, settlement_done, notifications_done),
    )
    con.commit()
    con.close()


def test_live_fixture_state_settlement_flip_lands_after_final(tmp_path):
    """A fixture already 'final' in D1 must still accept a later settlement_done/
    notifications_done flip (the Match Ended alert waits on exactly this) - the old
    single row-level WHERE guarded the whole UPDATE and blocked it once final."""
    base = str(tmp_path / "base.db")
    src = str(tmp_path / "src.db")
    _seed_live_fixture_state(base, status="final", settlement_done=0, notifications_done=0)
    import shutil
    shutil.copy(base, src)
    con = sqlite3.connect(src)
    con.execute("UPDATE live_fixture_state SET settlement_done=1, notifications_done=1,"
                " result_json='{\"h\":1,\"a\":0}' WHERE fixture_key='f1'")
    con.commit()
    con.close()
    out = str(tmp_path / "delta")
    assert sync("diff", base, src, "--out", out, "--tables", "live_fixture_state").returncode == 0
    tgt = str(tmp_path / "tgt.db")
    shutil.copy(base, tgt)
    assert sync("apply", tgt, out).returncode == 0
    row = sqlite3.connect(tgt).execute(
        "SELECT settlement_done, notifications_done, result_json, status FROM live_fixture_state WHERE fixture_key='f1'"
    ).fetchone()
    # settlement_done/notifications_done land via MAX() regardless of status; result_json
    # is NOT one of the two monotonic columns, so it keeps the old guard (status already
    # 'final' in the target blocks it) - exactly as the spec says: "all other columns keep
    # the existing guard".
    assert row == (1, 1, None, "final")


def test_live_fixture_state_status_guard_still_blocks_regression_once_final(tmp_path):
    """Non-monotonic columns (status, result_json, etc.) keep the old protection: a
    source that regresses status (e.g. a stale scratch copy) must not overwrite an
    already-final D1 row, even though settlement_done/notifications_done now always
    update via MAX()."""
    base = str(tmp_path / "base.db")
    src = str(tmp_path / "src.db")
    _seed_live_fixture_state(base, status="final", settlement_done=1, notifications_done=1)
    import shutil
    shutil.copy(base, src)
    con = sqlite3.connect(src)
    # A stale/reset source regressing status and settlement flags backward.
    con.execute("UPDATE live_fixture_state SET status='in_progress', settlement_done=0,"
                " notifications_done=0 WHERE fixture_key='f1'")
    con.commit()
    con.close()
    out = str(tmp_path / "delta")
    assert sync("diff", base, src, "--out", out, "--tables", "live_fixture_state").returncode == 0
    tgt = str(tmp_path / "tgt.db")
    shutil.copy(base, tgt)
    assert sync("apply", tgt, out).returncode == 0
    row = sqlite3.connect(tgt).execute(
        "SELECT status, settlement_done, notifications_done FROM live_fixture_state WHERE fixture_key='f1'"
    ).fetchone()
    # status never regresses (old guard); MAX() means settlement/notifications flags
    # never regress either, even though they are no longer gated by that guard.
    assert row == ("final", 1, 1)


def test_plan_separates_insert_growth_from_destructive_budget(tmp_path):
    import shutil
    base, src = str(tmp_path / "base.db"), str(tmp_path / "src.db")
    _seed_engine_tables(base)
    shutil.copy(base, src)
    with sqlite3.connect(src) as con:
        con.executemany("INSERT INTO picks VALUES (?, ?, ?)",
                        [(10 + i, "2026-10-11", "pending") for i in range(100)])
    out = str(tmp_path / "inserts")
    proc = sync("plan", base, src, "--out", out, "--tables", "picks",
                "--write-budget", "200", "--destructive-budget", "0")
    assert proc.returncode == 0, proc.stderr
    plan = json.load(open(os.path.join(out, "plan.json")))
    assert plan["inserts"] == 100 and plan["destructive_writes"] == 0
    with sqlite3.connect(src) as con:
        con.execute("UPDATE picks SET status='won' WHERE id=1")
    out = str(tmp_path / "updates")
    proc = sync("plan", base, src, "--out", out, "--tables", "picks",
                "--write-budget", "200", "--destructive-budget", "0")
    assert proc.returncode == 2
    assert "destructive budget" in proc.stderr
    assert not list(__import__('pathlib').Path(out).glob("*.sql"))
    plan = json.load(open(os.path.join(out, "plan.json")))
    assert plan["destructive_writes"] == 1
    proc = sync("plan", base, src, "--out", str(tmp_path / "both"), "--tables", "picks",
                "--write-budget", "1", "--destructive-budget", "0")
    assert proc.returncode == 2
    assert "write budget" in proc.stderr and "destructive budget" in proc.stderr


def test_plan_destructive_budget_counts_deletes(tmp_path, monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location("d1_sync_budget_test", SYNC)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Delete emission remains recovery-only; test auditing without weakening that guard.
    out = tmp_path / "deletes"
    def diff(*args):
        out.mkdir()
        (out / "delta-manifest.json").write_text(json.dumps({"tables": {"picks": {
            "added_or_changed": 0, "added": 0, "updated": 0, "deleted": 1}}}))
        (out / "p1_deletes.sql").write_text("DELETE FROM picks WHERE id=1;")
        return 0
    monkeypatch.setattr(module, "cmd_diff", diff)
    assert module.cmd_plan("base", "src", str(out), destructive_budget=0) == 2
    assert json.loads((out / "plan.json").read_text())["destructive_writes"] == 1
    assert not list(out.glob("*.sql"))
