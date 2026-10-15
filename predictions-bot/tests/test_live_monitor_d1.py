"""predbot/jobs/live_monitor_d1.py: the GitHub Actions D1-sink entrypoint.
No network access — covers the table/sink plumbing and the safe-no-op
paths directly; live_results.run()'s own status/settlement/sink-mode
behavior is already covered exhaustively by tests/test_live_results.py."""
from predbot.jobs import live_monitor_d1
from predbot.storage.db import Database


def test_ensure_match_events_table_is_idempotent(db):
    live_monitor_d1.ensure_match_events_table(db)
    live_monitor_d1.ensure_match_events_table(db)  # must not raise on a second call
    assert db.query("SELECT COUNT(*) AS n FROM match_events")[0]["n"] == 0


def test_sink_inserts_and_ignores_an_exact_duplicate(db):
    live_monitor_d1.ensure_match_events_table(db)
    sink = live_monitor_d1.make_d1_event_sink(db)
    sink("2026-10-15", "fx-1", "start", "🟢 Match Started")
    sink("2026-10-15", "fx-1", "start", "🟢 Match Started (resent)")  # same natural key
    rows = db.query("SELECT * FROM match_events")
    assert len(rows) == 1
    assert rows[0]["text"] == "🟢 Match Started", "the first write wins; a duplicate is a no-op, never an overwrite"


def test_sink_distinguishes_start_and_end_for_the_same_fixture(db):
    live_monitor_d1.ensure_match_events_table(db)
    sink = live_monitor_d1.make_d1_event_sink(db)
    sink("2026-10-15", "fx-1", "start", "🟢 Match Started")
    sink("2026-10-15", "fx-1", "end", "🏁 Match Ended")
    rows = {r["event_type"] for r in db.query("SELECT * FROM match_events")}
    assert rows == {"start", "end"}


def test_run_with_no_tracked_fixtures_is_a_safe_noop_zero_network(tmp_path):
    # No picks at all -> live_results._run's `groups` is empty -> the
    # provider-calling branch never executes, so this needs no network
    # mocking to stay offline; it proves the wiring (Config, build_providers,
    # ResultsResolver, sink, table creation) end to end.
    path = str(tmp_path / "scratch.db")
    summary = live_monitor_d1.run(path)
    assert summary == {"events_recorded": 0}
    db = Database(path)
    try:
        assert db.query("SELECT COUNT(*) AS n FROM match_events")[0]["n"] == 0
    finally:
        db.close()


def test_run_is_a_noop_when_live_updates_are_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("LIVE_UPDATES_ENABLED", "false")
    path = str(tmp_path / "scratch.db")
    summary = live_monitor_d1.run(path)
    assert summary == {"skipped": "LIVE_UPDATES_ENABLED is false"}
    import os
    assert not os.path.exists(path), "must not even create the scratch DB when disabled"
