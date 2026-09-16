"""BetMiner fetch_events: auth headers, budget, filtering, failure semantics.

Every test is fixture/mock driven - no live request is ever made.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone

import pytest

from predbot.providers.betminer import BetMinerProvider
from predbot.providers.budget import Budget
from predbot.util.http import Http, ProviderError

from tests.conftest import load

START = datetime(2030, 5, 31, 23, 0, tzinfo=timezone.utc)
END = datetime(2030, 6, 1, 23, 0, tzinfo=timezone.utc)


class FakeHttp(Http):
    def __init__(self, result=None, error=None):
        super().__init__("https://fake")
        self.result, self.error, self.calls = result, error, []

    def get(self, path, params=None):
        self.calls.append((path, params))
        if self.error:
            raise self.error
        return copy.deepcopy(self.result)


def make(db, result=None, error=None, **kw):
    p = BetMinerProvider("k", Budget(db), timezone="UTC", **kw)
    p.http = FakeHttp(result=result, error=error)
    return p


def payload():
    return load("betminer_matches.json")


# ------------------------------------------------------------------ transport
def test_rapidapi_auth_headers_are_set():
    p = BetMinerProvider("secret-key", None, host="betminer.p.rapidapi.com")
    assert p.http.session.headers["x-rapidapi-key"] == "secret-key"
    assert p.http.session.headers["x-rapidapi-host"] == "betminer.p.rapidapi.com"
    assert p.http.base_url == "https://betminer.p.rapidapi.com"


def test_single_broad_range_call_per_fetch(db):
    p = make(db, result=payload())
    p.fetch_events(START, END)
    assert len(p.http.calls) == 1
    path, params = p.http.calls[0]
    assert path == "/matches/2030-05-31/2030-06-01"
    assert params is None


# ---------------------------------------------------------------------- fetch
def test_fetch_events_filters_and_prices(db):
    p = make(db, result=payload())
    events = p.fetch_events(START, END)

    names = {e.name for e in events}
    # Arsenal (full odds) and Inter (partial odds) survive.
    assert names == {"Arsenal vs Chelsea", "Inter vs Napoli"}
    assert "Real Madrid vs Sevilla" not in names       # no odds -> no markets
    assert "Hobro vs Vejle" not in names               # league not configured
    assert "Bayern Munich vs Borussia Dortmund" not in names   # bad kickoff
    assert all(e.sport == "football" for e in events)
    assert p.last_fetch_complete is True
    assert p.fetch_counts["raw_matches"] == 5
    assert p.fetch_counts["league_rejected"] == 1
    assert p.fetch_counts["no_odds"] == 1
    assert p.fetch_counts["bad_kickoff"] == 1


def test_no_fabricated_odds(db):
    p = make(db, result=payload())
    for e in p.fetch_events(START, END):
        for m in e.markets:
            assert isinstance(m.price, float) and m.price > 1.0
            assert m.book_prices == [m.price]


def test_out_of_window_matches_are_dropped(db):
    p = make(db, result=payload())
    events = p.fetch_events(datetime(2030, 6, 1, 19, 0, tzinfo=timezone.utc), END)
    assert [e.name for e in events] == ["Inter vs Napoli"]    # Arsenal 18:00 dropped
    assert p.fetch_counts["out_of_window"] >= 1


# ------------------------------------------------------------------- failures
def test_http_failure_propagates_as_provider_error(db):
    p = make(db, error=ProviderError("429 rate limited"))
    with pytest.raises(ProviderError):
        p.fetch_events(START, END)
    assert Budget(db).used_today("betminer") == 1      # the attempt was counted


def test_malformed_envelope_is_failed_not_empty(db):
    p = make(db, result={"nope": True})
    with pytest.raises(ProviderError):
        p.fetch_events(START, END)
    assert p.last_fetch_complete is False


def test_success_false_is_failed_not_empty(db):
    p = make(db, result={"success": False, "data": []})
    with pytest.raises(ProviderError):
        p.fetch_events(START, END)
    assert p.last_fetch_complete is False


def test_empty_data_is_complete_and_empty(db):
    p = make(db, result={"success": True, "data": []})
    assert p.fetch_events(START, END) == []
    assert p.last_fetch_complete is True


def test_unparseable_kickoff_does_not_kill_the_fetch(db):
    data = payload()
    data["data"][0]["kickoff"] = "not-a-timestamp"
    p = make(db, result=data)
    events = p.fetch_events(START, END)
    assert p.last_fetch_complete is True
    assert "Arsenal vs Chelsea" not in {e.name for e in events}
    assert p.fetch_counts["bad_kickoff"] == 2
