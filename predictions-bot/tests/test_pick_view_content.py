from types import SimpleNamespace

import pytest

from predbot.bot.callbacks import handle_callback, handle_command
from predbot.bot.format import PickLine, picks_card
from predbot.cache.models import DailySnapshot, PickRecord
from predbot.config import Config
from tests.test_subscribers_broadcast import FakeTG, _cb


@pytest.mark.parametrize('view,expected,absent,total', [
    ('top', 'TOP ONLY', 'DAILY ONLY', '3.57'),
    ('daily', 'DAILY ONLY', 'NEVER INCLUDED', '54.72'),
])
def test_each_route_uses_its_own_picks_and_total(view, expected, absent, total):
    snapshot = DailySnapshot(date='2030-06-01', top_picks=[
        PickRecord(tier='top', label='TOP ONLY A', price=2.05),
        PickRecord(tier='top', label='TOP ONLY B', price=1.74),
    ], daily_picks=[
        PickRecord(tier='daily', label='DAILY ONLY A', price=2.0),
        PickRecord(tier='daily', label='DAILY ONLY B', price=7.67),
    ])
    # Exercise the serialized cache format as well as the command/button routes.
    restored = DailySnapshot.from_dict(snapshot.to_dict())
    service = SimpleNamespace(get_daily_snapshot=lambda: restored)
    tg = FakeTG()
    handle_command('/' + view, '42', service=service, tg=tg, cfg=Config())
    handle_callback(_cb('pg|' + view + '|0'), service=service, tg=tg, cfg=Config())
    for result in [tg.sends[0], tg.edits[0]]:
        assert expected in result['text']
        assert absent not in result['text']
        assert f'Combined odds: <b>{total}</b>' in result['text']


def test_empty_top_does_not_fall_back_to_daily():
    snapshot = DailySnapshot(date='2030-06-01', daily_picks=[
        PickRecord(label='DAILY ONLY', price=2.0)])
    tg = FakeTG()
    handle_command('/top', '42', service=SimpleNamespace(get_daily_snapshot=lambda: snapshot),
                   tg=tg, cfg=Config())
    assert 'No selection cleared the quality bar today.' in tg.sends[0]['text']
    assert 'DAILY ONLY' not in tg.sends[0]['text']


@pytest.mark.parametrize('prices,total', [([2.0, 7.67], '15.34'), ([2.05, 1.74], '3.57'), ([1.5, 2.0], '3.00')])
def test_broadcast_total_is_multiplied_and_rounded_to_two_places(prices, total):
    picks = [PickLine('Football', 'A vs B', 'Home', 'League', 0.7, '--:--', price)
             for price in prices]
    assert picks_card(picks, '2030-06-01').endswith(f'Combined odds: <b>{total}</b>')
