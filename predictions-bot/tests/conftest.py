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
