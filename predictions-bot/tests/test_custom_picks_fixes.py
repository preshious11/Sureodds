"""Regression tests for the reported Custom Picks issues (Worker parity).

Covers: universal 1.25 floor, count/target mixups, heading accuracy, ticket
variety, fixture validation, market labels, 50-odds recovery, and the final
delivery boundary. Top Picks and Daily Picks are untouched.
"""
from dataclasses import replace
from math import prod

import pytest

from predbot import custom_picks as cp
from predbot.config import Config
from predbot.custom_recovery import claimed_count, market_complete, valid_ticket
from predbot.model.engine import representative_predictions
from predbot.util.timeutil import iso
from tests.test_custom_pick_candidates import alternatives
from tests.test_custom_picks import NOW, ZONE, candidates, rows_for, store

DAY = '2030-06-01'


def pool_for(prices, start=1, probability=.7):
    rows = []
    for offset, price in enumerate(prices):
        rows += rows_for(start + offset, price=price, probability=probability)
    return candidates(rows)


def stamp(row):
    return dict(row, prediction_date=DAY, timezone=ZONE, generated_at=iso(NOW))


def all_rows(i=1):
    """Every evaluated market alternative for one fixture, as archived dicts."""
    return representative_predictions(alternatives(i)).candidates


def test_sub_floor_prices_never_appear_at_any_target():
    pool = pool_for([1.03, 1.5, 1.6, 1.7, 1.8, 2.0])
    ticket = cp.search(pool, cp.Request(target_odds=15))
    assert ticket is not None
    assert all(c.price >= 1.25 for c in ticket.picks)
    assert not any(c.price == 1.03 for c in ticket.picks)


@pytest.mark.parametrize('price', [1.03, 1.08, 1.12, 1.19, 1.21])
def test_only_sub_floor_pool_yields_no_ticket(price):
    pool = pool_for([price, price + .01, price + .02])
    assert max(c.price for c in pool) < 1.25
    assert cp.search(pool, cp.Request(target_odds=2.0)) is None
    assert cp.search(pool, cp.Request(target_odds=15)) is None
    assert cp.search(pool, cp.Request(pick_count=2)) is None


def test_valid_ticket_rejects_sub_floor_leg():
    pool = pool_for([1.12, 1.45])
    bad = cp.State(tuple(pool), prod(c.price for c in pool))
    assert not valid_ticket(bad, cp.Request(target_odds=2.0), NOW, ZONE)


def test_three_pick_request_returns_exactly_three():
    pool = pool_for([1.6] * 4)
    ticket = cp.search(pool, cp.Request(pick_count=3))
    assert len(ticket.picks) == 3
    text = cp.format_ticket(ticket, False, ZONE)
    assert text.startswith(f'\U0001f525 {prod(c.price for c in ticket.picks):.2f} ODDS')
    assert text.count('Confidence:') == 3


def test_fifteen_odds_is_not_fifteen_legs():
    pool = pool_for([1.8, 1.7, 1.6, 1.5, 1.4, 1.9, 2.0, 2.1, 2.2])
    request = cp.Request(target_odds=15)
    assert request.pick_count is None
    ticket = cp.search(pool, request)
    if ticket is not None:
        assert len(ticket.picks) <= cp.MAX_LEGS < 15
        assert all(c.price >= 1.25 for c in ticket.picks)


def distinct_rows(prices, probability=.7):
    """One row per fixture with distinct team names so tickets render distinctly."""
    rows = []
    for i, price in enumerate(prices, start=1):
        for row in rows_for(i, price=price, probability=probability):
            row = dict(row, home=f'Home{i}', away=f'Away{i}',
                       home_id=f'home-{i}', away_id=f'away-{i}')
            rows.append(row)
    return rows


def test_shortlist_and_search_agree_with_variety_across_users(db):
    pool = candidates(distinct_rows([1.7] * 5))
    request = cp.Request(target_odds=5)
    options = cp.shortlist(pool, request)
    assert len(options) > 1
    assert len({cp.ticket_signature(t) for t in options}) > 1
    assert cp.search(pool, request) == options[0]
    store(db, distinct_rows([1.7] * 5))
    custom = cp.CustomPicks(db, Config(timezone=ZONE))
    seen = {custom.reply('5 odds', str(user), NOW) for user in range(100, 110)}
    assert len(seen) > 1
    first = custom.reply('5 odds', 'repeat-user', NOW)
    second = custom.reply('5 odds', 'repeat-user', NOW)
    assert second != first
    lone = custom.recovery.choose(options[:1], 'anyone', DAY, request)
    assert lone == options[0]


def test_conflicting_fixture_identities_are_dropped(db):
    rows = rows_for(1) + rows_for(2)
    store(db, rows)
    assert cp.CustomPicks(db, Config(timezone=ZONE)).reply('2 picks', '42', NOW).startswith('\U0001f525 ')
    tampered = dict(rows_for(1)[0])
    tampered.update(home='Brighton', away='Kauno \u017dalgiris', home_id='bri', away_id='kau')
    store(db, [rows_for(1)[0], tampered])
    reply = cp.CustomPicks(db, Config(timezone=ZONE)).reply('1 pick', '42', NOW)
    assert 'Brighton' not in reply


def test_same_team_rows_are_rejected():
    row = stamp(rows_for(1)[0])
    assert cp.normalize(dict(row, away=row['home']), DAY, ZONE, NOW, NOW, {'football'}) is None
    assert cp.normalize(dict(row, away_id=row['home_id']), DAY, ZONE, NOW, NOW, {'football'}) is None


def test_total_and_handicap_labels_carry_scope_line_and_side():
    rows = {(row['market'], row['selection'], row.get('line')): row for row in all_rows()}
    over = cp.normalize(stamp(rows[('total', 'over', 2.5)]), DAY, ZONE, NOW, NOW, {'football'})
    assert 'Over 2.5 Goals' in over.outcome
    handicap = cp.normalize(stamp(rows[('handicap', 'home', -.5)]), DAY, ZONE, NOW, NOW, {'football'})
    assert handicap.outcome.startswith('Handicap: ') and '-0.5' in handicap.outcome
    basketball = cp.normalize(stamp(dict(rows[('total', 'over', 2.5)], sport='basketball')),
                              DAY, ZONE, NOW, NOW, {'basketball'})
    assert 'Over 2.5' in basketball.outcome and 'Points' in basketball.outcome


def test_fifty_odds_fails_honestly_with_proven_alternatives(db):
    store(db, [r for i, price in enumerate([1.8, 1.6, 1.9], start=1)
               for r in rows_for(i, price=price, probability=.7)])
    custom = cp.CustomPicks(db, Config(timezone=ZONE))
    response = custom.respond('50 odds', '42', '42', NOW)
    assert response.reason != 'success' and '🔥 ' not in response.text
    assert response.reply_markup
    for row in response.reply_markup['inline_keyboard']:
        suggestion = custom.recovery.ready[row[0]['callback_data'][len('cp|'):]]
        assert valid_ticket(suggestion.ticket, suggestion.request, NOW, ZONE)
        assert all(c.price >= 1.25 for c in suggestion.ticket.picks)
        claimed = claimed_count(suggestion.request)
        if claimed:
            assert len(suggestion.ticket.picks) == claimed


def test_delivery_boundary_rejects_incomplete_markets():
    totals = []
    for i in (1, 2):
        over = next(r for r in all_rows(i)
                    if (r['market'], r['selection'], r.get('line')) == ('total', 'over', 2.5))
        totals.append(over)
    assert all(r['line'] is not None for r in totals)
    pool = candidates([stamp(r) for r in totals])
    assert len(pool) == 2
    ticket = cp.search(pool, cp.Request(pick_count=2))
    assert market_complete(ticket) and valid_ticket(ticket, cp.Request(pick_count=2), NOW, ZONE)
    stripped = cp.State(tuple(replace(c, line=None) for c in ticket.picks), ticket.odds)
    assert not market_complete(stripped) and not valid_ticket(stripped, cp.Request(pick_count=2), NOW, ZONE)
