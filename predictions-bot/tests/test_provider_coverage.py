"""Provider-coverage completeness policy: required vs optional providers.

Policy under test (predbot/jobs/daily_picks.py):
- COMPLETE ⟺ at least one provider reported AND every REQUIRED provider
  (default: sharpapi) reported status 'complete'.
- Optional providers (SGO, Odds API by default) may be quota-gated, partial
  or failed without forcing PARTIAL; their state is recorded in
  diagnostics['provider_coverage'] instead.
- A usable backup card can satisfy the configured fallback policy; thin slates fail closed.
- Selection thresholds, pricing and settlement are untouched by this file.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest
from unittest.mock import Mock

from predbot.config import Config
from predbot.jobs import daily_picks
from predbot.storage.db import Database


def _chain(reports, order=("oddsapi", "sharpapi", "sportsgameodds")):
    chain = Mock()
    chain.collect.return_value = []
    chain.last_report = reports
    chain.providers = {}
    chain.order = list(order)
    return chain


def _run(reports, events=None, order=("oddsapi", "sharpapi", "sportsgameodds"), **cfg_kwargs):
    db = Database(":memory:")
    try:
        cfg = Config(timezone="UTC", **cfg_kwargs)
        now = datetime(2030, 6, 1, 10, 0, tzinfo=timezone.utc)
        chain = _chain(reports, order)
        chain.collect.return_value = events or []
        return daily_picks.build_picks(chain, db, cfg, now)
    finally:
        db.close()


def _ok(name="sharpapi"):
    return {"status": "complete", "events": {"football": 3}}


def test_sgo_quota_exhaustion_keeps_complete_with_degraded_record():
    """The October 12 staging case: SGO quota-gated, SharpAPI + Odds API fine."""
    ps = _run({"oddsapi": _ok("oddsapi"), "sharpapi": _ok(),
               "sportsgameodds": {"status": "partial", "quota": {"exhausted": True}}})
    assert ps.complete is True
    cov = ps.diagnostics["provider_coverage"]
    assert cov["required"] == ["sharpapi"]
    assert cov["missing_required"] == []
    assert cov["optional_degraded"] == {"sportsgameodds": "partial"}
    assert cov["complete"] is True


def test_failed_oddsapi_is_optional_by_default():
    ps = _run({"oddsapi": {"status": "failed", "error_type": "ProviderError"},
               "sharpapi": _ok(), "sportsgameodds": _ok("sportsgameodds")})
    assert ps.complete is True
    assert ps.diagnostics["provider_coverage"]["optional_degraded"] == {"oddsapi": "failed"}


def test_failed_sharpapi_forces_partial():
    ps = _run({"oddsapi": _ok("oddsapi"), "sharpapi": {"status": "failed"},
               "sportsgameodds": _ok("sportsgameodds")})
    assert ps.complete is False
    assert ps.diagnostics["provider_coverage"]["missing_required"] == ["sharpapi"]


def test_all_providers_failed_is_partial():
    ps = _run({"sharpapi": {"status": "failed"}, "sportsgameodds": {"status": "failed"}})
    assert ps.complete is False


def test_no_provider_reports_is_partial():
    ps = _run({})
    assert ps.complete is False


def test_custom_required_set_is_honored():
    ps = _run({"sharpapi": _ok(), "sportsgameodds": _ok("sportsgameodds")},
              required_providers=["sharpapi", "oddsapi"])
    assert ps.complete is False  # oddsapi absent from reports: fails closed
    assert ps.diagnostics["provider_coverage"]["missing_required"] == ["oddsapi"]


def test_unknown_required_name_fails_closed_with_name_visible():
    ps = _run({"sharpapi": _ok()}, order=("sharpapi", "bogus"),
              required_providers=["sharpapi", "bogus"])
    assert ps.complete is False
    assert "bogus" in ps.diagnostics["provider_coverage"]["missing_required"]


def test_required_outside_run_set_is_skipped_not_failed():
    """Mock/test doubles named after sports (not providers) must not force
    PARTIAL: required names outside the run's provider set are skipped."""
    ps = _run({"football": {"status": "complete"}, "baseball": {"status": "complete"}},
              order=("football", "baseball"))
    assert ps.complete is True
    assert ps.diagnostics["provider_coverage"]["missing_required"] == []


def test_all_complete_is_complete_with_empty_degraded():
    ps = _run({"oddsapi": _ok("oddsapi"), "sharpapi": _ok(), "sportsgameodds": _ok("sportsgameodds")})
    assert ps.complete is True
    cov = ps.diagnostics["provider_coverage"]
    assert cov["missing_required"] == [] and cov["optional_degraded"] == {}


@pytest.mark.parametrize("sharp_status,backup_status,count,enabled,usable,expected", [
    ("partial", "complete", 20, True, True, True),
    ("failed", "complete", 20, True, True, True),
    ("partial", "complete", 19, True, True, False),
    ("partial", "partial", 20, True, True, False),
    ("partial", "complete", 20, False, True, False),
    ("complete", "complete", 20, True, True, True),
    ("partial", "complete", 20, True, False, False),
])
def test_fallback_uses_real_collection_selection(monkeypatch, sharp_status, backup_status,
                                                  count, enabled, usable, expected):
    from tests.real_events import real_ev
    from tests.test_picker import sc
    now = datetime(2030, 6, 1, 10, tzinfo=timezone.utc)
    events = [real_ev(i, home=f"Club {chr(65+i)}", away=f"Team {chr(65+i)}",
                      start=now + timedelta(hours=4)) for i in range(count)]
    # Identity matching is tested separately; keep every synthetic fixture distinct.
    for i, event in enumerate(events):
        event.canonical_event_id = f"fixture-{i}"
    monkeypatch.setattr(daily_picks, "resolve_events_detailed", lambda batch, *args: (batch, []))
    monkeypatch.setattr(daily_picks, "score_events", lambda batch, *args: [
        sc(e, "result3", "home", .76, 1.40) for e in batch])
    ps = _run({"sharpapi": {"status": sharp_status}, "oddsapi": {"status": backup_status}},
              events=events, provider_fallback_enabled=enabled,
              top_min_prob=.62 if usable else .99, daily_min_prob=.55 if usable else .99)
    assert len(ps.predictions) == count
    assert ps.complete is expected
    cov = ps.diagnostics["provider_coverage"]
    fallback = expected and sharp_status != "complete"
    assert bool(cov.get("fallback_used")) is fallback
    assert cov["complete"] is expected
    assert ps.diagnostics["complete"] is expected
    if fallback:
        assert cov["fallback_reason"] == {"sharpapi": sharp_status}
        assert cov["complete_providers"] == ["oddsapi"]


def test_sharpapi_stop_reasons_are_in_coverage():
    report = {"status": "partial", "sport_status": {
        "football": {"status": "partial", "stop_reason": {"soccer": "max_pages"}}},
        "fetch": {"soccer": {"stop_reason": "max_pages"}}}
    ps = _run({"sharpapi": report})
    assert ps.diagnostics["provider_coverage"]["sharpapi"] == report
