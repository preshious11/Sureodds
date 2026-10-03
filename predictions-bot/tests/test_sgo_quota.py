"""SportsGameOdds' own monthly entity quota: read it, respect it, and fail fast when it is gone.

Regression: with the monthly entity cap used up (2,505 of 2,500) SGO answered every request ``429 Rate limit
exceeded``. The HTTP layer retried that with 6-36 s sleeps for every league, so generation stalled for ~291 s and
then failed - while our own object counter (1,000/month) never noticed."""
from __future__ import annotations

import copy

import pytest

from predbot.providers.budget import Budget
from predbot.providers.sportsgameodds import SportsGameOddsProvider
from predbot.util.http import ProviderError
from predbot.util.timeutil import parse_iso
from tests.conftest import load

START, END = parse_iso("2026-10-03T00:00:00Z"), parse_iso("2026-10-04T12:00:00Z")


def usage(used, cap):
    return {"success": True, "data": {"rateLimits": {"per-month": {"max-requests": "unlimited",
                                                                   "max-entities": cap, "current-entities": used}}}}


class FakeProbe:
    def __init__(self, payload=None, error=None):
        self.payload, self.error, self.calls = payload, error, 0

    def get(self, path, params=None):
        self.calls += 1
        assert path == "/account/usage"
        if self.error:
            raise self.error
        return self.payload


class FakeHttp:
    def __init__(self, events=None, rate_limited=False):
        self.calls, self.events, self.rate_limited = [], events or {}, rate_limited

    def get(self, path, params=None):
        self.calls.append((path, params))
        if path == "/leagues":
            return {"data": [{"leagueID": lg, "sportID": "BASEBALL"} for lg in ("MLB", "NBA", "NFL", "MLS")]}
        if self.rate_limited:
            raise ProviderError("rate limited")
        return {"success": True, "data": self.events.get(params.get("leagueID"), [])}


def provider(db, probe, http, leagues=("MLB", "NBA", "NFL", "MLS")):
    p = SportsGameOddsProvider("k", Budget(db), [], list(leagues), event_limit=50)
    p._probe, p.http = probe, http
    return p


def test_the_real_monthly_quota_is_read_from_account_usage(db):
    p = provider(db, FakeProbe(usage(2505, 2500)), FakeHttp())
    assert p.monthly_entities() == (2505, 2500)
    assert p.quota_exhausted() is True
    assert p.quota_state() == {"monthly_entities_used": 2505, "monthly_entities_max": 2500, "exhausted": True}


def test_an_exhausted_quota_fails_fast_without_calling_events(db):
    http = FakeHttp()
    p = provider(db, FakeProbe(usage(2505, 2500)), http)
    assert p.fetch_events(START, END) == []
    assert [c for c in http.calls if c[0] == "/events"] == []              # not one wasted (or sleeping) request
    assert p.last_fetch_complete is False
    assert p.budget.is_exhausted("sportsgameodds")                         # remembered for the rest of the day


def test_the_last_entities_are_kept_for_settlement(db):
    p = provider(db, FakeProbe(usage(2480, 2500)), FakeHttp())             # 20 left, reserve is 25
    assert p.quota_exhausted() is True


def test_the_fixture_allowance_never_exceeds_what_is_left(db):
    base = load("sgo_event.json")
    base = base["data"][0] if "data" in base else base
    ev = copy.deepcopy(base)
    ev["leagueID"], ev["status"]["startsAt"] = "MLB", "2026-10-03T20:00:00Z"
    http = FakeHttp({"MLB": [ev]})
    p = provider(db, FakeProbe(usage(2400, 2500)), http, leagues=("MLB",))   # 100 left - 25 reserve = 75: the 50-event limit still applies
    p.fetch_events(START, END)
    asked = [c[1]["limit"] for c in http.calls if c[0] == "/events"]
    assert asked and max(asked) <= 50
    tight = FakeHttp({"MLB": [ev]})
    p2 = provider(db, FakeProbe(usage(2460, 2500)), tight, leagues=("MLB",))  # 40 left - 25 reserve = 15
    p2.fetch_events(START, END)
    assert max(c[1]["limit"] for c in tight.calls if c[0] == "/events") <= 15


def test_an_unreadable_usage_endpoint_changes_nothing(db):
    p = provider(db, FakeProbe(error=ProviderError("401")), FakeHttp())
    assert p.monthly_entities() is None and p.quota_exhausted() is False
    assert p.quota_state()["exhausted"] is False


def test_a_plan_without_an_entity_cap_is_not_limited(db):
    payload = {"success": True, "data": {"rateLimits": {"per-month": {"max-entities": "unlimited"}}}}
    assert provider(db, FakeProbe(payload), FakeHttp()).monthly_entities() is None


def test_the_usage_answer_is_cached_so_it_is_not_asked_per_lookup(db):
    probe = FakeProbe(usage(10, 2500))
    p = provider(db, probe, FakeHttp())
    for _ in range(5):
        p.quota_exhausted()
    assert probe.calls == 1


def test_repeated_429s_stop_the_run_instead_of_sleeping_through_every_league(db):
    http = FakeHttp(rate_limited=True)
    p = provider(db, FakeProbe(usage(10, 2500)), http)                    # quota fine, but SGO still says 429
    with pytest.raises(ProviderError):
        p.fetch_events(START, END)
    assert len([c for c in http.calls if c[0] == "/events"]) == 2         # two leagues, then it gives up (4 configured)
    assert p.last_fetch_complete is False


def test_settlement_lookups_skip_sgo_when_its_quota_is_gone(db):
    http = FakeHttp()
    p = provider(db, FakeProbe(usage(2505, 2500)), http)
    assert p.fetch_result("evt-1") is None
    assert p.find_result("baseball", "A", "B", START) is None
    assert [c for c in http.calls if c[0] == "/events"] == []


def test_quota_state_is_a_plain_dict_for_the_provider_report(db):
    import json
    json.dumps(provider(db, FakeProbe(usage(1, 2500)), FakeHttp()).quota_state())
