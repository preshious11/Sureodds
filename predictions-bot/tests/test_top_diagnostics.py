"""Top coverage, constructor boundaries, complete gate audits and provider degradation."""
import math

import pytest

from predbot.bot.lastrun import _funnel
from predbot.config import Config
from predbot.providers.base import MarketLine, norm_book
from predbot.providers.chain import merge_events
from predbot.providers.oddsapi import OddsApiProvider
from predbot.providers.sharpapi import SharpApiProvider
from predbot.providers.sportsgameodds import SportsGameOddsProvider
from predbot.selection.evidence import MAIN, classify
from predbot.selection.picker import choose_top, select
from tests.conftest import dt, load
from tests.test_oddsapi import odds_payload
from tests.test_picker import ev, sc
from tests.test_prediction_quality import cand, books


def test_daily_survives_two_book_coverage_while_top_requires_three():
    pool = [cand(ev(i), p=.72, price=1.45, quotes=books(2, 1.45)) for i in range(4)]
    trace = {}
    result = select(pool, Config(), dt(), top_trace=trace)
    assert result.daily and result.top == []
    funnel = result.diagnostics['top_funnel']
    assert [funnel[k] for k in ('raw_candidates', 'main_market', 'probability_qualified', 'book_qualified')] == [4, 4, 4, 0]
    assert result.diagnostics['top_quality_book_coverage'] == {'2': 4}
    assert result.diagnostics['top_ticket_status'] == 'NO_QUALITY_CANDIDATES'
    assert all(trace[id(s)] == ['INSUFFICIENT_BOOKS'] for s in pool)


def test_top_and_daily_are_independently_populated_with_sufficient_books():
    pool = [cand(ev(i), p=.72, price=1.45) for i in range(8)]
    result = select(pool, Config(), dt())
    assert len(result.top) == 2 and result.daily
    assert set(map(id, result.top)).isdisjoint(map(id, result.daily))


def test_missing_sharp_reference_remains_eligible_in_top():
    pool = [cand(ev(i), p=.72, price=1.45) for i in range(2)]
    result = select(pool, Config(), dt())
    assert result.top == pool
    assert all(s.p_fair is None and s.edge_ev is None and s.notes['evidence']['edge'] is None for s in pool)
    assert all(s.se > Config().se_main for s in pool)


@pytest.mark.parametrize('cutoff,allowed', [(-.04, True), (-.04001, False), (.02, True)])
def test_exact_sharp_edge_cutoff_reaches_top(cutoff, allowed):
    pool = [cand(ev(i), p=.72, price=1.45, p_fair=.72, edge_ev=cutoff) for i in range(2)]
    result = select(pool, Config(), dt())
    assert bool(result.top) is allowed
    if not allowed:
        assert result.diagnostics['top_all_gate_failures']['NEGATIVE_EDGE_VS_SHARP'] == 2


@pytest.mark.parametrize('prices,expected', [([2.08], 1), ([1.38, 1.50], 2), ([1.27]*3, 3), ([1.4, 1.4], 2)])
def test_one_two_three_leg_and_approximate_target_combinations(prices, expected):
    pool = [cand(ev(i), p=.72, price=p) for i, p in enumerate(prices)]
    result = select(pool, Config(), dt())
    assert len(result.top) == expected
    assert result.diagnostics['top_ticket_status'] == 'SELECTED'
    assert 1.75 <= math.prod(s.price for s in result.top) <= 2.75


def test_single_requires_the_strict_band_and_does_not_use_band_fallback():
    result = select([cand(ev(1), p=.72, price=1.80)], Config(), dt())
    assert result.top == []
    assert result.diagnostics['top_ticket_status'] == 'NO_ACCEPTABLE_COMBINATION'


@pytest.mark.parametrize('target,ceiling', [(2., 2.5), (1.75, 2.75)])
def test_float_boundary_is_consistent_between_bisect_and_final_check(target, ceiling):
    a = 1.27
    pool = [sc(ev(1), 'result3', 'home', .7, a), sc(ev(2), 'result3', 'home', .7, target/a)]
    result, _ = choose_top(pool, Config(), target=target, ceiling=ceiling, min_legs=2)
    assert len(result) == 2 and math.prod(s.price for s in result) == pytest.approx(target)


def test_reasonable_overshoot_is_found_but_real_overshoot_is_rejected():
    pool = [cand(ev(i), p=.72, price=1.62) for i in range(2)]
    assert len(select(pool, Config(), dt()).top) == 2
    assert select(pool, Config(top_relaxed_fallback=False), dt()).top == []
    assert select([cand(ev(i), p=.72, price=1.8) for i in range(2)], Config(), dt()).top == []


def test_lower_priced_alternative_on_same_match_is_not_deduped_before_search():
    fixture = ev(1)
    pool = [cand(fixture, p=.90, price=1.15), cand(fixture, p=.72, price=1.5), cand(ev(2), p=.72, price=1.4)]
    result = select(pool, Config(top_relaxed_fallback=False), dt())
    assert sorted(s.price for s in result.top) == [1.4, 1.5]


def test_audit_keeps_all_failures_even_after_daily_overwrites_general_trace():
    weak = cand(ev(1), p=.60, price=1.45, quotes=books(2), p_fair=.60, edge_ev=-.06)
    strong = cand(ev(2), p=.72, price=1.45, quotes=books(2))
    trace, top_trace = {}, {}
    result = select([weak, strong], Config(), dt(), trace=trace, top_trace=top_trace)
    assert 'PROBABILITY_FLOOR' in top_trace[id(weak)]
    assert 'INSUFFICIENT_BOOKS' in top_trace[id(weak)]
    assert 'NEGATIVE_EDGE_VS_SHARP' in top_trace[id(weak)]
    assert strong in result.daily and top_trace[id(strong)] == ['INSUFFICIENT_BOOKS']


def test_neither_probability_nor_evidence_is_weakened_to_fill_top():
    pool = [cand(ev(i), p=.61, price=1.45) for i in range(8)]
    result = select(pool, Config(), dt())
    assert result.daily and result.top == []
    assert Config().top_relax_prob == 0


def test_no_sgo_dependency_when_other_providers_quote_enough_books():
    pool = [cand(ev(i), p=.72, price=1.45) for i in range(2)]
    for s in pool:
        s.event.sources = ['sharpapi', 'oddsapi']
        s.line.providers = ['sharpapi', 'oddsapi']
    assert len(select(pool, Config(), dt(), top_priority_source='oddsapi').top) == 2


def test_same_book_from_two_providers_counts_once_and_different_books_stay_distinct():
    a, b = ev(1), ev(1)
    a.provider, a.sources = 'sharpapi', ['sharpapi']
    b.provider, b.sources = 'oddsapi', ['oddsapi']
    a.markets = [MarketLine('result3', 'home', 1.45, book_quotes={norm_book('DraftKings'): 1.45, norm_book('FanDuel'): 1.46})]
    b.markets = [MarketLine('result3', 'home', 1.45, book_quotes={norm_book('draftkings'): 1.45, norm_book('BetMGM'): 1.44})]
    merged = merge_events([a], [b])[0].markets[0]
    assert merged.distinct_books() == 3
    assert set(merged.book_quotes) == {'draftkings', 'fanduel', 'betmgm'}


@pytest.mark.parametrize('market,selection,line,canonical', [
    ('moneyline', 'home', None, 'result3'), ('draw_no_bet', 'home', None, 'dnb'),
    ('double_chance', 'home_draw', None, 'dc'), ('total_goals', 'over', 2.5, 'total'),
    ('point_spread', 'home', -.5, 'handicap')])
def test_sharpapi_normalizes_main_markets(budget, market, selection, line, canonical):
    provider = SharpApiProvider('test', budget, [])
    ml = provider._row_to_line(dict(market_type=market, selection_type=selection, line=line,
                                   odds_decimal=1.45, sportsbook='DraftKings'), 'football')
    assert ml.market == canonical
    candidate = cand(ev(1)); candidate.line = ml
    assert classify(candidate) == MAIN


def test_oddsapi_main_markets_classify_correctly(budget):
    provider = OddsApiProvider('test', budget)
    event = provider._rows_to_events([('soccer_epl', r) for r in odds_payload()], dt(hh=0), dt(hh=23))[0]
    assert {m.market for m in event.markets} == {'result3', 'handicap', 'total'}
    for ml in event.markets:
        candidate = cand(event); candidate.line = ml
        assert classify(candidate) == MAIN


def test_sgo_main_markets_classify_correctly(budget):
    event = SportsGameOddsProvider('test', budget, [], [])._parse_event(load('sgo_event.json'))
    for market in ('result3', 'dnb', 'dc', 'total', 'handicap'):
        ml = next(m for m in event.markets if m.market == market and m.period == 'full' and m.stat == 'points')
        candidate = cand(event); candidate.line = ml
        assert classify(candidate) == MAIN


def test_sgo_spread_entities_share_one_main_line_and_far_alternates_stay_restricted():
    fixture = ev(1)
    fixture.markets = [MarketLine('handicap', side, price, line=ln, entity=side)
                       for side, price, ln in [('home', 1.9, -.5), ('away', 1.9, .5),
                                               ('home', 1.1, 2.5), ('away', 8., -2.5)]]
    candidates = []
    for ml in fixture.markets:
        candidate = cand(fixture); candidate.line = ml
        candidates.append(candidate)
    assert [classify(s) for s in candidates] == ['main', 'main', 'restricted', 'restricted']


def test_admin_funnel_has_stage_counts_and_no_secrets():
    result = select([cand(ev(i), p=.72, price=1.45) for i in range(2)], Config(telegram_token='secret'), dt())
    text = '\n'.join(_funnel(result.diagnostics))
    assert 'TOP FUNNEL' in text and 'book qualified: 2' in text
    assert 'top_min_books=3' in text and 'secret' not in text


def test_subscriber_card_never_contains_top_diagnostics(db):
    from unittest.mock import Mock
    from predbot.bot.broadcast import picks_text
    from predbot.cache.fetcher import UnifiedFetcher
    cfg = Config()
    ps = select([cand(ev(i), p=.72, price=1.45) for i in range(4)], cfg, dt())
    snapshot = UnifiedFetcher(Mock(), db, cfg)._snapshot_from_pickset(ps, '2030-06-01')
    text = picks_text(snapshot, cfg.timezone)
    assert 'TOP PICKS' in text
    assert all(x not in text for x in ('TOP FUNNEL', 'top_min_books', 'INSUFFICIENT_BOOKS', 'NO_QUALITY_CANDIDATES'))


def test_one_leg_default_and_stale_limits_do_not_make_top_impossible(monkeypatch):
    monkeypatch.setenv('TOP_LEGS_MIN', '2')
    monkeypatch.setenv('TOP_MAX_PICKS', '0')
    monkeypatch.delenv('TOP_LEGS_MAX', raising=False)
    cfg = Config()
    assert (cfg.top_legs_min, cfg.top_legs_max) == (1, 3)


def test_max_picks_alias_is_honoured_and_canonical_name_wins(monkeypatch):
    monkeypatch.setenv('TOP_MAX_PICKS', '2')
    monkeypatch.delenv('TOP_LEGS_MAX', raising=False)
    assert Config().top_legs_max == 2
    monkeypatch.setenv('TOP_LEGS_MAX', '3')
    assert Config().top_legs_max == 3
