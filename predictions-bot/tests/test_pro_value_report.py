"""Weekly shadow report: read-only, and each go-live criterion is judged by the rule in PRO_VALUE_GO_LIVE.md."""
import sqlite3
import time

import pytest

from predbot.pro_value import report
from predbot.pro_value.store import ShadowStore

DAY = 86400


def _ts(y, m, d):
    import datetime as dt
    return int(dt.datetime(y, m, d, tzinfo=dt.timezone.utc).timestamp())


NOW = _ts(2026, 11, 20)
RESET_TS = _ts(2026, 11, 1)


def _seed(path, clvs, kickoff_offset_days=-1, captured=True, stale=False):
    store = ShadowStore(path)
    rows = []
    for i, clv in enumerate(clvs):
        rows.append({"match_key": f"m{i}", "league": "x", "home": "a", "away": "b",
                     "kickoff_ts": NOW + kickoff_offset_days * DAY, "outcome": "home", "sporty_odds": 2.0,
                     "sporty_fetched_at": NOW - 100, "fair_probability": 0.5, "fair_book": "pinnacle",
                     "sharp_fetched_at": NOW - (3 * 3600 if stale else 60), "edge": 0.03, "second_book": None,
                     "second_edge": None, "status": "selected", "reason": None, "rank": i})
    store.insert_rows("r1", NOW - 60, rows)
    for i, clv in enumerate(clvs):
        if captured and clv is not None:
            store.set_closing(i + 1, 0.5, 2.0, NOW - 10, clv)
    store.add_spent("2026-11-18", 3)
    store.close()


def test_report_is_read_only(tmp_path):
    path = str(tmp_path / "s.db")
    _seed(path, [0.02, -0.01])
    report.weekly_report(path, NOW, reset_ts=RESET_TS)
    con = report._connect_ro(path)  # the connection the report itself uses
    with pytest.raises(sqlite3.OperationalError):
        con.execute("DELETE FROM pro_value_rows")
    con.close()
    check = sqlite3.connect(path)
    assert check.execute("SELECT COUNT(*) FROM pro_value_rows").fetchone()[0] == 2  # nothing was removed
    check.close()


def test_criteria_met_only_when_all_hold(tmp_path):
    path = str(tmp_path / "s.db")
    _seed(path, [0.02] * 40)  # 40 picks, all positive CLV, all captured, fresh quotes
    r = report.weekly_report(path, NOW, reset_ts=RESET_TS, remaining=212)
    c = r["criteria"]
    assert c["sample_and_time"]["met"] is True
    assert c["average_clv_above_zero"]["met"] is True
    assert c["more_than_half_beat_close"]["met"] is True
    assert c["data_quality"]["met"] is True
    assert r["credits_spent_total"] == 3 and r["credits_remaining"] == 212


def test_too_early_and_too_few_picks_fail_the_sample_gate(tmp_path):
    path = str(tmp_path / "s.db")
    _seed(path, [0.02] * 40)
    early = report.weekly_report(path, _ts(2026, 11, 10), reset_ts=RESET_TS)
    assert early["criteria"]["sample_and_time"]["met"] is False  # 40 picks but before 15 Nov
    few = str(tmp_path / "few.db")
    _seed(few, [0.02] * 5)
    assert report.weekly_report(few, NOW, reset_ts=RESET_TS)["criteria"]["sample_and_time"]["met"] is False


def test_missed_capture_and_stale_quote_fail_data_quality(tmp_path):
    missed = str(tmp_path / "missed.db")
    _seed(missed, [0.02, None], captured=False)
    assert report.weekly_report(missed, NOW, reset_ts=RESET_TS)["criteria"]["data_quality"]["met"] is False
    stale = str(tmp_path / "stale.db")
    _seed(stale, [0.02], stale=True)
    assert report.weekly_report(stale, NOW, reset_ts=RESET_TS)["criteria"]["data_quality"]["met"] is False


def test_render_names_every_criterion(tmp_path):
    path = str(tmp_path / "s.db")
    _seed(path, [0.02, -0.01])
    text = report.render_markdown(report.weekly_report(path, NOW, reset_ts=RESET_TS))
    for label in ("Sample and time", "Average CLV", "More than half beat close", "Data quality",
                  "Bet of the Day", "mismatches", "does not change any setting"):
        assert label in text


class _EmptyClient:
    """The league response contains no matching event, so every pick is a closing mismatch."""

    def fetch_league_h2h(self, league, now_ts):
        return []


def test_unmatched_closing_capture_is_logged_and_fails_data_quality(tmp_path):
    from predbot.pro_value import closing
    path = str(tmp_path / "s.db")
    store = ShadowStore(path)
    store.insert_rows("r1", NOW - 60, [{"match_key": "arsenal|chelsea|1", "league": "soccer_epl", "home": "a",
                                        "away": "b", "kickoff_ts": NOW + 45 * 60, "outcome": "away",
                                        "sporty_odds": 2.0, "status": "selected", "rank": 1}])
    picks = store.open_pending_picks(NOW)
    counts = closing.capture_group(_EmptyClient(), "soccer_epl", picks, store, NOW)
    store.close()
    assert counts["not_found"] == 1 and counts["captured"] == 0
    r = report.weekly_report(path, NOW, reset_ts=RESET_TS)
    assert r["mismatches_total"] == 1 and r["mismatch_kinds"] == {"closing_not_found": 1}
    assert r["criteria"]["data_quality"]["met"] is False


def _botd_runs(path, runs):
    store = ShadowStore(path)
    for run_id, flags in runs:
        for i, lost in enumerate(flags):
            store.insert_botd_compare(run_id, f"{run_id}-{i}", bool(lost), None)
    store.close()


def test_top_candidate_lost_on_exactly_one_run_in_four_passes(tmp_path):
    path = str(tmp_path / "s.db")
    _botd_runs(path, [("a", [1, 0]), ("b", [0, 1]), ("c", [0, 0]), ("d", [0, 0])])  # top lost 1 of 4 runs
    r = report.weekly_report(path, NOW, reset_ts=RESET_TS)["criteria"]["botd_not_regularly_lost_to_pro"]
    assert r["top_lost_share"] == 0.25 and r["met"] is True


def test_top_candidate_lost_on_more_than_one_run_in_four_fails(tmp_path):
    path = str(tmp_path / "s.db")
    _botd_runs(path, [("a", [1, 0]), ("b", [0, 1]), ("c", [0, 0]), ("d", [1, 0])])  # top lost 2 of 4 runs
    r = report.weekly_report(path, NOW, reset_ts=RESET_TS)["criteria"]["botd_not_regularly_lost_to_pro"]
    assert r["top_lost_share"] == 0.5 and r["met"] is False


def test_botd_criterion_has_no_verdict_without_runs(tmp_path):
    path = str(tmp_path / "s.db")
    _seed(path, [0.02])
    r = report.weekly_report(path, NOW, reset_ts=RESET_TS)["criteria"]["botd_not_regularly_lost_to_pro"]
    assert r["met"] is None
