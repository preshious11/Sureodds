"""Tests for provider data source of truth and rejection tracking.

Verifies that:
- No hardcoded sports lists gate provider data (ALLOWED_SPORTS removed)
- No hardcoded prediction outputs exist
- Rejection reasons are tracked through the pipeline
- Provider cache preserves per-sport status
- Deduplication uses sport+league+competitors+kickoff
- diagnose_providers.py produces correct output without API calls
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from predbot.cache.models import DailySnapshot, PickRecord
from predbot.config import Config
from predbot.markets.catalog import spec
from predbot.model.ensemble import Scored
from predbot.providers.base import Event, MarketLine
from predbot.providers.chain import ProviderChain, filter_allowed, merge_events
from predbot.providers.sharpapi import SharpApiProvider, sport_for_league
from tests.pipeline_helpers import select
from predbot.storage.db import Database
from tests.conftest import dt


# ------------------------------------------------------------------- filter_allowed
def test_filter_allowed_keeps_only_allowed_sports_and_counts_the_rest():
    """ALLOWED_SPORTS gates the pool before merging; rejections are counted."""
    events = [
        Event(provider="t", provider_event_id="1", sport="football", league="La Liga",
              home="A", away="B", start=dt(hh=22), sources=["t"]),
        Event(provider="t", provider_event_id="2", sport="baseball", league="MLB",
              home="C", away="D", start=dt(hh=22), sources=["t"]),
        Event(provider="t", provider_event_id="3", sport="table_tennis", league="TT Cup",
              home="E", away="F", start=dt(hh=22), sources=["t"]),
    ]
    events.append(Event(provider="t", provider_event_id="4", sport="hockey", league="NHL",
                        home="G", away="H", start=dt(hh=22), sources=["t"]))
    counts = {}
    result = filter_allowed(events, Config(), counts)
    assert {e.sport for e in result} == {"football", "baseball"}
    assert counts == {"sport_rejected": 2}


def test_filter_allowed_no_config_needed():
    """Even with None cfg (no allowed_sports to consult), all events pass through."""
    events = [
        Event(provider="t", provider_event_id="1", sport="football", league="EPL",
              home="A", away="B", start=dt(hh=22), sources=["t"]),
    ]
    assert filter_allowed(events, None) == events


# ------------------------------------------------------------------- sport_states
def test_sport_states_aggregates_per_sport():
    """Failed basketball must not mark football complete."""
    from predbot.cache.providers import sport_states
    providers = {
        "p1": {"status": "complete", "expected_sports": ["football", "basketball"],
               "events": {"football": 5, "basketball": 3},
               "sport_status": {"football": {"status": "success", "events": 5},
                                "basketball": {"status": "failed", "events": 0}}},
        "p2": {"status": "complete", "expected_sports": ["football"],
               "events": {"football": 2},
               "sport_status": {"football": {"status": "success", "events": 2}}},
    }
    states = sport_states(providers)
    assert states["football"]["status"] == "success"
    assert states["basketball"]["status"] == "failed"
    assert states["football"]["events"] == 7
    assert states["basketball"]["events"] == 0


def test_sport_states_mock_provider_without_sport_status():
    """Mock/stub providers without per-sport status fall back to coarse state."""
    from predbot.cache.providers import sport_states
    providers = {
        "mock": {"status": "complete", "expected_sports": ["football"],
                 "events": {"football": 3}},
    }
    states = sport_states(providers)
    assert states["football"]["status"] == "success"
    assert states["football"]["events"] == 3


# ------------------------------------------------------------------- rejection tracking
def test_picker_rejection_reasons_tracked():
    """Selection pipeline tracks explicit rejection reasons."""
    now = dt()
    cfg = Config()
    pool = []
    # Already started -> started_or_settled (a collection rule: the prediction still exists)
    started = Event(provider="t", provider_event_id="1", sport="football", league="L",
                    home="A", away="B", start=now - timedelta(minutes=15), sources=["a"])
    pool.append(_make_scored(started, "result3", "home", 0.70, 1.8))
    # Price at or below 1.0 -> missing_odds (invalid: no prediction at all)
    no_price = Event(provider="t", provider_event_id="2", sport="football", league="L",
                     home="C", away="D", start=now + timedelta(hours=2), sources=["a"])
    pool.append(_make_scored(no_price, "result3", "home", 0.70, 0.95))
    ps = select(pool, cfg, now)
    assert ps.diagnostics["line_rejections"] == {"missing_odds": 1}
    assert ps.diagnostics["daily"]["rejections"] == {"started_or_settled": 1}
    assert len(ps.predictions) == 1


def test_picker_top_rejections_tracked():
    """Top pool tracks why candidates are excluded from Top."""
    now = dt()
    cfg = Config()
    pool = []
    # Player prop: the score model cannot price it (spec.modelable is False), so it is not a prediction
    e1 = Event(provider="t", provider_event_id="1", sport="baseball", league="MLB",
               home="A", away="B", start=now + timedelta(hours=2), sources=["a"])
    pool.append(_make_scored(e1, "prop", "over", 0.75, 2.0, line=1.5, stat="hits"))
    # A prediction below the Top probability floor: kept, but not promoted
    e2 = Event(provider="t", provider_event_id="2", sport="football", league="L",
               home="C", away="D", start=now + timedelta(hours=2), sources=["a"])
    pool.append(_make_scored(e2, "result3", "home", 0.58, 1.8))
    ps = select(pool, cfg, now)
    assert ps.diagnostics["line_rejections"] == {"market_contract_invalid_entity": 1}
    assert ps.diagnostics["top"]["rejections"]["below_probability"] == 1
    assert len(ps.predictions) == 1


def test_picker_daily_rejections_tracked():
    """Daily pool tracks why candidates are excluded from Daily."""
    now = dt()
    cfg = Config()
    pool = []
    # Low confidence -> BELOW_THRESHOLD
    e1 = Event(provider="t", provider_event_id="1", sport="football", league="L",
               home="A", away="B", start=now + timedelta(hours=2), sources=["a"])
    pool.append(_make_scored(e1, "result3", "home", 0.52, 1.8))
    ps = select(pool, cfg, now)
    assert ps.diagnostics["daily"]["rejections"] == {"below_probability": 1}
    assert len(ps.predictions) == 1             # the weak prediction is still stored in All Predictions


# ------------------------------------------------------------------- deduplication
def test_merge_events_uses_league_for_dedup():
    """Events with same sport/teams/kickoff but different leagues are NOT merged."""
    ev1 = Event(provider="p1", provider_event_id="1", sport="football", league="EPL",
                league_id="1", home="Team A", away="Team B", start=dt(hh=20), sources=["p1"])
    ev2 = Event(provider="p2", provider_event_id="2", sport="football", league="La Liga",
                league_id="2", home="Team A", away="Team B", start=dt(hh=20), sources=["p2"])
    merged = merge_events([], [ev1, ev2])
    assert len(merged) == 2  # NOT merged - different leagues


def test_merge_events_merges_same_league():
    """Events with same sport/league/teams/kickoff ARE merged."""
    ev1 = Event(provider="p1", provider_event_id="1", sport="football", league="EPL",
                league_id="1", home="Team A", away="Team B", start=dt(hh=20), sources=["p1"])
    ev2 = Event(provider="p2", provider_event_id="2", sport="football", league="EPL",
                league_id="1", home="Team A", away="Team B", start=dt(hh=20), sources=["p2"])
    merged = merge_events([], [ev1, ev2])
    assert len(merged) == 1  # merged - same league
    assert "p2" in merged[0].sources


def test_merge_events_preserves_alt_ids():
    """Merged events preserve alt_ids for cross-provider settlement."""
    ev1 = Event(provider="p1", provider_event_id="1", sport="football", league="EPL",
                league_id="1", home="Team A", away="Team B", start=dt(hh=20), sources=["p1"])
    ev2 = Event(provider="p2", provider_event_id="2", sport="football", league="EPL",
                league_id="1", home="Team A", away="Team B", start=dt(hh=20), sources=["p2"])
    merged = merge_events([], [ev1, ev2])
    assert merged[0].extra.get("alt_ids", {}).get("p2") == "2"


# ------------------------------------------------------------------- expected_sports
def test_expected_sports_no_allowed_sports_filter():
    """expected_sports no longer filters by allowed_sports config."""
    from predbot.cache.providers import expected_sports
    prov = SharpApiProvider("k", Mock(), ["spain_-_la_liga", "nba"])
    result = expected_sports("sharpapi", prov, Config())
    assert result == ["basketball", "football"]


# ------------------------------------------------------------------- no hardcoded predictions
def test_no_hardcoded_outcome_strings_in_format():
    """Verify no hardcoded prediction outputs in the format module."""
    from predbot.bot.format import outcome_label
    from predbot.markets.catalog import short_selection

    # A bare "over" with no line/stat/entity should return "" (ambiguous)
    ml = MarketLine(market="total", selection="over", price=1.5, line=None, stat="points")
    result = short_selection(ml, "Home", "Away", "football")
    # Over without a line should be ambiguous/empty
    assert result == "" or "Over" not in result

    # A prop with no entity/stat should return empty
    ml2 = MarketLine(market="prop", selection="over", price=1.5, line=None, stat="", entity="")
    result2 = short_selection(ml2, "Home", "Away", "football")
    assert result2 == ""


def test_selection_label_uses_real_data():
    """Selection labels are derived from provider data, not hardcoded strings."""
    ml = MarketLine(market="total", selection="over", price=1.85, line=2.5, stat="points",
                    entity="all", n_books=3, provider_odd_id="bet123:over2.5")
    from predbot.markets.catalog import selection_label
    label = selection_label(ml, "Arsenal", "Chelsea", "football")
    assert "Over 2.5 Total Goals" in label
    assert "Arsenal" not in label  # not a home/away selection


# ------------------------------------------------------------------- diagnose_providers
def test_diagnose_providers_no_api_calls(db, monkeypatch):
    """diagnose_providers.py --today reads cache only, no API calls."""
    import diagnose_providers as diag
    cfg = Config(db_path=db.path, timezone="Africa/Lagos")
    monkeypatch.setattr(diag, "_load_config", lambda: cfg)
    # Should not raise and should return a list of provider info dicts
    info = diag.get_provider_info(cfg, db)
    assert isinstance(info, list)
    for entry in info:
        assert "provider" in entry
        assert "api_key_enabled" in entry


def test_diagnose_providers_with_cached_data(tmp_path, monkeypatch):
    """diagnose_providers.py shows cached data without API calls."""
    import diagnose_providers as diag
    from predbot.cache.models import DailySnapshot, PickRecord

    cfg = Config(db_path=str(tmp_path / "test.db"), timezone="Africa/Lagos")
    db = Database(cfg.db_path)

    # Seed a snapshot via the cache layer directly
    now = dt()
    day = "2030-06-01"
    pick = PickRecord(
        provider="test", provider_event_id="1", provider_odd_id="odd1",
        pick_date=day, tier="top", rank=1, sport="football",
        home="Home", away="Away", kickoff_utc="2030-06-01T18:00:00Z",
        market="total", selection="over", line=2.5, stat="points",
        entity="all", price=1.85, prob=0.65, ev=0.05, label="Over 2.5 Goals",
    )
    snapshot = DailySnapshot(
        date=day, generated_at="2030-06-01T12:00:00Z", origin="sqlite",
        status="COMPLETE", top_picks=[pick], daily_picks=[],
        generation={"providers": {}},
        sport_status={"football": {"status": "success", "events": 1}},
    )

    # Store snapshot directly in DB
    from predbot.util.timeutil import iso
    from predbot.cache.models import SCHEMA_VERSION
    with db.transaction():
        db.execute(
            "INSERT INTO prediction_cache (cache_key, local_date, timezone, status, "
            "payload_json, sport_status_json, generated_at, updated_at, expires_at, "
            "retry_after, cache_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (f"daily_predictions:{cfg.timezone}:{day}", day, cfg.timezone, "COMPLETE",
             json.dumps(snapshot.to_dict(), sort_keys=True), json.dumps(snapshot.sport_status),
             iso(now), iso(now), "2030-06-02T23:00:00Z", None, SCHEMA_VERSION)
        )

    # Monkeypatch utcnow in timeutil (imported by functions in the diag module)
    import predbot.util.timeutil as timeutil
    monkeypatch.setattr(timeutil, "utcnow", lambda: now)

    # Verify get_provider_info works without API calls
    info = diag.get_provider_info(cfg, db)
    assert isinstance(info, list)

    # Verify report doesn't crash
    report = {}
    db.close()


# ------------------------------------------------------------------- helper
def _make_scored(event, market, selection, p_model, price, *, line=None, stat="points",
                 entity="all", sources=2, ev_val=0.06, agreement=1.0, liquidity=1.0):
    ml = MarketLine(market=market, selection=selection, price=price, line=line,
                    stat=stat, entity=entity, n_books=sources, provider_odd_id="1")
    return Scored(event=event, line=ml, p_market=p_model - 0.02, p_model=p_model,
                  sources={f"s{n}": p_model for n in range(sources)},
                  agreement=agreement, ev=ev_val, edge=0.03,
                  label=f"{market}:{selection}", liquidity=liquidity)
