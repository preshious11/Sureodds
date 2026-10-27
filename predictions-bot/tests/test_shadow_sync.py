"""Tests for the Pro value shadow selection/closing sync link: the server has
no connection to Actions-side selection runs without this (see
docs/STAGE1_SERVER_MIGRATION_PLAN.md's note on the gap found during the
server-migration rehearsal). Covers both halves - export_shadow_rows.py
(Actions side, read-only) and apply_shadow_rows.py (server side, the one
that actually writes) - plus the idempotency guarantee a retried delivery
relies on."""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
import export_shadow_rows  # noqa: E402
import apply_shadow_rows  # noqa: E402

from predbot.pro_value.store import ShadowStore  # noqa: E402

KICK = 1_800_010_000


def _row(run_at: int, rank: int = 1, outcome: str = "home") -> dict:
    return {
        "match_key": f"team_a|team_b|{rank}", "league": "soccer_epl", "home": "Team A", "away": "Team B",
        "kickoff_ts": KICK, "outcome": outcome, "sporty_odds": 2.5, "sporty_fetched_at": run_at,
        "fair_probability": 0.3, "fair_book": "pinnacle", "sharp_fetched_at": run_at, "edge": 0.05,
        "status": "selected", "rank": rank,
    }


def _seed_actions_db(path: str, run_id: str, run_at: int, n_rows: int = 2) -> None:
    store = ShadowStore(path)
    store.insert_rows(run_id, run_at, [_row(run_at, rank=i + 1) for i in range(n_rows)])
    store.close()


class TestExportShadowRows:
    def test_exports_only_the_requested_run_id(self, tmp_path):
        db_path = str(tmp_path / "actions_shadow.db")
        _seed_actions_db(db_path, "run-a", 1000, n_rows=2)
        _seed_actions_db(db_path, "run-b", 2000, n_rows=1)

        rows = export_shadow_rows.export_rows(db_path, "run-a")
        assert len(rows) == 2
        assert all(r["run_id"] == "run-a" for r in rows)

    def test_exports_nothing_for_an_unknown_run_id(self, tmp_path):
        db_path = str(tmp_path / "actions_shadow.db")
        _seed_actions_db(db_path, "run-a", 1000)
        assert export_shadow_rows.export_rows(db_path, "no-such-run") == []

    def test_cli_writes_valid_json_and_reports_count(self, tmp_path):
        db_path = str(tmp_path / "actions_shadow.db")
        _seed_actions_db(db_path, "run-a", 1000, n_rows=3)
        out_path = str(tmp_path / "out.json")
        assert export_shadow_rows.main(["export_shadow_rows.py", db_path, "run-a", out_path]) == 0
        with open(out_path, encoding="utf-8") as fh:
            rows = json.load(fh)
        assert len(rows) == 3


class TestApplyShadowRows:
    def test_applies_a_fresh_delivery(self, tmp_path):
        rows_path = str(tmp_path / "rows.json")
        server_db = str(tmp_path / "server_shadow.db")
        rows = [dict(run_id="run-x", run_at=5000, **_row(5000, rank=i + 1)) for i in range(2)]
        with open(rows_path, "w", encoding="utf-8") as fh:
            json.dump(rows, fh)

        assert apply_shadow_rows.apply_rows(rows_path, server_db) == 0

        store = ShadowStore(server_db)
        try:
            n = store.conn.execute("SELECT COUNT(*) FROM pro_value_rows WHERE run_id = ?", ("run-x",)).fetchone()[0]
        finally:
            store.close()
        assert n == 2

    def test_a_retried_delivery_of_the_same_run_id_is_a_no_op(self, tmp_path):
        rows_path = str(tmp_path / "rows.json")
        server_db = str(tmp_path / "server_shadow.db")
        rows = [dict(run_id="run-y", run_at=6000, **_row(6000))]
        with open(rows_path, "w", encoding="utf-8") as fh:
            json.dump(rows, fh)

        assert apply_shadow_rows.apply_rows(rows_path, server_db) == 0
        assert apply_shadow_rows.apply_rows(rows_path, server_db) == 0  # the retry

        store = ShadowStore(server_db)
        try:
            n = store.conn.execute("SELECT COUNT(*) FROM pro_value_rows WHERE run_id = ?", ("run-y",)).fetchone()[0]
        finally:
            store.close()
        assert n == 1, "a retried delivery must not duplicate rows"

    def test_an_empty_delivery_is_a_clean_no_op(self, tmp_path):
        rows_path = str(tmp_path / "rows.json")
        server_db = str(tmp_path / "server_shadow.db")
        with open(rows_path, "w", encoding="utf-8") as fh:
            json.dump([], fh)
        assert apply_shadow_rows.apply_rows(rows_path, server_db) == 0

    def test_rejects_a_payload_that_is_not_a_json_array(self, tmp_path):
        rows_path = str(tmp_path / "rows.json")
        server_db = str(tmp_path / "server_shadow.db")
        with open(rows_path, "w", encoding="utf-8") as fh:
            json.dump({"not": "a list"}, fh)
        assert apply_shadow_rows.apply_rows(rows_path, server_db) == 1

    def test_rejects_a_row_missing_required_keys(self, tmp_path):
        rows_path = str(tmp_path / "rows.json")
        server_db = str(tmp_path / "server_shadow.db")
        with open(rows_path, "w", encoding="utf-8") as fh:
            json.dump([{"run_id": "run-z", "match_key": "a|b|1"}], fh)  # missing kickoff_ts/outcome/etc.
        assert apply_shadow_rows.apply_rows(rows_path, server_db) == 1

    def test_rejects_a_delivery_mixing_more_than_one_run_id(self, tmp_path):
        rows_path = str(tmp_path / "rows.json")
        server_db = str(tmp_path / "server_shadow.db")
        rows = [
            dict(run_id="run-1", run_at=1, **_row(1)),
            dict(run_id="run-2", run_at=2, **_row(2)),
        ]
        with open(rows_path, "w", encoding="utf-8") as fh:
            json.dump(rows, fh)
        assert apply_shadow_rows.apply_rows(rows_path, server_db) == 1

    def test_extra_columns_from_a_real_select_star_export_are_harmless(self, tmp_path):
        # export_shadow_rows.py does SELECT * - every row carries id, closing_*,
        # clv, etc. too. apply_shadow_rows.py must tolerate that, not reject it.
        rows_path = str(tmp_path / "rows.json")
        actions_db = str(tmp_path / "actions_shadow.db")
        server_db = str(tmp_path / "server_shadow.db")
        _seed_actions_db(actions_db, "run-real", 7000, n_rows=1)
        rows = export_shadow_rows.export_rows(actions_db, "run-real")
        assert "id" in rows[0] and "closing_captured_at" in rows[0]  # confirms SELECT * shape
        with open(rows_path, "w", encoding="utf-8") as fh:
            json.dump(rows, fh)

        assert apply_shadow_rows.apply_rows(rows_path, server_db) == 0
        store = ShadowStore(server_db)
        try:
            n = store.conn.execute("SELECT COUNT(*) FROM pro_value_rows WHERE run_id = ?", ("run-real",)).fetchone()[0]
        finally:
            store.close()
        assert n == 1

    def test_end_to_end_export_then_apply_round_trips_the_real_data(self, tmp_path):
        actions_db = str(tmp_path / "actions_shadow.db")
        server_db = str(tmp_path / "server_shadow.db")
        rows_path = str(tmp_path / "rows.json")
        _seed_actions_db(actions_db, "run-e2e", 8000, n_rows=3)

        export_shadow_rows.main(["export_shadow_rows.py", actions_db, "run-e2e", rows_path])
        apply_shadow_rows.main(["apply_shadow_rows.py", rows_path, server_db])

        store = ShadowStore(server_db)
        try:
            n = store.conn.execute("SELECT COUNT(*) FROM pro_value_rows WHERE run_id = ?", ("run-e2e",)).fetchone()[0]
        finally:
            store.close()
        assert n == 3
