from unittest.mock import Mock
import logging
import subprocess
import sys

import pytest
import requests

from predbot.bot import callbacks
from predbot.bot.commands import CommandLoop
from predbot.bot.runtime import PollingLease, log_identity
from predbot.bot.telegram import Telegram, PollingError
from predbot.config import Config
from tests.test_subscribers_broadcast import FakeTG, _cb, _snapshot


@pytest.mark.parametrize('state', ['new', 'subscribed', 'unsubscribed'])
def test_repeated_start_has_exact_copy_in_every_state(db, state):
    if state != 'new':
        db.subscriber_add('42')
    if state == 'unsubscribed':
        db.subscriber_remove('42')
    tg = FakeTG()
    for _ in range(3):
        callbacks.handle_start(db, tg, '42', {'first_name': 'Ada <&>'})
    expected = ('👋 Hi, Ada &lt;&amp;&gt;\n\n'
                'Use the menu below or these commands:\n\n'
                '<b>🏆 /top</b> – Top picks of the day\n'
                '<b>📚 /all</b> – All predictions\n'
                '<b>📋 /daily</b> – Daily picks · Accumulator slip\n'
                '<b>📊 /stats</b> – See prediction results')
    assert all(s['text'] == expected for s in tg.sends)
    # every superseded welcome copy, pinned so none of them can come back
    for old in ('Welcome to predbot', 'I price every market',
                'You are subscribed to the daily picks', 'Tap the menu below',
                'Strongest picks today', 'All predictions today', 'Prediction summary'):
        assert all(old not in s['text'] for s in tg.sends)
    # /start never pushes predictions - the menu is the only thing sent
    assert all('Confidence level:' not in s['text'] for s in tg.sends)
    assert all('Combined Odds' not in s['text'] for s in tg.sends)


def test_menu_callback_keeps_canonical_welcome_without_service(db):
    tg = FakeTG()
    callbacks.handle_start(db, tg, '42', {'first_name': 'Ada'})
    loop = CommandLoop(tg, db, Config(), {}, service=None)

    cb = _cb('menu')
    cb['from'] = {'first_name': 'Ada'}
    loop._dispatch_callback(cb)

    assert tg.edits[-1]['text'] == callbacks.welcome_message('Ada')
    assert tg.edits[-1]['reply_markup'] == callbacks.menu_markup()
    assert db.subscribers_active()                  # /start subscribed, nothing undid it


def test_subscription_toggle_callbacks_are_gone(db):
    """The sub|on / sub|off buttons were removed - they must stay removed."""
    tg = FakeTG()
    callbacks.handle_start(db, tg, '42', {'first_name': 'Ada'})
    loop = CommandLoop(tg, db, Config(), {}, service=None)
    before = len(tg.edits)

    for data in ('sub|off', 'sub|on'):
        cb = _cb(data)
        cb['from'] = {'first_name': 'Ada'}
        loop._dispatch_callback(cb)

    assert len(tg.edits) == before                  # unknown payloads change nothing
    assert db.subscribers_active()                  # and cannot unsubscribe anyone
    buttons = [b for row in callbacks.menu_markup()['inline_keyboard'] for b in row]
    assert not any(b['callback_data'].startswith('sub|') for b in buttons)


@pytest.mark.parametrize('view', ['top', 'daily', 'stats'])
def test_command_and_callback_share_renderer_and_result(db, monkeypatch, view):
    tg = FakeTG()
    service = Mock()
    service.get_daily_snapshot.return_value = _snapshot()
    shared = Mock(wraps=callbacks.open_view)
    monkeypatch.setattr(callbacks, 'open_view', shared)
    loop = CommandLoop(tg, db, Config(), {}, service=service)
    loop._send_view('/' + view, '42')
    loop._dispatch_callback(_cb('pg|' + view + '|0'))
    assert shared.call_count == 2
    assert all(c.args[0] == view for c in shared.call_args_list)
    assert tg.sends[0]['text'] == tg.edits[0]['text']
    assert tg.sends[0]['reply_markup'] == tg.edits[0]['reply_markup']


def test_restart_routes_exactly_once_and_thread_can_join(db):
    for _ in range(3):
        tg = FakeTG()
        loop = CommandLoop(tg, db, Config(), {})
        def updates(*args, **kwargs):
            loop.stop()
            return [{'update_id': 1, 'message': {'text': '/start@ExampleBot payload',
                     'chat': {'id': 42}, 'from': {'first_name': 'Ada'}}}]
        tg.get_updates = updates
        loop.start()
        loop.join(5)
        assert not loop.is_alive()
        assert len(tg.sends) == 1
        assert tg.sends[0]['text'] == callbacks.welcome_message('Ada')


def test_local_duplicate_lease_rejected_and_restart_releases_it():
    with PollingLease('unit-test-lease'):
        with pytest.raises(RuntimeError, match='Another local process'):
            with PollingLease('unit-test-lease'):
                pass
    with PollingLease('unit-test-lease'):
        pass


def test_fresh_process_start_uses_canonical_welcome(tmp_path):
    script = '''
import sys
from predbot.bot.commands import CommandLoop
from predbot.bot.callbacks import welcome_message
from predbot.config import Config
from predbot.storage.db import Database
from tests.test_subscribers_broadcast import FakeTG
db = Database(sys.argv[1])
tg = FakeTG()
loop = CommandLoop(tg, db, Config(), {})
def updates(*args, **kwargs):
    loop.stop()
    return [{"update_id": 1, "message": {"text": "/start", "chat": {"id": 42}, "from": {"first_name": "Ada"}}}]
tg.get_updates = updates
loop.start()
loop.join(5)
assert len(tg.sends) == 1
assert tg.sends[0]["text"] == welcome_message("Ada")
db.subscriber_remove("42")
db.conn.close()
'''
    for _ in range(2):
        subprocess.run([sys.executable, '-c', script, str(tmp_path / 'restart.db')],
                       check=True, capture_output=True, timeout=15)


@pytest.mark.parametrize('status', [409, 401, 429, 500])
def test_polling_http_failures_are_visible_and_safe(status):
    tg = Telegram('secret-token', '')
    tg.session = Mock()
    tg.session.get.return_value.status_code = status
    with pytest.raises(PollingError) as exc:
        tg.get_updates(None)
    assert str(status) in str(exc.value)
    assert 'secret-token' not in str(exc.value)


def test_transport_error_never_logs_token():
    tg = Telegram('secret-token', '')
    tg.session = Mock()
    tg.session.get.side_effect = requests.ConnectionError(tg.base)
    with pytest.raises(PollingError) as exc:
        tg.get_updates(None)
    assert 'secret-token' not in str(exc.value)


def test_polling_failure_backs_off(db):
    tg = FakeTG()
    tg.get_updates = Mock(side_effect=PollingError('conflict'))
    loop = CommandLoop(tg, db, Config(), {})
    loop._stop_event = Mock()
    loop._stop_event.is_set.side_effect = [False, True]
    loop._poll()
    loop._stop_event.wait.assert_called_once_with(30)


def test_identity_names_active_source(caplog):
    with caplog.at_level(logging.INFO):
        identity = log_identity(callbacks.handle_start)
    assert identity in caplog.text
    assert 'predbot.bot.callbacks.handle_start' in caplog.text
    assert 'mode=polling' in caplog.text
    assert 'start_handlers=1' in caplog.text


def test_scheduler_detects_dead_consumer_before_running_jobs(monkeypatch):
    from predbot.jobs import scheduler
    monkeypatch.setattr(scheduler.signal, 'signal', lambda *args: None)
    health = Mock(side_effect=RuntimeError('consumer stopped'))
    job = Mock()
    with pytest.raises(RuntimeError, match='consumer stopped'):
        scheduler.loop([job], health_check=health)
    job.due.assert_not_called()


# ------------------------------------------------------- command loop supervisor
class FakeLoop:
    def __init__(self):
        self.alive = True
        self.stopped = 0

    def is_alive(self):
        return self.alive

    def stop(self):
        self.stopped += 1


def test_supervisor_restarts_dead_consumer_and_backs_off_on_failure():
    from predbot.bot.runtime import CommandLoopSupervisor
    first, second = FakeLoop(), FakeLoop()
    # start, then one failing restart (e.g. the polling lease is held), then success
    factory = Mock(side_effect=[first, RuntimeError('lease held'), second])
    sup = CommandLoopSupervisor(factory, cooldown=60, backoff_max=600)
    sup.start(now=0)
    assert sup.loop is first

    sup.ensure(now=10)                        # alive -> untouched
    assert factory.call_count == 1

    first.alive = False
    sup.ensure(now=100)                       # dead + cooldown passed -> restart fails
    assert sup.loop is first                  # old loop kept, nothing replaced it
    assert first.stopped == 0
    assert factory.call_count == 2

    sup.ensure(now=130)                       # inside the doubled (120s) backoff
    assert factory.call_count == 2

    sup.ensure(now=221)                       # backoff elapsed -> restart succeeds
    assert sup.loop is second
    assert first.stopped == 1                 # the dead loop is shut down
    assert factory.call_count == 3

    sup.ensure(now=300)                       # alive again -> untouched
    assert factory.call_count == 3


def test_supervisor_stop_shuts_the_live_loop_down():
    from predbot.bot.runtime import CommandLoopSupervisor
    loop = FakeLoop()
    sup = CommandLoopSupervisor(lambda: loop, cooldown=60)
    sup.start()
    sup.stop(join_timeout=0)
    assert loop.stopped == 1
