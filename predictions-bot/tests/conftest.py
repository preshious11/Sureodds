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
