"""Scheduled publication and empty-Top navigation through real SQLite services, offline."""
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock
import socket

import pytest

from predbot.bot import callbacks, pagination
from predbot.bot.commands import CommandLoop
from predbot.cache.fetcher import UnifiedFetcher
from predbot.config import Config
from predbot.jobs import daily_picks, scheduler
from predbot.storage.db import Database
from tests.test_community_menu import MenuTelegram, COMMUNITY, BOT_USERNAME, button_update
from tests.test_subscribers_broadcast import _snapshot

NOW = datetime(2030, 6, 1, 9, 10, tzinfo=timezone.utc)
DAY = '2030-06-01'


class PublicationTelegram(MenuTelegram):
    chat_id = COMMUNITY

    def enabled(self):
        return True

    def send(self, text, chat_id=None, reply_markup=None, parse_mode='HTML'):
        return super().send(text, chat_id or self.chat_id, reply_markup, parse_mode)


@pytest.fixture
def publication(db, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('publication/navigation must not regenerate or access the network')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(daily_picks, 'build_picks', forbidden)
    monkeypatch.setattr('predbot.cache.fetcher.utcnow', lambda: NOW)
    cfg = Config(timezone='UTC', post_time='09:00', telegram_chat_id=COMMUNITY,
                 subscriber_delivery_enabled=False)
    chain = Mock()
    chain.collect.side_effect = forbidden
    service = UnifiedFetcher(chain, db, cfg)
    snapshot = _snapshot()
    snapshot.all_predictions = snapshot.top_picks + snapshot.daily_picks
    service._store(snapshot, NOW)
    return SimpleNamespace(cfg=cfg, tg=PublicationTelegram(), service=service, snapshot=snapshot, chain=chain)


def publish(db, p):
    return daily_picks.run(p.chain, db, p.cfg, p.tg, service=p.service)


@pytest.mark.parametrize('kind', ['picks_top', 'picks_daily'])
def test_destination_receipt_alone_prevents_republication(db, publication, kind):
    db.mark_delivery('community_' + kind, DAY, COMMUNITY)
    publish(db, publication)
    prefix = '🔥' if kind == 'picks_top' else '📋'
    assert not any(s['text'].startswith(prefix) for s in publication.tg.sends)


def test_completed_empty_generation_publishes_top_once(db, publication):
    snapshot = replace(publication.snapshot, top_picks=[], daily_picks=[], all_predictions=[])
    publication.service._store(snapshot, NOW)
    publish(db, publication)
    assert len(publication.tg.sends) == 2
    assert publication.tg.sends[0]['text'] == '🔥 BET OF THE DAY · Sat 1 Jun\nNo Bet of the Day for today.\n\nSee other picks:\n\nBet only what you can afford to lose.'
    assert db.delivery_done('community_picks_top', DAY, COMMUNITY)


@pytest.mark.parametrize('partial', [False, True])
@pytest.mark.parametrize('empty_top', [False, True])
def test_restart_and_repeated_polling_send_each_card_once(db, publication, partial, empty_top, monkeypatch):
    p = publication
    snapshot = replace(p.snapshot, status='PARTIAL' if partial else 'COMPLETE',
                       top_picks=[] if empty_top else p.snapshot.top_picks)
    p.service._store(snapshot, NOW)
    publish(db, p)
    assert len(p.tg.sends) == 2
    assert {r['kind'] for r in db.query('SELECT kind FROM posts')} == {'picks_top', 'picks_daily'}
    for _ in range(3):
        publish(db, p)
    path = db.path
    db.close()
    monkeypatch.setenv('AUDIT_GEMINI_ENABLED', 'true')
    monkeypatch.setenv('AUDIT_BOOKING_CODES_ENABLED', 'true')
    reopened = Database(path)
    try:
        p.service = UnifiedFetcher(p.chain, reopened, p.cfg)
        publish(reopened, p)
        assert len(p.tg.sends) == 2
        assert reopened.delivery_done('community_picks_top', DAY, COMMUNITY)
        assert reopened.delivery_done('community_picks_daily', DAY, COMMUNITY)
    finally:
        reopened.close()
    p.chain.collect.assert_not_called()


def test_before_schedule_and_legitimate_restart_catch_up(db, publication, monkeypatch):
    class InlineThread:
        def __init__(self, target, args, **kwargs):
            self.target, self.args = target, args
        def start(self):
            self.target(*self.args)
    monkeypatch.setattr(scheduler, 'threading', SimpleNamespace(Thread=InlineThread))
    p = publication
    before = NOW.replace(hour=8, minute=50)
    job = scheduler.DailyJob('picks', '09:00', 'UTC', lambda: publish(db, p))
    assert not job.due(before) and not p.tg.sends
    restarted = scheduler.DailyJob('picks', '09:00', 'UTC', lambda: publish(db, p))
    assert restarted.due(NOW)
    restarted.run(NOW)
    assert len(p.tg.sends) == 2 and not restarted.due(NOW)
    second_restart = scheduler.DailyJob('picks', '09:00', 'UTC', lambda: publish(db, p))
    assert second_restart.due(NOW)  # scheduler still catches up; receipts suppress sends
    second_restart.run(NOW)
    assert len(p.tg.sends) == 2


def assert_ctas(markup):
    rows = markup['inline_keyboard']
    assert [[b['text'] for b in row] for row in rows] == [['📋 Daily Picks', '📚 All Predictions'], ['✨ Custom Picks']]
    assert rows[0][0]['callback_data'] == pagination.encode_callback('daily', 0)
    assert rows[0][1]['callback_data'] == pagination.encode_callback('all', 0)
    assert rows[1][0] == callbacks.community_menu_markup(BOT_USERNAME)['inline_keyboard'][3][0]


def test_automatic_and_pinned_menu_empty_top_have_same_ctas_and_cached_navigation(db, publication, monkeypatch):
    p = publication
    snapshot = replace(p.snapshot, top_picks=[], status='PARTIAL')
    p.service._store(snapshot, NOW)
    publish(db, p)
    auto = p.tg.sends[0]
    assert_ctas(auto['reply_markup'])
    loop = CommandLoop(p.tg, db, p.cfg, {}, service=p.service)
    monkeypatch.setattr(callbacks, 'navigation_mode', lambda cfg: 'edit')
    loop._handle_update(button_update(pagination.encode_callback('top', 0)))
    pinned = p.tg.sends[-1]
    assert pinned['text'] == auto['text'] and pinned['chat_id'] == COMMUNITY
    assert_ctas(pinned['reply_markup'])
    assert not p.tg.edits  # pinned menu remains intact
    for entry in (auto, pinned):
        for button in entry['reply_markup']['inline_keyboard'][0]:
            loop._handle_update(button_update(button['callback_data'], text=entry['text']))
    p.chain.collect.assert_not_called()
    assert p.service.api_batches == 0
    for phrase in ('being analyzed', 'Our model', 'Check back soon', '09:00 AM'):
        assert phrase not in auto['text']


@pytest.mark.parametrize('reason', ['before_post_time', 'generating', 'awaiting scheduled fetch'])
def test_ungenerated_snapshot_is_not_a_completed_empty_publication(db, publication, reason):
    p = publication
    snapshot = replace(p.snapshot, status='PARTIAL', top_picks=[], daily_picks=[], all_predictions=[],
                       generation={'complete': False, 'reason': reason})
    p.service._store(snapshot, NOW)
    publish(db, p)
    assert not p.tg.sends and not db.query('SELECT 1 FROM posts')
    text, _ = pagination.render_page(snapshot, 'top', 0, 5, 'UTC')
    assert 'No Bet of the Day for today.' not in text


def test_partial_evaluation_with_no_qualifiers_still_publishes_empty_top(db, publication):
    p = publication
    snapshot = replace(p.snapshot, status='PARTIAL', top_picks=[], daily_picks=[], all_predictions=[],
                       generation={'complete': False, 'top': {'selected': 0}})
    p.service._store(snapshot, NOW)
    publish(db, p)
    assert len(p.tg.sends) == 2
    assert_ctas(p.tg.sends[0]['reply_markup'])


def test_snapshot_status_and_generation_timestamp_do_not_change_receipt_identity(db, publication):
    p = publication
    p.service._store(replace(p.snapshot, status='PARTIAL'), NOW)
    publish(db, p)
    p.service._store(replace(p.snapshot, status='COMPLETE', generated_at='2030-06-01T10:00:00Z'), NOW)
    publish(db, p)
    assert len(p.tg.sends) == 2


def test_foreign_destination_receipts_do_not_impersonate_this_destination(db, publication):
    for kind in ('picks_top', 'picks_daily'):
        db.mark_delivery('community_' + kind, DAY, '-1009999999999')
    publish(db, publication)
    assert len(publication.tg.sends) == 2
    assert all(s['chat_id'] == COMMUNITY for s in publication.tg.sends)
    for kind in ('picks_top', 'picks_daily'):
        assert db.delivery_done('community_' + kind, DAY, COMMUNITY)


def test_empty_top_without_a_private_bot_link_is_deferred_not_posted_as_a_dead_end(db, publication):
    p = publication
    p.tg.username = None
    p.service._store(replace(p.snapshot, top_picks=[]), NOW)
    publish(db, p)
    assert not p.tg.sends and not db.post_done('picks_top', DAY)
    p.tg.username = BOT_USERNAME
    publish(db, p)
    assert len(p.tg.sends) == 2
    assert_ctas(p.tg.sends[0]['reply_markup'])


@pytest.mark.parametrize('failed_kind', ['picks_top', 'picks_daily'])
def test_failed_send_retries_only_missing_publication_after_restart(db, publication, failed_kind):
    p = publication
    send = p.tg.send
    prefix = '🔥' if failed_kind == 'picks_top' else '📋'
    p.tg.send = lambda text, **kwargs: None if text.startswith(prefix) else send(text, **kwargs)
    publish(db, p)
    assert not db.delivery_done('community_' + failed_kind, DAY, COMMUNITY)
    p.tg.send = send
    path = db.path
    db.close()
    reopened = Database(path)
    try:
        p.service = UnifiedFetcher(p.chain, reopened, p.cfg)
        publish(reopened, p)
        assert len(p.tg.sends) == 2
    finally:
        reopened.close()


def test_overlapping_publications_from_separate_db_handles_send_once(db, publication):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    p = publication
    second_db = Database(db.path)
    second_service = UnifiedFetcher(p.chain, second_db, p.cfg)
    assert db.publication_lock is second_db.publication_lock
    entered, release = Event(), Event()
    send = p.tg.send

    def slow_send(text, **kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return send(text, **kwargs)

    p.tg.send = slow_send
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(publish, db, p)
            assert entered.wait(timeout=5)
            second = pool.submit(daily_picks.run, p.chain, second_db, p.cfg, p.tg, service=second_service)
            release.set()
            first.result(timeout=10)
            second.result(timeout=10)
        assert len(p.tg.sends) == 2
    finally:
        release.set()
        second_db.close()
