from datetime import timezone
from types import SimpleNamespace

import pytest

from predbot.bot.broadcast import picks_text, subscriber_tz
from predbot.bot.callbacks import handle_callback, handle_command
from predbot.bot.commands import CommandLoop
from predbot.bot.format import kickoff_label
from predbot.cache.fetcher import UnifiedFetcher
from predbot.cache.manager import CacheManager
from predbot.config import Config
from predbot.jobs.daily_picks import store_picks
from predbot.selection.picker import PickSet
from predbot.util.timeutil import iso, parse_iso, resolve_timezone
from tests.test_picks_format import event, sc
from tests.test_subscribers_broadcast import FakeTG, _cb


@pytest.mark.parametrize('stamp,zone,expected', [
    ('2030-06-01T01:38:00Z', 'Africa/Lagos', '02:38 AM WAT'),
    ('2030-06-01T19:07:00Z', 'Africa/Lagos', '08:07 PM WAT'),
    ('2030-06-01T23:38:00Z', 'Africa/Lagos', '12:38 AM WAT'),
    ('2030-01-01T18:00:00Z', 'America/New_York', '01:00 PM EST'),
    ('2030-06-01T18:00:00Z', 'America/New_York', '02:00 PM EDT'),
    ('2030-01-01T18:00:00Z', 'Europe/London', '06:00 PM GMT'),
    ('2030-06-01T18:00:00Z', 'Europe/London', '07:00 PM BST'),
    ('2026-03-08T06:59:00Z', 'America/New_York', '01:59 AM EST'),
    ('2026-03-08T07:00:00Z', 'America/New_York', '03:00 AM EDT'),
])
@pytest.mark.parametrize('view', ['top', 'daily'])
def test_utc_storage_to_local_command_button_and_broadcast(db, stamp, zone, expected, view):
    parsed = parse_iso(stamp)
    assert parsed.tzinfo == timezone.utc
    selection = sc(event(1, kickoff=stamp), 'Home Win', .81, 2.0)
    store_picks(PickSet(top=[selection]), db, stamp[:10])
    fetcher = UnifiedFetcher(None, db, Config(), CacheManager())
    rows = db.picks_for_date(stamp[:10])
    snapshot = fetcher._snapshot_from_rows(rows, parsed.date(), 'db')
    original = snapshot.to_dict()
    assert rows[0]['start_utc'] == stamp
    assert snapshot.top_picks[0].kickoff_utc == stamp
    db.subscriber_add('42', timezone=zone)
    tg = FakeTG()
    service = SimpleNamespace(get_daily_snapshot=lambda: snapshot)
    handle_command('/' + view, '42', service=service, tg=tg, db=db, cfg=Config(timezone='UTC'))
    handle_callback(_cb('pg|' + view + '|0'), service=service, tg=tg, db=db, cfg=Config(timezone='UTC'))
    broadcast = picks_text(snapshot, subscriber_tz(db.subscribers_active()[0], 'UTC'))
    for text in (tg.sends[0]['text'], tg.edits[0]['text'], broadcast):
        assert f'Confidence: <b>81%</b> | <b>{expected}</b>' in text
        assert 'UTC' not in text
        if zone != 'Africa/Lagos':
            assert 'WAT' not in text
    assert tg.sends[0]['text'] == tg.edits[0]['text']
    assert snapshot.to_dict() == original
    assert db.picks_for_date(stamp[:10])[0]['start_utc'] == stamp
    assert iso(parsed) == stamp


@pytest.mark.parametrize('stored,configured,expected', [
    ('Africa/Lagos', 'UTC', 'Africa/Lagos'),
    ('America/New_York', 'Africa/Lagos', 'America/New_York'),
    ('', 'Africa/Lagos', 'Africa/Lagos'),
    (None, 'Africa/Lagos', 'Africa/Lagos'),
    ('Invalid/Zone', 'Africa/Lagos', 'Africa/Lagos'),
    (' Africa/Lagos ', 'UTC', 'Africa/Lagos'),
    ('Invalid/Zone', 'Invalid/Default', 'UTC'),
])
def test_timezone_resolution_is_shared(stored, configured, expected):
    assert resolve_timezone(stored, configured) == expected
    assert subscriber_tz({'timezone': stored}, configured) == expected


def test_timezone_command_sets_and_reports_existing_preference(db):
    loop = CommandLoop(FakeTG(), db, Config(timezone='UTC'), {})
    loop.handle('/timezone', '42', '42', ['Africa/Lagos'])
    assert db.subscriber_timezone('42') == 'Africa/Lagos'
    assert 'Africa/Lagos' in loop.handle('/timezone', '42', '42')


def test_offset_input_is_normalized_to_utc_before_local_display():
    parsed = parse_iso('2030-06-01T03:38:00+02:00')
    assert iso(parsed) == '2030-06-01T01:38:00Z'
    assert kickoff_label(parsed, 'Africa/Lagos') == '02:38 AM WAT'
