"""Stage 2 run-once wrapper tests. All offline: no provider or Telegram calls."""
import json
import os
import subprocess
import sys

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
