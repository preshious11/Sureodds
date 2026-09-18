"""Picker rules: no padding, no top/daily overlap, no single-outcome bias."""
from __future__ import annotations

import math
from collections import Counter
from datetime import timedelta

import pytest

from predbot.config import Config
from predbot.markets.catalog import contradicts, is_equivalent, selection_family
from predbot.model.ensemble import Scored
from predbot.providers.base import Event, MarketLine
from predbot.selection.picker import select

from tests.conftest import dt


def ev(i, home=None, away=None, sport="football", start=None, league=None):
    # Each event gets its own league unless a test asks for a shared one, so
    # the per-league quota only bites in the tests written to exercise it.
    return Event(provider="test", provider_event_id=str(i), sport=sport,
                 league=league or f"L{i}", home=home or f"Home{i}", away=away or f"Away{i}",
                 start=start or dt(hh=22), sources=["a", "b"])


def sc(event, market, selection, p_model, price, *, line=None, sources=2,
       ev_val=0.06, agreement=1.0, liquidity=1.0):
    ml = MarketLine(market, selection, price, line=line, n_books=sources)
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


def test_no_overlap_between_top_and_daily():
    now = dt()
    pool = []
    for i in range(25):
        e = ev(i)
        # strong home pick (top candidate) and the same outcome again (should dedupe)
        pool.append(sc(e, "result3", "home", 0.70, 1.75))
        pool.append(sc(e, "result2", "home", 0.70, 1.70))   # equivalent-ish outcome
        pool.append(sc(e, "total", "over", 0.66, 1.8, line=2.5))
    ps = select(pool, cfg(), now)
    for t in ps.top:
        for d in ps.daily:
            if t.event.dedupe_key() == d.event.dedupe_key():
                assert not is_equivalent(t.line, d.line)
                assert not contradicts(t.line, d.line)


def test_same_fixture_same_outcome_from_different_providers_never_leaks_into_daily():
    """A Top pick's fixture/outcome must not reappear in Daily even when it
    arrives as a SEPARATE, unmerged Event object whose exact-match dedupe_key()
    differs - e.g. the Odds API priority sub-chain reported a kickoff time 90
    minutes off from what sharpapi reported for the very same match, which
    puts them in different dedupe_key() hour buckets even though they are
    obviously the same fixture."""
    now = dt()
    top_event = Event(provider="oddsapi", provider_event_id="oa1", sport="football",
                      league="L", home="Arsenal", away="Chelsea", start=dt(hh=21), sources=["oddsapi"])
    other_provider_event = Event(provider="sharpapi", provider_event_id="sp9", sport="football",
                                 league="L", home="Arsenal", away="Chelsea", start=dt(hh=22, mm=30),
                                 sources=["sharpapi"])
    assert top_event.dedupe_key() != other_provider_event.dedupe_key()

    top_candidate = sc(top_event, "result3", "home", 0.75, 1.60, ev_val=0.10)
    # Top is a 2-leg minimum accumulator: top_candidate needs a partner leg
    # under the 2.50 combined-odds ceiling (1.60 * 1.36 = 2.18) to be
    # selectable at all - a single leg can no longer win on its own.
    partner_event = Event(provider="oddsapi", provider_event_id="oa2", sport="football",
                          league="L", home="Porto", away="Braga", start=dt(hh=21), sources=["oddsapi"])
    partner_candidate = sc(partner_event, "result3", "home", 0.70, 1.36, ev_val=0.08)
    # Below top_min_odds (1.35 default) so it can never combinatorially join
    # Top itself - the only way it could reach the ticket is via Daily, which
    # is exactly the leak this test guards against.
    duplicate_candidate = sc(other_provider_event, "result3", "home", 0.70, 1.32, ev_val=0.08)
    # Distinct, dissimilar team names so the fuzzy same_team() match used by
    # _same_fixture() can't confuse one filler fixture for another. Priced
    # above the 2.50 ceiling in any pairing so they can't outscore the
    # intended top_candidate/partner_candidate combo.
    filler_names = ["Bayern", "Feyenoord", "Sporting", "Galatasaray", "Benfica",
                    "Napoli", "Sevilla", "Ajax", "Lazio", "Monaco"]
    filler = [sc(ev(10 + i, home=name, away=f"Opponent{i}"), "result3", "home", 0.62, 1.80)
             for i, name in enumerate(filler_names)]

    ps = select([top_candidate, partner_candidate, duplicate_candidate] + filler, cfg(), now)

    assert top_candidate in ps.top
    assert duplicate_candidate not in ps.daily
    assert ps.diagnostics["daily_pool_rejections"].get("DUPLICATE_OF_TOP") == 1


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


def test_single_source_never_top():
    now = dt()
    pool = [sc(ev(i), "result3", "home", 0.72, 1.8, sources=1, agreement=0.0) for i in range(10)]
    ps = select(pool, cfg(), now)
    assert ps.top == []   # top requires >= 2 agreeing sources


def test_family_cap_prevents_all_same_outcome():
    now = dt()
    # 40 strong home picks across distinct events; family cap must stop 20 homes
    pool = [sc(ev(i), "result3", "home", 0.66, 1.7) for i in range(40)]
    ps = select(pool, cfg(), now)
    homes = sum(1 for s in ps.daily if selection_family(s.line) == "home")
    assert homes < len(ps.daily) or len(ps.daily) < cfg().daily_target
    # with only one family available it still won't exceed the family cap share badly
    assert homes <= cfg().daily_target


def test_max_two_picks_per_event():
    now = dt()
    e = ev(1)
    pool = [
        sc(e, "result3", "home", 0.66, 1.7),
        sc(e, "total", "over", 0.64, 1.8, line=2.5),
        sc(e, "btts", "yes", 0.63, 1.9),
        sc(e, "dc", "home_draw", 0.75, 1.35),
    ]
    ps = select(pool, cfg(), now)
    count = sum(1 for s in ps.all() if s.event.dedupe_key() == e.dedupe_key())
    assert count <= cfg().max_picks_per_event


def test_min_lead_time_excludes_imminent():
    now = dt(hh=21, mm=50)
    soon = ev(1, start=dt(hh=22, mm=0))   # 10 min away, below 30 min lead
    ps = select([sc(soon, "result3", "home", 0.7, 1.8)], cfg(), now)
    assert ps.all() == []


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
    pool = [sc(ev(i), "result3", "home", 0.62, 1.45) for i in range(30)]
    ps = select(pool, cfg(), now)
    c = cfg()
    ticket = math.prod(s.price for s in ps.all())

    assert c.daily_odds_min <= ticket <= c.daily_odds_max   # 10 .. 20
    assert len(ps.all()) < 30                               # stops long before the pool runs out


def test_pick_count_never_passes_the_cap():
    now = dt()
    c = Config(daily_odds_min=900.0, daily_odds_max=1000.0)   # window far out of reach
    pool = [sc(ev(i), "result3", "home", 0.62, 1.45) for i in range(40)]
    ps = select(pool, c, now)

    assert len(ps.all()) <= c.daily_target


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
    # p_model 0.55 is below top_min_prob (0.62) but above daily_min_prob (0.30):
    # these candidates compete for Daily only, so the daily diversity quotas
    # are exercised in isolation. 1.30 keeps the odds window happy.
    s = sc(ev(i, sport=sport, league=league), market, sel, 0.55, 1.30, line=line)
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
def _corners_stat(i, stat, market, sel, line):
    s = sc(ev(i), market, sel, 0.66, 1.45, line=line)
    s.line.stat = stat
    return s


def _other_stat(i):
    market, sel, stat, line = _MIXED[i % len(_MIXED)]
    s = sc(ev(i), market, sel, 0.66, 1.30, line=line)
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


def test_trace_records_the_rule_that_stopped_each_candidate():
    now = dt()
    pool = [sc(ev(i), "result3", "home", 0.70 if i < 4 else 0.20, 1.45) for i in range(8)]
    trace: dict = {}
    c = cfg()
    ps = select(pool, c, now, trace=trace)
    assert trace
    # the weak candidates are recorded as failing the probability floor
    weak = [s for s in pool if s.p_model < 0.30]
    assert all(id(s) in trace for s in weak)
    assert all("p_model" in trace[id(s)] for s in weak)
    # daily picks are never marked as blocked; a Top leg legitimately carries
    # the "already a Top leg" note, and a candidate admitted via the relaxed
    # pass keeps its strict-pass note but setdefault keeps only the first one.
    daily_ids = {id(s) for s in ps.daily}
    for chosen in ps.all():
        if id(chosen) in daily_ids:
            continue
        assert id(chosen) not in trace or trace[id(chosen)] == "daily: already a Top leg"


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
    assert corners / len(ps.daily) <= c.daily_max_stat_share + 1e-9


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
    s = sc(ev(1, sport="basketball"), "result3", "home", 0.50, 1.87, sources=1,
           ev_val=-0.065, agreement=0.5)
    s.sources = {"market": 0.50, "score_model": 0.50}
    ps = select([s], cfg(), now)
    assert ps.daily


def test_independent_signal_candidates_have_a_disagreement_floor_not_an_edge_requirement():
    """With an independent signal (apif/elo), a candidate is rejected only when
    that signal says the market is MATERIALLY overpricing it (ev < -0.05);
    mildly negative EV no longer disqualifies."""
    now = dt()
    mildly_negative = sc(ev(1), "result3", "home", 0.70, 1.50, sources=1, ev_val=-0.03)
    mildly_negative.sources = {"market": 0.68, "apif": 0.72}
    strongly_overpriced = sc(ev(2), "result3", "home", 0.70, 1.50, sources=1, ev_val=-0.10)
    strongly_overpriced.sources = {"market": 0.68, "apif": 0.72}
    ps = select([mildly_negative, strongly_overpriced], cfg(), now)
    assert mildly_negative.event.dedupe_key() in {s.event.dedupe_key() for s in ps.all()}
    assert strongly_overpriced.event.dedupe_key() not in {s.event.dedupe_key() for s in ps.all()}
    assert ps.diagnostics["daily_pool_rejections"].get("BELOW_THRESHOLD", 0) == 1
