"""Regression for the first real SportyBet share-code decode (4 legs). Mocked transport only."""
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import re

import pytest

from predbot.bot import audit_format
from predbot.ticket_audit import booking as bk
from predbot.ticket_audit.booking_sources import SportyBetDirectResolver
from tests.test_ticket_audit_booking_codes import (Resp, direct, service, event, sporty, tap, code_prompt, texts,
                                                   firewall, store, ui)  # noqa: F401
from tests.test_ticket_audit_foundation import no_providers, NOW  # noqa: F401
from tests.test_ticket_audit_telegram import KEY

FIXTURE = json.loads((Path(__file__).parent / 'fixtures' / 'sportybet_share_4leg_sanitized.json').read_text(encoding='utf-8'))
INTERNAL = re.compile(r'\b(result2|result3|_1x2|over_under|dnb|btts|dc)\b')


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()


@pytest.fixture
def decoded(store):
    r, t = direct(store, Resp(body=FIXTURE))
    result = service(store, r).resolve('sportybet', 'REAL4L', NOW)
    return result, t, r


def test_four_legs_in_four_fully_normalized_out(decoded):
    result, t, _ = decoded
    events = FIXTURE['data']['outcomes']
    assert len(events) == 4 and len(t.calls) == 1
    assert result.status == bk.OK and len(result.selections) == 4
    for leg, e in zip(result.selections, events):
        assert (leg.home, leg.away) == (e['homeTeamName'], e['awayTeamName'])
        assert leg.competition == e['sport']['category']['tournament']['name']
        assert leg.kickoff == iso(e['estimateStartTime'])
        assert leg.odds == float(e['markets'][0]['outcomes'][0]['odds'])
        assert leg.unresolved == ()
    assert [l.sport for l in result.selections] == ['tennis', 'football', 'tennis', 'football']
    ticket = bk.to_ticket(result)
    assert len(ticket.selections) == 4 and ticket.extraction_warnings == () and ticket.extraction_confidence == 'HIGH'
    assert all(s.researchable() for s in ticket.selections)


def test_tennis_winner_home_resolves_to_named_participant(decoded):
    werner = decoded[0].selections[0]
    assert (werner.market, werner.outcome, werner.period, werner.stat, werner.entity) == \
        ('result2', 'home', 'full', 'points', 'all')
    assert werner.selected_participant == 'Werner, Caroline'
    s = bk.to_ticket(decoded[0]).selections[0]
    assert bk.selected_name(s) == 'Werner, Caroline' and s.raw_text == 'Winner — Werner, Caroline'
    assert audit_format.pick(s) == 'Werner, Caroline to Win'
    away = decoded[0].selections[2]
    assert away.outcome == 'away' and away.selected_participant == 'Test Opponent'


def test_football_1x2_home_maps_with_participant(decoded):
    leg = decoded[0].selections[1]
    assert (leg.market, leg.outcome, leg.period, leg.stat, leg.entity) == ('result3', 'home', 'full', 'points', 'all')
    assert leg.selected_participant == 'Placeholder FC'


def test_second_half_over_1_5_maps(decoded):
    leg = decoded[0].selections[3]
    assert (leg.sport, leg.competition, leg.home, leg.away) == ('football', 'U19 1. Liga', 'FC Viktoria Plzen', 'Dukla Prague')
    assert (leg.market, leg.period, leg.stat, leg.entity, leg.line, leg.outcome, leg.odds) == \
        ('total', '2h', 'points', 'all', 1.5, 'over', 1.54)
    assert leg.selected_participant is None and leg.provider_market_id == '90'


def test_user_facing_rendering_never_shows_internal_market_ids(decoded, db):
    from predbot.ticket_audit.local import LocalResolver
    from predbot.ticket_audit.service import AuditService
    result = decoded[0]
    ticket = bk.to_ticket(result)
    review = '\n'.join(audit_format.booking_review(ticket, result))
    assert not INTERNAL.search(review)
    for phrase in ('Market: Winner', 'Market: 1X2', 'Market: 2nd Half Over/Under', 'Werner, Caroline to Win',
                   'Placeholder FC to Win', 'Test Opponent to Win', 'over 1.5', 'Second half', '@ 1.54'):
        assert phrase in review
    pages = audit_format.results(ticket, AuditService(LocalResolver(db.path, 'UTC')).audit(ticket, NOW))
    assert not INTERNAL.search('\n'.join(pages))


def test_end_to_end_telegram_review_and_audit_have_no_internal_ids(ui):
    ui.transport.responses = [Resp(body=FIXTURE)]
    s = code_prompt(ui)
    ui.controller.callback(tap(ui.controller, 'bk:sportybet'))
    assert s.state == 'PENDING_REVIEW' and s.booking.status == bk.OK
    ui.controller.callback(tap(ui.controller, 'confirm'))
    out = texts(ui)
    assert 'Ticket Audit\n\n4 selections' in out and not INTERNAL.search(out)
    assert 'only partly decode' not in out and 'Not decoded' not in out


def test_combined_odds_absent_from_payload_stays_null(decoded):
    result, _, resolver = decoded
    assert result.total_odds is None and bk.to_ticket(result).submitted_combined_odds is None
    diag = resolver.last_diagnostic
    assert diag['ticket_keys'] == ['selections'] and diag['odds_like_paths'] == []
    assert diag['market_ids'] == ['186', '1', '186', '90']


def test_odds_like_ticket_paths_are_reported_not_mapped(store):
    body = json.loads(json.dumps(FIXTURE))
    body['data']['ticket']['totalOdds'] = '4.21'
    r, _ = direct(store, Resp(body=body))
    result = service(store, r).resolve('sportybet', 'ODDS12', NOW)
    assert r.last_diagnostic['odds_like_paths'] == ['ticket.totalOdds']
    assert result.total_odds is None  # field unverified: reported for the next run, never guessed


# ------------------------------------------------ general period Over/Under mapping (not fixture-specific)
@pytest.mark.parametrize('market_id', ['18', '68', '90', '999', None])
@pytest.mark.parametrize('label,period', [('Over/Under', 'full'), ('1st Half - Over/Under', '1h'),
                                          ('2nd Half - Over/Under', '2h'), ('2nd half over/under', '2h'),
                                          ('First Half - Total Goals', '1h')])
@pytest.mark.parametrize('outcome_id,outcome_label,side,line', [('12', 'Over 2.5', 'over', 2.5), ('13', 'Under 0.5', 'under', 0.5)])
def test_football_period_over_under_by_id_or_label(store, market_id, label, period, outcome_id, outcome_label, side, line):
    id_period = {'18': 'full', '68': '1h', '90': '2h'}.get(market_id)
    e = event('Home Side', 'Away Side', 'sr:sport:1', 'Any League', market_id, outcome_id, '1.9',
              f'total={line}', label, outcome_label)
    r, _ = direct(store, Resp(body=sporty(e)))
    leg = service(store, r).resolve('sportybet', 'OU1234', NOW).selections[0]
    if id_period is not None and id_period != period:
        assert leg.market is None and 'market' in leg.unresolved  # id/label conflict: never pick one
    else:
        assert (leg.market, leg.period, leg.stat, leg.entity, leg.outcome, leg.line) == \
            ('total', period, 'points', 'all', side, line)
        assert leg.unresolved == ()


def test_line_from_outcome_label_when_specifier_missing(store):
    e = event('A', 'B', 'sr:sport:1', 'L', None, None, '1.7', None, '2nd Half - Over/Under', 'Under 1.5')
    r, _ = direct(store, Resp(body=sporty(e)))
    leg = service(store, r).resolve('sportybet', 'LINE12', NOW).selections[0]
    assert (leg.market, leg.period, leg.outcome, leg.line) == ('total', '2h', 'under', 1.5)


@pytest.mark.parametrize('kw,missing', [
    (dict(specifier='total=1.5', odesc='Over 2.5'), 'line'),          # specifier vs label
    (dict(outcome_id='12', odesc='Under 1.5'), 'outcome'),             # outcome id vs label
    (dict(market_id='18', mdesc='2nd Half - Over/Under'), 'market'),   # full-match id vs 2nd-half label
    (dict(sport='sr:sport:5'), 'market'),                              # football rule on a tennis event
    (dict(specifier=None, odesc='Over'), 'line'),                      # no line anywhere
])
def test_conflicting_or_missing_evidence_is_unresolved_not_chosen(store, kw, missing):
    args = dict(sport='sr:sport:1', market_id='90', outcome_id='12', specifier='total=1.5',
                mdesc='2nd Half - Over/Under', odesc='Over 1.5')
    args.update(kw)
    e = event('A', 'B', args['sport'], 'L', args['market_id'], args['outcome_id'], '1.6', args['specifier'],
              args['mdesc'], args['odesc'])
    r, _ = direct(store, Resp(body=sporty(e)))
    result = service(store, r).resolve('sportybet', 'CONF12', NOW)
    assert result.status == bk.PARTIAL and missing in result.selections[0].unresolved


def test_winner_label_naming_participant_and_unknown_id(store):
    e = event('Werner, Caroline', 'Blinkova, Anna', 'sr:sport:5', 'WTA', None, None, '2.2', None, 'Winner', 'Blinkova, Anna')
    r, _ = direct(store, Resp(body=sporty(e)))
    leg = service(store, r).resolve('sportybet', 'WIN123', NOW).selections[0]
    assert (leg.market, leg.outcome, leg.selected_participant) == ('result2', 'away', 'Blinkova, Anna')


def test_identical_participant_names_cannot_resolve_side_from_label(store):
    e = event('Same', 'Same', 'sr:sport:5', 'WTA', None, None, '2.2', None, 'Winner', 'Same')
    r, _ = direct(store, Resp(body=sporty(e)))
    leg = service(store, r).resolve('sportybet', 'SAME12', NOW).selections[0]
    assert leg.outcome is None and 'outcome' in leg.unresolved


def test_first_and_second_half_1x2(store):
    legs = [event('A', 'B', 'sr:sport:1', 'L', mid, '3', '3.1', None, label, 'Away')
            for mid, label in (('60', '1st Half - 1X2'), ('83', '2nd Half - 1X2'), (None, '2nd Half - 1X2'))]
    r, _ = direct(store, Resp(body=sporty(*legs)))
    out = service(store, r).resolve('sportybet', 'HALF12', NOW).selections
    assert [(l.market, l.period, l.outcome, l.selected_participant) for l in out] == \
        [('result3', '1h', 'away', 'B'), ('result3', '2h', 'away', 'B'), ('result3', '2h', 'away', 'B')]
