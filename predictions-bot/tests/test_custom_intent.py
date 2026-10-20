"""Conversational intent, compact Telegram UI and strict private isolation."""
from dataclasses import replace
from datetime import timedelta
from unittest.mock import Mock

import pytest
import requests

from predbot import custom_picks as cp, ticket_audit_switch
from predbot.custom_recovery import TTL, eligible, valid_ticket
from predbot.custom_recovery import refine
from tests.test_community_menu import message_update
from tests.test_custom_picks import NOW, ZONE, candidates, rows_for, store, private_bot, last_reply
from tests.test_custom_recovery import buttons, callback, request


@pytest.mark.parametrize('text,odds,count,quality', [
    ('2 odds', 2, None, 'best'),
    ('give me 2 odds', 2, None, 'best'),
    ('give me sure 2 odds', 2, None, 'safe'),
    ('sure 2 odds', 2, None, 'safe'),
    ('I need safe 4 odds', 4, None, 'safe'),
    ('find me the strongest 5 odds', 5, None, 'strong'),
    ("check today's games and give me 5 odds", 5, None, 'best'),
    ("research today's games give me sure 18 odds", 18, None, 'safe'),
    ('I want around 10 odds', 10, None, 'best'),
    ('give me 3 strong games', None, 3, 'strong'),
    ('best three', None, 3, 'best'),
    ('give me 4 football picks', None, 4, 'best'),
    ('give me safe basketball picks', None, None, 'safe'),
    ('give me 6 odds from football', 6, None, 'best'),
    ('EPL 4 odds', 4, None, 'best'),
    ('give me something from premier league', None, None, 'best'),
    ('double chance only around 3 odds', 3, None, 'best'),
    ('no BTTS', None, None, 'best'),
    ('over 1.5 only', None, None, 'best'),
    ('football and basketball 8 odds', 8, None, 'best'),
    ('give me the safest games tonight', None, None, 'safe'),
    ('I need something strong for today around 6 odds', 6, None, 'strong'),
    ('I want you to check and give me 5 odds', 5, None, 'best'),
    ('abeg check today matches I need like 5 odds something safe', 5, None, 'safe'),
    ('give me 5 games', None, 5, 'best'),
    ('give me 5 picks around 10 odds', 10, 5, 'best'),
    ('over 1.5 around 4 odds', 4, None, 'best'),
    ('double chance only 3 odds', 3, None, 'best'),
    ('no BTTS give me 5 odds', 5, None, 'best'),
    ('football and basketball around 8 odds', 8, None, 'best'),
    ('bro pls check all today game jdjfj give me sure 4 odds', 4, None, 'safe'),
])
def test_natural_examples_extract_concepts(text, odds, count, quality):
    parsed = cp.parse_request(text)
    assert (parsed.target_odds, parsed.pick_count, parsed.preference) == (odds, count, quality)


@pytest.mark.parametrize('noise', ['abeg', 'bro jdjfj', 'PLEASE!!!', 'erm erm', 'anything something'])
@pytest.mark.parametrize('phrase', ['safe 4 odds', 'SAEF 4 ODSS', '4, odds SAFE', '4 total odds something safe'])
def test_noise_typos_case_and_punctuation(noise, phrase):
    parsed = cp.parse_request(noise + ' ' + phrase + ' ' + noise)
    assert parsed.target_odds == 4 and parsed.preference == 'safe'


@pytest.mark.parametrize('alias,expected', [('EPL', 'epl'), ('Premier League', 'epl'),
    ('La Liga', 'la_liga'), ('Bundesliga', 'bundesliga'), ('Champions League', 'ucl')])
def test_competition_aliases(alias, expected):
    assert cp.parse_request(f'{alias} 4 odds').competitions == {expected}


def test_number_contexts_and_hard_constraints():
    parsed = cp.parse_request('5 football games, over 1.5 only, no BTTS, around 10 odds')
    assert parsed.pick_count == 5 and parsed.target_odds == 10
    assert parsed.market_line == 1.5 and parsed.market_selection == 'over'
    assert parsed.allowed_markets == {'total'} and parsed.excluded_markets == {'btts'}
    assert parsed.sports == {'football'}
    assert cp.parse_request('odds: 4').allowed_markets == frozenset()
    assert cp.parse_request('4 combined odds').allowed_markets == frozenset()
    league = cp.parse_request('Ligue 1 odds 4')
    assert league.target_odds == 4 and league.pick_count is None and league.competitions == {'ligue_1'}
    assert cp.parse_request('Ligue 1 games').pick_count is None


@pytest.mark.parametrize('phrase', ['no more BTTS', 'without any BTTS', "I don’t want BTTS", 'do not include BTTS'])
def test_natural_exclusions(phrase):
    parsed = cp.parse_request(phrase + ' 4 odds')
    assert parsed.excluded_markets == {'btts'} and not parsed.allowed_markets


def test_refinement_preserves_compatible_league_and_never_weakens_stronger():
    previous = cp.parse_request('safe EPL 4 odds no BTTS')
    updated = refine(previous, cp.parse_request('football only'))
    assert updated.competitions == {'epl'} and updated.sports == {'football'}
    updated = refine(updated, cp.parse_request('give me stronger ones'))
    assert updated.preference == 'safe' and updated.excluded_markets == {'btts'}
    updated = refine(updated, cp.parse_request('basketball only'))
    assert not updated.competitions and updated.sports == {'basketball'}


@pytest.mark.parametrize('text', ['jdjfj zzz', 'hello bro', '4', '5 odds 8',
    'Martian league 4 odds', 'basketball EPL picks', 'over 1.5 under 2.5 4 odds', 'football news'])
def test_ambiguous_or_unsupported_intent_clarifies(text):
    with pytest.raises(cp.RequestError):
        cp.parse_request(text)


@pytest.mark.parametrize('word,label', [
    ('hockey', 'Hockey'), ('ice hockey', 'Hockey'), ('cricket', 'Cricket'),
    ('rugby', 'Rugby'), ('boxing', 'Boxing'), ('golf', 'Golf'), ('darts', 'Darts'),
])
def test_unsupported_sport_names_the_actual_sport_and_lists_supported_ones(word, label):
    with pytest.raises(cp.RequestError) as exc:
        cp.parse_request(f'3 {word} picks')
    assert exc.value.reason == 'unsupported_sport'
    assert str(exc.value).startswith(f'We do not cover {label} yet. Please choose one of our supported sports.')
    for sport_label in ('Football', 'Basketball', 'Tennis'):
        assert sport_label in str(exc.value)


def test_unrecognized_word_never_invents_a_sport_name():
    try:
        cp.parse_request('asdkjfh picks please')
    except cp.RequestError as exc:
        assert exc.reason != 'unsupported_sport'


@pytest.mark.parametrize('word', ['hoops', 'basket'])
def test_extra_basketball_aliases(word):
    assert cp.parse_request(f'3 {word} picks').sports == {'basketball'}


def test_league_line_and_local_evening_filters():
    rows = []
    for i, line, side, league, start in [
        (1, 1.5, 'over', 'epl', NOW.replace(hour=17)),
        (2, 2.5, 'over', 'epl', NOW.replace(hour=18)),
        (3, 1.5, 'under', 'epl', NOW.replace(hour=18)),
        (4, 1.5, 'over', 'la_liga', NOW.replace(hour=18)),
        (5, 1.5, 'over', 'epl', NOW.replace(hour=16)),
    ]:
        row = rows_for(i, start=start)[0]
        row.update(market='total', selection=side, line=line, canonical_competition_id=league)
        rows.append(row)
    pool = candidates(rows)
    parsed = cp.parse_request('EPL over 1.5 tonight')
    assert [c.fixture_id for c in eligible(pool, parsed, NOW, ZONE)] == ['evt-1']
    # Corner/team/half totals must not masquerade as the requested full-match line.
    for changes in [dict(stat='corners'), dict(entity='home'), dict(period='1h')]:
        assert not eligible([replace(pool[0], **changes)], parsed, NOW, ZONE)


@pytest.mark.parametrize('status', ['cancelled', 'postponed', 'completed', 'final', 'live', 'in_progress'])
@pytest.mark.parametrize('location', ['row', 'extra'])
def test_known_ineligible_status_never_served(db, status, location):
    row = rows_for(1)[0]
    if location == 'row': row['status'] = status
    else: row['extra'] = {'status': status}
    store(db, [row])
    result = cp.CustomPicks(db, Mock(timezone=ZONE, allowed_sports=['football'])).respond('1 pick', '42', '42', NOW)
    assert result.reason != 'success' and not buttons(result)


def test_canonical_and_cross_provider_identity_duplicates(db):
    original = rows_for(1)[0]
    duplicate = dict(original, canonical_event_id='different-provider-id', price=1.7)
    pool = candidates([original, duplicate])
    assert cp.search(pool, cp.Request(pick_count=2)) is None
    impossible = cp.State(tuple(pool), pool[0].price * pool[1].price)
    assert not valid_ticket(impossible, cp.Request(pick_count=2), NOW)
    # One canonical ID referring to conflicting fixtures is excluded entirely.
    store(db, [original, dict(original, home='Different team', home_id='other-team', market='result2')])
    custom = cp.CustomPicks(db, Mock(timezone=ZONE, allowed_sports=['football']))
    assert custom.respond('1 pick', '42', '42', NOW).reason != 'success'


def test_followup_updates_only_explicit_constraints(db):
    store(db, [r for i in range(1, 7) for r in rows_for(i)])
    custom = cp.CustomPicks(db, Mock(timezone=ZONE, allowed_sports=['football', 'basketball']))
    custom.respond('safe EPL over 1.5 no BTTS around 4 odds', '42', '42', NOW)
    custom.respond('make it 6 odds', '42', '42', NOW)
    spec = custom.recovery.conversations['42', '42'].request
    assert spec.target_odds == 6 and spec.competitions == {'epl'} and spec.market_line == 1.5
    assert spec.excluded_markets == {'btts'} and spec.preference == 'safe'
    custom.respond('football only', '42', '42', NOW)
    spec = custom.recovery.conversations['42', '42'].request
    assert spec.sports == {'football'} and spec.target_odds == 6 and spec.market_line == 1.5
    custom.respond('give me safer ones', '42', '42', NOW)
    assert custom.recovery.conversations['42', '42'].request == spec
    custom.respond('jdjfj', '42', '42', NOW)
    assert custom.recovery.conversations['42', '42'].request == spec
    custom.respond('3 basketball picks', '42', '42', NOW)
    spec = custom.recovery.conversations['42', '42'].request
    assert spec.target_odds is None and spec.sports == {'basketball'} and spec.market_line is None


def test_conversation_is_private_bounded_and_expires(db):
    custom = cp.CustomPicks(db, Mock(timezone=ZONE, allowed_sports=['football']))
    custom.respond('safe 4 odds no BTTS', '42', '42', NOW)
    custom.respond('make it 6 odds', '99', '99', NOW)
    assert custom.recovery.conversations['99', '99'].request.excluded_markets == frozenset()
    custom.respond('make it 6 odds', '42', '42', NOW + TTL)
    assert custom.recovery.conversations['42', '42'].request.excluded_markets == frozenset()
    for i in range(520):
        custom.respond('2 odds', str(i), str(i), NOW)
    assert len(custom.recovery.conversations) == 512
    # Prepared taps use the same bounded context store, including evicted users.
    custom.recovery.remember('evicted', 'evicted', cp.Request(), NOW + TTL)
    assert len(custom.recovery.conversations) == 512


@pytest.mark.parametrize('exit_command', ['/stop', '/start'])
def test_exit_clears_context_and_old_buttons(private_bot, db, exit_command):
    store(db, rows_for(1) + rows_for(2) + rows_for(3))
    sent = request(private_bot, 'safe baseball picks no BTTS')
    button = buttons(sent)[0]
    private_bot._handle_update(message_update(exit_command, user_id='42'))
    assert ('42', '42') not in private_bot._custom_picks.recovery.conversations
    assert not private_bot._custom_picks.recovery.ready
    before = len(private_bot.tg.sends)
    private_bot._dispatch_callback(callback(button['callback_data']))
    assert len(private_bot.tg.sends) == before
    assert 'Open your own' in private_bot.tg.answers[-1]['text']


def test_alternative_tap_becomes_followup_context(private_bot, db):
    store(db, rows_for(1) + rows_for(2) + rows_for(3))
    sent = request(private_bot, 'safe baseball picks no BTTS')
    token = buttons(sent)[0]['callback_data']
    prepared = private_bot._custom_picks.recovery.ready[token[3:]].request
    private_bot._dispatch_callback(callback(token))
    request(private_bot, 'make it 6 odds')
    refined = private_bot._custom_picks.recovery.conversations['42', '42'].request
    assert refined == replace(prepared, target_odds=6)


def test_recovery_never_weakens_floor_or_exclusions(private_bot, db):
    strong = rows_for(1, probability=.8) + rows_for(2, probability=.8)
    weak = rows_for(3, probability=.55)
    btts = dict(rows_for(4, probability=.95)[0], market='btts', selection='yes')
    store(db, strong + weak + [btts])
    sent = request(private_bot, 'safe 18 odds no BTTS')
    assert 'Try these instead:' in sent['text']
    for button in buttons(sent):
        spec = private_bot._custom_picks.recovery.ready[button['callback_data'][3:]]
        assert spec.request.preference == 'safe' and spec.request.excluded_markets == {'btts'}
        assert all(c.probability >= .65 and c.market != 'btts' for c in spec.ticket.picks)
        private_bot._dispatch_callback(callback(button['callback_data']))
        assert private_bot.tg.sends[-1]['text'].startswith('🔥 ')


def test_loading_precedes_search_and_is_edited_in_place(private_bot, db, monkeypatch):
    store(db, rows_for(1, price=2))
    real = private_bot._custom_picks.respond
    def respond(*args):
        assert private_bot.tg.sends[-1]['text'] == '🔎 Finding your strongest picks...'
        assert not private_bot.tg.edits
        return real(*args)
    monkeypatch.setattr(private_bot._custom_picks, 'respond', respond)
    request(private_bot, 'give me sure 2 odds')
    assert len(private_bot.tg.sends) == 1 and len(private_bot.tg.edits) == 1
    final = private_bot.tg.edits[0]
    assert final['message_id'] == 123 and final['chat_id'] == '42'
    assert final['text'].startswith('🔥 2.00 ODDS')


def test_clarification_has_no_loading_or_search(private_bot, monkeypatch):
    forbidden = Mock(side_effect=AssertionError('unintelligible request must not search'))
    monkeypatch.setattr(cp, 'search', forbidden)
    response = request(private_bot, 'jdjfj')
    assert 'What odds' in response['text']
    assert not private_bot.tg.edits and len(private_bot.tg.sends) == 1
    forbidden.assert_not_called()


@pytest.mark.parametrize('failure', ['send', 'edit', 'edit_exception', 'respond'])
def test_loading_delivery_failure_falls_back_safely(private_bot, db, monkeypatch, failure):
    store(db, rows_for(1, price=2))
    if failure == 'send':
        real = private_bot.tg.send
        monkeypatch.setattr(private_bot.tg, 'send', lambda text, **kw:
            None if text == '🔎 Finding your strongest picks...' else real(text, **kw))
    elif failure == 'edit':
        monkeypatch.setattr(private_bot.tg, 'edit_message_text', Mock(return_value=False))
    elif failure == 'edit_exception':
        monkeypatch.setattr(private_bot.tg, 'edit_message_text', Mock(side_effect=RuntimeError('failure')))
    else:
        monkeypatch.setattr(private_bot._custom_picks, 'respond', Mock(side_effect=RuntimeError('private data')))
    result = request(private_bot, '2 odds')
    assert result['text'].startswith('🔥 ') if failure != 'respond' else 'private data' not in result['text']
    assert result['text'] != '🔎 Finding your strongest picks...'


def test_compact_natural_output_uses_stored_competition_and_confidence():
    row = rows_for(1, price=2, probability=.72)[0]
    row.update(league='Premier League', market='result3', selection='home')
    pick = candidates([row])[0]
    text = cp.format_ticket(cp.State((pick,), 2), True, ZONE)
    assert text == ('🔥 2.00 ODDS\n\n⚽ Arsenal vs Chelsea\nPremier League\n'
                    'Arsenal to Win\nOdds: 2.00 · Confidence: 72%\n\nBet only what you can afford to lose.')
    for term in ['Combined Odds', 'Custom Picks', 'Stronger available', 'selection', 'pool', 'Moneyline', 'result3']:
        assert term not in text
    row.update(league=None, market='result2', sport='tennis')
    text = cp.format_ticket(cp.State(tuple(candidates([row])), 2), False, ZONE)
    assert 'Premier League' not in text and ' to Win' in text and 'Moneyline' not in text


def test_shorter_equal_quality_ticket_preferred_but_never_weaker():
    pool = candidates(rows_for(1, price=2, probability=.8) + rows_for(2, price=1.4, probability=.8) +
                      rows_for(3, price=1.4, probability=.8, market='result2'))
    ticket = cp.search(pool, cp.Request(target_odds=2))
    assert len(ticket.picks) == 1 and ticket.picks[0].fixture_id == 'evt-1'
    weaker = [replace(c, probability=.7) if c.fixture_id == 'evt-1' else c for c in pool]
    ticket = cp.search(weaker, cp.Request(target_odds=2))
    assert len(ticket.picks) == 2 and all(c.probability == .8 for c in ticket.picks)
    exact = cp.search(pool, cp.Request(target_odds=2, pick_count=2))
    assert len(exact.picks) == 2


def test_no_external_requests_or_tracked_side_effects_for_natural_flow(private_bot, db, monkeypatch):
    store(db, rows_for(1, price=2) + rows_for(2, price=1.5) + rows_for(3, price=1.5))
    before = list(db.conn.iterdump())
    forbidden = Mock(side_effect=requests.Timeout('no provider credits permitted'))
    monkeypatch.setattr(requests.sessions.Session, 'request', forbidden)
    for text in ['research today’s games give me sure 18 odds', 'make it 2 odds', 'football only', 'give me safer ones']:
        request(private_bot, text)
    assert list(db.conn.iterdump()) == before
    assert private_bot.service.mock_calls == [] and private_bot._admin_threads == []
    forbidden.assert_not_called()
    assert not ticket_audit_switch.ENABLED
