"""Exact acquired contracts and request coordinates; no collection or real DB."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path

import pytest

from predbot import custom_picks as cp
from predbot.config import Config
from predbot.custom_recovery import scoped, refine
from predbot.markets.settlement import grade
from predbot.model.engine import representative_predictions
from predbot.providers.base import MarketLine, MatchResult
from predbot.providers.budget import Budget
from predbot.providers.sharpapi import SharpApiProvider
from predbot.selection.picker import prediction_identity, PredictionPool, PickSet
from predbot.storage.db import Database
from tests.conftest import dt, load
from tests.test_custom_pick_candidates import alternatives
from predbot.util.timeutil import iso


CASES = load('market_requests.json')


def candidate(case):
    sport = 'basketball' if 'basketball' in case['text'] else 'tennis' if 'tennis' in case['text'] else 'football'
    row = dict(prediction_date='2030-06-01', timezone='Africa/Lagos', generated_at=iso(dt(hh=12)),
               canonical_event_id='fixture', sport=sport, home='Arsenal', away='Chelsea',
               start_utc=iso(dt(hh=20)), price=1.5, p_model=.8,
               **{k: case[k] for k in ('market', 'period', 'stat', 'entity', 'selection', 'line')})
    return cp.normalize(row, '2030-06-01', 'Africa/Lagos', dt(hh=12), dt(hh=12), Config().allowed_sports)


@pytest.mark.parametrize('case', CASES, ids=lambda c: c['text'])
def test_exact_requests_accept_only_the_requested_scope(case):
    req = cp.parse_request(case['text'])
    c = candidate(case)
    assert c and scoped(c, req)
    for field, wrong in [('period', '1h' if c.period == 'full' else 'full'),
                         ('stat', 'sets' if c.stat != 'sets' else 'games'),
                         ('entity', 'away' if c.entity != 'away' else 'home')]:
        assert not scoped(replace(c, **{field: wrong}), req), field
    if req.market_line is not None:
        assert not scoped(replace(c, line=c.line+.5), req)
        assert not scoped(replace(c, selection='under' if c.selection == 'over' else 'over'), req)


def test_scope_refinement_and_unavailable_recovery_do_not_substitute():
    req = cp.parse_request('home team over 1.5 goals')
    req = refine(req, cp.parse_request('make it safer'))
    assert req.market_entity == 'home' and req.market_line == 1.5
    req = refine(req, cp.parse_request('first half instead'))
    assert req.market_entity == 'home' and req.market_period == '1h' and req.market_line == 1.5
    wrong = candidate(CASES[0])
    assert not scoped(wrong, req)
    changed = refine(req, cp.parse_request('btts instead'))
    assert changed.market_period is None and changed.market_entity is None


def test_tennis_games_legacy_alias_and_btts_outcome_are_exact():
    c = candidate(CASES[5])
    req = cp.parse_request('tennis match games over 21.5')
    assert scoped(replace(c, stat='points'), req)
    assert not scoped(replace(c, stat='sets'), req)
    req = cp.parse_request('btts no')
    assert req.market_selection == 'no'
    assert not scoped(replace(c, market='btts',selection='yes'), req)


@pytest.mark.parametrize('text,period,entity', [
    ('first 5 innings over 4.5 runs','1i5',None),
    ('1h over 0.5 goals','1h',None),
    ('home over 1.5 goals',None,'home'),
    ('away team handicap +1.5',None,'all'),
])
def test_coordinate_aliases(text,period,entity):
    req=cp.parse_request(text)
    assert req.market_period==period and req.market_entity==entity


@pytest.mark.parametrize('sport', Config().allowed_sports)
def test_unknown_period_and_stat_never_fall_back_to_full_points(sport):
    db = Database(':memory:')
    try:
        p = SharpApiProvider('', Budget(db), [])
        base = dict(odds_decimal=1.8, market_type='total_points', selection_type='over', line=2.5)
        assert p._row_to_line(dict(base, market_segment='mystery_period'), sport) is None
        assert p._row_to_line(dict(base, market_type='total_unknown_stat'), sport) is None
        assert p._row_to_line(dict(base, market_type='shots_handicap', selection_type='home'), sport) is None
        assert p._row_to_line(dict(base, market_type='1st_inning_total_runs'), sport) is None
        assert p._row_to_line(dict(base,is_stale_pregame_price=True),sport) is None
        good = p._row_to_line(dict(base, market_segment='full_game'), sport)
        assert (good.period, good.stat, good.line) == ('full', 'points', 2.5)
    finally:
        db.close()


def test_recorded_mlb_ambiguous_contracts_quarantined_and_known_rows_preserved():
    db = Database(':memory:')
    try:
        p = SharpApiProvider('', Budget(db), [])
        rows = load('sharp_mlb_main.json')['data']
        parsed = [(r, p._row_to_line(r, 'baseball')) for r in rows]
        assert sum(m is not None for _, m in parsed) == 88
        assert all(m is None for r, m in parsed if r['market_type'] in ('1st_inning_total_runs', 'total_home_runs'))
        first_five = [m for r, m in parsed if r['market_type'] == '1st_5_innings_total_runs']
        assert len(first_five) == 10 and all(m.period == '1i5' for m in first_five)
        assert len([m for r,m in parsed if r['market_type']=='total_runs' and m]) == 16
    finally:
        db.close()


def test_acquired_team_total_reaches_shared_archives_and_custom_response():
    from datetime import timedelta
    from predbot.storage.identity import resolve_events_detailed
    from predbot.model.engine import generate, ModelSettings
    from predbot.markets.settlement import make_results_gate
    from predbot.util.timeutil import parse_iso, to_local
    rows = load('sharp_pl_wide_main.json')['data']
    earliest = min(parse_iso(r['event_start_time']) for r in rows)
    now = earliest - timedelta(hours=1)
    day = to_local(now, 'Africa/Lagos').date().isoformat()
    db = Database(':memory:')
    try:
        cfg = Config()
        p = SharpApiProvider('', Budget(db), [])
        events = p._rows_to_events(rows, now, earliest + timedelta(days=30))
        events, failed = resolve_events_detailed(events, db, cfg.supported_competitions)
        assert failed == []
        gen = generate(events, db, ModelSettings(), make_results_gate(cfg, {'espn': object()}))
        assert len(gen.candidates) == 120
        pool = PredictionPool(db, day)
        pool.store(PickSet(predictions=gen.predictions, ticket_predictions=gen.ticket_predictions,
                           candidates=gen.candidates, candidate_timezone=cfg.timezone,
                           diagnostics={'generated_at': iso(now)}))
        # Top/Daily have the same 120 independently scored contracts in their
        # archive; their probability/book gates remain separate from Custom.
        assert len(db.ticket_candidate_rows(day)) == 120
        assert len(db.get_custom_pick_candidates(day, cfg.timezone)) == 120
        response = cp.CustomPicks(db, cfg).respond('1 picks home team over 1.5 goals', 'test', 'test', now)
        assert response.reason == 'success'
        assert 'Over 1.5' in response.text and 'Total Goals' not in response.text
    finally:
        db.close()


@pytest.mark.parametrize('market_name',['1St Inning Total Runs','Total Home Runs','Total Punts','Total Touchdowns'])
def test_legacy_cached_ambiguous_contract_is_not_reissued_or_regraded(market_name):
    values = alternatives()[:1]
    values[0].line = MarketLine('total','over',1.8,line=.5,market_name=market_name)
    gen = representative_predictions(values)
    assert gen.candidates == [] and gen.diagnostics['line_rejections'] == {'ambiguous_contract':1}
    row_case = CASES[0]
    c = candidate(row_case)
    assert c  # ordinary full-match totals remain available


def test_disabled_family_is_not_served_but_its_archive_is_preserved():
    from predbot.custom_recovery import Recovery
    value=alternatives()[0]
    value.line=MarketLine('odd_even','odd',1.8)
    gen=representative_predictions([value])
    db=Database(':memory:')
    try:
        db.store_custom_pick_candidates('2030-06-01','Africa/Lagos',gen.candidates,
            generated_at=iso(dt(hh=12)),complete=True,diagnostics={})
        assert len(db.get_custom_pick_candidates('2030-06-01','Africa/Lagos'))==1
        pool=Recovery(db,Config()).load(dt(hh=12))
        assert pool and pool.candidates==()
    finally:db.close()


@pytest.mark.parametrize('key,value', [('entity','home'),('entity','away'),('period','1h'),('stat','corners'),('line',2.5),('selection','under')])
def test_prediction_identity_preserves_each_contract_coordinate(key,value):
    row = dict(canonical_event_id='fx',market='total',period='full',stat='points',entity='all',line=1.5,selection='over')
    assert prediction_identity(row) != prediction_identity(dict(row, **{key:value}))
    assert prediction_identity(row) == prediction_identity(dict(row, provider='another'))


def test_side_market_entity_tags_do_not_create_duplicate_predictions():
    row=dict(canonical_event_id='fx',market='handicap',selection='home',period='full',stat='points',line=-1.5,entity='all')
    assert prediction_identity(row)==prediction_identity(dict(row,entity='home'))


def identity_replay(legacy=False):
    from predbot.selection import picker
    values = alternatives()[:1]
    s = values[0]
    home = deepcopy(s); home.line = MarketLine('total','over',1.5,line=1.5,entity='home',n_books=6)
    home.p_model, home.rank_score = .9, .9
    match = deepcopy(home); match.line = replace(home.line,entity='all'); match.p_model,match.rank_score=.8,.8
    pool = PredictionPool(Database(':memory:'),'2030-06-01')
    original = picker.prediction_identity
    try:
        pool.store(PickSet(predictions=[],ticket_predictions=[home,match]))
        if legacy:
            picker.prediction_identity=lambda row: original(dict(row,entity='all'))
        cfg=replace(Config(),top_odds_target=1.5,top_odds_max=2.,top_legs_max=1,daily_odds_min=1.5,daily_odds_max=2.)
        pool.package(cfg,iso(dt(hh=12)))
        return {kind:[r['entity'] for r in pool.rows(kind)] for kind in ('top','daily')}
    finally:
        picker.prediction_identity=original
        pool.db.close()


def test_fixed_identity_replay_recovers_daily_alternative_without_correlated_ticket():
    before,after=identity_replay(True),identity_replay(False)
    assert before=={'top':['home'],'daily':[]}
    assert after=={'top':['home'],'daily':['all']}


@pytest.mark.parametrize('market,side,entity,line,score,result',[
    ('total','over','home',1.5,(2,0),'won'),
    ('total','over','all',2.5,(2,0),'lost'),
    ('handicap','away','all',1.5,(2,1),'won'),
    ('total','over','home',2.25,(2,0),'half_lost'),
])
def test_exact_settlement_identity(market,side,entity,line,score,result):
    ml=MarketLine(market,side,1.8,entity=entity,line=line,period='1h')
    res=MatchResult(status='final',scores={'1h':score,'full':(5,4)})
    assert grade(ml,res,'football')==result


@pytest.mark.parametrize('text',['third half over 1.5 goals','home team away +1.5 handicap','over 2.5 home runs','+1.5 handicap'])
def test_ambiguous_requests_fail_closed(text):
    with pytest.raises(cp.RequestError):
        cp.parse_request(text)
