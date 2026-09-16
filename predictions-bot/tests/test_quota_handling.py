"""Free-tier quota handling: exhaustion marks stop doomed calls for the day."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from predbot.providers.api_football import ApiFootballProvider
from predbot.providers.budget import Budget
from predbot.providers.sportsgameodds import SportsGameOddsProvider
from predbot.util.http import Http, ProviderError


class FakeHttp(Http):
    """Replays canned responses / errors instead of hitting the network."""
    def __init__(self, result=None, error=None):
        super().__init__("https://fake")
        self.result, self.error, self.calls = result, error, []

    def get(self, path, params=None):
        self.calls.append((path, params))
        if self.error:
            raise ProviderError(self.error)
        return self.result


def apif(http, db):
    p = ApiFootballProvider("k", Budget(db))
    p.http = http
    return p


def sgo(http, db, leagues=None):
    p = SportsGameOddsProvider("k", Budget(db), sports=["SOCCER"], leagues=leagues or [])
    p.http = http
    return p


START = datetime(2030, 6, 1, 12, 0, tzinfo=timezone.utc)
END = datetime(2030, 6, 1, 23, 0, tzinfo=timezone.utc)


def test_apif_quota_error_marks_exhausted_and_short_circuits(db):
    http = FakeHttp(result={"errors": {"requests": "You have reached the request limit "
                                                "for the day, Go to https://dashboard.api-football.com"}})
    p = apif(http, db)

    assert p.fetch_events(START, END) == []
    assert not p.last_fetch_complete

    b = Budget(db)
    assert b.is_exhausted("api_football")
    n = len(http.calls)
    assert p.fetch_events(START, END) == []    # short-circuits before the network
    assert len(http.calls) == n                # no extra HTTP call


def test_sgo_free_tier_requires_leagues_and_marks_exhausted(db):
    http = FakeHttp(result={"data": []})
    p = sgo(http, db, leagues=[])

    out = p.fetch_events(START, END)           # no doomed request, no exception

    assert out == [] and http.calls == []
    assert Budget(db).is_exhausted("sportsgameodds")


def test_sgo_leagueid_error_marks_exhausted_for_the_day(db):
    http = FakeHttp(error='400 from https://api.sportsgameodds.com/v2/events: '
                          '{"error":"You must specify either a leagueID or an eventID '
                          'at this subscription tier"}')
    p = sgo(http, db, leagues=["ESPR_1"])

    with pytest.raises(ProviderError):
        p.fetch_events(START, END)

    assert Budget(db).is_exhausted("sportsgameodds")


def test_sgo_drops_leagues_not_covered_by_the_tier(db):
    http = FakeHttp(result={"data": []})
    # /leagues says the tier covers LA_LIGA only - EPL must be dropped
    http.get_results = [{"data": [{"leagueID": "LA_LIGA"}]}, {"data": []}]

    def get(path, params=None):
        http.calls.append((path, params))
        return http.get_results.pop(0)

    http.get = get
    p = sgo(http, db, leagues=["EPL", "LA_LIGA"])
    out = p.fetch_events(START, END)

    assert out == []
    events_call = [c for c in http.calls if c[0] == "/events"][0]
    assert events_call[1]["leagueID"] == "LA_LIGA"        # EPL filtered out, no 400


def test_sgo_all_leagues_unavailable_marks_exhausted(db):
    http = FakeHttp(result={"data": []})
    http.get_results = [{"data": [{"leagueID": "NBA"}]}, {"data": []}]

    def get(path, params=None):
        http.calls.append((path, params))
        return http.get_results.pop(0)

    http.get = get
    p = sgo(http, db, leagues=["EPL"])
    assert p.fetch_events(START, END) == []
    assert Budget(db).is_exhausted("sportsgameodds")
    assert all(c[0] != "/events" for c in http.calls)     # no doomed events call


def test_sgo_leagues_call_failure_falls_back_to_configured(db):
    http = FakeHttp(error="503 from /leagues")
    p = sgo(http, db, leagues=["EPL"])

    with pytest.raises(ProviderError):
        p.fetch_events(START, END)                        # /leagues failed -> still tries EPL

    assert any(c[0] == "/events" for c in http.calls)


def test_exhaustion_marks_are_scoped_to_the_day_bucket(db):
    b = Budget(db)
    b.mark_exhausted("api_football")
    day = b._day()
    assert db.budget_used("api_football:exhausted", day) == 1
    # a fresh UTC day uses a new bucket key, so the mark no longer applies
    assert db.budget_used("api_football:exhausted", "2030-01-01") == 0


def test_budget_marks_are_scoped_per_provider(db):
    b = Budget(db)
    b.mark_exhausted("api_football")
    assert b.is_exhausted("api_football")
    assert not b.is_exhausted("sportsgameodds")
