"""H1: providers that need a refresh are fetched concurrently; the merged pool is still built in PROVIDER_ORDER."""
from __future__ import annotations

import time
from datetime import datetime, timezone

from predbot.config import Config
from predbot.providers.base import Event, MarketLine
from predbot.providers.chain import ProviderChain
from tests.conftest import dt


class Slow:
    def __init__(self, name, delay, home, fail=False):
        self.name, self.delay, self.home, self.fail = name, delay, home, fail
        self.last_fetch_complete = True
        self.fetch_counts, self.normalization_counts, self.parse_counts = {}, {}, {}
        self.calls = 0
        self.leagues, self.sports = [], ["football"]

    def enabled(self):
        return True

    def fetch_events(self, start, end):
        self.calls += 1
        time.sleep(self.delay)
        if self.fail:
            raise RuntimeError("boom")
        return [Event(provider=self.name, provider_event_id=f"{self.name}-1", sport="football", league="England - Premier League", home=self.home,
                      away=f"{self.home} Visitors", start=dt(hh=22), markets=[MarketLine("result3", "home", 2.0)], sources=[self.name])]


def _chain(db, providers, order):
    chain = ProviderChain({p.name: p for p in providers}, order)
    chain.db, chain.cfg = db, Config()
    return chain


def test_two_stale_providers_are_fetched_at_the_same_time_and_merged_in_provider_order(db):
    a, b = Slow("sharpapi", 0.4, "Alpha"), Slow("sportsgameodds", 0.4, "Bravo")
    chain = _chain(db, [a, b], ["sharpapi", "sportsgameodds"])
    t0 = time.perf_counter()
    pool = chain.collect(dt(hh=0), dt(hh=23, mm=59))
    wall = time.perf_counter() - t0
    assert wall < 0.7, f"serial would take 0.8s, took {wall:.2f}s"
    assert [e.home for e in pool] == ["Alpha", "Bravo"]                     # provider order, not finish order
    assert (a.calls, b.calls) == (1, 1)
    assert set(chain.last_timings) == {"sharpapi", "sportsgameodds"}


def test_a_failing_provider_neither_delays_nor_breaks_the_other(db):
    good, bad = Slow("sharpapi", 0.05, "Alpha"), Slow("sportsgameodds", 0.3, "Bravo", fail=True)
    chain = _chain(db, [good, bad], ["sharpapi", "sportsgameodds"])
    pool = chain.collect(dt(hh=0), dt(hh=23, mm=59))
    assert [e.home for e in pool] == ["Alpha"]
    assert chain.last_report["sharpapi"]["outcome"] == "success" and chain.last_report["sportsgameodds"]["outcome"] == "failed"


def test_a_provider_with_a_fresh_cache_row_is_not_fetched_again(db):
    a, b = Slow("sharpapi", 0.01, "Alpha"), Slow("sportsgameodds", 0.01, "Bravo")
    chain = _chain(db, [a, b], ["sharpapi", "sportsgameodds"])
    chain.collect(dt(hh=0), dt(hh=23, mm=59))
    chain.collect(dt(hh=0), dt(hh=23, mm=59))
    assert (a.calls, b.calls) == (1, 1)
