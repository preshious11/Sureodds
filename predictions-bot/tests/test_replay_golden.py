"""The gates for every change: one recorded day of provider data replayed through the whole pipeline (no network).

* ``golden_slate.json``  - which picks, their order, prices, labels, deliveries counts and diagnostics (timings
  excepted). Byte-identical for any presentation or performance change; re-record only for an intended
  change to what is picked or how a team is named:  python tools/replay.py --record slate
* ``golden_render.json`` - every text a reader sees (cards, views, pages, deliveries, recap, stats, /yesterday,
  empty states). Re-record for an intended presentation change:  python tools/replay.py --record render

Float policy (see tests/golden_compare.py): floats compare with rel_tol=1e-12
(platform libm exp/log/lgamma drift by ~1 ulp between Windows and Linux -
measured decision margins are >= 1e-6, so no selection can flip). Every
non-float leaf, every key set, every list length and every ordering is exact."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests.golden_compare import assert_golden_equal

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import replay                                                 # noqa: E402


def test_replay_reproduces_the_golden_slate():
    result = replay.run(send_delay=0.0)
    assert_golden_equal(replay.golden_text(result, "slate") + "\n",
                        replay.GOLDEN_SLATE.read_text(encoding="utf-8"), label="slate")


def test_replay_reproduces_the_golden_render():
    result = replay.run(send_delay=0.0)
    assert_golden_equal(replay.golden_text(result, "render") + "\n",
                        replay.GOLDEN_RENDER.read_text(encoding="utf-8"), label="render")


def test_replay_is_offline_and_every_prediction_is_one_row_per_event_and_family():
    """Top and Daily are views over the same stored prediction rows: one stored row per canonical event x market family
    (a fixture holds several), memberships that never copy a row, and no network."""
    slate = replay.run(send_delay=0.0)["slate"]
    diag = slate["diagnostics"]
    rows = slate["stored_picks"]
    assert len(rows) == len({(r["canonical_event_id"], r["market"]) for r in rows}) == diag["valid_predictions_generated"]
    assert len({r["canonical_event_id"] for r in rows}) == diag["fixtures_with_predictions"]
    assert len(rows) > diag["fixtures_with_predictions"]                       # several predictions per fixture
    assert diag["stored_all_predictions"] == diag["all_predictions"] == len(rows)
    assert {r["tier"] for r in rows} <= {"top", "daily", "all"}
    assert diag["merged_events"] >= 15
    assert slate["delivery"]["second_pass"] == 0                # deliveries are once per subscriber per day
