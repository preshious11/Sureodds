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


@pytest.fixture(autouse=True)
def _before_kickoff(monkeypatch):
    """Legs carry a Started/Finished marker once kickoff passes, so the exact
    layout is only stable relative to a fixed 'now' - not the real clock."""
    from datetime import datetime, timezone
    monkeypatch.setattr('predbot.bot.format.utcnow',
                        lambda: datetime(2030, 6, 1, 6, 0, tzinfo=timezone.utc))


def snapshot():
    def pick(name, price, tier):
        return PickRecord(home=name, away='Chelsea', label='Over 1.5 Goals',
                          sport='football', league='Hidden League', prob=.84,
                          kickoff_utc='2030-06-01T18:00:00Z', price=price, tier=tier,
                          market='total', selection='over', line=1.5)
    return DailySnapshot(date='2030-06-01', top_picks=[pick('Arsenal', 2.0, 'top')],
                         daily_picks=[pick('Liverpool', 2.0, 'daily'), pick('Villa', 3.98575, 'daily')])


NL = "\n"


@pytest.mark.parametrize('view,title,total,count', [('top', '🔥 TOP PICKS · Sat 1 Jun', '2.00', 1),
                                                    ('daily', '📋 DAILY PICKS · Sat 1 Jun', '7.97', 2)])
@pytest.mark.parametrize('zone,time,label', [('Africa/Lagos', '7:00 PM', 'WAT'), ('America/New_York', '2:00 PM', 'EDT')])
def test_commands_and_buttons_have_exact_layout_and_subscriber_timezone(db, view, title, total, count, zone, time, label):
    db.subscriber_add('42', timezone=zone)
    service = SimpleNamespace(get_daily_snapshot=snapshot)
    tg = FakeTG()
    handle_command('/' + view, '42', service=service, tg=tg, cfg=Config(timezone='UTC'), db=db)
    handle_callback(_cb('pg|' + view + '|0'), service=service, tg=tg, cfg=Config(timezone='UTC'), db=db)
    text = tg.sends[0]['text']
    assert text == tg.edits[0]['text']
    assert tg.sends[0]['reply_markup'] == tg.edits[0]['reply_markup']
    first = 'Arsenal' if view == 'top' else 'Liverpool'
    plural = 'pick' if count == 1 else 'picks'
    # header (own odds + count, the reader's zone once), then one block per pick: icon league · match / prediction / odds · confidence · time
    assert text.startswith(title + NL +
                           f'🎟 Combined odds {total} · {count} {plural} · 🕘 times in {label}' + NL + NL +
                           f'⚽ Hidden League · {first} vs Chelsea' + NL +
                           '🎯 Over 1.5 Goals' + NL +
                           f'💰 2.00  ·  📊 84%  ·  🕘 {time}')
    assert 'predictions——' not in text and 'Generated' not in text and 'odds as of' not in text
    assert ('Liverpool' in text) == (view == 'daily') and ('Arsenal' in text) == (view == 'top')
    assert 'Page' not in text                     # a single page has no footer


def test_every_daily_page_uses_entire_ticket_total_and_count():
    for page, fixture in enumerate(['Liverpool', 'Villa']):
        text, _ = render_page(snapshot(), 'daily', page, 1, 'UTC')
        assert fixture + ' vs Chelsea' in text
        assert text.count('📊 84%') == 1
        assert '🎟 Combined odds 7.97 · 2 picks' in text        # the whole ticket, on every page
        assert text.endswith(f'Page {page + 1} of 2')


@pytest.mark.parametrize('zone', ['', 'Not/AZone'])
def test_invalid_or_unset_subscriber_timezone_uses_config(db, zone):
    db.subscriber_add('42', timezone=zone)
    tg = FakeTG()
    handle_command('/top', '42', service=SimpleNamespace(get_daily_snapshot=snapshot),
                   tg=tg, cfg=Config(timezone='Africa/Lagos'), db=db)
    assert 'times in WAT' in tg.sends[0]['text'] and '🕘 7:00 PM' in tg.sends[0]['text']


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
    assert len(restored.view_items('daily')) == len(picks.daily) and len(restored.view_items('top')) == len(picks.top)
    assert 4 <= daily_total <= 20
    for view, total in [('top', top_total), ('daily', daily_total)]:
        n = len(restored.view_items(view))
        text, _ = render_page(restored, view, 0, 1, 'UTC')
        assert f'Combined odds {total:.2f} · {n} pick' in text
