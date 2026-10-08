"""Production pause tests. Enabled Ticket Audit tests remain separately intact."""
from datetime import datetime, timezone
import logging
from pathlib import Path
import subprocess
import sys
from unittest.mock import Mock

import pytest
import requests

from predbot import ticket_audit_switch
from predbot.bot import callbacks
from predbot.bot.commands import CommandLoop
from predbot.config import Config
from tests.test_community_menu import (  # noqa: F401
    MenuTelegram, COMMUNITY, BOT_USERNAME, menu_loop, cached_loop,
    message_update, button_update, PUBLIC_VIEWS,
)

NOW = datetime(2030, 6, 1, 12, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def pause_and_block_http(monkeypatch):
    monkeypatch.setattr(ticket_audit_switch, 'ENABLED', False)
    def forbidden(*args, **kwargs):
        pytest.fail('Disabled feature made an external HTTP request')
    monkeypatch.setattr(requests.Session, 'request', forbidden)


def test_shipped_switch_is_off_and_env_flags_cannot_reactivate(menu_loop, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    namespace = {}
    exec((root / 'predbot/ticket_audit_switch.py').read_text(encoding='utf-8'), namespace)
    assert namespace['ENABLED'] is False
    for flag in ('AUDIT_GEMINI_ENABLED', 'AUDIT_GEMINI_FREE_TIER_CONFIRMED',
                 'AUDIT_BOOKING_CODES_ENABLED', 'SPORTYBET_DIRECT_ENABLED', 'MYBETCODE_ENABLED'):
        monkeypatch.setenv(flag, 'true')
    loop = CommandLoop(menu_loop.tg, menu_loop.db, menu_loop.cfg, {})
    assert loop._ticket_audit is None


def test_fresh_main_import_and_command_startup_never_import_audit_modules():
    # Fresh interpreter proves isolation even when other tests imported retained
    # Ticket Audit modules. No bot/process is started and HTTP/DNS are forbidden.
    script = '''
import importlib.abc
import socket
import sys
from unittest.mock import Mock
import requests
def forbidden(*args, **kwargs):
    raise AssertionError('No external networking allowed')
socket.socket.connect = forbidden
socket.getaddrinfo = forbidden
requests.Session.request = forbidden
class BlockAudit(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'predbot.bot.ticket_audit' or fullname == 'predbot.bot.audit_format' or fullname == 'predbot.ticket_audit' or fullname.startswith('predbot.ticket_audit.'):
            raise AssertionError('Disabled startup imported feature: ' + fullname)
sys.meta_path.insert(0, BlockAudit())
import main
from predbot.bot.commands import CommandLoop
from predbot.config import Config
loop = CommandLoop(Mock(), Mock(), Config(), {})
assert loop._ticket_audit is None
assert not any(name.startswith('predbot.ticket_audit.') for name in sys.modules)
print('isolated')
'''
    completed = subprocess.run([sys.executable, '-c', script],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == 'isolated'


def test_menu_help_and_commands_have_no_audit_entry(menu_loop):
    menu_loop._handle_update(message_update('/menu'))
    rows = menu_loop.tg.sends[0]['reply_markup']['inline_keyboard']
    assert len(rows) == 4
    assert rows[3][0]['url'].endswith('?start=custom_picks')
    assert 'ticket_audit' not in str(rows) and 'Ticket Audit' not in str(rows)
    for chat in ('42', COMMUNITY):
        assert 'ticket_audit' not in menu_loop.handle('/help', '9', chat)
    assert 'audit' not in str(callbacks.BOT_COMMANDS).lower()


@pytest.mark.parametrize('kind', ['private', 'group', 'supergroup', 'channel'])
def test_old_audit_deep_link_is_blocked_without_handlers_or_subscribing(menu_loop, db, kind):
    before = tuple(db.conn.iterdump())
    controller = Mock()
    menu_loop._ticket_audit = controller  # Even a stale/injected controller cannot run.
    chat = '42' if kind == 'private' else COMMUNITY
    update = message_update('/start ticket_audit', chat, kind, user_id='42')
    if kind == 'channel':
        update['channel_post'] = update.pop('message')
    menu_loop._handle_update(update)
    assert controller.mock_calls == []
    assert tuple(db.conn.iterdump()) == before
    assert menu_loop.tg.sends[-1]['text'] == 'This feature is temporarily unavailable.'
    assert not menu_loop.tg.sends[-1].get('reply_markup')
    assert menu_loop._custom_users == set()


@pytest.mark.parametrize('action', ['confirm', 'cancel', 'resend', 'bk:sportybet', 'bk:bet9ja'])
def test_old_audit_callbacks_never_reach_controller(menu_loop, action):
    controller = Mock()
    menu_loop._ticket_audit = controller
    menu_loop._handle_update(button_update('ta|old-token|' + action, '42'))
    assert controller.mock_calls == [] and not menu_loop.tg.sends
    assert menu_loop.tg.answers[-1]['text'] == 'This feature is temporarily unavailable.'


@pytest.mark.parametrize('media', [
    {'text': 'BOOK123'}, {'text': 'Full pasted ticket: Home vs Away, over 2.5'},
    {'photo': [{'file_id': 'mock-photo', 'width': 100, 'height': 100}]},
    {'document': {'file_id': 'mock-image', 'mime_type': 'image/png', 'file_size': 200}},
])
def test_ticket_inputs_never_reach_any_audit_handler_or_worker(menu_loop, db, media):
    before = tuple(db.conn.iterdump())
    controller = Mock()
    menu_loop._ticket_audit = controller
    msg = message_update('', user_id='42')['message']
    msg.update(media)
    menu_loop._handle_update({'message': msg})
    assert controller.mock_calls == [] and not menu_loop.tg.sends
    assert not menu_loop._admin_threads and tuple(db.conn.iterdump()) == before


def test_custom_picks_runs_without_audit_session_interception(menu_loop, monkeypatch):
    from predbot.custom_recovery import CALLBACK_PREFIX
    controller = Mock()
    menu_loop._ticket_audit = controller
    menu_loop._handle_update(message_update('/start custom_picks', user_id='42'))
    assert ('42', '42') in menu_loop._custom_users
    menu_loop._custom_picks.respond = Mock(return_value=Mock(text='Custom reply', reply_markup=None))
    menu_loop._handle_update(message_update('target 5 odds', user_id='42'))
    menu_loop._custom_picks.respond.assert_called_once_with('target 5 odds', '42', '42')
    menu_loop._custom_picks.select = Mock(return_value=Mock(toast='Custom callback', response=None))
    menu_loop._handle_update(button_update(CALLBACK_PREFIX + 'mock-token', '42'))
    menu_loop._custom_picks.select.assert_called_once()
    assert controller.mock_calls == []


def test_normal_start_and_stop_do_not_call_audit(menu_loop, db):
    controller = Mock()
    menu_loop._ticket_audit = controller
    menu_loop._handle_update(message_update('/start', user_id='42'))
    assert [r['chat_id'] for r in db.subscribers_active()] == ['42']
    menu_loop._handle_update(message_update('/stop', user_id='42'))
    assert not db.subscribers_active() and controller.mock_calls == []


def test_direct_controller_handlers_and_worker_submission_are_inert(menu_loop, monkeypatch):
    import predbot.bot.ticket_audit as audit
    factory, booking_factory, job, runner = Mock(), Mock(), Mock(), Mock()
    thread = Mock(side_effect=AssertionError('disabled feature created a thread'))
    monkeypatch.setattr(audit.threading, 'Thread', thread)
    controller = audit.TicketAuditController(menu_loop.tg, menu_loop.cfg,
        backend_factory=factory, booking_factory=booking_factory, runner=runner)
    message = message_update('/start ticket_audit', user_id='42')['message']
    assert controller.start(message) is False
    assert controller.message({**message, 'text': 'BOOK123'}) is False
    controller.callback(button_update('ta|old|confirm', '42')['callback_query'])
    assert controller._submit(job) is False
    assert not controller.sessions and not controller.rates
    assert not menu_loop.tg.sends and not menu_loop.tg.answers
    assert not any(m.mock_calls for m in (factory, booking_factory, job, runner, thread))


@pytest.mark.parametrize('view', PUBLIC_VIEWS)
def test_existing_prediction_views_work_without_audit(cached_loop, view):
    controller = Mock()
    cached_loop._ticket_audit = controller
    cached_loop._handle_update(message_update('/' + view, user_id='42'))
    cached_loop._handle_update(button_update('pg|' + view + '|0'))
    assert len(cached_loop.tg.sends) == 2
    assert controller.mock_calls == []
    cached_loop.service.chain.collect.assert_not_called()


def test_backend_factories_do_not_read_settings_create_sidecars_or_clients(tmp_path, monkeypatch):
    from predbot.ticket_audit import booking, runtime
    from predbot.ticket_audit.schemas import AuditUnavailable
    cfg = Config(db_path=str(tmp_path / 'predictions.sqlite3'))
    settings = Mock(side_effect=AssertionError('disabled settings must not be read'))
    monkeypatch.setattr(runtime.Settings, 'from_environment', settings)
    monkeypatch.setattr(booking.BookingSettings, 'from_environment', settings)
    with pytest.raises(AuditUnavailable, match='ticket_audit_disabled'):
        runtime.build_backend(cfg)
    assert booking.build_booking_service(cfg) is None
    settings.assert_not_called()
    assert list(tmp_path.iterdir()) == []


def test_gemini_gate_blocks_before_quota_and_request():
    from predbot.ticket_audit.gemini import GeminiClient, Settings
    from predbot.ticket_audit.schemas import AuditUnavailable
    usage, transport = Mock(), Mock()
    client = GeminiClient(Settings('mock-only', 'gemini-3.5-flash-lite', True, True),
                          usage, transport, lambda: NOW)
    with pytest.raises(AuditUnavailable, match='ticket_audit_disabled'):
        client._request('POST', 'models/gemini-3.5-flash-lite:generateContent', {})
    assert not usage.mock_calls and not transport.mock_calls


@pytest.mark.parametrize('source', ['sportybet', 'mybetcode', 'fetch'])
def test_booking_transport_cannot_run_when_paused(source):
    from predbot.ticket_audit import booking_sources as sources
    from predbot.ticket_audit.booking import BookingCodeFailure, DISABLED
    store, transport = Mock(), Mock()
    with pytest.raises(BookingCodeFailure) as error:
        if source == 'sportybet':
            sources.SportyBetDirectResolver(store, 50, transport).retrieve('sportybet', 'MOCK123', 'hash', NOW)
        elif source == 'mybetcode':
            sources.MyBetCodeResolver(store, 'mock-only', 10, 100, transport).retrieve('bet9ja', 'MOCK123', 'hash', NOW)
        else:
            sources.fetch(transport, 'https://example.invalid/', 'example.invalid', '/')
    assert error.value.status == DISABLED
    assert not store.mock_calls and not transport.mock_calls


def test_research_transport_stops_before_budget_and_http():
    from predbot.ticket_audit.free_research import OpenFootballAdapter
    from predbot.ticket_audit.schemas import AuditUnavailable
    budget, transport = Mock(), Mock()
    adapter = OpenFootballAdapter(budget, transport)
    with pytest.raises(AuditUnavailable, match='ticket_audit_disabled'):
        adapter._download(adapter.base + '2029-30/en.1.json', NOW)
    assert not budget.mock_calls and not transport.mock_calls


def test_structural_logging_is_silent_even_at_debug(monkeypatch, caplog):
    from predbot.ticket_audit import booking_structure
    probe = Mock(side_effect=AssertionError('disabled diagnostic must not inspect payload'))
    monkeypatch.setattr(booking_structure, 'structural_report', probe)
    with caplog.at_level(logging.DEBUG):
        booking_structure.log_structure({'mock': 'data'})
    probe.assert_not_called()
    assert not caplog.records
