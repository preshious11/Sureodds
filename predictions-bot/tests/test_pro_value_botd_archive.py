"""Bet of the Day shadow input: the candidate archive is read-only and the Pro exclusion removes whole events."""
import json
import sqlite3

import pytest

from predbot.pro_value import botd_archive


def _row(home, away, start, selection="home"):
    return {"home": home, "away": away, "start_utc": start, "selection": selection, "sources": {}}


@pytest.fixture
def archive_db(tmp_path):
    path = tmp_path / "generation.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE ticket_candidates (pick_date TEXT NOT NULL, candidate_id TEXT NOT NULL, "
                "payload_json TEXT NOT NULL, PRIMARY KEY(pick_date, candidate_id))")
    for day, cid, row in [("2026-10-24", "old", _row("A", "B", "2026-10-24T18:00:00Z")),
                          ("2026-10-25", "c1", _row("Arsenal", "Chelsea", "2026-10-25T15:00:00Z")),
                          ("2026-10-25", "c2", _row("Lyon", "Nice", "2026-10-25T17:00:00Z"))]:
        con.execute("INSERT INTO ticket_candidates VALUES (?,?,?)", (day, cid, json.dumps(row)))
    con.commit()
    con.close()
    return str(path)


def test_latest_pick_date_and_rows_for_that_day(archive_db):
    assert botd_archive.latest_pick_date(archive_db) == "2026-10-25"
    rows = botd_archive.load_archive_rows(archive_db, "2026-10-25")
    assert [r["id"] for r in rows] == ["c1", "c2"]
    assert all(isinstance(r["sources"], str) for r in rows)


def test_archive_connection_cannot_write(archive_db):
    con = botd_archive._connect_ro(archive_db)
    try:
        with pytest.raises(sqlite3.OperationalError):
            con.execute("DELETE FROM ticket_candidates")
    finally:
        con.close()
    assert botd_archive.latest_pick_date(archive_db) == "2026-10-25"  # nothing was removed


def test_event_key_is_the_shadow_match_key():
    from datetime import datetime, timezone
    from predbot.pro_value.matching import match_key
    row = _row("Arsenal", "Chelsea", "2026-10-25T15:00:00Z")
    kickoff = int(datetime(2026, 10, 25, 15, tzinfo=timezone.utc).timestamp())
    assert botd_archive.event_key(row) == match_key("Arsenal", "Chelsea", kickoff)


def test_pro_exclusion_removes_the_whole_event(monkeypatch):
    pool = [_row("Arsenal", "Chelsea", "2026-10-25T15:00:00Z", "home"),
            _row("Arsenal", "Chelsea", "2026-10-25T15:00:00Z", "over"),
            _row("Lyon", "Nice", "2026-10-25T17:00:00Z", "away")]
    monkeypatch.setattr(botd_archive, "select_top", lambda rows, cfg, now: (list(rows), {}))
    excluded = {botd_archive.event_key(pool[0])}
    base = botd_archive.bet_of_the_day(pool, cfg=None, now_utc="2026-10-25T10:00:00Z")
    without = botd_archive.bet_of_the_day(pool, cfg=None, now_utc="2026-10-25T10:00:00Z", exclude=excluded)
    assert len(base) == 3
    assert [b["match_key"] for b in without] == [botd_archive.event_key(pool[2])]
