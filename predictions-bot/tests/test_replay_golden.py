"""The gate for every performance change: one recorded day of provider data replayed through the whole
pipeline (no network) must reproduce tests/fixtures/replay/golden.json byte for byte - picks, order, prices,
labels, cards, /top and /daily views, deliveries and diagnostics (timing fields excepted).

Re-record ONLY for an intended behaviour change:  python tools/replay.py --record"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import replay                                                 # noqa: E402


def test_replay_reproduces_the_golden_slate_byte_for_byte():
    result = replay.run(send_delay=0.0)
    assert replay.golden_text(result) + "\n" == replay.GOLDEN.read_text(encoding="utf-8")


def test_replay_is_offline_and_cross_tier_counters_are_zero():
    slate = replay.run(send_delay=0.0)["slate"]
    diag = slate["diagnostics"]
    assert not any(k.startswith("CROSS_TIER") for k in diag["selection_rejections"])
    assert diag["merged_events"] >= 15
    assert slate["delivery"]["second_pass"] == 0                # deliveries are once per subscriber per day
