"""Shared test fixtures and helpers."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import pytest

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def load(name: str):
    with open(os.path.join(FIX, name), "r", encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture
def db(tmp_path):
    from predbot.storage.db import Database
    return Database(str(tmp_path / "test.db"))


@pytest.fixture
def budget(db):
    from predbot.providers.budget import Budget
    return Budget(db)


def dt(y=2030, m=6, d=1, hh=18, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _classic_button_navigation(request, monkeypatch):
    """The suite predates VIEW_NAVIGATION=new: its fakes only implement ``edit_message_text`` for buttons.
    They keep pinning the classic ``edit`` behaviour (still supported); tests/test_view_navigation.py covers all
    three modes, and the shipped default, with explicit settings; the replay golden runs on the shipped default."""
    if request.module.__name__.rsplit(".", 1)[-1] not in ("test_view_navigation", "test_replay_golden"):
        from predbot.bot import callbacks
        monkeypatch.setattr(callbacks, "navigation_mode", lambda cfg: "edit")


@pytest.fixture(autouse=True)
def _no_live_sgo_quota_probe(request, monkeypatch):
    """SportsGameOddsProvider asks SGO's /account/usage for its real monthly quota. Tests must never reach the network
    for that: by default the probe reports 'unknown' (no cap), exactly as when the endpoint cannot be read.
    tests/test_sgo_quota.py exercises the real method with a fake client."""
    if request.module.__name__.rsplit(".", 1)[-1] in ("test_sgo_quota", "test_sgo_usage_control"):
        return
    from predbot.providers.sportsgameodds import SportsGameOddsProvider
    monkeypatch.setattr(SportsGameOddsProvider, "monthly_entities", lambda self: None)


@pytest.fixture(autouse=True)
def _sgo_origin_is_scheduled(monkeypatch):
    """cli.main() / the diagnose scripts tag SGO requests via PREDBOT_SGO_ORIGIN; a test that runs one must not leave
    that behind for the next test (manual/diagnostic origins are throttled earlier by the quota policy)."""
    monkeypatch.delenv("PREDBOT_SGO_ORIGIN", raising=False)
