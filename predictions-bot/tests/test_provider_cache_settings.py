"""A completed provider cache must not freeze old leagues or empty discovery all day."""
import json
from dataclasses import replace
from datetime import timedelta
from unittest.mock import Mock

import pytest

from predbot.cache.fetcher import UnifiedFetcher
from predbot.cache.providers import collection_signature, encode_events
from predbot.config import Config
from predbot.jobs import daily_picks
from predbot.providers.chain import ProviderChain
from predbot.util.timeutil import iso
from tests.conftest import dt
from tests.real_events import real_ev as ev
from tests.test_picker import sc


class Source:
    def __init__(self, name, events=()):
        self.name = name
        self.sports, self.leagues = ['football'], []
        self.fetch_events = Mock(return_value=list(events))
        self.fetch_counts, self.parse_counts, self.normalization_counts = {}, {}, {}
        self.last_fetch_complete = True

    def enabled(self):
        return True


@pytest.fixture
def clock(monkeypatch):
    now = [dt(hh=10)]
    for module in ('predbot.cache.providers', 'predbot.cache.fetcher', 'predbot.jobs.daily_picks'):
        monkeypatch.setattr(module + '.utcnow', lambda: now[0])
    return now


def chain(db, source, cfg):
    c = ProviderChain({source.name: source}, [source.name])
    c.db, c.cfg = db, cfg
    return c


@pytest.mark.parametrize('name,field,new', [
    ('sharpapi', 'sharpapi_leagues', ['uefa_-_nations_league']),
    ('oddsapi', 'odds_api_league_allowlist', ['soccer_uefa_nations_league']),
    ('sharpapi', 'sharpapi_main_lines_only', False),
    ('sportsgameodds', 'sgo_allowed_stats', ['points']),
])
def test_changed_collection_inputs_refresh_completed_rows(db, clock, name, field, new):
    source = Source(name, [ev(1)])
    cfg = Config(timezone='UTC')
    c = chain(db, source, cfg)
    c.collect(dt(hh=0), dt(hh=23))
    c.force_retry = True
    c.cfg = replace(cfg, **{field: new})
    c.collect(dt(hh=0), dt(hh=23))
    assert source.fetch_events.call_count == 2
    assert c.last_report[name]['refresh_reason'] == 'settings_changed'
    c.collect(dt(hh=0), dt(hh=23))
    assert source.fetch_events.call_count == 2
    assert c.last_report[name]['cache'] == 'reused'


def test_selection_thresholds_do_not_rebuy_provider_data(db, clock):
    source = Source('oddsapi', [ev(1)])
    c = chain(db, source, Config(timezone='UTC'))
    c.collect(dt(hh=0), dt(hh=23))
    c.cfg = replace(c.cfg, top_min_prob=.70)
    c.force_retry = True
    c.collect(dt(hh=0), dt(hh=23))
    assert source.fetch_events.call_count == 1


def test_legacy_date_only_provider_row_refreshes_once(db, clock):
    source = Source('sharpapi', [ev(2)])
    cfg = Config(timezone='UTC')
    key = 'provider:v1:UTC:2030-06-01:sharpapi'
    db.upsert_provider_cache(key, '2030-06-01', encode_events([ev(1)]),
                             {'status': 'complete', 'events': {'football': 1}}, iso(dt(hh=23)))
    c = chain(db, source, cfg)
    assert c.collect(dt(hh=0), dt(hh=23))[0].provider_event_id == '2'
    assert c.last_report['sharpapi']['refresh_reason'] == 'settings_changed'
    c.collect(dt(hh=0), dt(hh=23))
    assert source.fetch_events.call_count == 1
    metadata = json.loads(db.get_provider_cache(key)['metadata_json'])
    assert metadata['collection_signature'] == collection_signature('sharpapi', cfg)


def test_empty_completed_provider_retries_after_interval_not_at_midnight(db, clock):
    source = Source('oddsapi')
    c = chain(db, source, Config(timezone='UTC'))
    assert c.collect(dt(hh=0), dt(hh=23)) == []
    clock[0] += timedelta(minutes=5)
    assert c.collect(dt(hh=0), dt(hh=23)) == []
    assert source.fetch_events.call_count == 1
    source.fetch_events.return_value = [ev(2)]
    clock[0] += timedelta(minutes=11)
    assert c.collect(dt(hh=0), dt(hh=23))
    assert source.fetch_events.call_count == 2
    assert c.last_report['oddsapi']['refresh_reason'] == 'empty_retry'


def test_failure_after_settings_change_never_restores_old_league_quotes(db, clock):
    source = Source('sharpapi', [ev(1)])
    c = chain(db, source, Config(timezone='UTC'))
    c.collect(dt(hh=0), dt(hh=23))
    c.cfg = replace(c.cfg, sharpapi_leagues=['world_-_international_friendlies'])
    source.fetch_events.side_effect = RuntimeError('unavailable')
    assert c.collect(dt(hh=0), dt(hh=23)) == []
    assert c.last_report['sharpapi']['status'] == 'failed'


def test_new_international_coverage_reaches_top_without_sgo_or_weaker_gates(db, clock, monkeypatch):
    source = Source('oddsapi')
    cfg = Config(timezone='UTC', odds_api_league_allowlist=['soccer_epl'], provider_order=['oddsapi'])
    c = chain(db, source, cfg)
    monkeypatch.setattr(daily_picks, 'score_events', lambda events, *a:
                        [sc(e, 'result3', 'home', .72, 1.45, books=3) for e in events])
    monkeypatch.setattr(daily_picks, 'make_results_gate', lambda *a: lambda e: 'test')
    app = UnifiedFetcher(c, db, cfg)
    assert app.get_daily_snapshot(scheduled=True).top_picks == []
    cfg = replace(cfg, odds_api_league_allowlist=['soccer_epl', 'soccer_uefa_nations_league'])
    app.cfg = c.cfg = cfg
    source.fetch_events.return_value = [ev(i) for i in range(4)]
    result = app.get_daily_snapshot(force_refresh=True, scheduled=True)
    assert len(result.top_picks) == 2 and result.daily_picks
    assert result.generation['providers']['oddsapi']['refresh_reason'] == 'settings_changed'
    assert cfg.top_min_books == 3                        # the Top book-coverage gate was not weakened to get picks
    assert not hasattr(cfg, 'top_relax_prob')            # and the relaxed fallback no longer exists
    source.fetch_events.reset_mock()
    app.get_daily_snapshot()  # subscriber reads never spend calls, even after retry expiry
    clock[0] += timedelta(minutes=16)
    app.get_daily_snapshot()
    source.fetch_events.assert_not_called()
