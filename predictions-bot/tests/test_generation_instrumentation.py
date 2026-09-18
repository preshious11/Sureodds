"""What a generation cycle records so it can be read back later: API calls spent
this cycle, the Odds API quota gate's state, and a persistent log file."""
from __future__ import annotations

import logging
from datetime import timedelta

import pytest

from predbot.cache import providers as cache_providers
from predbot.config import Config
from predbot.jobs import daily_picks
from predbot.log import setup_logging
from predbot.providers.base import BaseProvider
from predbot.providers.budget import Budget
from predbot.providers.chain import ProviderChain
from tests.conftest import dt
from tests.test_oddsapi import PRIORITY_FOOTBALL, _alloc_provider

NOW = dt(hh=12)


@pytest.fixture(autouse=True)
def _frozen(monkeypatch):
    monkeypatch.setattr(cache_providers, "utcnow", lambda: NOW)


class SpendingProvider(BaseProvider):
    """Stands in for API-Football: every fetch spends a known number of calls."""
    name = "api_football"
    daily_limit = 100

    def __init__(self, budget, calls):
        self.budget, self.calls = budget, calls

    def fetch_events(self, start, end):
        self.budget.spend(self.name, self.calls)
        return []


def test_generation_records_the_api_calls_this_cycle_spent(db, monkeypatch):
    monkeypatch.setattr(daily_picks, "score_events", lambda *a, **k: [])
    budget = Budget(db)
    budget.spend("api_football", 30)                     # earlier cycles today
    chain = ProviderChain({"api_football": SpendingProvider(budget, 7)}, ["api_football"], 1)
    chain.db, chain.cfg = db, Config(timezone="UTC")

    ps = daily_picks.build_picks(chain, db, Config(timezone="UTC"), NOW)

    assert ps.diagnostics["api_usage"] == {"api_football": {"cycle": 7, "today": 37, "limit": 100}}


def test_a_cache_hit_cycle_reports_zero_api_calls(db, monkeypatch):
    """The provider snapshot is reused on the second cycle: zero calls, and the
    accounting must say so rather than repeating the first cycle's number."""
    monkeypatch.setattr(daily_picks, "score_events", lambda *a, **k: [])
    budget = Budget(db)
    prov = SpendingProvider(budget, 7)
    chain = ProviderChain({"api_football": prov}, ["api_football"], 1)
    chain.db, chain.cfg = db, Config(timezone="UTC")
    daily_picks.build_picks(chain, db, Config(timezone="UTC"), NOW)

    ps = daily_picks.build_picks(chain, db, Config(timezone="UTC"), NOW)

    assert ps.diagnostics["api_usage"]["api_football"]["cycle"] == 0
    assert ps.diagnostics["api_usage"]["api_football"]["today"] == 7


def test_provider_report_carries_the_odds_api_quota_and_the_refusal(db):
    p, requested = _alloc_provider(Budget(db), {k: 6 for k in PRIORITY_FOOTBALL}, {"tennis_atp_us_open": 3})
    p.requests_remaining = 10                            # x-requests-remaining seen from the headers
    chain = ProviderChain({"oddsapi": p}, ["oddsapi"], 1)
    chain.db, chain.cfg = db, Config(timezone="UTC")

    chain.collect(NOW, NOW + timedelta(hours=12))

    quota = chain.last_report["oddsapi"]["quota"]
    assert requested == []                               # nothing was spent...
    assert quota["refused_to_spend"] is True             # ...because the gate refused
    assert quota["requests_remaining"] == 10 and quota["reserve_credits"] == 50
    assert "refused to spend" in quota["refusal"]


def test_a_healthy_odds_api_quota_reports_no_refusal(db):
    p, requested = _alloc_provider(Budget(db), {"soccer_epl": 4}, {})
    p.requests_remaining = 400
    chain = ProviderChain({"oddsapi": p}, ["oddsapi"], 1)
    chain.db, chain.cfg = db, Config(timezone="UTC")

    chain.collect(NOW, NOW + timedelta(hours=12))

    assert requested == ["soccer_epl"]
    assert chain.last_report["oddsapi"]["quota"]["refused_to_spend"] is False


# ------------------------------------------------------------------ log file
@pytest.fixture
def restore_logging():
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    for h in root.handlers[:]:
        if h not in handlers:
            h.close()
    root.handlers[:] = handlers
    root.setLevel(level)


def test_setup_logging_writes_a_persistent_file(tmp_path, restore_logging):
    path = tmp_path / "predbot.log"

    setup_logging("INFO", str(path))
    logging.getLogger("predbot.test").info("Cache MISS | date=2030-06-01")
    for h in logging.getLogger().handlers:
        h.flush()

    assert "Cache MISS | date=2030-06-01" in path.read_text(encoding="utf-8")


def test_an_unwritable_log_path_never_stops_the_bot(tmp_path, restore_logging, capsys):
    setup_logging("INFO", str(tmp_path / "missing_dir" / "predbot.log"))     # must not raise

    logging.getLogger("predbot.test").info("still logging to stdout")

    assert "could not open log file" in capsys.readouterr().err


def test_log_file_defaults_next_to_the_database_and_can_be_switched_off(monkeypatch):
    monkeypatch.delenv("LOG_FILE", raising=False)
    monkeypatch.setenv("DB_PATH", "/var/data/predbot.db")
    assert Config().log_file.replace("\\", "/") == "/var/data/predbot.log"
    monkeypatch.setenv("LOG_FILE", "none")
    assert Config().log_file == ""
