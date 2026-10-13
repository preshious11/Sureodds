"""Pure/in-memory regressions; no providers, historical files, or ACL patches."""
import json
from dataclasses import asdict

import pytest

from predbot.config import Config
from predbot.jobs.daily_picks import prediction_row
from predbot.markets.settlement import grade
from predbot.model.calibration import Calibrator
from predbot.model.devig import devig
from predbot.model.elo import Elo
from predbot.model.ensemble import EventModel
from predbot.model.margin import MarginModel
from predbot.model.payoff import Payoff, requires_distribution
from predbot.providers.base import MarketLine, MatchResult
from predbot.selection.top import promotion_reason
from predbot.storage.db import Database
from tests.test_picker import ev
from tests.conftest import dt
from predbot.util.timeutil import iso


def event_model(three_way=True, period='full', matched=True):
    event = ev('nfl', sport='american_football')
    event.canonical_event_id = 'nfl-fixture'
    event.markets = [MarketLine('result2','home',1.25,n_books=5),
                     MarketLine('result2','away',4.5,n_books=5),
                     MarketLine('handicap','home',1.9,line=-3,n_books=5),
                     MarketLine('handicap','away',1.9,line=3,n_books=5)]
    if three_way:
        for side, price in [('home',1.3),('draw',35.),('away',4.5)]:
            quotes = {f'book{i}' if matched else side+str(i): price for i in range(5)}
            event.markets.append(MarketLine('result3',side,price,period=period,book_quotes=quotes,n_books=5))
    db = Database(':memory:')
    return EventModel(event, Elo(db), Calibrator(db,scope='settlement_v2')), db


@pytest.mark.parametrize('three_way,period,matched', [(False,'full',True),(True,'reg',True),(True,'full',False)])
def test_two_way_prices_regulation_draws_or_unmatched_books_cannot_supply_final_tie(three_way,period,matched):
    model, db = event_model(three_way,period,matched)
    try:
        assert model.score(model.ev.markets[0]) is None
        assert requires_distribution(model.ev.markets[0], 'american_football')
        assert model.score(model.ev.markets[2]) is not None  # supported nonzero spreads remain evaluated
    finally:
        db.close()


def test_generation_storage_and_selection_preserve_tie_strict_win_and_unit_ev():
    model, db = event_model()
    try:
        scored = model.score(model.ev.markets[0])
        expected = devig({'home':1.3,'draw':35.,'away':4.5})['draw']
        settlement = scored.notes['settlement']
        assert settlement['push'] == pytest.approx(expected)
        assert scored.p_model == pytest.approx((1-expected)*settlement['fair_probability'])
        assert scored.ev == pytest.approx(settlement['won']*(scored.price-1)-settlement['lost'])
        row = prediction_row(scored,'2030-06-01'); row.update(quality='main', rank_score=.8)
        pid = db.store_prediction(row)
        stored = dict(db.query('SELECT * FROM picks WHERE id=?',(pid,))[0])
        assert stored['prob'] == scored.p_model
        assert json.loads(stored['sources'])['_notes']['settlement']['push'] == pytest.approx(expected)
        cfg = Config()
        eligible = dict(stored)
        assert promotion_reason(eligible,cfg,iso(dt())) is None
        thin = json.loads(eligible['sources'])
        thin['_notes']['nfl_tie_evidence']['books'] = ['book0']
        assert promotion_reason(dict(eligible,sources=json.dumps(thin)),cfg,iso(dt())) == 'insufficient_tie_books'
        eligible['sources'] = json.dumps({'_notes':{'probability_basis':'strict_win_v2'}})
        assert promotion_reason(eligible,cfg,iso(dt())) == 'unknown_nfl_tie_probability'
    finally:
        db.close()


@pytest.mark.parametrize('line,settled', [(0.,'push'),(-.25,'half_lost'),(.25,'half_won'),(-3.,'lost'),(3.,'won')])
def test_same_final_tie_has_correct_full_partial_or_decisive_settlement(line,settled):
    model = MarginModel('american_football',2.,45.,tie_probability=.02)
    ml = MarketLine('handicap','home',1.9,line=line)
    payoff = model.settlement(ml)
    assert grade(ml,MatchResult('final',{'full':(20,20)}),'american_football') == settled
    assert sum(asdict(payoff).values()) == pytest.approx(1)
    repriced = payoff.reprice(.6,model.fixed_tie(ml))
    assert repriced.fair_probability == pytest.approx(.6)
    if settled in ('push','half_won','half_lost'):
        assert getattr(repriced,settled) == pytest.approx(.02)
    assert repriced.ev(1/.6) == pytest.approx(0.)


def test_moneyline_and_pickem_share_payoff_but_quote_and_strict_win_are_distinct():
    model = MarginModel('american_football',4.,45.,tie_probability=.03)
    moneyline = MarketLine('result2','home',1.8)
    pickem = MarketLine('handicap','home',1.8,line=0.)
    assert model.settlement(moneyline) == model.settlement(pickem)
    assert grade(moneyline,MatchResult('final',{'full':(24,24)}),'american_football') == 'push'
    p = model.settlement(moneyline).reprice(.6)
    assert p.won == pytest.approx(.582)
    assert p.ev(1.8) == pytest.approx(.0776)
    binary = Payoff.binary(.59)
    assert binary.won > p.won and binary.ev(1.8) < p.ev(1.8)


def test_unknown_final_tie_fails_closed_but_period_ties_and_nonzero_pushes_remain_supported():
    model = MarginModel('american_football',0.,45.)
    for market,line in [('result2',None),('dnb',None),('handicap',0.),('handicap',-.25)]:
        ml = MarketLine(market,'home',1.9,line=line)
        assert model.prob(ml) is None and model.settlement(ml) is None
    reg = MarketLine('result2','home',1.9,period='reg')
    assert model.settlement(reg).push > 0
    spread = MarketLine('handicap','home',1.9,line=-3)
    assert model.settlement(spread).push > 0
    assert grade(spread,MatchResult('final',{'full':(23,20)}),'american_football') == 'push'


def test_inconsistent_quote_cannot_erase_fixed_tie_loss_mass():
    payoff = Payoff(won=.5,lost=.3,half_lost=.2)
    with pytest.raises(ValueError,match='inconsistent'):
        payoff.reprice(.99,('half_lost',.2))


def test_one_sided_model_cannot_ignore_the_quote_and_create_an_ev_advantage():
    payoff = Payoff(won=.98,push=.02)
    assert payoff.reprice(1.) == payoff
    with pytest.raises(ValueError,match='one-sided'):
        payoff.reprice(.6)


@pytest.mark.parametrize('two_way', [True,False])
def test_rare_priced_three_way_tie_is_not_inflated_by_the_generic_probability_floor(two_way):
    model, db = event_model()
    try:
        for line in model.ev.markets:
            if line.market == 'result3':
                line.price = {'home':1.2,'draw':10000.,'away':4.5}[line.selection]
                line.book_quotes = {f'book{i}':line.price for i in range(5)}
        if not two_way:
            model.ev.markets = [line for line in model.ev.markets if line.market=='result3']
        model = EventModel(model.ev,Elo(db),Calibrator(db,scope='settlement_v2'))
        draw = next(line for line in model.ev.markets if line.market=='result3' and line.selection=='draw')
        scored = model.score(draw)
        tie = model.margin_model.tie_probability
        assert 0 < tie < .02
        assert scored.p_model == pytest.approx(tie)
        assert scored.ev == pytest.approx(tie*draw.price-1)
        assert scored.p_model_basis == 'market_tie_reference' and scored.independent == []
    finally:
        db.close()


def test_rare_nfl_winner_is_not_given_an_artificial_two_percent_confidence_floor():
    model, db = event_model()
    try:
        away = model.ev.markets[1]
        away.price = 10000.
        model = EventModel(model.ev,Elo(db),Calibrator(db,scope='settlement_v2'),shrink=1.)
        scored = model.score(away)
        tie = model.margin_model.tie_probability
        assert 0 < scored.p_model < .02
        assert scored.p_model == pytest.approx((1-tie)*model.p_market[away.key()])
        assert scored.ev == pytest.approx((1-tie)*(model.p_market[away.key()]*away.price-1))
    finally:
        db.close()


def test_legacy_forecast_is_neither_rewritten_nor_promoted_as_corrected():
    model, db = event_model()
    try:
        scored = model.score(model.ev.markets[0])
        original = prediction_row(scored,'2030-06-01')
        original['sources']['_notes'].pop('nfl_tie_evidence')
        original['sources']['_notes'].pop('nfl_settlement_contract')
        pid = db.store_prediction(original)
        before = dict(db.query('SELECT * FROM picks WHERE id=?',(pid,))[0])
        db.archive_ticket_candidate(prediction_row(scored,'2030-06-01'))
        returned = db.ticket_candidate_rows('2030-06-01')[0]
        assert promotion_reason(returned,Config(),iso(dt())) == 'unknown_nfl_tie_probability'
        assert dict(db.query('SELECT * FROM picks WHERE id=?',(pid,))[0]) == before
    finally:
        db.close()


def test_legacy_nfl_samples_do_not_train_the_corrected_scope_or_rewrite_history():
    model, db = event_model()
    try:
        scored = model.score(model.ev.markets[0])
        for i in range(160):
            row = prediction_row(scored,'2030-06-01')
            row.update(canonical_event_id=str(i),fixture_key=str(i))
            row['sources']['_notes'].pop('nfl_tie_evidence')
            row['sources']['_notes'].pop('nfl_settlement_contract')
            pid = db.store_prediction(row)
            db.settle_pick(pid,'won')
        before = [tuple(r) for r in db.query('SELECT * FROM picks ORDER BY id')]
        corrected = Calibrator(db,scope='settlement_v2')
        assert corrected.refit() is None and corrected.brier() is None
        assert Calibrator(db).refit() is not None
        assert [tuple(r) for r in db.query('SELECT * FROM picks ORDER BY id')] == before
    finally:
        db.close()


def test_cached_binary_confidence_cannot_be_disguised_with_tie_provenance():
    model, db = event_model()
    try:
        scored = model.score(model.ev.markets[0])
        row = prediction_row(scored,'2030-06-01')
        row.update(id='synthetic',status='pending',sources=json.dumps(row['sources']),prob=.95)
        assert promotion_reason(row,Config(),iso(dt())) == 'unknown_nfl_tie_probability'
    finally:
        db.close()
