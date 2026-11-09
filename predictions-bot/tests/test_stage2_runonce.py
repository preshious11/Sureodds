"""Stage 2 run-once wrapper tests. All offline: no provider or Telegram calls."""
import json
import os
import subprocess
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from run_once import summarize_snapshot
from run_once import summarize_selection_candidates
from predbot.config import Config

RUN_ONCE = os.path.join(os.path.dirname(__file__), "..", "run_once.py")


def run(*args, env_extra=None):
    env = dict(os.environ)
    env.pop("PREDBOT_ALLOW_LIVE", None)
    env.update(env_extra or {})
    return subprocess.run([sys.executable, RUN_ONCE, *args], capture_output=True, text=True, env=env)


def test_live_requires_guard():
    proc = run("generate", "--live")
    assert proc.returncode == 2
    assert "PREDBOT_ALLOW_LIVE" in proc.stderr


def test_offline_settle_reports_lagos_times():
    proc = run("settle", "--offline")
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout)
    assert report["mode"] == "offline"
    assert report["timezone"] == "Africa/Lagos"
    assert report["post_time"] == "09:00"
    assert report["delivery_time"] == "09:30"
    assert report["recap_time"] == "07:45"


def test_offline_recap_reports_timing():
    proc = run("recap", "--offline")
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout)
    assert report["compute_wall_s"] >= 0


def test_offline_generate_replays_recorded_day():
    proc = run("generate", "--offline")
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout)
    assert report["valid_predictions_generated"] > 0
    assert report["fixtures_with_predictions"] > 0


def _snapshot(**kwargs):
    base = dict(status="PARTIAL", date="2026-10-13", top_picks=[1, 2],
                daily_picks=[1], all_predictions=[1, 2, 3], generation={})
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_summarize_snapshot_partial_with_degraded_coverage():
    snap = _snapshot(generation={
        "complete": False,
        "provider_coverage": {"required": ["sharpapi"], "missing_required": [],
                              "optional_degraded": {"sportsgameodds": "partial"},
                              "complete": False},
        "api_usage": {"sharpapi": {"today": 23}},
        "quota": {"requests_remaining": 475},
    })
    summary = summarize_snapshot(snap)
    assert summary["snapshot_status"] == "PARTIAL"
    assert summary["snapshot_date"] == "2026-10-13"
    assert (summary["top_picks"], summary["daily_picks"], summary["all_predictions"]) == (2, 1, 3)
    assert summary["provider_coverage"]["optional_degraded"] == {"sportsgameodds": "partial"}
    assert summary["error_type"] is None


def test_summarize_snapshot_never_leaks_secrets_or_internals():
    snap = _snapshot(status="COMPLETE", generation={
        "complete": True,
        "provider_coverage": {"required": ["sharpapi"], "missing_required": [],
                              "optional_degraded": {}, "complete": True},
        "api_key": "sk-live-SECRET",
        "token": "tok-SECRET",
        "payload_json": "huge",
    })
    text = json.dumps(summarize_snapshot(snap))
    assert "SECRET" not in text
    assert set(json.loads(text)) == {"snapshot_status", "snapshot_date", "top_picks",
                                     "daily_picks", "all_predictions", "selection",
                                     "provider_coverage", "error_type", "error_traceback"}


def test_summarize_snapshot_includes_allowlisted_selector_diagnostics():
    snap = _snapshot(status="COMPLETE", generation={
        "ticket_candidates": 490,
        "top": {"candidates": 0, "selected": 0, "rejections": {"below_probability": 456},
                "provider_payload": "must not appear"},
        "daily": {"candidates": 13, "selected": 0, "combined_odds": 1.92,
                  "reason": "target_unreachable", "rejections": {"price_range": 1}},
        "provider_coverage": {},
        "api_key": "sk-live-SECRET",
    })
    summary = summarize_snapshot(snap)
    assert summary["selection"] == {
        "ticket_candidates": 490,
        "top": {"candidates": 0, "selected": 0, "rejections": {"below_probability": 456}},
        "daily": {"candidates": 13, "selected": 0, "combined_odds": 1.92,
                  "reason": "target_unreachable", "rejections": {"price_range": 1}},
    }
    assert "SECRET" not in json.dumps(summary)


def test_summarize_selection_candidates_replays_the_stored_ticket_pool():
    rows = []
    for number in range(2):
        rows.append({
            "id": f"candidate-{number}",
            "pick_date": "2030-06-01",
            "fixture_key": f"fixture-{number}",
            "canonical_event_id": f"event-{number}",
            "status": "pending",
            "home": f"Home {number}",
            "away": f"Away {number}",
            "sport": "football",
            "league": f"League {number}",
            "start_utc": "2030-06-02T12:00:00Z",
            "market": "result3",
            "selection": "home",
            "period": "full",
            "stat": "points",
            "entity": "all",
            "line": None,
            "price": 1.5,
            "prob": 0.6,
            "ev": -0.1,
            "edge_ev": None,
            "rank_score": 0.6,
            "quality": "main",
            "books": 3,
            "sources": json.dumps({"_notes": {
                "evidence": {"lcb": 0.55},
                "independent": False,
                "edge": 0,
            }}),
        })

    class CandidateDb:
        def ticket_candidate_rows(self, day):
            assert day == "2030-06-01"
            return rows

    snapshot = SimpleNamespace(
        date="2030-06-01",
        generation={"generated_at": "2030-06-01T18:00:00Z"},
    )
    trace = summarize_selection_candidates(CandidateDb(), Config(), snapshot)

    assert trace["available"] is True
    assert trace["evaluated_at"] == "2030-06-01T18:00:00Z"
    assert trace["ticket_candidates"] == 2
    assert trace["top"]["rejections"] == {"below_probability": 2}
    assert trace["daily"]["reason"] == "target_unreachable"
    assert trace["daily"]["combined_odds"] == 2.25
    assert len(trace["daily_eligible_candidates"]) == 2
    assert "sources" not in trace["daily_eligible_candidates"][0]


def test_summarize_snapshot_carries_sanitized_traceback():
    snap = _snapshot(generation={"complete": False, "error_type": "AttributeError",
                                 "error_traceback": "daily_picks.py:166 in build_picks",
                                 "provider_coverage": {}})
    summary = summarize_snapshot(snap)
    assert summary["error_type"] == "AttributeError"
    assert summary["error_traceback"] == "daily_picks.py:166 in build_picks"
