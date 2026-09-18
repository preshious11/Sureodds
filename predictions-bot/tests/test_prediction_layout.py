from types import SimpleNamespace

import pytest

from predbot.bot.callbacks import handle_callback, handle_command
from predbot.bot.pagination import render_page
from predbot.cache.models import DailySnapshot, PickRecord
from predbot.cache.fetcher import UnifiedFetcher
from predbot.config import Config
from predbot.jobs.daily_picks import store_picks
from predbot.selection.picker import select
from tests.conftest import dt
from tests.test_picker import cfg, ev, sc
from tests.test_subscribers_broadcast import FakeTG, _cb


def snapshot():
    def pick(name, price, tier):
        return PickRecord(home=name, away='Chelsea', label='Over 1.5 Goals',
                          sport='football', league='Hidden League', prob=.84,
                          kickoff_utc='2030-06-01T18:00:00Z', price=price, tier=tier,
                          market='total', selection='over', line=1.5)
    return DailySnapshot(date='2030-06-01', top_picks=[pick('Arsenal', 2.0, 'top')],
                         daily_picks=[pick('Liverpool', 2.0, 'daily'), pick('Villa', 3.98575, 'daily')])


@pytest.mark.parametrize('view,heading,total', [('top', '🏆 Top Picks for Today', '2.00'),
                                                ('daily', '📋 Daily Picks for Today', '15.94')])
@pytest.mark.parametrize('zone,time', [('Africa/Lagos', '7:00 PM WAT'), ('America/New_York', '2:00 PM EDT')])
def test_commands_and_buttons_have_exact_layout_and_subscriber_timezone(db, view, heading, total, zone, time):
    db.subscriber_add('42', timezone=zone)
    service = SimpleNamespace(get_daily_snapshot=snapshot)
    tg = FakeTG()
    handle_command('/' + view, '42', service=service, tg=tg, cfg=Config(timezone='UTC'), db=db)
    handle_callback(_cb('pg|' + view + '|0'), service=service, tg=tg, cfg=Config(timezone='UTC'), db=db)
    text = tg.sends[0]['text']
    assert text == tg.edits[0]['text']
    assert tg.sends[0]['reply_markup'] == tg.edits[0]['reply_markup']
    # the requested format: sport / league / fixture / market / prediction / odds / confidence | time
    assert text.startswith(f'{heading} 2030-06-01\n\n'
                           f'Football\nHidden League\nArsenal vs Chelsea\n'
                           f'<b>Market:</b> Totals\n'
                           f'<b>Prediction:</b> Over 1.5 Goals\n'
                           f'Odds: 2.00\n'
                           f'Confidence level: <b>84%</b> | {time}\n')
    assert 'predictions——' not in text          # the superseded copy stays gone
    picks_in_section = 1 if view == 'top' else 3
    assert text.endswith(f'Total Picks: {picks_in_section}\nCombined Odds: {total}')
    assert ('Liverpool' in text) == (view == 'daily')


def test_every_daily_page_uses_entire_ticket_total():
    for page, fixture in enumerate(['Arsenal', 'Liverpool', 'Villa']):
        text, _ = render_page(snapshot(), 'daily', page, 1, 'UTC')
        assert fixture + ' vs Chelsea' in text
        assert text.count('Confidence level:') == 1
        assert 'Combined Odds: 15.94' in text       # whole ticket, not just this page


@pytest.mark.parametrize('zone', ['', 'Not/AZone'])
def test_invalid_or_unset_subscriber_timezone_uses_config(db, zone):
    db.subscriber_add('42', timezone=zone)
    tg = FakeTG()
    handle_command('/top', '42', service=SimpleNamespace(get_daily_snapshot=snapshot),
                   tg=tg, cfg=Config(timezone='Africa/Lagos'), db=db)
    assert '7:00 PM WAT' in tg.sends[0]['text']


def test_selected_ticket_survives_database_and_cache_with_correct_totals(db):
    configuration = cfg()
    picks = select([sc(ev(i), 'result3', 'home', .70, 1.45) for i in range(40)], configuration, dt())
    store_picks(picks, db, '2030-06-01')
    fetcher = UnifiedFetcher(None, db, configuration)
    snap = fetcher._snapshot_from_rows(db.picks_for_date('2030-06-01'), dt().date(), 'db')
    restored = DailySnapshot.from_dict(snap.to_dict())
    from math import prod
    top_total = prod(p.price for p in restored.view_items('top'))
    daily_total = prod(p.price for p in restored.view_items('daily'))
    assert top_total == pytest.approx(2.1025)
    assert 10 <= daily_total <= 20
    assert len(restored.view_items('daily')) == len(picks.all())
    for view, total in [('top', top_total), ('daily', daily_total)]:
        text, _ = render_page(restored, view, 0, 1, 'UTC')
        assert f'Combined Odds: {total:.2f}' in text
