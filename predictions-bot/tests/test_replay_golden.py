"""The gates for every change: one recorded day of provider data replayed through the whole pipeline (no network).

* ``golden_slate.json``  - which picks, their order, prices, labels, deliveries counts and diagnostics (timings
  excepted). Byte-identical for any presentation or performance change; re-record only for an intended
  change to what is picked or how a team is named:  python tools/replay.py --record slate
* ``golden_render.json`` - every text a reader sees (cards, views, pages, deliveries, recap, stats, /yesterday,
  empty states). Re-record for an intended presentation change:  python tools/replay.py --record render"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import replay                                                 # noqa: E402


def test_replay_reproduces_the_golden_slate_byte_for_byte():
    result = replay.run(send_delay=0.0)
    assert replay.golden_text(result, "slate") + "\n" == replay.GOLDEN_SLATE.read_text(encoding="utf-8")


def test_replay_reproduces_the_golden_render_byte_for_byte():
    result = replay.run(send_delay=0.0)
    assert replay.golden_text(result, "render") + "\n" == replay.GOLDEN_RENDER.read_text(encoding="utf-8")


def test_replay_is_offline_and_cross_tier_counters_are_zero():
    slate = replay.run(send_delay=0.0)["slate"]
    diag = slate["diagnostics"]
    # The selection layer refuses Daily legs that duplicate or nest inside a Top leg (CROSS_TIER_DUPLICATE / _NESTED are
    # those refusals and are expected). What must stay zero is the SAFETY NET: a Daily leg the selection layer let
    # through and the post-hoc invariant had to remove.
    assert "CROSS_TIER_DUPLICATE_REMOVED" not in diag["selection_rejections"]
    assert not diag.get("cross_tier_removed")
    assert diag["merged_events"] >= 15
    assert slate["delivery"]["second_pass"] == 0                # deliveries are once per subscriber per day
