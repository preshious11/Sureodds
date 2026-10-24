"""Private local-only Custom Picks, parser/search bounds and prediction isolation."""
from copy import deepcopy
from datetime import timedelta
from math import prod
import logging
from unittest.mock import Mock

import pytest
import requests

from predbot.bot import callbacks
from predbot.bot.commands import CommandLoop
from predbot.cache.fetcher import UnifiedFetcher
from predbot.config import Config
from predbot import custom_picks as cp
from predbot.custom_picks import (CustomPicks, Request, RequestError, INTRO, UNAVAILABLE, TODAY_ONLY,
                                  MIN_PROBABILITY, MAX_LEGS, normalize, parse_request, search, intro_text)
from predbot.jobs import daily_picks, settle, live_results, stats
from predbot.model.engine import representative_predictions
from predbot.model.ensemble import EventModel
from predbot.providers.chain import ProviderChain, ResultsResolver
from predbot.providers.oddsapi import OddsApiProvider
from predbot.providers.sharpapi import SharpApiProvider
from predbot.providers.sportsgameodds import SportsGameOddsProvider
from predbot.util.timeutil import iso, to_local
from tests.conftest import dt
from tests.test_community_menu import MenuTelegram, message_update
from tests.test_custom_pick_candidates import alternatives, prepared

NOW, DAY, ZONE = dt(hh=12), '2030-06-01', 'Africa/Lagos'


def last_reply(tg):
    # Telegram edits the loading message rather than sending a second result.
    return tg.edits[-1] if tg.sends[-1]['text'] == '\U0001f50e Finding your strongest picks...' and tg.edits else tg.sends[-1]


@pytest.mark.parametrize('text,target,count,sports,included,excluded,preference', [
    ('safe 4 odds', 4, None, set(), set(), set(), 'safe'),
    ('5 odds', 5, None, set(), set(), set(), 'best'),
    ('5 picks', None, 5, set(), set(), set(), 'best'),
    ('3 games', None, 3, set(), set(), set(), 'best'),
    ('3 picks around 5 odds', 5, 3, set(), set(), set(), 'best'),
    ('4 odds double chance only', 4, None, set(), {'dc'}, set(), 'best'),
    ('10 odds no BTTS', 10, None, set(), set(), {'btts'}, 'best'),
    ('3 basketball picks', None, 3, {'basketball'}, set(), set(), 'best'),
    ('2 tennis picks', None, 2, {'tennis'}, set(), set(), 'best'),
    ('6 odds football and basketball', 6, None, {'football', 'basketball'}, set(), set(), 'best'),
    ('best picks across all sports', None, None, set(), set(), set(), 'best'),
    ('basketball double chance only', None, None, {'basketball'}, {'dc'}, set(), 'best'),
    ('Give me 5 strong football picks', None, 5, {'football'}, set(), set(), 'strong'),
    ('I need 5 odds for today', 5, None, set(), set(), set(), 'best'),
    ('Give me safe 2 odds from today\'s games', 2, None, set(), set(), set(), 'safe'),
    ('Give me 3.5 odds', 3.5, None, set(), set(), set(), 'best'),
    ('around 6 odds', 6, None, set(), set(), set(), 'best'),
    ('Give me 4 selections', None, 4, set(), set(), set(), 'best'),
    ('3 games, football only', None, 3, {'football'}, set(), set(), 'best'),
    ('Safe 3 odds, no handicap', 3, None, set(), set(), {'handicap'}, 'safe'),
    ('make it football only', None, None, {'football'}, set(), set(), 'best'),
    ('odds of 4, 3 picks', 4, 3, set(), set(), set(), 'best'),
    ('2 picks no BTTS and handicap', None, 2, set(), set(), {'btts', 'handicap'}, 'best'),
])
def test_request_schema(text, target, count, sports, included, excluded, preference):
    request = parse_request(text)
    assert request == Request(target, count, frozenset(sports), frozenset(included), frozenset(excluded), preference, time_intent='today' if 'today' in text else None)


@pytest.mark.parametrize('alias,market', [
    ('DC', 'dc'), ('double chance', 'dc'), ('moneyline', 'result2'), ('money line', 'result2'),
    ('totals', 'total'), ('over under', 'total'), ('over/under', 'total'), ('over / under', 'total'),
    ('BTTS', 'btts'), ('DNB', 'dnb'), ('draw no bet', 'dnb'), ('handicap', 'handicap'),
    ('spread', 'handicap'), ('1X2', 'result3'), ('correct score', 'correct_score'),
])
def test_market_aliases(alias, market):
    assert parse_request('3 picks ' + alias + ' only').allowed_markets == {market}
    assert parse_request('3 picks without ' + alias).excluded_markets == {market}
    assert parse_request('3 picks avoid ' + alias).excluded_markets == {market}


@pytest.mark.parametrize('alias,sport', [('soccer', 'football'), ('NBA', 'basketball'), ('ATP', 'tennis'),
                                       ('American football', 'american_football'), ('NFL', 'american_football'),
                                       ('baseball', 'baseball'), ('MLB', 'baseball')])
def test_sport_aliases_and_extensibility(alias, sport):
    assert parse_request('2 ' + alias + ' picks').sports == {sport}
    assert parse_request('2 hockey picks', {'hockey'}).sports == {'hockey'}


@pytest.mark.parametrize('alias', ['MMA', 'UFC'])
def test_mma_is_recognized_but_disabled_by_default(alias):
    """MMA stays fully recognized by name (RECOGNIZED_SPORTS, SPORT_ALIASES)
    even though production's real ALLOWED_SPORTS doesn't enable it yet -
    recognized and enabled are different questions (2026-10 audit fix)."""
    with pytest.raises(RequestError) as exc:
        parse_request('2 ' + alias + ' picks')
    assert exc.value.reason == 'unsupported_sport'
    assert str(exc.value).startswith('We do not cover MMA yet. Please choose one of our supported sports.')
    assert 'MMA' not in str(exc.value).split('\n\n')[-1]  # never listed among the supported sports themselves
    # Recognition itself is untouched: explicitly enabling it (a future config
    # change, not simulated here via a hardcoded list) makes it parse clean.
    assert parse_request('2 ' + alias + ' picks', allowed_sports=['mma']).sports == {'mma'}


@pytest.mark.parametrize('text', ['tomorrow', '2 picks tomorrow', '3 picks yesterday', '2 picks next week',
                                '4 odds on 2030-06-02'])
def test_other_dates_are_rejected(text):
    with pytest.raises(RequestError, match="today's"):
        parse_request(text)


@pytest.mark.parametrize('text', ['0 picks', '9 picks', '-4 odds', '1 odds', '0 odds',
                                '4 odds 5 odds', '2 picks 3 picks', 'DC only no DC', 'hello',
                                '3 hockey picks', 'audit this ticket', 'replace Arsenal', '5' * 513, 'football only across all sports',
                                '3 picks no basketball'])
def test_unsupported_or_conflicting_constraints_are_not_silently_ignored(text):
    with pytest.raises(RequestError):
        parse_request(text)


def test_a_non_integer_count_is_an_ambiguous_line_not_a_silently_ignored_error():
    # "2.5 picks" used to hard-error ("I can offer 1-8 games"); it's really
    # an unnamed goal/point/set line (2026-10-20 screenshot fix) - deferred
    # to Recovery.respond(), which asks "Over or Under?", never silently
    # ignored or treated as a valid integer count.
    r = parse_request('2.5 picks')
    assert r.ambiguous_line == 2.5 and r.pick_count is None


def rows_for(i, sport='football', price=1.6, probability=.75, market=None, start=None):
    s = alternatives(i, start=start or dt(hh=15))[0]
    s.event.sport = sport
    s.line.market = market or ('result3' if sport == 'football' else 'result2')
    s.line.price, s.p_model = price, probability
    return representative_predictions([s]).candidates


def store(db, rows, day=DAY, at=NOW, partial=False, zone=ZONE):
    db.store_custom_pick_candidates(day, zone, rows, generated_at=iso(at), complete=not partial,
                                    diagnostics={'complete': not partial})


def candidates(rows, now=NOW):
    stamped = [dict(row, prediction_date=DAY, timezone=ZONE, generated_at=iso(NOW)) for row in rows]
    return [c for row in stamped if (c := normalize(row, DAY, ZONE, now, NOW, set(Config().allowed_sports)))]


@pytest.mark.parametrize('field,value', [
    ('price', 1), ('price', float('nan')), ('price', float('inf')), ('price', 'bad'),
    ('p_model', 0), ('p_model', 1), ('p_model', float('nan')), ('p_model', float('inf')),
    ('start_utc', 'bad'), ('start_utc', '2030-06-01T15:00:00'), ('start_utc', iso(NOW)),
    ('start_utc', iso(NOW - timedelta(minutes=1))), ('canonical_event_id', ''),
    ('sport', 'made_up'), ('sport', []), ('market', 'made_up'), ('market', []),
    ('selection', 'made_up'), ('selection', None), ('home', None), ('period', None),
    ('generated_at', iso(NOW + timedelta(minutes=1))), ('generated_at', 'bad'),
    ('generated_at', iso(NOW - timedelta(days=1))), ('prediction_date', '2030-05-31'),
    ('timezone', 'UTC'),
])
def test_malformed_started_or_stale_rows_are_skipped(field, value):
    row = dict(rows_for(1)[0], prediction_date=DAY, timezone=ZONE, generated_at=iso(NOW))
    row[field] = value
    assert normalize(row, DAY, ZONE, NOW, NOW, set(Config().allowed_sports)) is None


def test_line_validation_and_fixture_fallback():
    row = dict(rows_for(1)[0], prediction_date=DAY, timezone=ZONE, generated_at=iso(NOW))
    row.update(canonical_event_id=None, fixture_key='legacy-fixture')
    assert normalize(row, DAY, ZONE, NOW, NOW, {'football'}).fixture_id == 'legacy-fixture'
    row.update(market='total', selection='over', line=None)
    assert normalize(row, DAY, ZONE, NOW, NOW, {'football'}) is None
    row['line'] = float('inf')
    assert normalize(row, DAY, ZONE, NOW, NOW, {'football'}) is None
    assert normalize([], DAY, ZONE, NOW, NOW, {'football'}) is None


@pytest.mark.parametrize('sport,market,selection,stat,period,line,expected', [
    ('basketball', 'result2', 'home', 'points', '1q', None, '1st Qtr'),
    ('tennis', 'total', 'over', 'games', 'full', 2.5, 'Games'),
    ('football', 'result3', 'draw', 'points', 'full', None, 'Draw'),
    ('football', 'total', 'over', 'corners', '1h', 8.0, 'Corners'),
    ('football', 'dc', 'home_draw', 'points', '1h', None, '1st Half'),
])
def test_outcomes_preserve_market_stat_and_period_context(sport, market, selection, stat, period, line, expected):
    row = dict(rows_for(1, sport=sport)[0], sport=sport, market=market, selection=selection,
               stat=stat, period=period, line=line,
               prediction_date=DAY, timezone=ZONE, generated_at=iso(NOW))
    candidate = normalize(row, DAY, ZONE, NOW, NOW, {sport})
    assert candidate and expected.lower() in candidate.outcome.lower()


def test_target_count_product_and_determinism():
    pool = candidates([r for i in range(1, 7) for r in rows_for(i)])
    for phrase in ('4 odds', '3 picks around 4 odds', '3 picks'):
        request = parse_request(phrase)
        ticket = search(pool, request)
        assert ticket is not None
        assert len({c.fixture_id for c in ticket.picks}) == len(ticket.picks)
        assert ticket.odds == pytest.approx(prod(c.price for c in ticket.picks))
        if request.target_odds:
            assert .9 * request.target_odds <= ticket.odds <= 1.1 * request.target_odds
        if request.pick_count:
            assert len(ticket.picks) == request.pick_count
        assert search(list(reversed(pool)), request) == ticket


def test_all_fixture_alternatives_still_produce_only_one_pick_per_fixture():
    rows = representative_predictions(alternatives()).candidates + representative_predictions(alternatives(2)).candidates
    pool = candidates(rows)
    ticket = search(pool, Request(pick_count=2))
    assert len(ticket.picks) == len({c.fixture_id for c in ticket.picks}) == 2
    assert search(pool, Request(pick_count=3)) is None


def test_stronger_probability_beats_large_ev_and_unavailable_ranking_fields():
    rows = rows_for(1, probability=.85, price=1.6) + rows_for(2, probability=.20, price=100)
    rows[1].update(ev=19, rank_score=999, p_lcb=.999)
    pool = candidates(rows)
    ticket = search(pool, Request(pick_count=1, preference='safe'))
    assert ticket.picks[0].probability == .85
    assert not hasattr(ticket.picks[0], 'rank_score') and not hasattr(ticket.picks[0], 'p_lcb')
    ties = candidates(rows_for(2) + rows_for(1))
    assert search(ties, Request(pick_count=1)) == search(list(reversed(ties)), Request(pick_count=1))


def test_diversity_prefers_comparable_markets_but_explicit_market_request_wins():
    rows = []
    for i in range(1, 4):
        rows += rows_for(i, probability=.75, market='result3')
        alternate = rows_for(i, probability=.749, market='dnb')[0]
        rows.append(alternate)
    pool = candidates(rows)
    ticket = search(pool, Request(pick_count=3))
    assert len({c.market for c in ticket.picks}) == 2
    explicit = [c for c in pool if c.market == 'dnb']
    ticket = search(explicit, Request(pick_count=3, allowed_markets=frozenset({'dnb'})))
    assert len(ticket.picks) == 3 and {c.market for c in ticket.picks} == {'dnb'}


@pytest.mark.parametrize('sport,count', [('basketball', 3), ('tennis', 2), ('baseball', 3), ('american_football', 2)])
def test_nonfootball_tickets(db, sport, count):
    store(db, [r for i in range(1, 5) for r in rows_for(i, sport=sport)])
    reply = CustomPicks(db, Config(timezone=ZONE)).reply(f'{count} {sport} picks', '42', NOW)
    assert reply.startswith('\U0001f525 ') and 'selections' not in reply
    assert reply.count('Confidence: 75%') == count
    assert ' to Win' in reply and 'Moneyline' not in reply


def test_multisport_search_and_no_forced_football(db):
    store(db, rows_for(1, 'basketball') + rows_for(2, 'tennis') + rows_for(3, 'football'))
    custom = CustomPicks(db, Config(timezone=ZONE))
    reply = custom.reply('best 3 picks across all sports', '42', NOW)
    assert all(icon in reply for icon in ('⚽', '🏀', '🎾'))
    assert reply.startswith('\U0001f525 4.10 ODDS') and 'Combined Odds' not in reply
    reply = custom.reply('2 picks football and basketball', '42', NOW)
    assert '⚽' in reply and '🏀' in reply and '🎾' not in reply


def test_market_filters_are_hard_constraints(db):
    rows = []
    for i in range(1, 5):
        rows += representative_predictions(alternatives(i)).candidates
    store(db, rows)
    custom = CustomPicks(db, Config(timezone=ZONE))
    dc = custom.reply('3 picks double chance only', '42', NOW)
    assert dc.count('Double Chance:') == 3 and 'BTTS' not in dc
    totals = custom.reply('2 picks over under only', '42', NOW)
    assert totals.count('Goals') == 2 and 'Total Goals' not in totals
    no_btts = custom.reply('3 picks no BTTS', '42', NOW)
    assert '🔥 ' in no_btts and 'BTTS' not in no_btts
    assert 'No basketball on the menu' in custom.reply('basketball double chance only', '42', NOW)


def test_sport_specific_missing_market_is_not_reinterpreted(db):
    store(db, rows_for(1, 'basketball') + rows_for(2, 'basketball'))
    reply = CustomPicks(db, Config(timezone=ZONE)).reply('basketball double chance only', '42', NOW)
    assert "That combo isn't on the board" in reply and '🔥 ' not in reply


def test_exact_count_and_odds_constraints_fail_together_instead_of_relaxing(db):
    store(db, rows_for(1, price=1.2) + rows_for(2, price=1.2) + rows_for(3, price=1.2))
    reply = CustomPicks(db, Config(timezone=ZONE)).reply('3 picks around 5 odds', '42', NOW)
    assert "Couldn't stretch it" in reply and '🔥 ' not in reply


@pytest.mark.parametrize('preference,floor', list(MIN_PROBABILITY.items()))
def test_preferences_use_documented_probability_floor(db, preference, floor):
    store(db, rows_for(1, probability=floor - .001))
    reply = CustomPicks(db, Config(timezone=ZONE)).reply(f'1 {preference} pick', '42', NOW)
    assert '🔥 ' not in reply


def test_unavailable_partial_insufficient_and_general_requests(db):
    custom = CustomPicks(db, Config(timezone=ZONE))
    assert custom.reply('safe 4 odds', '42', NOW) == UNAVAILABLE
    store(db, [])
    assert custom.reply('3 picks', '42', NOW) == UNAVAILABLE
    store(db, rows_for(1) + rows_for(2) + rows_for(3), partial=True)
    general = custom.reply('give me best picks', '42', NOW)
    assert general.count('Confidence:') == 3 and 'pool' not in general and 'selections' not in general
    assert 'Slim pickings right now' in custom.reply('5 picks', '42', NOW)
    assert 'Slim pickings right now' in custom.reply('100 odds', '42', NOW)
    assert 'Slim pickings right now' in custom.reply('2 tennis picks', '42', NOW)
    assert custom.reply('3 picks tomorrow', '42', NOW).startswith(TODAY_ONLY)


def test_current_date_and_evaluation_freshness(db):
    custom = CustomPicks(db, Config(timezone=ZONE))
    store(db, rows_for(1), day='2030-05-31', at=NOW - timedelta(days=1))
    assert custom.reply('1 pick', '42', NOW) == UNAVAILABLE
    store(db, rows_for(1), day=DAY, at=NOW - timedelta(days=1))
    assert custom.reply('1 pick', '42', NOW) == UNAVAILABLE
    # Lagos has rolled into June 2 while UTC is still June 1.
    midnight = dt(hh=23, mm=30)
    assert custom.reply('1 pick', '42', midnight) == UNAVAILABLE
    store(db, rows_for(2, start=dt(d=2, hh=2)), day='2030-06-02', at=midnight)
    assert '🔥 ' in custom.reply('1 pick', '42', midnight)


def test_started_fixtures_are_not_recommended_and_no_fallback_runs(db):
    store(db, rows_for(1, start=NOW) + rows_for(2, start=NOW - timedelta(hours=4)))
    reply = CustomPicks(db, Config(timezone=ZONE)).reply('2 picks', '42', NOW)
    assert 'We missed the window' in reply


@pytest.fixture
def private_bot(db, monkeypatch):
    monkeypatch.setattr('predbot.custom_picks.utcnow', lambda: NOW)
    cfg = Config(timezone=ZONE, telegram_chat_id='-1001234567890')
    loop = CommandLoop(MenuTelegram(), db, cfg, {}, service=Mock())
    loop.instance = 'custom-test'
    return loop


def test_private_entry_text_exit_and_normal_start(private_bot, db):
    loop = private_bot
    store(db, rows_for(1) + rows_for(2) + rows_for(3))
    loop._handle_update(message_update('3 picks', user_id='42'))
    assert loop.tg.sends == []
    before = list(db.conn.iterdump())
    loop._handle_update(message_update('/start custom_picks', user_id='42'))
    assert last_reply(loop.tg)['text'] == INTRO
    assert list(db.conn.iterdump()) == before
    loop._handle_update(message_update('3 picks', user_id='42'))
    assert last_reply(loop.tg)['text'].startswith('🔥 ')
    assert all(message['chat_id'] == '42' for message in loop.tg.sends)
    assert list(db.conn.iterdump()) == before
    loop._handle_update(message_update('/start', user_id='42'))
    assert ('42', '42') not in loop._custom_users
    assert last_reply(loop.tg)['text'] == callbacks.welcome_message('Ada')
    assert db.subscribers_active()[0]['chat_id'] == '42'
    sends = len(loop.tg.sends)
    loop._handle_update(message_update('3 picks', user_id='42'))
    assert len(loop.tg.sends) == sends


@pytest.mark.parametrize('chat_type', ['group', 'supergroup', 'channel', None])
def test_ordinary_community_text_is_always_ignored(private_bot, chat_type):
    loop = private_bot
    loop._custom_users.add(('42', '-1001234567890'))
    loop._handle_update(message_update('safe 4 odds', '-1001234567890', chat_type, user_id='42'))
    assert loop.tg.sends == []


def test_user_modes_are_independent_ephemeral_and_stop_exits(private_bot, db):
    loop = private_bot
    loop._handle_update(message_update('/start custom_picks', user_id='42'))
    loop._handle_update(message_update('3 picks', '99', 'private', user_id='99'))
    assert len(loop.tg.sends) == 1
    new_loop = CommandLoop(loop.tg, db, loop.cfg, {})
    new_loop._handle_update(message_update('3 picks', user_id='42'))
    assert len(loop.tg.sends) == 1
    loop._handle_update(message_update('/stop', user_id='42'))
    assert ('42', '42') not in loop._custom_users


@pytest.mark.parametrize('users,requests_per_user,has_archive', [(1, 1, True), (1, 10, True), (10, 2, True),
                                                              (1, 10, False), (10, 2, False)])
def test_request_path_has_zero_provider_calls_and_zero_persistence(private_bot, db, monkeypatch, users, requests_per_user, has_archive):
    if has_archive:
        store(db, rows_for(1) + rows_for(2, 'basketball') + rows_for(3, 'tennis'))
    # Existing published predictions/history must be untouched as well as the archive.
    ps = prepared(alternatives(), private_bot.cfg, archive=False)
    daily_picks.store_picks(ps, db, DAY)
    from predbot.selection.picker import package_predictions
    package_predictions(db, DAY, private_bot.cfg, iso(NOW))
    before = list(db.conn.iterdump())
    observed = stats.compute(db, NOW)
    memberships = {c: [dict(r) for r in db.collection_predictions(c, DAY)] for c in ('all', 'top', 'daily')}
    watches = db.live_picks(iso(NOW), iso(dt(hh=23)))
    pending = [dict(r) for r in db.pending_picks()]
    forbidden = Mock(side_effect=AssertionError('Custom Picks called a prediction provider/model/fetcher'))
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
            private_bot._handle_update(message_update('/start custom_picks', str(user), 'private', str(user)))
            for _ in range(requests_per_user):
                private_bot._handle_update(message_update('3 picks around 4 odds', str(user), 'private', str(user)))
                assert last_reply(private_bot.tg)['chat_id'] == str(user)
                if has_archive:
                    assert last_reply(private_bot.tg)['text'].startswith('🔥 ')
                else:
                    assert last_reply(private_bot.tg)['text'] == UNAVAILABLE
    finally:
        db.conn.set_trace_callback(None)
    assert statements and all(sql.lstrip().upper().startswith('SELECT') for sql in statements)
    assert list(db.conn.iterdump()) == before
    assert stats.compute(db, NOW) == observed
    assert {c: [dict(r) for r in db.collection_predictions(c, DAY)] for c in memberships} == memberships
    assert db.live_picks(iso(NOW), iso(dt(hh=23))) == watches
    assert [dict(r) for r in db.pending_picks()] == pending
    forbidden.assert_not_called()
    private_bot.service.assert_not_called()
    assert private_bot.service.mock_calls == []


def test_output_escapes_names_is_compact_and_has_no_internal_data(db):
    rows = []
    for i in range(1, MAX_LEGS + 1):
        row = rows_for(i)[0]
        row['home'], row['away'] = '<script>' + '&' * 200, '>' * 200
        rows.append(row)
    store(db, rows)
    reply = CustomPicks(db, Config(timezone=ZONE)).reply('8 picks', '42', NOW)
    assert '<script>' not in reply and '&lt;' in reply
    assert len(reply) < 4000
    assert all(word not in reply for word in ('rank_score', 'p_lcb', 'database', 'provider', 'EV:', 'API'))


def test_intro_lists_exactly_the_currently_enabled_sports():
    # Production's real ALLOWED_SPORTS default excludes MMA (2026-10 audit):
    # the welcome message must never advertise a sport that can't actually
    # be served, even though MMA stays fully recognized/parseable.
    for label in ('Football', 'Basketball', 'American Football', 'Baseball', 'Tennis'):
        assert label in INTRO
    assert 'MMA' not in INTRO
    assert 'Example:' in INTRO
    for word in ('hockey', 'cricket', 'boxing', 'rugby'):
        assert word not in INTRO.lower()
    # Re-enabling MMA is a config change only: intro_text() reflects whatever
    # allowed_sports it's given, never a separately hardcoded list.
    assert 'MMA' in intro_text(list(cp.RECOGNIZED_SPORTS))


def test_named_team_scopes_the_ticket_to_its_own_fixture(db):
    store(db, rows_for(1) + rows_for(2))  # 1: Arsenal v Chelsea, 2: Everton v Fulham
    reply = CustomPicks(db, Config(timezone=ZONE)).reply('1 picks Arsenal', '42', NOW)
    assert 'Arsenal' in reply
    assert 'Everton' not in reply and 'Fulham' not in reply


def test_a_named_team_persists_across_a_refinement_that_does_not_repeat_it(db):
    store(db, rows_for(1) + rows_for(2))
    custom = CustomPicks(db, Config(timezone=ZONE))
    first = custom.reply('1 picks Arsenal', '42', NOW)
    assert 'Arsenal' in first
    second = custom.reply('make it safer instead', '42', NOW)
    assert 'Arsenal' in second


def test_operational_logs_do_not_include_conversation(db, caplog):
    store(db, rows_for(1) + rows_for(2) + rows_for(3))
    with caplog.at_level(logging.INFO):
        CustomPicks(db, Config(timezone=ZONE)).reply('give me safe 4 odds', '42', NOW)
    assert 'Custom Picks request | user=u' in caplog.text and 'target=4.0' in caplog.text
    assert 'user=42' not in caplog.text                     # pseudonymous: never the raw Telegram id
    assert 'give me safe' not in caplog.text


def test_large_pool_search_is_bounded(monkeypatch):
    from predbot import custom_picks
    pool = candidates([r for i in range(1, 121) for r in rows_for(i)])
    calls = [0]
    original = custom_picks.State
    def state(*args, **kwargs):
        calls[0] += 1
        return original(*args, **kwargs)
    monkeypatch.setattr(custom_picks, 'State', state)
    ticket = search(pool, Request(target_odds=10))
    assert ticket and len(ticket.picks) <= MAX_LEGS
    assert calls[0] <= custom_picks.MAX_EXPANSIONS + 1


def test_corrupt_local_read_fails_without_fallback(db, monkeypatch):
    monkeypatch.setattr(db, 'get_custom_pick_pool', Mock(side_effect=ValueError('corrupt json')))
    assert CustomPicks(db, Config(timezone=ZONE)).reply('safe 4 odds', '42', NOW) == UNAVAILABLE
