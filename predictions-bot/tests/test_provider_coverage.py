"""Provider-coverage completeness policy: required vs optional providers.

Policy under test (predbot/jobs/daily_picks.py):
- COMPLETE ⟺ at least one provider reported AND every REQUIRED provider
  (default: sharpapi) reported status 'complete'.
- Optional providers (SGO, Odds API by default) may be quota-gated, partial
  or failed without forcing PARTIAL; their state is recorded in
  diagnostics['provider_coverage'] instead.
- Unknown/absent required names fail closed (PARTIAL with the name visible).
- Selection thresholds, pricing and settlement are untouched by this file.
"""
from __future__ import annotations

from datetime import datetime, timezone
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


def _run(reports, order=("oddsapi", "sharpapi", "sportsgameodds"), **cfg_kwargs):
    db = Database(":memory:")
    try:
        cfg = Config(timezone="UTC", **cfg_kwargs)
        now = datetime(2030, 6, 1, 10, 0, tzinfo=timezone.utc)
        return daily_picks.build_picks(_chain(reports, order), db, cfg, now)
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
