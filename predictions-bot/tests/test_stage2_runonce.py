"""Stage 2 run-once wrapper tests. All offline: no provider or Telegram calls."""
import json
import os
import subprocess
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from run_once import summarize_snapshot

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
                                     "daily_picks", "all_predictions", "provider_coverage",
                                     "error_type", "error_traceback"}


def test_summarize_snapshot_carries_sanitized_traceback():
    snap = _snapshot(generation={"complete": False, "error_type": "AttributeError",
                                 "error_traceback": "daily_picks.py:166 in build_picks",
                                 "provider_coverage": {}})
    summary = summarize_snapshot(snap)
    assert summary["error_type"] == "AttributeError"
    assert summary["error_traceback"] == "daily_picks.py:166 in build_picks"
