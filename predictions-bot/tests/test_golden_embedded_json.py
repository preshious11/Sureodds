"""Embedded-JSON comparator tests: the `sources` DB column holds JSON inside
a string, so the structural comparator must look through it — but ONLY for
explicitly allowlisted fields. Everything else stays byte-strict."""
from __future__ import annotations

import json

import pytest

from tests.golden_compare import GoldenMismatch, assert_golden_equal

GOOD_SOURCES = json.dumps({
    "market": 0.6016161434761262,
    "score_model": 0.5956297492600446,
    "edge": -0.0011749189830230211,
    "evidence": {"books": 2, "quality": "secondary", "rank": 0.5626},
    "independent": [],
    "selection": "home",
})


def wrap(sources: str, label: str = "X"):
    return json.dumps({"stored_picks": [{"label": label, "sources": sources}]})


def test_ulp_drift_inside_sources_passes():
    drifted = GOOD_SOURCES.replace("0.5956297492600446", "0.5956297492600451")
    assert_golden_equal(wrap(drifted), wrap(GOOD_SOURCES))


def test_meaningful_numeric_change_inside_sources_fails():
    changed = GOOD_SOURCES.replace("0.5956297492600446", "0.59562975")
    with pytest.raises(GoldenMismatch):
        assert_golden_equal(wrap(changed), wrap(GOOD_SOURCES))
    changed_edge = GOOD_SOURCES.replace("-0.0011749189830230211", "-0.002")
    with pytest.raises(GoldenMismatch):
        assert_golden_equal(wrap(changed_edge), wrap(GOOD_SOURCES))


def test_ids_keys_selections_arrays_strings_inside_sources_fail():
    with pytest.raises(GoldenMismatch):
        assert_golden_equal(wrap(GOOD_SOURCES.replace('"secondary"', '"main"')),
                            wrap(GOOD_SOURCES))
    with pytest.raises(GoldenMismatch):
        assert_golden_equal(wrap(GOOD_SOURCES.replace('"selection": "home"', '"selection": "away"')),
                            wrap(GOOD_SOURCES))
    with pytest.raises(GoldenMismatch):
        assert_golden_equal(wrap(GOOD_SOURCES.replace('"independent": []', '"independent": [1]')),
                            wrap(GOOD_SOURCES))
    with pytest.raises(GoldenMismatch):
        assert_golden_equal(wrap(GOOD_SOURCES.replace('"rank": 0.5626', '"rank2": 0.5626')),
                            wrap(GOOD_SOURCES))


def test_malformed_embedded_json_fails_loudly():
    with pytest.raises(GoldenMismatch):
        assert_golden_equal(wrap(GOOD_SOURCES + "{"), wrap(GOOD_SOURCES))
    with pytest.raises(GoldenMismatch):
        assert_golden_equal(wrap("not json at all"), wrap(GOOD_SOURCES))
    # Identical malformed strings are identical output: no false alarm.
    assert_golden_equal(wrap("not json at all"), wrap("not json at all"))


def test_non_allowlisted_json_looking_strings_stay_strict():
    left = json.dumps({"note": '{"a": 1.000000000001}'})
    right = json.dumps({"note": '{"a": 1.0}'})
    with pytest.raises(GoldenMismatch):
        assert_golden_equal(left, right)
    assert_golden_equal(left, left)


def test_label_outside_sources_stays_strict():
    with pytest.raises(GoldenMismatch):
        assert_golden_equal(wrap(GOOD_SOURCES, label="Y"), wrap(GOOD_SOURCES, label="X"))
