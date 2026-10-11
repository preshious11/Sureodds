"""Guards the float-tolerance contract in two directions.

1. The comparator itself: 1-ulp platform drift passes; anything at or above
   1e-9, any structural change, any reordering, any count or type change fails.
2. Decision margins on the recorded replay day: every selection threshold
   and rank gap must stay far above the tolerance, so a passing comparison
   can never hide a flipped pick. Measured 2026-10 (158 stored picks):
   prob margins >= 4.2e-04, ev floor >= 1.9e-04, rank gaps >= 9.8e-07.
   If a future fixture/code change pushes a margin under 1e-7, this fails
   loudly and the tolerance must be re-justified, not silently kept.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests.golden_compare import GoldenMismatch, assert_golden_equal

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import replay  # noqa: E402

#: Hard floor: 1,000x above the 1e-12 comparison tolerance and ~10x below the
#: tightest measured margin (rank gap 9.8e-07).
MARGIN_FLOOR = 1e-7


def test_one_ulp_drift_passes():
    assert_golden_equal('{"p": 0.5858900751146209}', '{"p": 0.5858900751146211}')
    assert_golden_equal('{"p": 0.5656255982863924}', '{"p": 0.5656255982863921}')


def test_meaningful_float_change_fails():
    with pytest.raises(GoldenMismatch):
        assert_golden_equal('{"p": 0.5858900751146211}', '{"p": 0.5858900751246211}')  # ~1e-11
    with pytest.raises(GoldenMismatch):
        assert_golden_equal('{"p": 0.55}', '{"p": 0.550000001}')  # 1e-9


def test_structure_is_exact():
    with pytest.raises(GoldenMismatch):
        assert_golden_equal('{"a": 1}', '{"a": 1, "b": 2}')          # extra key
    with pytest.raises(GoldenMismatch):
        assert_golden_equal('{"a": 1}', '{}')                        # missing key
    with pytest.raises(GoldenMismatch):
        assert_golden_equal('[1, 2]', '[2, 1]')                      # reorder
    with pytest.raises(GoldenMismatch):
        assert_golden_equal('[1, 2]', '[1, 2, 3]')                   # length
    with pytest.raises(GoldenMismatch):
        assert_golden_equal('{"n": 20}', '{"n": 21}')                # int count
    with pytest.raises(GoldenMismatch):
        assert_golden_equal('{"s": "a"}', '{"s": "b"}')              # string
    with pytest.raises(GoldenMismatch):
        assert_golden_equal('{"s": "1"}', '{"s": 1}')                # type change
    with pytest.raises(GoldenMismatch):
        assert_golden_equal('{"b": true}', '{"b": 1}')               # bool vs int
    assert_golden_equal('{"b": true}', '{"b": true}')


def test_full_slate_survives_one_ulp_platform_drift():
    """Simulate the Linux libm: bump EVERY float in the slate by 1 ulp toward
    +inf. The comparison must still pass with identical decisions."""
    import json
    import math

    import replay as _replay
    actual = _replay.golden_text(replay.run(send_delay=0.0), "slate")
    expected = _replay.GOLDEN_SLATE.read_text(encoding="utf-8")

    def bump(obj):
        if isinstance(obj, dict):
            return {k: bump(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [bump(v) for v in obj]
        if isinstance(obj, float):
            return math.nextafter(obj, math.inf)
        return obj

    drifted = json.dumps(bump(json.loads(actual)))
    assert_golden_equal(drifted, expected, label="slate+1ulp")


def test_decision_margins_stay_above_tolerance():
    """Replay-day margins must exceed MARGIN_FLOOR: thresholds 0.55 (Daily),
    0.62 (Top), 0.50 (LCB), ev floor -0.05, and every adjacent rank gap."""
    slate = replay.run(send_delay=0.0)["slate"]
    rows = slate["stored_picks"]
    assert len(rows) > 0
    min_prob = min(min(abs(r["prob"] - t) for t in (0.55, 0.62, 0.50)) for r in rows)
    assert min_prob > MARGIN_FLOOR, f"prob margin {min_prob:.3e} too close to tolerance"
    evs = [r["ev"] for r in rows if isinstance(r.get("ev"), (int, float))]
    assert evs
    min_ev = min(abs(e + 0.05) for e in evs)
    assert min_ev > MARGIN_FLOOR, f"ev margin {min_ev:.3e} too close to tolerance"
    scores = sorted((r.get("rank_score") or 0 for r in rows), reverse=True)
    gaps = [a - b for a, b in zip(scores, scores[1:]) if a != b]
    assert gaps
    assert min(gaps) > MARGIN_FLOOR, f"rank gap {min(gaps):.3e} too close to tolerance"
