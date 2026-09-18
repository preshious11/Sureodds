from datetime import datetime, timedelta, timezone
from math import prod
from unittest.mock import Mock

import pytest

from predbot.cache.fetcher import UnifiedFetcher
from predbot.cache.models import DailySnapshot
from predbot.config import Config
from predbot.jobs import daily_picks
from predbot.providers.chain import ProviderChain
from predbot.providers.base import MarketLine
from predbot.selection.picker import select
from predbot.util.http import ProviderError
from tests.conftest import dt
from tests.test_picker import ev, sc


def provider(events=None, error=None):
    p = Mock()
    p.enabled.return_value = True
    p.fetch_events.side_effect = error
    p.fetch_events.return_value = events or []
    return p


def test_baseball_count_does_not_stop_football_provider():
    baseball = ev(1, sport='baseball')
    baseball.markets = [MarketLine('result2', 'home', 2.0)]
    football = ev(2)
    football.markets = [MarketLine('result3', 'home', 2.0)]
    chain = ProviderChain({'a': provider([baseball]), 'b': provider([football])}, ['a', 'b'], 1)
    assert {e.sport for e in chain.collect(dt(), dt() + timedelta(days=1))} == {'football', 'baseball'}


@pytest.mark.parametrize('failed', [False, True])
def test_empty_or_failed_primary_calls_fallback(failed):
    first = provider(error=ProviderError('unavailable') if failed else None)
    second = provider([ev(2)])
    chain = ProviderChain({'a': first, 'b': second}, ['a', 'b'], 1)
    assert chain.collect(dt(), dt() + timedelta(days=1))
    second.fetch_events.assert_called_once()


def test_retry_reuses_successful_provider_only():
    first = provider([ev(1, sport='baseball')])
    second = provider(error=ProviderError('temporarily unavailable'))
    chain = ProviderChain({'a': first, 'b': second}, ['a', 'b'], 1)
    chain.collect(dt(), dt() + timedelta(days=1))
    second.fetch_events.side_effect = None
    second.fetch_events.return_value = [ev(2)]
    assert len(chain.collect(dt(), dt() + timedelta(days=1))) == 2
    assert first.fetch_events.call_count == 1
    assert second.fetch_events.call_count == 2


def test_top_search_finds_better_pair_than_greedy_single():
    pool = [sc(ev(1), 'result3', 'home', .80, 2.26),
            sc(ev(2), 'result3', 'home', .75, 1.38),
            sc(ev(3), 'result3', 'home', .73, 1.50)]
    result = select(pool, Config(), dt())
    assert sorted(s.price for s in result.top) == [1.38, 1.5]
    assert prod(s.price for s in result.top) == pytest.approx(2.07)
    assert result.diagnostics['combinations_checked'] >= 1     # complete in-band tickets evaluated


def test_top_search_bounds_count_and_unique_matches():
    # Top Picks is a 2-3 leg accumulator (TOP_LEGS_MIN/MAX): a single leg is
    # never selected, and sizes above 3 (when configured) search a trimmed
    # pool to keep the combinatorial search bounded.
    pool = [sc(ev(i // 2), 'result3', 'home', .75, 1.25 + i / 1000) for i in range(120)]
    result = select(pool, Config(top_leg_odds_min=1.0, top_legs_max=5), dt())
    assert 2 <= len(result.top) <= 5
    assert len({s.event.dedupe_key() for s in result.top}) == len(result.top)
    assert result.diagnostics['combinations_checked'] <= 60000


def test_top_three_pick_combination():
    result = select([sc(ev(i), 'result3', 'home', .8, 1.27) for i in range(3)],
                    Config(top_leg_odds_min=1.2), dt())
    assert len(result.top) == 3
    assert prod(s.price for s in result.top) == pytest.approx(1.27 ** 3)


def test_football_above50_precedes_many_lower_probability_baseball():
    pool = [sc(ev(i, sport='baseball'), 'result2', 'home', .49, 2.5) for i in range(40)]
    pool += [sc(ev(100), 'result3', 'home', .60, 1.8)]
    result = select(pool, Config(), dt())
    assert result.all()
    assert {s.event.sport for s in result.all()} == {'football'}


def test_below50_only_used_when_no_eligible_preferred_candidates():
    pool = [sc(ev(1), 'result3', 'home', .49, 2.5)]
    assert select(pool, Config(), dt()).daily


def test_generation_uses_lagos_day_including_utc_previous_date(db, monkeypatch):
    now = datetime(2030, 6, 1, 23, 0, tzinfo=timezone.utc)  # June 2 in Lagos
    event = ev(1, start=now + timedelta(minutes=45))
    event2 = ev(2, start=now + timedelta(minutes=45))  # Top is a 2-leg minimum accumulator
    chain = provider()
    chain.collect = Mock(return_value=[event, event2])
    chain.last_report = {}
    monkeypatch.setattr(daily_picks, 'score_events', lambda *args: [
        sc(event, 'result3', 'home', .75, 1.45), sc(event2, 'result3', 'home', .75, 1.45)])
    result = daily_picks.build_picks(chain, db, Config(timezone='Africa/Lagos'), now)
    assert result.top
    start, end = chain.collect.call_args.args
    assert start <= event.start < end
    # end = min(local midnight + DAY_WINDOW_END_OFFSET_HOURS, now + 24h):
    # with PICK_WINDOW_HOURS=24 the +24h bound binds here.
    assert end == datetime(2030, 6, 2, 23, tzinfo=timezone.utc)


def test_partial_generation_keeps_valid_picks(db, monkeypatch):
    now = dt()
    monkeypatch.setattr('predbot.cache.fetcher.utcnow', lambda: now)
    monkeypatch.setattr(daily_picks, 'utcnow', lambda: now)
    # Top is a 2-leg minimum accumulator, so two independent candidates are needed.
    candidates = [sc(ev(1), 'result3', 'home', .75, 1.45), sc(ev(2), 'result3', 'home', .75, 1.45)]
    monkeypatch.setattr(daily_picks, 'score_events', lambda *args: candidates)
    chain = Mock()
    chain.collect.return_value = [c.event for c in candidates]
    chain.last_report = {'football': {'status': 'failed'}}
    # Old retry exhaustion must no longer prevent usable providers from running.
    db.set_kv('generation:' + now.date().isoformat(), '{"complete": false, "attempts": 3}')
    cache = None
    fetcher = UnifiedFetcher(chain, db, Config(timezone='UTC'))
    first = fetcher.get_daily_snapshot()
    assert first.generation['complete'] is False
    assert first.top_picks
    assert db.has_picks_for_date(now.date().isoformat())
    fetcher.get_daily_snapshot()
    assert chain.collect.call_count == 1
    from predbot.bot.pagination import render_page
    text, _ = render_page(first, 'top', 0, 5, 'UTC')
    assert 'temporarily incomplete' not in text


def test_sgo_probes_each_league_and_reuses_spare_allowance(budget):
    from predbot.providers.sportsgameodds import SportsGameOddsProvider
    p = SportsGameOddsProvider('k', budget, ['SOCCER', 'BASEBALL'], ['MLB', 'MLS'], event_limit=4)
    p._available_leagues = {'MLB', 'MLS'}
    p._parse_event = lambda raw: ev(raw['id'], sport=raw['sport'])
    requests = []
    def get(path, params):
        requests.append(dict(params))
        if params.get('cursor'):
            return {'data': []}
        if params['leagueID'] == 'MLB':
            return {'data': [{'id': 1, 'sport': 'baseball'}, {'id': 2, 'sport': 'baseball'}], 'nextCursor': 'next'}
        return {'data': []}
    p.http.get = get
    p.fetch_events(dt(), dt()+timedelta(days=1))
    assert [r['leagueID'] for r in requests[:2]] == ['MLB', 'MLS']
    assert requests[2]['cursor'] == 'next'
    assert requests[2]['limit'] == requests[0]['limit']
    assert sum(c.get('raw_events', 0) for c in p.fetch_counts.values()) == 2


def test_api_football_configured_league_filter(budget):
    import copy
    from tests.conftest import load
    from predbot.providers.api_football import ApiFootballProvider
    fixture = copy.deepcopy(load('apif_fixtures.json')['response'][0])
    fixture['fixture']['date'] = '2030-06-01T22:00:00Z'
    fixture['fixture']['status']['short'] = 'NS'
    odds = copy.deepcopy(load('apif_odds.json')['response'][0])
    odds['fixture']['id'] = fixture['fixture']['id']
    for allowed, expected in [([str(fixture['league']['id'])], 1), (['999999'], 0)]:
        p = ApiFootballProvider('k', budget, league_ids=allowed, predictions_per_run=0)
        p._call = lambda path, *args, **kwargs: {'response': [fixture] if path == '/fixtures' else [odds]}
        assert len(p.fetch_events(dt(), dt(hh=23))) == expected


def test_partial_snapshot_is_not_presented_as_quality_rejection():
    from predbot.bot.pagination import render_page
    snapshot = DailySnapshot(date='2030-06-01', generation={'complete': False})
    text, _ = render_page(snapshot, 'top', 0, 5, 'UTC')
    assert 'temporarily incomplete' not in text
