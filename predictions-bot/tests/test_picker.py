"""Picker rules: no padding, no top/daily overlap, no single-outcome bias."""
from __future__ import annotations

import math
from collections import Counter
from datetime import timedelta

import pytest

from predbot.config import Config
from predbot.model.ensemble import Scored
from predbot.providers.base import Event, MarketLine
from tests.pipeline_helpers import select as _select


def select(scored, cfg, now, **kw):
    """Top/Daily mechanics on the given predictions (see pipeline_helpers.select)."""
    kw.setdefault("raw", True)
    return _select(scored, cfg, now, **kw)

from tests.conftest import dt


def ev(i, home=None, away=None, sport="football", start=None, league=None):
    # Each event gets its own league unless a test asks for a shared one, so
    # the per-league quota only bites in the tests written to exercise it.
    return Event(provider="test", provider_event_id=str(i), sport=sport,
                 league=league or f"L{i}", home=home or f"Home{i}", away=away or f"Away{i}",
                 start=start or dt(hh=22), sources=["a", "b"])


def sc(event, market, selection, p_model, price, *, line=None, sources=2,
       ev_val=0.06, agreement=1.0, liquidity=1.0, books=5):
    """``sources`` = estimates in the model blend; ``books`` = distinct bookmakers quoting the line (default: a
    well-covered line, so coverage is only a factor in the tests written to exercise it)."""
    ml = MarketLine(market, selection, price, line=line, n_books=max(sources, books))
    return Scored(event=event, line=ml, p_market=p_model - 0.02, p_model=p_model,
                  sources={f"s{n}": p_model for n in range(sources)},
                  agreement=agreement, ev=ev_val, edge=0.03,
                  label=f"{market}:{selection}", liquidity=liquidity)


def cfg():
    return Config()


def test_no_padding_when_pool_small():
    now = dt()
    pool = [sc(ev(i), "result3", "home", 0.68, 1.7) for i in range(5)]
    ps = select(pool, cfg(), now)
    assert len(ps.daily) + len(ps.top) <= 5   # never invents picks


def test_weak_picks_excluded():
    now = dt()
    # p below daily_min_prob and negative EV -> should not appear
    weak = [sc(ev(i), "result3", "home", 0.10, 1.5, ev_val=-0.2, agreement=0.0) for i in range(30)]
    ps = select(weak, cfg(), now)
    assert ps.top == []
    assert ps.daily == []


def test_one_prediction_per_event_and_family_even_when_it_sits_in_top_and_daily():
    """Collections reference predictions; they never copy them. An event offering three families yields ONE prediction
    per family, and a single prediction may sit in both Top and Daily."""
    now = dt()
    pool = []
    for i in range(25):
        e = ev(i)
        pool.append(sc(e, "result3", "home", 0.70, 1.75))
        pool.append(sc(e, "result2", "home", 0.70, 1.70))
        pool.append(sc(e, "total", "over", 0.66, 1.8, line=2.5))
    ps = _select(pool, cfg(), now, raw=False)
    per_event = Counter(s.event.canonical_event_id for s in ps.predictions)
    assert len(ps.predictions) == 75 and set(per_event.values()) == {3}
    assert len({(s.event.canonical_event_id, s.line.market) for s in ps.predictions}) == 75
    for chosen in ps.top + ps.daily:
        assert chosen in ps.predictions            # a membership of a stored prediction, not a new one


def test_top_picks_meet_quality_bar():
    now = dt()
    # 1.42 so a 2-leg combo (1.42 * 1.42 = 2.02) lands inside the 2.00-2.50
    # accumulator band; 1.8 would make every pairing exceed the 2.50 ceiling.
    # 1.42 so a 2-leg combo (1.42 * 1.42 = 2.02) lands inside the 2.00-2.50
    # accumulator band; 1.8 would make every pairing exceed the 2.50 ceiling.
    pool = [sc(ev(i), "result3", "home", 0.70, 1.42, sources=3) for i in range(20)]
    ps = select(pool, cfg(), now)
    c = cfg()
    for t in ps.top:
        assert t.p_model >= c.top_min_prob
        assert t.ev >= c.top_max_neg_ev      # disagreement floor, not an edge requirement
        assert len(t.sources) >= 2
    ticket = math.prod(t.price for t in ps.top)
    assert ps.top and ticket >= c.top_odds_target


def test_single_book_never_top():
    now = dt()
    pool = [sc(ev(i), "result3", "home", 0.72, 1.8, sources=1, books=1, agreement=0.0) for i in range(10)]
    ps = select(pool, cfg(), now)
    assert ps.top == []   # Top needs real bookmaker coverage ...
    assert len(ps.predictions) == 10   # ... but the predictions themselves still exist
    assert ps.diagnostics["top"]["rejections"]["insufficient_books"] == 10





def test_min_lead_time_excludes_imminent():
    now = dt(hh=21, mm=50)
    started = ev(1, start=dt(hh=21, mm=40))   # already under way when the collections are built
    ps = select([sc(started, "result3", "home", 0.7, 1.8)], cfg(), now)
    assert ps.top == [] and ps.daily == []
    assert ps.diagnostics["top"]["rejections"]["started_or_settled"] == 1


# ------------------------------------------------------------- ticket sizing
def test_top_ticket_is_sized_by_odds_not_by_a_count():
    now = dt()
    pool = [sc(ev(i), "result3", "home", 0.70, 1.45) for i in range(6)]
    ps = select(pool, cfg(), now)

    assert len(ps.top) == 2                                # 1.45 * 1.45 = 2.10 >= 2.00
    assert math.prod(t.price for t in ps.top) >= cfg().top_odds_target


def test_two_legs_already_at_target_beat_adding_a_third():
    now = dt()
    # 1.43 * 1.40 = 2.002, already inside the tight target band; a third leg
    # at 1.36 would push any 3-leg combo (2.723) past the 2.50 hard ceiling,
    # so it's physically excluded, not just deprioritized.
    pool = [sc(ev(1), "result3", "home", 0.70, 1.43),
            sc(ev(2), "result3", "home", 0.70, 1.40),
            sc(ev(3), "result3", "home", 0.70, 1.36)]
    ps = select(pool, cfg(), now)

    assert len(ps.top) == 2
    assert math.prod(s.price for s in ps.top) == pytest.approx(2.002)


def test_daily_list_is_sized_by_the_combined_odds_window():
    now = dt()
    # varied markets so the Daily's own per-market share is not what ends the ticket
    kinds = [("result3", "home", None), ("total", "over", 2.5), ("btts", "yes", None), ("dnb", "home", None)]
    pool = [sc(_fx(i), kinds[i % 4][0], kinds[i % 4][1], 0.62, 1.35, line=kinds[i % 4][2]) for i in range(30)]
    ps = select(pool, cfg(), now)
    c = cfg()
    ticket = math.prod(s.price for s in ps.daily)

    assert c.daily_odds_min <= ticket <= c.daily_odds_max   # 10 .. 12 on the Daily's own legs
    assert len(ps.daily) == 8                               # 1.35**8 = 11.03: it stops the moment 10 is passed
    assert ps.diagnostics["daily"]["target_reached"]
    assert len(ps.all()) == 30                              # the Daily never deletes a prediction


def test_pick_count_never_passes_the_cap():
    now = dt()
    c = Config(daily_odds_min=900.0, daily_odds_max=1000.0)   # window far out of reach
    pool = [sc(ev(i), "result3", "home", 0.62, 1.45) for i in range(40)]
    ps = select(pool, c, now)

    assert len(ps.daily) <= c.daily_target
    assert len(ps.top) <= 3
    assert len(ps.all()) == 40                                # ... while every prediction is still stored


# ------------------------------------------------------------- diversity quotas
def _corners(i, sel, price=1.45):
    s = sc(ev(i), "total", sel, 0.66, price, line=8.0)
    s.line.stat = "corners"
    return s


def test_single_market_pool_cannot_fill_the_daily_list():
    """100 strong candidates that are ALL corners totals: the market-share
    quota must stop one market taking the list. Selections alternate
    over/under so the per-outcome family cap is not what stops them."""
    now = dt()
    pool = [_corners(i, "over" if i % 2 == 0 else "under") for i in range(100)]
    c = cfg()
    ps = select(pool, c, now)

    corners = sum(1 for s in ps.daily if (s.line.market, s.line.stat) == ("total", "corners"))
    strict_cap = math.ceil(c.daily_max_market_share * c.daily_target)
    assert len(ps.daily) < c.daily_target
    # the quota may only be exceeded when the pool could not otherwise reach
    # the combined-odds floor, and then the relaxation must be on record
    assert corners <= strict_cap or ps.diagnostics.get("daily_caps_relaxed")


# Five distinct (market, stat) combinations spanning four distinct stats
# ("points" no more than 2 of 5, everything else 1 of 5): under the 25%
# market share AND 40% stat share, the strict pass needs the full spread on
# offer to keep growing the ticket, so a pool this varied must reach the
# combined-odds floor WITHOUT relaxing.
_MIXED = [("result3", "home", "points", None), ("total", "over", "corners", 8.0),
          ("odd_even", "even", "cards", None), ("btts", "yes", "points", None),
          ("total", "under", "rounds", 2.5)]


def _mixed(i, sport="football", league=None):
    market, sel, stat, line = _MIXED[i % len(_MIXED)]
    # p_model 0.60 is below top_min_prob (0.62) but above daily_min_prob (0.55) and, even for a restricted
    # market (uncertainty ~0.054), keeps the lower confidence bound above DAILY_MIN_LCB: these candidates
    # compete for Daily only, so the daily diversity quotas are exercised in isolation.
    # Distinct club names: fixtures are matched fuzzily (one pick per real fixture),
    # and "Home10"/"Home11"-style names would count as the same match.
    home, away = _names(random.Random(i))
    s = sc(ev(i, sport=sport, league=league, home=f"{home}{i}", away=f"{away}{i}"), market, sel, 0.60, 1.30, line=line)
    s.line.stat = stat
    return s


def test_mixed_pool_spreads_markets_and_sports():
    now = dt()
    pool = [_mixed(i, sport="football" if i % 2 == 0 else "basketball") for i in range(30)]
    c = cfg()
    ps = select(pool, c, now)

    markets = Counter((s.line.market, s.line.stat) for s in ps.daily)
    sports = Counter(s.event.sport for s in ps.daily)
    assert len(ps.daily) >= 4
    assert len(markets) >= 3 and len(sports) >= 2
    assert max(markets.values()) <= math.ceil(c.daily_max_market_share * len(ps.daily))
    assert max(sports.values()) <= math.ceil(c.daily_max_sport_share * len(ps.daily))
    assert not ps.diagnostics.get("daily_caps_relaxed")


def test_league_cap_limits_picks_from_one_competition():
    now = dt()
    # the Serie A block sorts first (equal rank, stable sort), so without the
    # league cap it would take every slot before the other competitions
    same = [_mixed(i, league="Serie A") for i in range(10)]
    others = [_mixed(100 + i) for i in range(20)]
    c = cfg()
    ps = select(same + others, c, now)

    assert sum(1 for s in ps.daily if s.event.league == "Serie A") <= c.daily_max_per_league
    assert len({s.event.league for s in ps.daily}) > 1
    assert not ps.diagnostics.get("daily_caps_relaxed")


# ------------------------------------------------------------- stat share cap
def _fx(i):
    """A fixture whose teams are genuinely different from every other index."""
    home, away = _names(random.Random(10_000 + i))
    return ev(i, home=f"{home}{i}", away=f"{away}{i}")


def _corners_stat(i, stat, market, sel, line):
    s = sc(_fx(i), market, sel, 0.66, 1.45, line=line)
    s.line.stat = stat
    return s


def _other_stat(i):
    market, sel, stat, line = _MIXED[i % len(_MIXED)]
    s = sc(_fx(i), market, sel, 0.66, 1.30, line=line)
    s.line.stat = stat
    return s


def test_points_stat_is_not_capped_by_the_stat_share():
    """'points' is the DEFAULT stat for main markets (result/total/handicap) in
    every sport - basketball has nothing else. Capping it silently blocked
    every basketball candidate, so the stat cap must apply to DERIVED stats
    (corners/cards/games) only."""
    now = dt()
    markets = [("result3", "home", None), ("result3", "away", None),
               ("total", "over", 2.5), ("total", "under", 3.5)]
    pool = []
    for i in range(16):
        market, sel, line = markets[i % len(markets)]
        s = sc(ev(i, sport="basketball"), market, sel, 0.55, 1.30, line=line,
               agreement=0.5, ev_val=-0.06)
        s.line.stat = "points"
        pool.append(s)
    c = cfg()
    ps = select(pool, c, now)

    points_picks = [s for s in ps.daily if s.line.stat == "points"]
    assert points_picks                                    # basketball-style slate exists
    assert len(points_picks) > math.ceil(c.daily_max_stat_share * len(ps.daily))
    assert "stat" not in (ps.diagnostics.get("daily_caps_relaxed") or {}).get("exceeded", {})




def test_corners_across_several_markets_cannot_take_the_list():
    """The (market, stat) cap sees corners totals and Corners 1X2 as two
    different markets, so a pool dominated by corners lines - spread over
    several corners markets and selections to sidestep the outcome-family cap
    - used to fill the list with corners-stat picks anyway. The stat share
    cap must hold corners-stat to DAILY_MAX_STAT_SHARE (0.40) even when
    corners dominate the pool across different corners markets."""
    now = dt()
    corners_pool = []
    i = 0
    for stat, market, sels, line in (
        ("corners", "total", ("over", "under"), 8.0),          # Corners totals
        ("corners", "result3", ("home", "away"), None),        # Corners 1X2
        ("corners", "odd_even", ("odd", "even"), None),        # Corners odd/even
    ):
        for sel in sels:
            # fresh events per (market, selection) so per-event/league caps
            # and cross-market contradiction checks never interfere
            for _ in range(20):
                corners_pool.append(_corners_stat(i, stat, market, sel, line))
                i += 1
    # Distinct non-corners markets priced below top_min_odds so everything
    # competes for Daily: enough variety that the ticket can reach the floor
    # while every cap is obeyed.
    others = [_other_stat(1000 + j) for j in range(40)]

    c = cfg()
    ps = select(corners_pool + others, c, now)

    corners = sum(1 for s in ps.daily if s.line.stat == "corners")
    assert len(ps.daily) >= 4
    assert corners <= max(1, math.ceil(c.daily_max_stat_share * len(ps.daily)))
    # (the cap rounds UP - ceil(0.40 x 8) = 4 of 8 - so a bare ratio check would be stricter than the cap itself)
    assert corners < len(ps.daily)


# ------------------------------------------------------------ EV gate branches
def test_market_only_candidates_skip_the_ev_gate():
    """A market-only candidate's p_model is de-vigged market probability, so
    its EV is negative by construction - it must NOT be rejected for that."""
    now = dt()
    # tennis-flavoured: only "market" in sources, EV strongly negative
    pool = [sc(ev(i, sport="tennis"), "result2", "home", 0.65, 1.50,
               sources=1, ev_val=-0.05, agreement=0.5) for i in range(12)]
    ps = select(pool, cfg(), now)
    assert ps.daily                                  # admitted despite negative EV


def test_market_derived_score_model_does_not_trigger_the_disagreement_floor():
    """score_model is fitted to the market's own lines, so a
    {market, score_model} candidate carries no independent edge - its EV is
    just the bookmaker margin and it must skip the EV gate (this is what kept
    every basketball candidate out of the slate)."""
    now = dt()
    s = sc(ev(1, sport="basketball"), "result3", "home", 0.60, 1.60, sources=1,
           ev_val=-0.065, agreement=0.5)
    s.sources = {"market": 0.60, "score_model": 0.60}
    ps = select([s], cfg(), now)
    assert ps.daily


def test_independent_signal_candidates_have_a_disagreement_floor_not_an_edge_requirement():
    """With an independent signal (elo), a candidate is kept out of Top/Daily only when that signal says the
    market is MATERIALLY overpricing it (ev < -0.05). That is a value judgement on the collections: the model
    prediction itself still exists in All Predictions."""
    now = dt()
    mildly_negative = sc(ev(1), "result3", "home", 0.70, 1.50, sources=1, ev_val=-0.03)
    mildly_negative.sources = {"market": 0.68, "elo": 0.72}
    mildly_negative.independent = ["elo"]
    strongly_overpriced = sc(ev(2), "result3", "home", 0.70, 1.50, sources=1, ev_val=-0.10)
    strongly_overpriced.sources = {"market": 0.68, "elo": 0.72}
    strongly_overpriced.independent = ["elo"]
    ps = select([mildly_negative, strongly_overpriced], cfg(), now)
    keys = lambda rows: {s.event.dedupe_key() for s in rows}
    assert keys(ps.all()) == {mildly_negative.event.dedupe_key(), strongly_overpriced.event.dedupe_key()}
    assert keys(ps.daily) == {mildly_negative.event.dedupe_key()}
    assert strongly_overpriced.event.dedupe_key() not in keys(ps.top)
    assert ps.diagnostics["daily"]["rejections"].get("poor_model_value", 0) == 1










import random
from itertools import combinations as _combos


def _names(rng):
    return ("".join(rng.choice("bcdfghjklmnpqrstvwxz") + rng.choice("aeiou") for _ in range(4)).title(),
            "".join(rng.choice("bcdfghjklmnpqrstvwxz") + rng.choice("aeiou") for _ in range(4)).title())


def _leg(rng, i, price, p):
    home, away = _names(rng)
    return sc(ev(i, home=f"{home}{i}", away=f"{away}{i}"), "result3", "home", p, price, sources=3)














# ------------------------------------------------- one selection per real fixture

def test_two_selections_from_the_same_fixture_never_both_reach_top_or_daily():
    """Every fixture offers three strong, different families: all three are All Predictions (one per family), but Top and
    Daily each take at most ONE prediction per fixture."""
    rng = random.Random(11)
    pool = []
    for i in range(24):
        home, away = _names(rng)
        e = ev(i, home=f"{home}{i}", away=f"{away}{i}")
        pool.append(sc(e, "result3", "home", 0.70, 1.45, sources=3))
        pool.append(sc(e, "total", "over", 0.68, 1.50, line=2.5, sources=3))
        pool.append(sc(e, "btts", "yes", 0.66, 1.55, sources=3))
    ps = _select(pool, cfg(), dt(), raw=False)

    picks = ps.all()
    assert len(picks) == 72                                       # three families on each of 24 fixtures
    per_fixture = Counter(p.event.dedupe_key() for p in picks)
    assert set(per_fixture.values()) == {3}
    assert max(Counter(p.event.dedupe_key() for p in ps.top).values(), default=0) <= 1
    assert max(Counter(p.event.dedupe_key() for p in ps.daily).values(), default=0) <= 1


def test_one_fixture_offering_many_selections_yields_one_prediction_per_family():
    e = ev(1)
    pool = [sc(e, "result3", "home", 0.72, 1.45, sources=3), sc(e, "total", "over", 0.70, 1.50, line=2.5, sources=3),
            sc(e, "btts", "yes", 0.68, 1.55, sources=3), sc(e, "dc", "home_draw", 0.75, 1.35, sources=3)]

    ps = _select(pool, cfg(), dt(), raw=False)

    # a lone double-chance line cannot be shown to cover the two most likely outcomes (no complete 1X2): omitted
    assert sorted(s.line.market for s in ps.all()) == ["btts", "result3", "total"]
    assert len(ps.top) <= 1 and len(ps.daily) <= 1
