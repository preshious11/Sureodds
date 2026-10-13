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
    state = p.quota_state()
    assert (state["monthly_entities_used"], state["monthly_entities_max"], state["exhausted"]) == (2505, 2500, True)
    assert state["level"] == 100 and state["generation_allowance_today"] == 0


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


def test_the_fixture_allowance_never_exceeds_what_is_left(db, tmp_path):
    from predbot.storage.db import Database
    base = load("sgo_event.json")
    base = base["data"][0] if "data" in base else base
    ev = copy.deepcopy(base)
    ev["leagueID"], ev["status"]["startsAt"] = "MLB", "2026-10-03T20:00:00Z"
    http = FakeHttp({"MLB": [ev]})
    p = provider(db, FakeProbe(usage(0, 2500)), http, leagues=("MLB",))      # plenty left: the 50-event limit applies
    p.fetch_events(START, END)
    asked = [c[1]["limit"] for c in http.calls if c[0] == "/events"]
    assert asked and max(asked) <= 50
    tight = FakeHttp({"MLB": [ev]})
    # 1,700 used: (2,500 - 1,700 - 600 held back for reserve / settlement / diagnostics) / 30 days = 6 a day
    p2 = provider(Database(str(tmp_path / "b.db")), FakeProbe(usage(1700, 2500)), tight, leagues=("MLB",))
    p2.fetch_events(START, END)
    assert max(c[1]["limit"] for c in tight.calls if c[0] == "/events") <= 6
    spent = provider(Database(str(tmp_path / "c.db")), FakeProbe(usage(2400, 2500)), FakeHttp({"MLB": [ev]}), leagues=("MLB",))
    spent.fetch_events(START, END)                                           # allowance 0: not one /events request
    assert not [c for c in spent.http.calls if c[0] == "/events"]


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
    # exactly ONE request: the 429 starts a cool-down that stops the other leagues (it used to be 3 attempts x leagues)
    assert len([c for c in http.calls if c[0] == "/events"]) == 1
    assert p.meter.in_cooldown()
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


@pytest.mark.parametrize('clock', [0., 1., 60., 1799., 1800., 100000.])
def test_first_usage_probe_is_independent_of_host_uptime(db, monkeypatch, clock):
    from types import SimpleNamespace
    from predbot.providers import sportsgameodds
    monkeypatch.setattr(sportsgameodds, 'time', SimpleNamespace(monotonic=lambda: clock))
    probe = FakeProbe(usage(2505, 2500))
    p = provider(db, probe, FakeHttp())
    assert p.monthly_entities() == (2505, 2500)
    assert p.quota_exhausted()
    assert probe.calls == 1


def test_real_probe_failure_at_clock_zero_keeps_the_full_cooldown(db, monkeypatch):
    from types import SimpleNamespace
    from predbot.providers import sportsgameodds
    clock = [0.]
    monkeypatch.setattr(sportsgameodds, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    probe = FakeProbe(error=ProviderError('401'))
    p = provider(db, probe, FakeHttp())
    assert p.monthly_entities() is None and probe.calls == 1
    probe.error, probe.payload = None, usage(2505, 2500)
    clock[0] = 1799.
    assert p.monthly_entities() is None and probe.calls == 1
    clock[0] = 1800.
    assert p.monthly_entities() == (2505, 2500) and probe.calls == 2


def test_429_forces_a_usage_refresh_even_on_a_fresh_host(db, monkeypatch):
    from types import SimpleNamespace
    from predbot.providers import sportsgameodds
    monkeypatch.setattr(sportsgameodds, 'time', SimpleNamespace(monotonic=lambda: 0.))
    probe = FakeProbe(error=ProviderError('401'))
    p = provider(db, probe, FakeHttp())
    assert p.monthly_entities() is None
    probe.error, probe.payload = None, usage(2505, 2500)
    p._on_rate_limited()
    assert probe.calls == 2
    assert p.meter.exhausted() and p.meter.in_cooldown()
    assert p.budget.is_exhausted('sportsgameodds')
