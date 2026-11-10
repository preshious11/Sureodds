from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from predbot.config import Config
from predbot.sporty_booking import (
    AnonymousAuth,
    BookingError,
    BookingQuota,
    BookingRunLock,
    PublicTransport,
    Selection,
    SportyBooking,
    USER_AGENT,
    default_quota_path,
    derive_competition_bindings,
    load_top_picks,
    payload_fingerprint,
)
from predbot.sporty_booking_delivery import (
    VERIFIED_STATUS,
    preflight_owner_delivery,
    send_owner_booking_result,
)
from predbot.storage.db import Database

NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)
START = '2030-01-02T12:00:00Z'


def selection(**changes):
    base = Selection(
        'canonical-1', 'epl', 'football', 'Home', 'Away', START,
        'total', 'full', 'points', 'all', 'over', 2.5, 1.8,
    )
    return replace(base, **changes)


def event_for(item, event_id='sr:match:1'):
    kickoff = int(datetime.fromisoformat(START.replace('Z', '+00:00')).timestamp() * 1000)
    return ('football', 'tournament-1', {
        'eventId': event_id,
        'homeTeamName': item.home,
        'awayTeamName': item.away,
        'estimateStartTime': kickoff,
        'matchStatus': 'Not start',
        'markets': [{
            'id': '18',
            'specifier': 'total=2.5',
            'status': 0,
            'outcomes': [{'id': '12', 'odds': '1.8', 'isActive': 1}],
        }],
    })


def adapter(transport=None):
    return SportyBooking(
        {'epl': 'tournament-1'}, transport=transport, clock=lambda: NOW,
    )


def assert_booking_error(status, call):
    with pytest.raises(BookingError) as exc:
        call()
    assert exc.value.status == status


def readback_for(resolved):
    ticket_selections = []
    outcomes = []
    kickoff = int(datetime.fromisoformat(START.replace('Z', '+00:00')).timestamp() * 1000)
    for leg in resolved:
        ticket_selections.append({
            **leg.payload(),
            'sportId': 'sr:sport:1',
            'parentBetBuilderMarketId': '',
        })
        outcomes.append({
            'eventId': leg.event_id,
            'estimateStartTime': kickoff,
            'matchStatus': 'Not start',
            'homeTeamName': leg.original.home,
            'awayTeamName': leg.original.away,
            'sport': {
                'id': 'sr:sport:1',
                'category': {'tournament': {'id': leg.tournament_id}},
            },
            'markets': [{
                'id': leg.market_id,
                'specifier': leg.specifier,
                'status': 0,
                'outcomes': [{
                    'id': leg.outcome_id,
                    'odds': str(leg.current_odds),
                    'isActive': 1,
                }],
            }],
        })
    return {
        'shareCode': 'SAFE42',
        'ticket': {'selections': ticket_selections},
        'deadline': int((NOW + timedelta(days=1)).timestamp() * 1000),
        'outcomes': outcomes,
        'unavailableOutcomes': [],
    }


class FakeTransport:
    def __init__(self, *, readback=None, post_error=None):
        self.readback = readback
        self.post_error = post_error
        self.calls = []

    def request(self, method, path, *, params=None, payload=None):
        self.calls.append((method, path, payload))
        if method == 'POST':
            if self.post_error:
                raise self.post_error
            return {'bizCode': 10000, 'data': {'shareCode': 'SAFE42'}}
        return {'bizCode': 10000, 'data': self.readback}


def test_exact_fixture_resolution_and_unique_active_market():
    item = selection()
    resolved, = adapter().resolve_selections([item], [event_for(item)])
    assert resolved.payload() == {
        'eventId': 'sr:match:1',
        'marketId': '18',
        'specifier': 'total=2.5',
        'outcomeId': '12',
    }
    changed = event_for(item)
    changed[2]['estimateStartTime'] += 1
    assert_booking_error(
        'fixture_missing_or_ambiguous',
        lambda: adapter().resolve_selections([item], [changed]),
    )


def test_observed_competition_binding_must_be_unique():
    row = {'canonical_competition_id': 'epl', 'league': 'Premier League'}
    assert derive_competition_bindings(
        [row], [{'id': 'observed-17', 'name': 'Premier League'}],
    ) == {'epl': 'observed-17'}
    assert_booking_error(
        'binding_missing',
        lambda: derive_competition_bindings([row], [{'id': 't-2', 'name': 'Other'}]),
    )
    assert_booking_error(
        'binding_ambiguous',
        lambda: derive_competition_bindings(
            [row], [
                {'id': 't-2', 'name': 'Premier League'},
                {'id': 't-3', 'name': 'Premier League'},
            ],
        ),
    )


class CollectionDB:
    def __init__(self, rows, active):
        self.rows = rows
        self.active = active
        self.calls = []

    def collection_predictions(self, collection, day):
        self.calls.append(('collection', collection, day))
        return self.rows

    def active_ticket_revision(self, day):
        self.calls.append(('active', day))
        return self.active


def pick_row(revision=7):
    return {
        'revision_id': revision,
        'canonical_event_id': 'canonical-1',
        'canonical_competition_id': 'epl',
        'league': 'Premier League',
    }


def test_top_picks_use_only_the_active_revision_and_never_regenerate():
    db = CollectionDB([pick_row()], active=7)
    rows, revision = load_top_picks(db, '2030-01-02')
    assert revision == 7
    assert rows == [pick_row()]
    assert db.calls == [
        ('collection', 'top', '2030-01-02'),
        ('active', '2030-01-02'),
    ]


@pytest.mark.parametrize('rows,active', [
    ([pick_row(6), pick_row(7)], 7),
    ([pick_row(7)], None),
    ([pick_row(7)], 6),
])
def test_top_picks_reject_missing_or_mixed_active_revision(rows, active):
    assert_booking_error(
        'revision_mixed',
        lambda: load_top_picks(CollectionDB(rows, active), '2030-01-02'),
    )


def test_database_collection_api_can_open_production_data_read_only(tmp_path):
    path = tmp_path / 'predictions.sqlite3'
    with sqlite3.connect(path) as raw:
        raw.executescript(
            'CREATE TABLE picks (id INTEGER PRIMARY KEY, pick_date TEXT, revision_id INTEGER, '
            'status TEXT, canonical_event_id TEXT, canonical_competition_id TEXT, league TEXT);'
            'CREATE TABLE prediction_memberships (prediction_id INTEGER, collection TEXT, rank INTEGER);'
            'CREATE TABLE active_ticket_revisions (pick_date TEXT, revision_id INTEGER);'
            "INSERT INTO picks VALUES (1,'2030-01-02',7,'pending','canonical-1','epl','Premier League');"
            "INSERT INTO prediction_memberships VALUES (1,'top',1);"
            "INSERT INTO active_ticket_revisions VALUES ('2030-01-02',7);"
        )
    db = Database(str(path), read_only=True)
    try:
        assert len(db.collection_predictions('top', '2030-01-02')) == 1
        assert db.active_ticket_revision('2030-01-02') == 7
        with pytest.raises(sqlite3.OperationalError):
            db.conn.execute("UPDATE picks SET status='won' WHERE id=1")
    finally:
        db.close()
    with sqlite3.connect(path) as raw:
        assert raw.execute('SELECT status FROM picks WHERE id=1').fetchone()[0] == 'pending'


def test_odds_drift_prevents_creation_post(tmp_path):
    item = selection()
    event = event_for(item)
    event[2]['markets'][0]['outcomes'][0]['odds'] = '1.81'
    transport = FakeTransport()
    quota = BookingQuota(str(tmp_path / 'quota.sqlite3'), 1)
    booking = adapter(transport)
    assert_booking_error(
        'odds_changed',
        lambda: booking.create_booking_code([item], quota=quota, events=[event]),
    )
    assert transport.calls == []
    assert quota.used('2030-01-01') == 0


def test_partial_slip_is_rejected_before_quota_or_post(tmp_path):
    first = selection()
    second = selection(
        canonical_event_id='canonical-2',
        home='Other Home',
        away='Other Away',
    )
    transport = FakeTransport()
    quota = BookingQuota(str(tmp_path / 'quota.sqlite3'), 1)
    assert_booking_error(
        'fixture_missing_or_ambiguous',
        lambda: adapter(transport).create_booking_code(
            [first, second], quota=quota, events=[event_for(first)],
        ),
    )
    assert transport.calls == []
    assert quota.used('2030-01-01') == 0


def test_ambiguous_creation_consumes_quota_and_is_never_retried(tmp_path):
    item = selection()
    transport = FakeTransport(post_error=BookingError('timeout'))
    quota = BookingQuota(str(tmp_path / 'quota.sqlite3'), 1)
    assert_booking_error(
        'creation_outcome_unverified_do_not_retry',
        lambda: adapter(transport).create_booking_code(
            [item], quota=quota, events=[event_for(item)],
        ),
    )
    assert [call[:2] for call in transport.calls] == [('POST', '/orders/share')]
    assert quota.used('2030-01-01') == 1
    assert_booking_error(
        'duplicate_payload_do_not_retry',
        lambda: adapter(transport).create_booking_code(
            [item], quota=quota, events=[event_for(item)],
        ),
    )
    assert [call[:2] for call in transport.calls] == [('POST', '/orders/share')]


def test_booking_code_is_extracted_only_after_exact_readback(tmp_path):
    item = selection()
    resolved = adapter().resolve_selections([item], [event_for(item)])
    transport = FakeTransport(readback=readback_for(resolved))
    quota = BookingQuota(str(tmp_path / 'quota.sqlite3'), 1)
    result = adapter(transport).create_booking_code(
        [item], quota=quota, events=[event_for(item)],
    )
    assert result.code == 'SAFE42'
    assert result.programmatic_verified is True
    assert result.verification_reason == VERIFIED_STATUS
    assert [call[:2] for call in transport.calls] == [
        ('POST', '/orders/share'),
        ('GET', '/orders/share/SAFE42'),
    ]


def _readback_adapter(changes=None):
    item1 = selection()
    item2 = selection(
        canonical_event_id='canonical-2',
        home='Other Home',
        away='Other Away',
    )
    events = [event_for(item1), event_for(item2, 'sr:match:2')]
    events[1][2]['estimateStartTime'] = events[0][2]['estimateStartTime']
    booking = adapter()
    resolved = booking.resolve_selections([item1, item2], events)
    body = readback_for(resolved)
    if changes:
        changes(body)
    transport = FakeTransport(readback=body)
    booking.transport = transport
    return booking, resolved


def test_exact_readback_verifies_all_legs():
    booking, resolved = _readback_adapter()
    assert booking.verify_booking_code('SAFE42', resolved) == resolved


@pytest.mark.parametrize('mutation', [
    'missing_selection',
    'extra_selection',
    'wrong_outcome',
    'wrong_line',
    'wrong_kickoff',
    'wrong_odds',
])
def test_all_demonstrated_readback_mutations_reject_the_entire_code(mutation):
    def change(body):
        ticket = body['ticket']['selections']
        if mutation == 'missing_selection':
            ticket.pop()
        elif mutation == 'extra_selection':
            ticket.append(dict(ticket[0]))
        elif mutation == 'wrong_outcome':
            ticket[0]['outcomeId'] = '13'
        elif mutation == 'wrong_line':
            ticket[0]['specifier'] = 'total=3.5'
        elif mutation == 'wrong_kickoff':
            body['outcomes'][0]['estimateStartTime'] += 1
        elif mutation == 'wrong_odds':
            body['outcomes'][0]['markets'][0]['outcomes'][0]['odds'] = '1.81'

    booking, resolved = _readback_adapter(change)
    with pytest.raises(BookingError, match='readback_'):
        booking.verify_booking_code('SAFE42', resolved)


def test_readback_requires_exact_code_and_explicit_empty_unavailable_list():
    def wrong_code(body):
        body['shareCode'] = 'OTHER42'

    booking, resolved = _readback_adapter(wrong_code)
    assert_booking_error(
        'readback_code_mismatch',
        lambda: booking.verify_booking_code('SAFE42', resolved),
    )

    def unavailable_missing(body):
        body.pop('unavailableOutcomes')

    booking, resolved = _readback_adapter(unavailable_missing)
    assert_booking_error(
        'readback_selection_unavailable',
        lambda: booking.verify_booking_code('SAFE42', resolved),
    )


@pytest.mark.parametrize('mutation,status', [
    ('parent_market', 'readback_parent_market'),
    ('wrong_sport', 'readback_sport_mismatch'),
    ('wrong_tournament', 'readback_tournament_mismatch'),
    ('wrong_teams', 'readback_teams_mismatch'),
    ('wrong_status', 'readback_status_unverified'),
    ('duplicate_market', 'readback_market_mismatch'),
    ('inactive_market', 'readback_market_mismatch'),
    ('duplicate_outcome', 'readback_outcome_mismatch'),
    ('inactive_outcome', 'readback_outcome_mismatch'),
])
def test_readback_requires_exact_active_event_and_market_identity(mutation, status):
    def change(body):
        ticket_selection = body['ticket']['selections'][0]
        event = body['outcomes'][0]
        market = event['markets'][0]
        outcome = market['outcomes'][0]
        if mutation == 'parent_market':
            ticket_selection['parentBetBuilderMarketId'] = 'builder-market'
        elif mutation == 'wrong_sport':
            ticket_selection['sportId'] = 'sr:sport:2'
        elif mutation == 'wrong_tournament':
            event['sport']['category']['tournament']['id'] = 'other-tournament'
        elif mutation == 'wrong_teams':
            event['awayTeamName'] = 'Unexpected Away'
        elif mutation == 'wrong_status':
            event['matchStatus'] = 'Live'
        elif mutation == 'duplicate_market':
            event['markets'].append(dict(market))
        elif mutation == 'inactive_market':
            market['status'] = 1
        elif mutation == 'duplicate_outcome':
            market['outcomes'].append(dict(outcome))
        elif mutation == 'inactive_outcome':
            outcome['isActive'] = 0

    booking, resolved = _readback_adapter(change)
    assert_booking_error(
        status,
        lambda: booking.verify_booking_code('SAFE42', resolved),
    )


def test_quota_reservation_is_durable_and_exhaustion_prevents_post(tmp_path):
    path = str(tmp_path / 'quota.sqlite3')
    quota = BookingQuota(path, 1, prediction_db=str(tmp_path / 'predictions.sqlite3'))
    assert Path(default_quota_path('/var/lib/sureodds/predictions.sqlite3')) == Path(
        '/var/lib/sureodds/sporty_booking_quota.sqlite3')
    quota.reserve('payload-a', now=NOW)
    reopened = BookingQuota(path, 1)
    assert reopened.used('2030-01-01') == 1
    item = selection()
    transport = FakeTransport()
    assert_booking_error(
        'quota_exhausted',
        lambda: adapter(transport).create_booking_code(
            [item], quota=reopened, events=[event_for(item)],
        ),
    )
    assert transport.calls == []


def test_quota_ledger_blocks_repeating_the_same_payload_with_remaining_capacity(tmp_path):
    quota = BookingQuota(str(tmp_path / 'quota.sqlite3'), 3)
    payload = {'selections': [{
        'eventId': 'sr:match:1', 'marketId': '18',
        'specifier': 'total=2.5', 'outcomeId': '12',
    }]}
    fingerprint = payload_fingerprint(payload)
    quota.reserve(fingerprint, now=NOW)
    assert_booking_error(
        'duplicate_payload_do_not_retry',
        lambda: quota.reserve(fingerprint, now=NOW),
    )
    assert quota.used('2030-01-01') == 1


def test_quota_ledger_refuses_prediction_database(tmp_path):
    path = tmp_path / 'database.sqlite3'
    with sqlite3.connect(path) as raw:
        raw.execute('CREATE TABLE picks (id INTEGER)')
    assert_booking_error(
        'quota_db_unrelated_tables',
        lambda: BookingQuota(str(path), 1),
    )
    assert_booking_error(
        'quota_db_is_prediction_db',
        lambda: BookingQuota(str(tmp_path / 'other.sqlite3'), 1, prediction_db=str(tmp_path / 'other.sqlite3')),
    )


def test_job_lock_rejects_a_concurrent_generation(tmp_path):
    path = str(tmp_path / 'booking.lock')
    with BookingRunLock(path):
        assert_booking_error(
            'booking_job_already_running',
            lambda: BookingRunLock(path).__enter__(),
        )


def test_booking_defaults_off_with_a_single_daily_creation_slot():
    config = Config()
    assert config.sportybet_booking_enabled is False
    assert config.sportybet_booking_max_per_day == 1


def test_transport_enforces_routes_budget_and_production_user_agent():
    transport = PublicTransport(limit=1)
    assert USER_AGENT == 'Sureodds-SportyBooking/1.0'
    assert_booking_error(
        'forbidden_route',
        lambda: transport.request('GET', '/unapproved'),
    )
    transport.used = 1
    assert_booking_error(
        'request_budget_exhausted',
        lambda: transport.request('GET', '/factsCenter/pcUpcomingEvents'),
    )


def test_catalogue_transport_sends_the_production_user_agent(monkeypatch):
    class Response:
        status_code = 200
        headers = {'Content-Type': 'application/json'}

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def iter_content(self, _):
            return [b'{"ok":true}']

    class Session:
        auth = None

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def request(self, *args, **kwargs):
            self.call = (args, kwargs)
            return Response()

    session = Session()
    monkeypatch.setattr('predbot.sporty_booking.requests.Session', lambda: session)
    assert PublicTransport().request(
        'GET', '/factsCenter/pcUpcomingEvents',
    ) == {'ok': True}
    args, kwargs = session.call
    assert session.auth is not None
    assert isinstance(session.auth, AnonymousAuth)
    assert kwargs['headers']['User-Agent'] == USER_AGENT
    assert kwargs['allow_redirects'] is False
    assert kwargs['stream'] is True
    assert args[0] == 'GET'


class TelegramResponse:
    status_code = 200

    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body


def telegram_poster(calls, *, chat_type='private'):
    def post(url, payload, timeout):
        calls.append((url.rsplit('/', 1)[-1], payload, timeout))
        if url.endswith('/getChat'):
            return TelegramResponse({
                'ok': True,
                'result': {'id': int(payload['chat_id']), 'type': chat_type},
            })
        return TelegramResponse({'ok': True, 'result': {'message_id': 9}})
    return post


def test_owner_confirmation_is_private_only_and_not_published_elsewhere():
    cfg = SimpleNamespace(telegram_admin_ids=['42'], telegram_token='test-token')
    calls = []
    post = telegram_poster(calls)
    assert preflight_owner_delivery(cfg, post=post) == {
        'ready': True, 'recipient': 'owner_private',
    }
    result = send_owner_booking_result(
        cfg, day='2030-01-02', revision=7, selection_count=2,
        code='SAFE42', verification=VERIFIED_STATUS, post=post,
    )
    assert result['sent'] is True
    assert [call[0] for call in calls] == ['getChat', 'getChat', 'sendMessage']
    sent = calls[-1][1]
    assert sent['chat_id'] == '42'
    assert 'Top Picks date: 2030-01-02' in sent['text']
    assert 'Prediction revision: 7' in sent['text']
    assert 'Selections: 2' in sent['text']
    assert 'Code: SAFE42' in sent['text']
    assert 'Verification: exact_membership_verified' in sent['text']
    assert all(call[1]['chat_id'] == '42' for call in calls)


def test_unverified_codes_and_non_private_owner_chats_are_never_sent():
    cfg = SimpleNamespace(telegram_admin_ids=['42'], telegram_token='test-token')
    calls = []
    rejected = send_owner_booking_result(
        cfg, day='2030-01-02', revision=7, selection_count=2,
        code='SAFE42', verification='readback_failed',
        post=telegram_poster(calls),
    )
    assert rejected == {'sent': False, 'failure': 'verification_required'}
    assert calls == []

    rejected = send_owner_booking_result(
        cfg, day='2030-01-02', revision=7, selection_count=2,
        code='SAFE42', verification=VERIFIED_STATUS,
        post=telegram_poster(calls, chat_type='group'),
    )
    assert rejected == {'sent': False, 'failure': 'owner_chat_unverified'}
    assert [call[0] for call in calls] == ['getChat']


def test_multiple_admins_refuse_owner_delivery():
    cfg = SimpleNamespace(telegram_admin_ids=['42', '43'], telegram_token='test-token')
    calls = []
    result = send_owner_booking_result(
        cfg, day='2030-01-02', revision=7, selection_count=1,
        code='SAFE42', verification=VERIFIED_STATUS,
        post=telegram_poster(calls),
    )
    assert result == {'sent': False, 'failure': 'owner_not_configured'}
    assert calls == []
