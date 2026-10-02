"""Tests for the six targeted pipeline improvements."""
from __future__ import annotations

import math
from datetime import timedelta

import pytest

from predbot.config import Config
from predbot.model.ensemble import Scored
from predbot.providers.base import Event, MarketLine
from predbot.selection.picker import select

from tests.conftest import dt


# ── helpers (same pattern as test_picker.py) ────────────────────────────────

def ev(i, sport="football", league=None, home=None, away=None):
    # One real-world match per index: a distinct day per kickoff keeps the
    # picker's fuzzy same-fixture linker (same sport + kickoff within 3h +
    # similar names) from treating two different synthetic events - e.g.
    # "Home10" and "Home11", similar enough on their own - as one match.
    return Event(provider="test", provider_event_id=str(i), sport=sport,
                 league=league or f"L{i}", home=home or f"Home{i}",
                 away=away or f"Away{i}", start=dt(hh=20) + timedelta(days=i), sources=["a", "b"])


def sc(event, market="result3", selection="home", p_model=0.72, price=1.55, *,
       line=None, n_books=3, period="full", agreement=1.0, ev_val=0.05):
    ml = MarketLine(market, selection, price, line=line, n_books=n_books,
                    period=period, stat="points", entity="all")
    return Scored(event=event, line=ml, p_market=p_model - 0.02,
                  p_model=p_model, sources={"market": p_model, "elo": p_model},
                  agreement=agreement, ev=ev_val, edge=0.01,
                  label=f"{market}:{selection}", liquidity=1.0)


def cfg(**overrides) -> Config:
    c = Config()
    for k, v in overrides.items():
        object.__setattr__(c, k, v)
    return c


# ── Fix 2: period cap ────────────────────────────────────────────────────────

def test_period_cap_limits_1h_legs():
    """With DAILY_MAX_PERIOD_SHARE=0.15, period-tagged (1H) legs must stay
    within their share of however big a slate the pool actually supports."""
    now = dt()
    # Five distinct real markets/selections (own family + own market-share
    # bucket each) so the market/family diversity caps don't starve the
    # ticket before the period cap gets a chance to bind; daily_odds_min is
    # dropped to a floor every ticket clears trivially, so the strict pass
    # never has a reason to relax the very share caps under test.
    combos = [("result3", "home"), ("result2", "away"), ("dc", "draw"),
              ("total", "over"), ("btts", "yes")]
    pool = []
    for i in range(20):
        m, s = combos[i % len(combos)]
        pool.append(sc(ev(i), market=m, selection=s, period="1h", n_books=5, p_model=0.75, price=1.60))
    for i in range(20, 40):
        m, s = combos[i % len(combos)]
        pool.append(sc(ev(i), market=m, selection=s, period="full", n_books=5, p_model=0.72, price=1.60))
    c = cfg(daily_max_period_share=0.15, daily_min_prob=0.65)
    ps = select(pool, c, now)
    period_picks = [p for p in ps.daily if p.line.period not in ("full", "reg", "", None)]
    n = len(ps.daily)
    assert n >= 4, f"Slate too small to meaningfully exercise the period cap (n={n})"
    max_allowed = max(1, math.ceil(0.15 * n))
    assert len(period_picks) <= max_allowed, (
        f"Expected <= {max_allowed} period picks in {n}-pick slate, got {len(period_picks)}"
    )


def test_period_cap_config_field_exists():
    c = Config()
    assert hasattr(c, "daily_max_period_share")
    assert 0.0 < c.daily_max_period_share <= 1.0


# ── Fix 3: top min books ─────────────────────────────────────────────────────

def test_top_single_book_leg_excluded():
    """A leg with n_books=1 must not enter the Top ticket when TOP_MIN_BOOKS=2."""
    now = dt()
    # One event with only 1 book — high enough probability to otherwise qualify
    lone = sc(ev(0), p_model=0.75, price=1.45, n_books=1, agreement=0.9)
    # Pad daily pool
    daily_pad = [sc(ev(i), p_model=0.72, price=1.35, n_books=4) for i in range(1, 20)]
    c = cfg(top_min_prob=0.62, top_min_books=2, top_min_agreement=0.50)
    ps = select([lone] + daily_pad, c, now)
    assert all(s.line.n_books >= 2 for s in ps.top), (
        f"Top pick with n_books < 2 slipped through: {[(s.label, s.line.n_books) for s in ps.top]}"
    )


def test_top_min_books_config_field_exists():
    c = Config()
    assert hasattr(c, "top_min_books")
    assert c.top_min_books >= 1


# ── Fix 4: daily_min_prob respected ──────────────────────────────────────────

def test_daily_min_prob_gate_respected():
    """Picks below daily_min_prob must not appear in the daily list."""
    now = dt()
    # Raises the bar to 0.70
    c = cfg(daily_min_prob=0.70)
    below = [sc(ev(i), p_model=0.65, price=1.60) for i in range(20)]
    above = [sc(ev(j), p_model=0.75, price=1.35, n_books=4) for j in range(20, 40)]
    ps = select(below + above, c, now)
    for pick in ps.daily:
        assert pick.p_model >= 0.70, (
            f"Daily pick with p_model={pick.p_model:.3f} below daily_min_prob=0.70"
        )


# ── Fix 5: baseball period scale ─────────────────────────────────────────────

def test_baseball_period_scale_1h_is_full():
    """For baseball a '1h' period should use scale 1.0, not 0.44."""
    from predbot.model.poisson import _period_scale
    assert _period_scale("1h", "baseball") == 1.0, (
        "Baseball '1h' should scale to 1.0 (no halves in baseball)"
    )


def test_football_period_scale_1h_unchanged():
    """Football '1h' must still use 0.44."""
    from predbot.model.poisson import _period_scale
    assert _period_scale("1h", "football") == 0.44


def test_period_scale_function_exported():
    """_period_scale must be importable from predbot.model.poisson."""
    from predbot.model.poisson import _period_scale  # noqa: F401


# ── Fix 6: Elo harvest helper is importable and callable ─────────────────────

def test_elo_harvest_function_exists():
    """_harvest_elo_from_espn must exist and return 0 when no ESPN provider is present."""
    from predbot.jobs.settle import _harvest_elo_from_espn
    from predbot.storage.db import Database
    from predbot.model.elo import Elo

    # Minimal stub resolver with no ESPN provider
    class FakeResolver:
        providers = {}

    db = Database(":memory:")
    elo = Elo(db)
    result = _harvest_elo_from_espn(FakeResolver(), db, elo)
    assert result == 0


# ── Fix 1: .gitignore replay exception ───────────────────────────────────────

def test_gitignore_has_replay_db_exception():
    """.gitignore must have the replay.db exception so the file can be committed."""
    import os
    gitignore_path = os.path.join(os.path.dirname(__file__), "..", ".gitignore")
    with open(gitignore_path) as f:
        content = f.read()
    assert "!tests/fixtures/replay/replay.db" in content, (
        ".gitignore is still blocking tests/fixtures/replay/replay.db"
    )
