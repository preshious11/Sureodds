"""Ready-result promises, private replies, time safety and permanent isolation."""
from dataclasses import replace
from datetime import timedelta
import logging
from math import prod
from unittest.mock import Mock

import pytest
import requests

from predbot import custom_picks as cp
from predbot.bot import callbacks, pagination
from predbot.bot.commands import CommandLoop, recap_card
from predbot.cache.fetcher import UnifiedFetcher
from predbot.config import Config
from predbot.custom_recovery import (CALLBACK_PREFIX, COPY, TTL, MAX_RECOVERY_SEARCHES,
                                    Response, eligible, valid_ticket)
from predbot.jobs import daily_picks, live_results, settle, stats
from predbot.model.ensemble import EventModel
from predbot.providers.chain import ProviderChain, ResultsResolver
from predbot.providers.oddsapi import OddsApiProvider
from predbot.providers.sharpapi import SharpApiProvider
from predbot.providers.sportsgameodds import SportsGameOddsProvider
from predbot.util.timeutil import day_bounds, iso, to_local
from tests.test_community_menu import message_update
from tests.test_custom_pick_candidates import alternatives, prepared
from tests.test_custom_picks import last_reply, NOW, DAY, ZONE, rows_for, store, private_bot


def rows(sports=('football', 'basketball', 'tennis'), count=3, **kwargs):
    identities = {'football': 0, 'basketball': 10, 'tennis': 20}
    return [row for sport in sports for i in range(count)
            for row in rows_for(identities[sport] + i + 1, sport, **kwargs)]


def buttons(response):
    markup = response.reply_markup if isinstance(response, Response) else response['reply_markup']
    return [b for row in (markup or {}).get('inline_keyboard', []) for b in row]


def callback(data, user='42', chat='42', kind='private'):
    return {'id': 'recovery-tap', 'data': data, 'from': {'id': int(user)},
            'message': {'message_id': 123, 'chat': {'id': int(chat), 'type': kind}}}


def request(loop, text, user='42', chat='42'):
    loop._custom_users.add((user, chat))
    loop._handle_update(message_update(text, chat, 'private', user))
    return last_reply(loop.tg)


def promise(loop, button, now=NOW):
    data = button['callback_data']
    assert data.startswith(CALLBACK_PREFIX) and len(data.encode('utf-8')) <= 64
    assert data[len(CALLBACK_PREFIX):].isalnum() or all(c.isalnum() or c in '-_' for c in data[len(CALLBACK_PREFIX):])
    suggestion = loop._custom_picks.recovery.ready[data[len(CALLBACK_PREFIX):]]
    assert suggestion.label == button['text']
    assert valid_ticket(suggestion.ticket, suggestion.request, now)
    ticket, spec = suggestion.ticket, suggestion.request
    assert len(ticket.picks) == len({c.fixture_id for c in ticket.picks})
    assert all(c.start > now and c.probability >= cp.MIN_PROBABILITY[spec.preference] for c in ticket.picks)
    assert all(not spec.sports or c.sport in spec.sports for c in ticket.picks)
    assert all(not spec.allowed_markets or c.market in spec.allowed_markets for c in ticket.picks)
    assert all(c.market not in spec.excluded_markets for c in ticket.picks)
    assert ticket.odds == pytest.approx(prod(c.price for c in ticket.picks))
    if spec.pick_count:
        assert len(ticket.picks) == spec.pick_count
    if spec.target_odds:
        assert .9 * spec.target_odds <= ticket.odds <= 1.1 * spec.target_odds
    assert suggestion.created_at == NOW and suggestion.prediction_date == DAY
    assert suggestion.pool_generated_at == NOW
    assert suggestion.evaluation_times == tuple(c.evaluated_at for c in ticket.picks)
    assert suggestion.fixture_start_times == tuple(c.start for c in ticket.picks)
    assert suggestion.expires_at == min(NOW + TTL, day_bounds(NOW, ZONE)[1], min(suggestion.fixture_start_times))
    return suggestion


@pytest.mark.parametrize('text,pool_sports,options,reason,headline', [
    ('3 picks', (), {}, 'archive_unavailable', 'Still cooking'),
    ('3 football picks', ('basketball',), {}, 'sport_unavailable', 'No football picks made the cut today'),
    ('3 basketball picks', ('football',), {}, 'sport_unavailable', 'No basketball on the menu today'),
    ('2 tennis picks', ('football',), {}, 'sport_unavailable', 'The court is quiet today'),
    ('2 baseball picks', ('football',), {}, 'sport_unavailable', 'Nothing cooking for baseball today'),
    ('BTTS only', ('football',), {}, 'market_unavailable', 'That market is a little dry today'),
    ('basketball double chance only', ('basketball',), {}, 'sport_market_unavailable', "That combo isn't on the board today"),
    ('5 picks', ('football',), {}, 'insufficient_count', "We're a few games short"),
    ('20 odds', ('football',), {}, 'target_unreachable', "Couldn't stretch it to 20 odds"),
    ('1.1 odds', ('football',), {}, 'target_too_low', "That's a tight squeeze"),
    ('DC only no DC', ('football',), {}, 'conflicting_constraints', "I didn't quite catch that"),
    ('3 picks', ('football',), {'start': NOW}, 'all_started', 'We missed the window'),
    ('3 basketball picks', ('football',), {'partial': True}, 'partial_insufficient', 'Slim pickings right now'),
    ('hello', ('football',), {}, 'invalid_request', "I didn't quite catch that"),
    ('3 picks tomorrow', ('football',), {}, 'future_date', "I'm living in today for now"),
    ('safe 3 picks', ('football',), {'probability': .3}, 'below_quality_floor', 'Nothing made the cut today'),
    ('3 odds', ('football',), {}, 'search_failed', "Almost, but I won't force it"),
    ('no match result', ('football',), {}, 'conflicting_constraints', 'You boxed me in there'),
])
def test_every_empty_reason_gets_a_private_response(private_bot, db, text, pool_sports, options, reason, headline):
    options = dict(options)
    partial = options.pop('partial', False)
    if pool_sports:
        store(db, rows(pool_sports, **options), partial=partial)
    structured = private_bot._custom_picks.respond(text, '42', '42', NOW)
    assert structured.reason == reason
    sent = request(private_bot, text)
    assert sent['chat_id'] == '42' and headline in sent['text']
    assert not sent['text'].startswith('🔥 ')
    assert all(word not in sent['text'] for word in ('SQLite', 'provider', 'database', 'Traceback', 'beam', 'TARGET_ODDS'))
    for button in buttons(sent):
        promise(private_bot, button)
    if reason == 'archive_unavailable':
        assert sent['text'] == cp.UNAVAILABLE and not buttons(sent)


@pytest.mark.parametrize('sports', [('football',), ('basketball',), ('tennis',),
                                  ('football', 'basketball'), ('basketball', 'tennis'),
                                  ('football', 'basketball', 'tennis')])
def test_recovery_reflects_actual_multisport_pool(private_bot, db, sports):
    store(db, rows(sports))
    sent = request(private_bot, '3 baseball picks')
    assert 1 <= len(buttons(sent)) <= 3
    recovered_sports = set()
    for button in buttons(sent):
        suggestion = promise(private_bot, button)
        recovered_sports |= {c.sport for c in suggestion.ticket.picks}
        assert suggestion.request.sports <= set(sports)
        assert 'Baseball' not in button['text']
    assert recovered_sports <= set(sports) and recovered_sports
    if len(sports) == 3:
        assert len(buttons(sent)) == 3


def test_insufficient_tennis_offers_proven_smaller_count(private_bot, db):
    store(db, rows(('tennis',), count=2) + rows(('basketball',), count=5) + rows(('football',), count=5))
    sent = request(private_bot, '5 tennis picks')
    proven = [promise(private_bot, b) for b in buttons(sent)]
    assert proven[0].request.sports == {'tennis'} and proven[0].request.pick_count == 2
    assert '2 Tennis Picks' in proven[0].label
    assert any(not s.request.sports for s in proven)
    assert all('3 Tennis Picks' not in s.label and '5 Tennis Picks' not in s.label for s in proven)


def test_missing_basketball_market_only_offers_real_alternatives(private_bot, db):
    store(db, rows(('basketball', 'tennis')))
    sent = request(private_bot, 'basketball double chance only')
    proven = [promise(private_bot, b) for b in buttons(sent)]
    assert proven and proven[0].request.sports == {'basketball'}
    assert all('Double Chance' not in s.label for s in proven)
    assert all('dc' not in {c.market for c in s.ticket.picks} for s in proven)


def test_moneyline_recovery_is_prevalidated_when_it_differs(private_bot, db):
    pool = rows(('basketball',))
    # Three strongest totals on one set of fixtures; moneylines on another set.
    for row in pool:
        row.update(market='total', selection='over', line=200.5, p_model=.85)
    pool += [r for i in range(51, 54) for r in rows_for(i, 'basketball', probability=.75)]
    store(db, pool)
    sent = request(private_bot, 'basketball double chance only')
    proven = [promise(private_bot, b) for b in buttons(sent)]
    assert any(s.request.allowed_markets == {'result2'} and 'To Win' in s.label for s in proven)


def test_odds_ladder_only_displays_successful_distinct_tickets(private_bot, db, monkeypatch):
    store(db, rows(('football', 'basketball', 'tennis'), count=2))
    original = cp.search
    searched = []
    def recorded(pool, spec, *budget):
        ticket = original(pool, spec, *budget)
        searched.append((spec, ticket))
        return ticket
    monkeypatch.setattr(cp, 'search', recorded)
    sent = request(private_bot, '100 odds')
    proven = [promise(private_bot, b) for b in buttons(sent)]
    assert 1 <= len(proven) <= 3
    assert len(searched) <= 1 + MAX_RECOVERY_SEARCHES
    assert any(spec.target_odds == 20 and ticket is None for spec, ticket in searched)
    assert any(s.request.target_odds and s.request.target_odds < 100 for s in proven)
    assert all((s.request, s.ticket) in searched for s in proven)
    assert len({frozenset(c.fixture_id for c in s.ticket.picks) for s in proven}) == len(proven)
    targets = [s.ticket.odds for s in proven if s.request.target_odds]
    assert len(targets) <= 2
    assert all(max(a, b) / min(a, b) >= 1.35 for i, a in enumerate(targets) for b in targets[i + 1:])


def test_original_quality_is_not_lowered_and_alternative_is_explicit(private_bot, db):
    store(db, rows(('basketball',), probability=.55))
    sent = request(private_bot, 'safe 3 basketball picks')
    assert 'Nothing made the cut' in sent['text'] and not sent['text'].startswith('🔥 ')
    for button in buttons(sent):
        suggestion = promise(private_bot, button)
        assert suggestion.request.preference == 'safe'
    assert not buttons(sent)


def test_bounded_search_failure_is_not_silent_or_speculative(private_bot, db, monkeypatch):
    store(db, rows())
    monkeypatch.setattr(cp, 'search', Mock(return_value=None))
    sent = request(private_bot, '3 picks')
    assert COPY['search_failed'] == sent['text'] and not buttons(sent)


@pytest.mark.parametrize('failure', ['read', 'search', 'parse', 'respond'])
def test_unexpected_local_errors_receive_safe_private_reply(private_bot, db, monkeypatch, failure):
    store(db, rows())
    broken = Mock(side_effect=RuntimeError('secret SQL database stack trace'))
    if failure == 'read':
        monkeypatch.setattr(db, 'get_custom_pick_pool', broken)
    elif failure == 'respond':
        monkeypatch.setattr(private_bot._custom_picks, 'respond', broken)
    else:
        monkeypatch.setattr(cp, 'search' if failure == 'search' else 'parse_request', broken)
    sent = request(private_bot, '3 picks')
    assert sent['chat_id'] == '42' and sent['text'] == COPY['search_failed']
    assert 'secret' not in sent['text']


def test_malformed_row_does_not_hide_good_pool(private_bot, db, monkeypatch):
    store(db, rows())
    original = db.get_custom_pick_candidates
    monkeypatch.setattr(db, 'get_custom_pick_candidates', lambda *args: [None, {'sport': []}] + original(*args))
    assert request(private_bot, '3 picks')['text'].startswith('🔥 ')


def test_discovery_uses_one_snapshot_and_valid_tap_needs_no_reads_or_search(private_bot, db, monkeypatch):
    store(db, rows())
    pool_reads = Mock(wraps=db.get_custom_pick_pool)
    candidate_reads = Mock(wraps=db.get_custom_pick_candidates)
    monkeypatch.setattr(db, 'get_custom_pick_pool', pool_reads)
    monkeypatch.setattr(db, 'get_custom_pick_candidates', candidate_reads)
    sent = request(private_bot, '3 baseball picks')
    pool_reads.assert_called_once_with(DAY, ZONE)
    candidate_reads.assert_called_once_with(DAY, ZONE)
    button = buttons(sent)[0]
    suggestion = promise(private_bot, button)
    forbidden = Mock(side_effect=AssertionError('valid tap recomputed a promise'))
    monkeypatch.setattr(cp, 'search', forbidden)
    monkeypatch.setattr(db, 'get_custom_pick_pool', forbidden)
    monkeypatch.setattr(db, 'get_custom_pick_candidates', forbidden)
    private_bot.service = None  # Custom callbacks must work independently of the normal view service.
    before = len(private_bot.tg.sends)
    private_bot._dispatch_callback(callback(button['callback_data']))
    assert len(private_bot.tg.sends) == before + 1
    assert private_bot.tg.sends[-1]['text'] == cp.format_ticket(suggestion.ticket, suggestion.partial, ZONE)
    assert private_bot.tg.sends[-1]['chat_id'] == '42'
    assert private_bot.tg.answers[-1]['id'] == 'recovery-tap'
    forbidden.assert_not_called()


@pytest.mark.parametrize('user,chat,kind', [('99', '42', 'private'), ('42', '99', 'private'),
                                         ('42', '-1001234', 'supergroup'), ('42', '-1234', 'group'),
                                         ('42', '-1234', 'channel'), ('42', '42', None)])
def test_ownership_or_public_chat_is_rejected(private_bot, db, user, chat, kind):
    store(db, rows())
    button = buttons(request(private_bot, 'baseball picks'))[0]
    before = len(private_bot.tg.sends)
    private_bot._dispatch_callback(callback(button['callback_data'], user, chat, kind))
    assert len(private_bot.tg.sends) == before
    assert 'your own' in private_bot.tg.answers[-1]['text']


@pytest.mark.parametrize('offset', [TTL, TTL + timedelta(seconds=1), timedelta(hours=11)])
def test_ttl_and_local_midnight_expire_then_recompute_proven_options(private_bot, db, monkeypatch, offset):
    store(db, rows(start=NOW + timedelta(days=1)))
    sent = request(private_bot, 'baseball picks')
    old = {b['callback_data'] for b in buttons(sent)}
    later = NOW + offset
    monkeypatch.setattr(cp, 'utcnow', lambda: later)
    private_bot._dispatch_callback(callback(next(iter(old))))
    fresh = private_bot.tg.sends[-1]
    assert fresh['chat_id'] == '42' and 'That option has expired' in fresh['text']
    assert not old & {b['callback_data'] for b in buttons(fresh)}
    for button in buttons(fresh):
        suggestion = private_bot._custom_picks.recovery.ready[button['callback_data'][len(CALLBACK_PREFIX):]]
        assert valid_ticket(suggestion.ticket, suggestion.request, later)
        assert suggestion.prediction_date == to_local(later, ZONE).date().isoformat()
    if offset == timedelta(hours=11):
        assert fresh['text'] == COPY['expired_empty'] and not buttons(fresh)
        assert not private_bot._custom_picks.recovery.ready


def test_exact_start_expires_and_uses_other_cached_options_without_reads(private_bot, db, monkeypatch):
    store(db, rows(('football',), start=NOW + timedelta(minutes=2)) + rows(('tennis',), start=NOW + timedelta(hours=2)))
    sent = request(private_bot, 'baseball picks')
    proven = [promise(private_bot, b) for b in buttons(sent)]
    stale = next(s for s in proven if min(s.fixture_start_times) == NOW + timedelta(minutes=2))
    # A football-only selected option cannot be silently replaced with tennis.
    private_bot._custom_picks.recovery.ready[stale.token] = replace(
        stale, request=replace(stale.request, sports=frozenset({'football'})))
    monkeypatch.setattr(cp, 'utcnow', lambda: NOW + timedelta(minutes=2))
    forbidden = Mock(side_effect=AssertionError('already cached replacement must not reread'))
    monkeypatch.setattr(db, 'get_custom_pick_pool', forbidden)
    monkeypatch.setattr(cp, 'search', forbidden)
    private_bot._dispatch_callback(callback(CALLBACK_PREFIX + stale.token))
    sent = private_bot.tg.sends[-1]
    assert 'expired' in sent['text']
    assert buttons(sent)
    for button in buttons(sent):
        suggestion = private_bot._custom_picks.recovery.ready[button['callback_data'][len(CALLBACK_PREFIX):]]
        assert all(c.sport == 'tennis' and c.start > NOW + timedelta(minutes=2) for c in suggestion.ticket.picks)
    forbidden.assert_not_called()


def test_compatible_cached_replacement_serves_without_relaxation(private_bot, db, monkeypatch):
    store(db, rows(('football',), start=NOW + timedelta(minutes=2)) + rows(('tennis',), start=NOW + timedelta(hours=2)))
    sent = request(private_bot, 'baseball picks')
    proven = [promise(private_bot, b) for b in buttons(sent)]
    stale = next(s for s in proven if min(s.fixture_start_times) == NOW + timedelta(minutes=2))
    later_ticket = next(s for s in proven if all(c.start > NOW + timedelta(minutes=2) for c in s.ticket.picks))
    # Explicit all-sports, count-three request: the surviving ticket also fulfills it.
    original = replace(stale, request=cp.Request(pick_count=3))
    private_bot._custom_picks.recovery.ready[stale.token] = original
    monkeypatch.setattr(cp, 'utcnow', lambda: NOW + timedelta(minutes=2))
    private_bot._dispatch_callback(callback(CALLBACK_PREFIX + stale.token))
    assert private_bot.tg.sends[-1]['text'] == cp.format_ticket(later_ticket.ticket, later_ticket.partial, ZONE)


def test_all_started_tap_is_helpful_and_never_returns_stale_ticket(private_bot, db, monkeypatch):
    store(db, rows(('football',), start=NOW + timedelta(minutes=2)))
    button = buttons(request(private_bot, 'baseball picks'))[0]
    monkeypatch.setattr(cp, 'utcnow', lambda: NOW + timedelta(minutes=2))
    private_bot._dispatch_callback(callback(button['callback_data']))
    assert private_bot.tg.sends[-1]['text'] == COPY['expired_empty']
    assert not buttons(private_bot.tg.sends[-1])


@pytest.mark.parametrize('missing', ['random', 'pruned', 'restart'])
def test_missing_tokens_are_recoverable_with_proven_options(private_bot, db, missing):
    store(db, rows())
    button = buttons(request(private_bot, 'baseball picks'))[0]
    if missing == 'random':
        button = {'callback_data': CALLBACK_PREFIX + 'unknown'}
    else:
        private_bot._custom_picks.recovery.ready.clear()
    if missing == 'restart':
        private_bot = CommandLoop(private_bot.tg, db, private_bot.cfg, {}, service=None)
        private_bot._handle_update(message_update('/start custom_picks', user_id='42'))
    private_bot._dispatch_callback(callback(button['callback_data']))
    sent = private_bot.tg.sends[-1]
    assert sent['chat_id'] == '42' and 'expired' in sent['text']
    assert buttons(sent)
    for button in buttons(sent):
        promise(private_bot, button)


def test_missing_token_no_archive_has_no_fake_buttons(private_bot):
    private_bot._custom_users.add(('42', '42'))
    private_bot._dispatch_callback(callback(CALLBACK_PREFIX + 'unknown'))
    assert private_bot.tg.sends[-1]['text'] == COPY['expired_empty']
    assert not buttons(private_bot.tg.sends[-1])


def test_local_midnight_is_expiry_even_before_ttl(private_bot, db, monkeypatch):
    now = NOW.replace(hour=22, minute=58)
    monkeypatch.setattr(cp, 'utcnow', lambda: now)
    store(db, rows(start=NOW + timedelta(days=1)), at=now)
    button = buttons(request(private_bot, 'baseball picks'))[0]
    suggestion = private_bot._custom_picks.recovery.ready[button['callback_data'][len(CALLBACK_PREFIX):]]
    assert suggestion.expires_at == NOW.replace(hour=23)
    assert suggestion.expires_at < now + TTL
    monkeypatch.setattr(cp, 'utcnow', lambda: now + timedelta(minutes=2))
    private_bot._dispatch_callback(callback(button['callback_data']))
    assert private_bot.tg.sends[-1]['text'] == COPY['expired_empty']


def test_followups_do_not_invalidate_still_current_tokens(private_bot, db):
    store(db, rows())
    button = buttons(request(private_bot, 'baseball picks'))[0]
    for _ in range(10):
        request(private_bot, '3 picks')
    private_bot._dispatch_callback(callback(button['callback_data']))
    assert private_bot.tg.sends[-1]['text'].startswith('🔥 ')


def test_acknowledgement_failure_does_not_hide_private_ticket(private_bot, db, monkeypatch):
    store(db, rows())
    button = buttons(request(private_bot, 'baseball picks'))[0]
    monkeypatch.setattr(private_bot.tg, 'answer_callback_query', Mock(side_effect=RuntimeError('temporary Telegram failure')))
    private_bot._dispatch_callback(callback(button['callback_data']))
    assert private_bot.tg.sends[-1]['text'].startswith('🔥 ')


@pytest.mark.parametrize('view', ['top', 'daily', 'all', 'stats', 'yesterday'])
@pytest.mark.parametrize('chat', ['42', '-1001234567890'])
def test_existing_views_keep_their_dispatch(private_bot, monkeypatch, view, chat):
    routed = Mock()
    monkeypatch.setattr('predbot.bot.commands.handle_callback', routed)
    cb = callback(pagination.encode_callback(view, 0), chat=chat, kind='private' if chat == '42' else 'supergroup')
    private_bot._dispatch_callback(cb)
    routed.assert_called_once_with(cb, service=private_bot.service, tg=private_bot.tg, db=private_bot.db,
                                  cfg=private_bot.cfg, page_size=private_bot.page_size)


@pytest.mark.parametrize('users,iterations', [(1, 1), (1, 10), (5, 2)])
def test_normal_preparation_taps_expiry_are_read_only_untracked_zero_provider(private_bot, db, monkeypatch, users, iterations):
    store(db, rows())
    ps = prepared(alternatives(), private_bot.cfg, archive=False)
    daily_picks.store_picks(ps, db, DAY)
    from predbot.selection.picker import package_predictions
    package_predictions(db, DAY, private_bot.cfg, iso(NOW))
    def snapshot():
        return {
            'database': list(db.conn.iterdump()),
            'pending': [dict(r) for r in db.pending_picks()],
            'watches': db.live_picks(iso(NOW), iso(NOW + timedelta(hours=10))),
            'stats': stats.compute(db, NOW),
            'calibration_samples': [dict(r) for r in db.all_settled_for_calibration()],
            'recap': recap_card(db, to_local(NOW, ZONE)),
            'collections': {c: [dict(r) for r in db.collection_predictions(c, DAY)] for c in ('all', 'top', 'daily')},
        }
    before = snapshot()
    forbidden = Mock(side_effect=AssertionError('Custom Picks invoked providers/models/jobs/tracking'))
    for obj, method in ((ProviderChain, 'collect'), (UnifiedFetcher, 'get_daily_snapshot'),
                        (daily_picks, 'build_picks'), (EventModel, 'score'), (EventModel, 'score_all'),
                        (OddsApiProvider, 'fetch_events'), (OddsApiProvider, '_get'),
                        (SharpApiProvider, 'fetch_events'), (SportsGameOddsProvider, 'fetch_events'),
                        (SportsGameOddsProvider, '_call'), (requests.Session, 'request'),
                        (settle, 'run'), (live_results, 'run'), (ResultsResolver, 'resolve')):
        monkeypatch.setattr(obj, method, forbidden)
    statements = []
    db.conn.set_trace_callback(statements.append)
    try:
        for user in range(42, 42 + users):
            for _ in range(iterations):
                assert request(private_bot, '3 picks', str(user), str(user))['text'].startswith('🔥 ')
                sent = request(private_bot, 'baseball picks', str(user), str(user))
                assert len(buttons(sent)) == 3
                private_bot._dispatch_callback(callback(buttons(sent)[0]['callback_data'], str(user), str(user)))
                assert private_bot.tg.sends[-1]['text'].startswith('🔥 ')
                monkeypatch.setattr(cp, 'utcnow', lambda: NOW + TTL)
                private_bot._dispatch_callback(callback(buttons(sent)[0]['callback_data'], str(user), str(user)))
                assert 'expired' in private_bot.tg.sends[-1]['text']
                for b in buttons(private_bot.tg.sends[-1]):
                    cached = private_bot._custom_picks.recovery.ready[b['callback_data'][len(CALLBACK_PREFIX):]]
                    assert valid_ticket(cached.ticket, cached.request, NOW + TTL)
                monkeypatch.setattr(cp, 'utcnow', lambda: NOW)
                private_bot._dispatch_callback(callback(CALLBACK_PREFIX + 'missing', str(user), str(user)))
    finally:
        db.conn.set_trace_callback(None)
    assert snapshot() == before  # Includes archive rows, subscribers, ALL tables and result-work inputs.
    assert statements and all(s.lstrip().upper().startswith('SELECT') for s in statements)
    assert all('custom_pick_candidates' in s or 'custom_pick_pools' in s for s in statements)
    forbidden.assert_not_called()
    assert private_bot.service.mock_calls == []
    assert private_bot._admin_threads == []


def test_aggregate_logs_have_reasons_labels_and_status_without_raw_text(private_bot, db, caplog):
    store(db, rows())
    with caplog.at_level(logging.INFO):
        sent = request(private_bot, 'please give me 3 baseball picks')
        private_bot._dispatch_callback(callback(buttons(sent)[0]['callback_data']))
        private_bot._custom_users.add(('99', '42'))
        private_bot._dispatch_callback(callback(buttons(sent)[0]['callback_data'], user='99'))
        private_bot._dispatch_callback(callback(CALLBACK_PREFIX + 'malicious-raw-payload'))
    assert 'reason=sport_unavailable' in caplog.text and 'suggestions=3' in caplog.text
    assert 'status=served' in caplog.text and 'status=ownership_rejected' in caplog.text and 'status=missing' in caplog.text
    assert 'please give me' not in caplog.text and 'malicious-raw-payload' not in caplog.text
    assert 'Combined Odds' not in caplog.text
