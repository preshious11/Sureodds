"""Picker rules: no padding, no top/daily overlap, no single-outcome bias."""
from __future__ import annotations

from datetime import timedelta

from predbot.config import Config
from predbot.markets.catalog import contradicts, is_equivalent, selection_family
from predbot.model.ensemble import Scored
from predbot.providers.base import Event, MarketLine
from predbot.selection.picker import select

from tests.conftest import dt


def ev(i, home=None, away=None, sport="football", start=None):
    return Event(provider="test", provider_event_id=str(i), sport=sport,
                 league="L", home=home or f"Home{i}", away=away or f"Away{i}",
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


def test_top_picks_meet_quality_bar():
    now = dt()
    pool = [sc(ev(i), "result3", "home", 0.70, 1.8, sources=3) for i in range(20)]
    ps = select(pool, cfg(), now)
    c = cfg()
    for t in ps.top:
        assert t.p_model >= c.top_min_prob
        assert t.ev >= c.top_min_ev
        assert len(t.sources) >= 2
    assert len(ps.top) <= c.top_target


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
