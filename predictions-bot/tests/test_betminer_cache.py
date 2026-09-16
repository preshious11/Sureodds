"""BetMiner rides the generic SQLite provider cache: one call per day, zero on restart."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from predbot.cache.providers import collect_persisted, expected_sports
from predbot.config import Config
from predbot.providers.betminer import BetMinerProvider
from predbot.providers.budget import Budget
from predbot.providers.chain import ProviderChain, build_providers
from predbot.util.timeutil import day_bounds

from tests.conftest import load
from tests.test_betminer_provider import FakeHttp

NOW = datetime(2030, 6, 1, 6, 0, tzinfo=timezone.utc)


@pytest.fixture
def clock(monkeypatch):
    monkeypatch.setattr('predbot.cache.providers.utcnow', lambda: NOW)


def chain_for(db, http, order=('betminer',), providers=None):
    cfg = Config(timezone='UTC', cache_retry_seconds=900)
    p = BetMinerProvider('k', Budget(db), timezone='UTC', daily_limit=5)
    p.http = http
    provs = dict(providers or {})
    provs['betminer'] = p
    chain = ProviderChain(provs, list(order), 1)
    chain.db, chain.cfg = db, cfg
    return chain, p


def window():
    return day_bounds(NOW, 'UTC')


def test_first_run_fetches_then_restart_makes_zero_calls(db, clock):
    http = FakeHttp(result=load('betminer_matches.json'))
    chain, p = chain_for(db, http)
    start, end = window()

    pool = collect_persisted(chain, start, end)
    assert {e.name for e in pool} == {'Arsenal vs Chelsea', 'Inter vs Napoli'}
    assert len(http.calls) == 1
    assert chain.last_report['betminer']['status'] == 'complete'
    assert chain.last_report['betminer']['expected_sports'] == ['football']

    # "restart": brand new chain object, same db -> cache hit, no HTTP at all
    http2 = FakeHttp(result=load('betminer_matches.json'))
    chain2, _ = chain_for(db, http2)
    pool2 = collect_persisted(chain2, start, end)
    assert {e.name for e in pool2} == {e.name for e in pool}
    assert http2.calls == []
    assert Budget(db).used_today('betminer') == 1


def test_daily_limit_blocks_further_calls_and_marks_exhausted(db):
    http = FakeHttp(result=load('betminer_matches.json'))
    p = BetMinerProvider('k', Budget(db), timezone='UTC', daily_limit=1)
    p.http = http
    start, end = window()

    assert p.fetch_events(start, end)
    assert len(http.calls) == 1

    assert p.fetch_events(start, end) == []      # budget refuses the second call
    assert p.last_fetch_complete is False
    assert len(http.calls) == 1
    assert Budget(db).is_exhausted('betminer')

    # exhaustion mark short-circuits before any further HTTP, even after restart
    p2 = BetMinerProvider('k', Budget(db), timezone='UTC', daily_limit=1)
    p2.http = FakeHttp(result=load('betminer_matches.json'))
    assert p2.fetch_events(start, end) == []
    assert p2.http.calls == []


def test_other_sports_are_unaffected_by_betminer_failure(db, clock):
    from unittest.mock import Mock
    from predbot.providers.base import Event, MarketLine
    from predbot.util.http import ProviderError

    baseball_ev = Event(provider='sgo', provider_event_id='b1', sport='baseball', league='MLB',
                        home='Mets', away='Cubs', start=NOW.replace(hour=20), sources=['sgo'],
                        markets=[MarketLine(market='result2', selection='home', price=1.9)])
    other = Mock(spec=['enabled', 'fetch_events', 'sports'])
    other.sports = ['baseball']
    other.enabled.return_value = True
    other.fetch_events.return_value = [baseball_ev]

    chain, _ = chain_for(db, FakeHttp(error=ProviderError('boom')),
                         order=('sportsgameodds', 'betminer'), providers={'sportsgameodds': other})
    start, end = window()
    pool = collect_persisted(chain, start, end)

    assert [e.name for e in pool] == ['Mets vs Cubs']
    assert chain.last_report['sportsgameodds']['status'] == 'complete'
    assert chain.last_report['betminer']['status'] == 'failed'


def test_expected_sports_and_wiring(db):
    cfg = Config(betminer_key='k', api_football_key='', sgo_key='', sharpapi_key='')
    provs = build_providers(cfg, db)
    assert isinstance(provs['betminer'], BetMinerProvider)
    assert expected_sports('betminer', provs['betminer']) == ['football']
    assert 'betminer' in cfg.enabled_providers()
    # no settlement support -> BaseProvider defaults
    assert provs['betminer'].fetch_result('bm-1001') is None
    assert provs['betminer'].find_result('football', 'Arsenal', 'Chelsea', NOW) is None
