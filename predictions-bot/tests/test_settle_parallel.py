"""H16: settlement looks results up with one thread per first-choice source (football-data's pacing overlaps ESPN's),
each source is asked by one thread at a time, and the graded outcome is exactly what a serial run gives."""
from __future__ import annotations

import json
import threading
import time

from predbot.config import Config
from predbot.jobs import settle
from predbot.providers.base import MatchResult
from predbot.providers.chain import ResultsResolver
from tests.conftest import dt


class Source:
    """A results source that takes ``delay`` seconds per distinct (competition, day) request, like a paced API."""

    def __init__(self, name, delay, score):
        self.name, self.delay, self.score = name, delay, score
        self.requests, self.inside, self.max_inside, self._l = [], 0, 0, threading.Lock()
        self._seen = {}

    def begin_run(self):
        self._seen = {}

    def find_result(self, sport, home, away, start, league_id="", league=""):
        with self._l:
            self.inside += 1
            self.max_inside = max(self.max_inside, self.inside)
        try:
            key = (league_id, start.date().isoformat())
            if key not in self._seen:
                time.sleep(self.delay)
                self.requests.append(key)
                self._seen[key] = True
            return MatchResult(status="final", scores={"full": self.score}, source=self.name)
        finally:
            with self._l:
                self.inside -= 1


def _pick(db, i, sport, league_id, home):
    db.insert_pick({"pick_date": "2030-06-01", "tier": "daily", "rank": i, "provider": "sharpapi", "provider_event_id": f"e{i}",
                    "sport": sport, "league": "L", "home": home, "away": f"Away{i}", "start_utc": "2030-06-01T12:00:00Z",
                    "market": "result3" if sport == "football" else "result2", "period": "full", "stat": "points", "entity": "all",
                    "line": None, "selection": "home", "label": "x", "price": 1.5, "prob": 0.7, "ev": 0.05, "p_market": 0.6,
                    "sources": json.dumps({"_league_id": league_id, "_sport_key": "", "_alt_ids": {}}), "provider_odd_id": ""})


def _setup(db, monkeypatch):
    monkeypatch.setattr(settle, "utcnow", lambda: dt(d=1, hh=23))  # within each sport's settlement grace
    for i in range(4):
        _pick(db, i, "football", "england_-_premier_league", f"Home{i}")      # football-data first, ESPN second
    for i in range(4, 7):
        _pick(db, i, "baseball", "mlb", f"Home{i}")                           # ESPN only
    fd, espn = Source("football_data", 0.4, (2, 1)), Source("espn", 0.4, (3, 1))
    return ResultsResolver({"football_data": fd, "espn": espn}), fd, espn


def _statuses(db):
    return sorted((r["id"], r["status"]) for r in db.query("SELECT id, status FROM picks"))


def test_sources_are_asked_in_parallel_and_the_outcome_equals_a_serial_run(db, monkeypatch, tmp_path):
    from predbot.storage.db import Database
    resolver, fd, espn = _setup(db, monkeypatch)
    t0 = time.perf_counter()
    counts = settle.run(resolver, db, Config())
    parallel = time.perf_counter() - t0
    assert parallel < 0.7, f"serial would take 0.8 s, took {parallel:.2f}"
    assert counts["settled"] == 7
    assert len(fd.requests) == 1 and len(espn.requests) == 1                    # 4 (and 3) picks -> ONE request each
    assert fd.max_inside == 1 and espn.max_inside == 1                           # one thread per source at a time

    db2 = Database(str(tmp_path / "serial.db"))                                  # the same day, prefetch switched off
    monkeypatch.setattr(settle, "_prefetch", lambda *a, **k: None)
    resolver2, fd2, espn2 = _setup(db2, monkeypatch)
    settle.run(resolver2, db2, Config())
    assert _statuses(db) == _statuses(db2)
    assert len(fd2.requests) == 1 and len(espn2.requests) == 1


def test_a_failing_lookup_is_asked_once_not_twice(db, monkeypatch):
    from predbot.util.http import ProviderError
    resolver, fd, espn = _setup(db, monkeypatch)
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise ProviderError("429")

    fd.find_result = boom
    settle.run(resolver, db, Config())
    assert len(calls) == 4                                       # once per pick (its own lookup), never repeated by the grading pass
