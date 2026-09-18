"""API-Football: /predictions must be fetched, attached to events, and never
re-spent when a partial fetch is retried.

These drive the REAL fetch_events()/collect_persisted() with canned HTTP
responses (only the network layer is faked), so a bug anywhere on the
predictions path - a missing import, a broken cache write - fails here.
"""
from __future__ import annotations

from collections import Counter
from datetime import timedelta
from unittest.mock import Mock

import pytest

from predbot.cache import providers as cache_providers
from predbot.config import Config
from predbot.providers import api_football as apif_module
from predbot.providers.api_football import ApiFootballProvider
from predbot.providers.budget import Budget
from predbot.providers.chain import ProviderChain
from predbot.util.http import ProviderError
from tests.conftest import dt

NOW = dt(hh=12)                       # frozen "now": tests never depend on the real clock
KICKOFF = "2030-06-01T18:00:00Z"


def _fixture(fid, home, away):
    return {"fixture": {"id": fid, "date": KICKOFF, "status": {"short": "NS"}},
            "league": {"id": 1, "name": "League", "country": "X"},
            "teams": {"home": {"id": fid * 10, "name": home}, "away": {"id": fid * 10 + 1, "name": away}}}


def _odds_item(fid):
    return {"fixture": {"id": fid}, "bookmakers": [{"name": "Book", "bets": [
        {"id": 1, "name": "Match Winner", "values": [
            {"value": "Home", "odd": "2.0"}, {"value": "Draw", "odd": "3.4"}, {"value": "Away", "odd": "3.8"}]}]}]}


FIXTURES = [_fixture(101, "Alpha FC", "Beta United"),
            _fixture(102, "Gamma Rovers", "Delta Town"),
            _fixture(103, "Epsilon City", "Zeta Athletic")]


class FakeApi:
    """Canned API-Football. Counts real HTTP calls per path, so tests can
    assert how many /predictions requests were actually spent."""

    def __init__(self, odds_pages):
        self.calls = Counter()
        self.odds_pages = odds_pages          # list: per page -> list of fixture ids, or an Exception

    def get(self, path, params=None):
        self.calls[path] += 1
        if path == "/fixtures":
            return {"response": FIXTURES}
        if path == "/odds":
            page = params["page"]
            spec = self.odds_pages[page - 1] if page <= len(self.odds_pages) else []
            if isinstance(spec, Exception):
                raise spec
            return {"paging": {"total": len(self.odds_pages)}, "response": [_odds_item(f) for f in spec]}
        if path == "/predictions":
            return {"response": [{"predictions": {"winner": {"name": f"fixture-{params['fixture']}"}}}]}
        raise AssertionError(f"unexpected path {path}")


@pytest.fixture
def frozen_clock(monkeypatch):
    monkeypatch.setattr(apif_module, "utcnow", lambda: NOW)
    monkeypatch.setattr(cache_providers, "utcnow", lambda: NOW)


def _provider(db, api):
    p = ApiFootballProvider("key", Budget(db), timezone="UTC", predictions_per_run=25)
    p.http = Mock()
    p.http.rate = None
    p.http.get = Mock(side_effect=api.get)
    return p


def test_fetch_events_attaches_predictions_from_a_real_predictions_call(db, frozen_clock):
    api = FakeApi(odds_pages=[[101, 102, 103]])
    prov = _provider(db, api)

    events = prov.fetch_events(NOW, NOW + timedelta(hours=12))

    assert len(events) == 3
    assert all(e.extra.get("prediction") for e in events)
    assert api.calls["/predictions"] == 3


def test_partial_fetch_retry_spends_no_additional_predictions_calls(db, frozen_clock):
    """Original failure mode: page 3 of /odds errors, the cycle is PARTIAL, and
    the retry after CACHE_RETRY_SECONDS must NOT re-buy the predictions the
    first attempt already paid for."""
    api = FakeApi(odds_pages=[[101], [102], ProviderError("boom")])
    prov = _provider(db, api)
    cfg = Config(timezone="UTC")
    chain = ProviderChain({"api_football": prov}, ["api_football"], 1)
    chain.db, chain.cfg = db, cfg

    first = chain.collect(NOW, NOW + timedelta(hours=12))
    assert len(first) == 2
    assert chain.last_report["api_football"]["status"] == "partial"
    assert api.calls["/predictions"] == 2

    # Retry: page 3 recovers (nothing new on it). Force past the retry_after throttle.
    api.odds_pages = [[101], [102], []]
    chain.force_retry = True
    second = chain.collect(NOW, NOW + timedelta(hours=12))

    assert len(second) == 2
    assert all(e.extra.get("prediction") for e in second)
    assert api.calls["/predictions"] == 2, "retry re-spent /predictions calls it already paid for"
    assert chain.last_report["api_football"]["status"] == "complete"


def test_restarted_process_reuses_todays_predictions(db, frozen_clock):
    """A fresh provider instance (bot restart) on the same DB must also hit the cache."""
    api = FakeApi(odds_pages=[[101, 102, 103]])
    _provider(db, api).fetch_events(NOW, NOW + timedelta(hours=12))
    assert api.calls["/predictions"] == 3

    events = _provider(db, api).fetch_events(NOW, NOW + timedelta(hours=12))

    assert api.calls["/predictions"] == 3
    assert all(e.extra.get("prediction") for e in events)
