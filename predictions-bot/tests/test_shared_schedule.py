from datetime import datetime, timezone

import pytest

from predbot.bot.commands import CommandLoop
from predbot.config import Config
from tests.test_subscribers_broadcast import FakeTG


def test_nigeria_defaults(monkeypatch):
    monkeypatch.delenv('TIMEZONE', raising=False)
    monkeypatch.delenv('POST_TIME', raising=False)
    assert Config().timezone == 'Africa/Lagos'
    assert Config().post_time == '09:00'


@pytest.mark.parametrize('month,expected', [(1, '03:00 AM EST'), (6, '04:00 AM EDT')])
def test_schedule_description_converts_same_instant_for_dst(db, monkeypatch, month, expected):
    monkeypatch.setattr('predbot.bot.commands.utcnow', lambda: datetime(2030, month, 1, 7, tzinfo=timezone.utc))
    loop = CommandLoop(FakeTG(), db, Config(timezone='Africa/Lagos', post_time='09:00'), {})
    text = loop._schedule_text('America/New_York')
    assert '09:00 AM WAT' in text
    assert expected in text
