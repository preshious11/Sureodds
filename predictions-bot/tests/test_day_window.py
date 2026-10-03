"""The day window (local midnight + DAY_WINDOW_END_OFFSET_HOURS) must be the same
on the cached and uncached paths, and the prior-day dedupe must use the fuzzy
fixture matcher - without ever dropping a genuine rematch at another time."""
from __future__ import annotations

from datetime import timedelta

import pytest

from predbot.cache import providers as cache_providers
from predbot.config import Config
from predbot.jobs import daily_picks
from predbot.providers.base import Event
from predbot.providers.chain import ProviderChain
from predbot.util.timeutil import day_bounds
from tests.conftest import dt
from predbot.providers.base import MarketLine
from tests.test_picker import sc

TZ = "Africa/Lagos"          # UTC+1, so local midnight == 23:00 UTC
NOW = dt(hh=9)               # 10:00 Lagos on 2030-06-01


class WindowedProvider:
    """Like a real provider: returns only what falls inside the window it is asked for."""
    name = "fake"

    def __init__(self, events):
        self.events, self.windows = events, []

    def enabled(self):
        return True

    def fetch_events(self, start, end):
        self.windows.append((start, end))
        return [e for e in self.events if start <= e.start < end]


def _event(pid, home, away, start, provider="fake"):
    e = Event(provider=provider, provider_event_id=pid, sport="football", league="England - Premier League",
              home=home, away=away, start=start, sources=[provider])
    e.markets = [MarketLine("result3", "home", 1.5, n_books=5)]
    return e


EVENING = _event("e1", "Evening Town", "Dusk United", dt(hh=20))          # 21:00 Lagos
LATE = _event("l1", "Night Owls", "Midnight Rangers", dt(hh=23, mm=30))   # 00:30 Lagos NEXT day


@pytest.fixture
def scored_events(monkeypatch):
    """Capture whatever build_picks hands to the model."""
    seen = []

    def fake_score(events, db, cfg, independent=None):
        seen.extend(events)
        return [sc(e, "result3", "home", 0.7, 1.45) for e in events]

    monkeypatch.setattr(daily_picks, "score_events", fake_score)
    monkeypatch.setattr(cache_providers, "utcnow", lambda: NOW)
    return seen


def _chain(db, cfg, events, cached=True):
    prov = WindowedProvider(events)
    chain = ProviderChain({"fake": prov}, ["fake"], 1)
    chain.db, chain.cfg = (db if cached else None), cfg
    return chain, prov


@pytest.mark.parametrize("offset,late_included", [(3, True), (0, False)])
def test_kickoff_after_local_midnight_is_fetched_only_inside_the_configured_window(
        db, scored_events, offset, late_included):
    cfg = Config(timezone=TZ, day_window_end_offset_hours=offset)
    chain, prov = _chain(db, cfg, [EVENING, LATE])

    daily_picks.build_picks(chain, db, cfg, NOW)

    names = {e.home for e in scored_events}
    assert "Evening Town" in names
    assert ("Night Owls" in names) is late_included


def test_cached_and_uncached_paths_ask_providers_for_the_same_window_end(db, scored_events):
    cfg = Config(timezone=TZ, day_window_end_offset_hours=3)
    expected_end = day_bounds(NOW, TZ)[1] + timedelta(hours=3)

    cached_chain, cached_prov = _chain(db, cfg, [EVENING, LATE], cached=True)
    daily_picks.build_picks(cached_chain, db, cfg, NOW)
    uncached_chain, uncached_prov = _chain(db, cfg, [EVENING, LATE], cached=False)
    daily_picks.build_picks(uncached_chain, db, cfg, NOW)

    assert cached_prov.windows[-1][1] == expected_end
    assert uncached_prov.windows[-1][1] == expected_end


# ------------------------------------------------------- prior-day dedupe
def _store_pick(db, day, home, away, start):
    db.insert_pick({"pick_date": day, "tier": "daily", "rank": 1, "provider": "sharpapi",
                    "provider_event_id": "x", "sport": "football", "league": "L", "home": home,
                    "away": away, "start_utc": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "market": "result3", "period": "full", "stat": "points", "entity": "all",
                    "line": None, "selection": "home", "label": "x", "price": 1.5, "prob": 0.7,
                    "ev": 0.0, "p_market": 0.6})


def test_prior_day_dedupe_uses_the_fuzzy_matcher(db, scored_events):
    """Yesterday's stored pick has different provider naming ("Arsenal FC") from
    today's event ("Arsenal") and a kickoff 40 minutes apart - an exact string
    match misses it, the shared fuzzy matcher must not."""
    cfg = Config(timezone=TZ, day_window_end_offset_hours=3)
    _store_pick(db, "2030-05-31", "Arsenal FC", "Chelsea", dt(hh=20))
    repeat = _event("r1", "Arsenal", "Chelsea", dt(hh=20, mm=40))
    other = _event("o1", "Porto", "Braga", dt(hh=20))
    chain, _ = _chain(db, cfg, [repeat, other])

    daily_picks.build_picks(chain, db, cfg, NOW)

    names = {e.home for e in scored_events}
    assert "Arsenal" not in names and "Porto" in names


def test_prior_day_dedupe_never_drops_a_rematch_at_another_time(db, scored_events):
    """Same teams, but the stored pick is a months-old fixture (the exact-string,
    all-time dedupe used to drop this) - a rematch is a different fixture."""
    cfg = Config(timezone=TZ, day_window_end_offset_hours=3)
    _store_pick(db, "2030-03-10", "Arsenal", "Chelsea", dt(m=3, d=10, hh=20))
    _store_pick(db, "2030-05-31", "Arsenal", "Chelsea", dt(d=31, m=5, hh=12))   # yesterday, but 32h before today's kickoff
    rematch = _event("r1", "Arsenal", "Chelsea", dt(hh=20))
    chain, _ = _chain(db, cfg, [rematch])

    daily_picks.build_picks(chain, db, cfg, NOW)

    assert "Arsenal" in {e.home for e in scored_events}


def test_previous_pick_fixtures_is_bounded_and_carries_kickoff(db):
    _store_pick(db, "2030-05-31", "A", "B", dt(d=31, m=5, hh=20))
    _store_pick(db, "2030-05-30", "C", "D", dt(d=30, m=5, hh=20))
    _store_pick(db, "2030-03-01", "E", "F", dt(d=1, m=3, hh=20))    # far outside the lookback
    _store_pick(db, "2030-06-01", "G", "H", dt(hh=20))              # today, not "previous"

    rows = db.previous_pick_fixtures("2030-06-01")

    assert {(r["home"], r["away"]) for r in rows} == {("A", "B"), ("C", "D")}
    assert all(r["start_utc"] for r in rows)
