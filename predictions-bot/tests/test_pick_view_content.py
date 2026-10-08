from types import SimpleNamespace

import pytest

from predbot.bot.callbacks import handle_callback, handle_command
from predbot.bot.format import PickLine, picks_card
from predbot.cache.models import DailySnapshot, PickRecord
from predbot.config import Config
from tests.test_subscribers_broadcast import FakeTG, _cb


@pytest.mark.parametrize('view,expected,absent,total', [
    ('top', 'TOP ONLY', 'DAILY ONLY', '3.57'),
    ('daily', 'DAILY ONLY', 'TOP ONLY', '15.34'),        # the Daily card is the Daily ticket alone: 2.0 x 7.67
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
    service = SimpleNamespace(get_daily_snapshot=lambda **_kw: restored)
    tg = FakeTG()
    handle_command('/' + view, '42', service=service, tg=tg, cfg=Config())
    handle_callback(_cb('pg|' + view + '|0'), service=service, tg=tg, cfg=Config())
    for result in [tg.sends[0], tg.edits[0]]:
        assert expected in result['text']
        assert absent not in result['text']
        assert f'Combined odds {total} · 2 picks' in result['text']


def test_empty_top_does_not_fall_back_to_daily():
    snapshot = DailySnapshot(date='2030-06-01', daily_picks=[
        PickRecord(label='DAILY ONLY', price=2.0)])
    tg = FakeTG()
    handle_command('/top', '42', service=SimpleNamespace(get_daily_snapshot=lambda **_kw: snapshot),
                   tg=tg, cfg=Config())
    assert 'No Top Picks for today.' in tg.sends[0]['text']
    assert 'DAILY ONLY' not in tg.sends[0]['text']


# ------------------------------------------------------------- honest empty states
def test_partial_card_with_picks_says_quality_bar_not_outage():
    """A PARTIAL day with picks must not read like the bot itself is down."""
    from predbot.bot.pagination import render_page
    snapshot = DailySnapshot(date='2030-06-01', status='PARTIAL', daily_picks=[
        PickRecord(label='DAILY ONLY', price=2.0)])
    text, _ = render_page(snapshot, 'top', 0, 5, 'UTC')
    assert 'No Top Picks for today.' in text
    assert 'temporarily unavailable' not in text
    assert 'DAILY ONLY' not in text          # never a silent fallback to the daily tier


def test_partial_day_without_any_picks_reports_incomplete_data():
    """No picks at all + a failed provider = data problem, not a quality verdict."""
    from predbot.bot.pagination import render_page
    snapshot = DailySnapshot(date='2030-06-01', status='PARTIAL', generation={
        'providers': {'sharpapi': {'status': 'failed'}},
        'retry_after': '2030-06-01T18:15:00+00:00'})
    text, _ = render_page(snapshot, 'daily', 0, 5, 'UTC')
    assert 'still being assembled' in text
    assert '18:15 UTC' in text
    assert 'quality bar' not in text
    assert 'temporarily unavailable' not in text


def test_complete_day_without_picks_keeps_the_quality_bar_line():
    from predbot.bot.pagination import render_page
    snapshot = DailySnapshot(date='2030-06-01', status='COMPLETE')
    text, _ = render_page(snapshot, 'daily', 0, 5, 'UTC')
    assert 'No valid model predictions were generated today.' in text
    assert 'still being assembled' not in text


@pytest.mark.parametrize('prices,total', [([2.0, 7.67], '15.34'), ([2.05, 1.74], '3.57'), ([1.5, 2.0], '3.00')])
def test_broadcast_total_is_the_product_of_every_pick(prices, total):
    picks = [PickLine('Football', 'A vs B', 'Home', 'League', 0.7, '--:--', price)
             for price in prices]
    assert f'🎟 Combined odds {total} · 2 picks' in picks_card(picks, '2030-06-01')
