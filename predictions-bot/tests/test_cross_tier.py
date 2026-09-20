"""The cross-tier rule (owner): the same match may appear in the Top ticket and in the Daily list, but never
with the same tip - nor a tip nested in it. Under CROSS_TIER_SAME_FIXTURE=forbid the match is off-limits."""
from __future__ import annotations

import logging

import pytest

from predbot.config import Config
from predbot.providers.base import Event, MarketLine
from predbot.selection import cross_tier, picker
from predbot.selection.picker import select
from tests.conftest import dt
from tests.test_picker import sc


def _arsenal(provider="oddsapi", swapped=False, pid="1"):
    home, away = ("Arsenal", "Chelsea") if not swapped else ("Chelsea FC", "Arsenal FC")
    return Event(provider=provider, provider_event_id=pid, sport="football", league="Premier League", home=home,
                 away=away, start=dt(hh=21), sources=["a", "b"])


def _partner():
    return Event(provider="oddsapi", provider_event_id="9", sport="football", league="Primeira", home="Porto",
                 away="Braga", start=dt(hh=21), sources=["a", "b"])


def _top_legs():
    """Arsenal to win (1.40) + Porto to win (1.45) = 2.03: the Top ticket."""
    return [sc(_arsenal(), "result3", "home", 0.75, 1.40, sources=3), sc(_partner(), "result3", "home", 0.72, 1.45, sources=3)]


def _candidates(event, swapped):
    """The Daily candidates on the Top's fixture, in the orientation ``event`` lists the sides. Each is
    (name, Scored, expected rejection or None)."""
    home, away = ("away", "home") if swapped else ("home", "away")
    arsenal_or_draw = "away_draw" if swapped else "home_draw"
    return [
        ("Arsenal to win", sc(event, "result3", home, 0.73, 1.38, sources=3), "CROSS_TIER_DUPLICATE"),
        ("Arsenal or draw", sc(event, "dc", arsenal_or_draw, 0.85, 1.25, sources=3), "CROSS_TIER_NESTED"),
        ("Arsenal DNB", sc(event, "dnb", home, 0.78, 1.30, sources=3), "CROSS_TIER_NESTED"),
        ("Chelsea to win", sc(event, "result3", away, 0.62, 1.60, sources=3), "CONFLICT"),          # contradicts
        ("Over 2.5 goals", sc(event, "total", "over", 0.66, 1.55, line=2.5, sources=3), None),
        ("BTTS Yes", sc(event, "btts", "yes", 0.64, 1.60, sources=3), None),
    ]


def _run(candidate, mode="different_outcome", **kw):
    pool = _top_legs() + [candidate]
    return select(pool, Config(cross_tier_same_fixture=mode, **kw), dt())


@pytest.mark.parametrize("swapped", [False, True], ids=["same-order", "home-away-swapped-and-respelled"])
def test_each_daily_candidate_on_a_top_fixture_is_judged_against_the_top_leg(swapped):
    for name, cand, expected in _candidates(_arsenal("sportsgameodds", swapped, pid="2"), swapped):
        ps = _run(cand)
        assert {t.line.selection for t in ps.top} == {"home"} and len(ps.top) == 2, name        # Top is unchanged
        rejections = ps.diagnostics["selection_rejections"]
        if expected is None:
            assert cand in ps.daily, name
            assert "cross_tier" not in cand.notes, name
            assert not rejections.get("CROSS_TIER_DUPLICATE") and not rejections.get("CROSS_TIER_NESTED"), name
        else:
            assert cand not in ps.daily, name
            if expected != "CONFLICT":
                assert rejections.get(expected) == 1, (name, rejections)
                assert cand.notes["cross_tier"].startswith(expected), name


@pytest.mark.parametrize("swapped", [False, True], ids=["same-order", "swapped"])
def test_under_forbid_every_candidate_on_a_top_fixture_is_rejected(swapped):
    for name, cand, _ in _candidates(_arsenal("sportsgameodds", swapped, pid="2"), swapped):
        ps = _run(cand, mode="forbid")
        assert cand not in ps.daily, name
        if "Chelsea" not in name:                                     # a contradiction is judged by the earlier rule too
            assert ps.diagnostics["selection_rejections"].get("CROSS_TIER_DUPLICATE") == 1, name


def test_the_same_match_appears_in_both_tiers_with_different_outcomes():
    over = sc(_arsenal("sportsgameodds", pid="2"), "total", "over", 0.66, 1.55, line=2.5, sources=3)
    ps = _run(over)
    fixtures = {(s.event.home, s.line.market, s.line.selection) for s in ps.all()}
    assert ("Arsenal", "result3", "home") in fixtures and ("Arsenal", "total", "over") in fixtures
    assert ps.top[0].event is not ps.daily[0].event or ps.top[0].line.market != ps.daily[0].line.market


def test_max_picks_per_event_counts_per_tier_unless_forbid():
    over = sc(_arsenal("sportsgameodds", pid="2"), "total", "over", 0.66, 1.55, line=2.5, sources=3)
    btts = sc(_arsenal("sportsgameodds", pid="2"), "btts", "yes", 0.64, 1.60, sources=3)
    ps = select(_top_legs() + [over, btts], Config(), dt())
    on_arsenal = [s for s in ps.all() if s.event.home == "Arsenal"]
    assert len(on_arsenal) == 2                                       # one Top leg + ONE Daily pick (cap 1 per tier)
    assert sum(1 for s in ps.daily if s.event.home == "Arsenal") == 1
    ps = select(_top_legs() + [over, btts], Config(cross_tier_same_fixture="forbid"), dt())
    assert len([s for s in ps.all() if s.event.home == "Arsenal"]) == 1


def test_nesting_in_totals_ignores_the_line_and_in_handicaps_ignores_the_line():
    top = MarketLine("total", "over", 1.9, line=1.5)
    assert cross_tier.relation(top, MarketLine("total", "over", 1.9, line=2.5)) == cross_tier.NESTED
    assert cross_tier.relation(top, MarketLine("total", "under", 1.9, line=2.5)) is None
    assert cross_tier.relation(top, MarketLine("total", "over", 1.9, line=1.5)) == cross_tier.DUPLICATE
    assert cross_tier.relation(top, MarketLine("total", "over", 1.9, line=0.5, entity="home")) is None
    h = MarketLine("handicap", "home", 1.9, line=-1.5)
    assert cross_tier.relation(h, MarketLine("handicap", "home", 1.9, line=0.5)) == cross_tier.NESTED
    assert cross_tier.relation(h, MarketLine("btts", "yes", 1.9)) is None


def test_mirror_swaps_sides_lines_and_entities():
    m = cross_tier.mirror(MarketLine("handicap", "home", 1.9, line=-1.5))
    assert (m.selection, m.line) == ("away", -1.5)          # the same team keeps the same line
    assert cross_tier.mirror(MarketLine("dc", "home_draw", 1.3)).selection == "away_draw"
    assert cross_tier.mirror(MarketLine("total", "over", 1.9, line=1.5, entity="home")).entity == "away"
    assert cross_tier.mirror(MarketLine("correct_score", "2-1", 9.0)).selection == "1-2"
    assert cross_tier.mirror(MarketLine("btts", "yes", 1.9)).selection == "yes"


# ------------------------------------------------------------------ hard invariant (selection layer, Phase 3 layer 1)
def test_the_invariant_removes_a_duplicate_the_selection_layer_missed_and_never_touches_the_top(monkeypatch, caplog):
    over = sc(_arsenal("sportsgameodds", pid="2"), "total", "over", 0.66, 1.55, line=2.5, sources=3)
    dup = sc(_arsenal("sportsgameodds", pid="3"), "dc", "home_draw", 0.90, 1.25, sources=3)      # nested in the Top leg
    other = sc(_partner_two(), "result3", "home", 0.70, 1.30, sources=3)
    pool = _top_legs() + [dup, over, other]
    clean = select(pool, Config(), dt())
    assert dup not in clean.daily
    monkeypatch.setattr(picker, "_cross_tier_verdict", lambda *a, **k: None)      # the pre-filter fails to see it
    with caplog.at_level(logging.WARNING):
        ps = select(pool, Config(), dt())
    assert over in ps.daily and dup not in ps.daily                                                    # the invariant caught it
    assert ps.diagnostics["selection_rejections"].get("CROSS_TIER_DUPLICATE_REMOVED", 0) >= 1
    assert any("cross-tier invariant" in r.message for r in caplog.records)
    assert [(s.event.provider_event_id, s.line.market, s.line.selection) for s in ps.top] == \
           [(s.event.provider_event_id, s.line.market, s.line.selection) for s in clean.top]
    assert cross_tier.violations(ps.top, ps.daily, picker.fixture_relation, "different_outcome") == []


def _partner_two():
    return Event(provider="oddsapi", provider_event_id="10", sport="football", league="Eredivisie", home="Ajax",
                 away="Feyenoord", start=dt(hh=21), sources=["a", "b"])


def test_config_default_and_bad_value(monkeypatch, caplog):
    monkeypatch.delenv("CROSS_TIER_SAME_FIXTURE", raising=False)
    assert Config().cross_tier_same_fixture == "different_outcome"
    monkeypatch.setenv("CROSS_TIER_SAME_FIXTURE", "FORBID")
    assert Config().cross_tier_same_fixture == "forbid"
    monkeypatch.setenv("CROSS_TIER_SAME_FIXTURE", "nonsense")
    with caplog.at_level(logging.WARNING):
        assert Config().cross_tier_same_fixture == "different_outcome"
    assert any("CROSS_TIER_SAME_FIXTURE" in r.message for r in caplog.records)


# ------------------------------------------------------------------ render (Phase 3 layer 3)
def test_the_rendered_card_never_repeats_a_top_block_in_daily():
    from predbot.bot.format import duplicate_pick_lines, picks_message
    over = sc(_arsenal("sportsgameodds", pid="2"), "total", "over", 0.66, 1.55, line=2.5, sources=3)
    ps = _run(over)
    text = picks_message(ps.top, ps.daily, dt(), "UTC")
    blocks = [b for b in text.split("\n\n") if "🎯" in b]
    assert len(blocks) == len(set(blocks)) == len(ps.all())
    assert duplicate_pick_lines([], []) == []


def test_the_card_warns_when_it_is_handed_the_same_pick_in_both_sections(caplog):
    from predbot.bot.format import PickLine, duplicate_pick_lines, summary_card
    top = [PickLine("Football", "Arsenal vs Chelsea", "Arsenal to Win", "PL", 0.7, "21:00", 1.4)]
    daily = [PickLine("Football", "Chelsea vs Arsenal", "Arsenal to Win", "PL", 0.7, "21:00", 1.4)]
    assert duplicate_pick_lines(top, daily)
    with caplog.at_level(logging.WARNING):
        summary_card(top, daily)
    assert any("same pick in Top and Daily" in r.message for r in caplog.records)
    assert not duplicate_pick_lines(top, [PickLine("Football", "Arsenal vs Chelsea", "Over 2.5 Goals", "PL", 0.6, "21:00", 1.6)])


# ------------------------------------------------------------------ database (Phase 3 layer 2)
def _row(**kw):
    row = {"pick_date": "2030-06-01", "tier": "top", "rank": 1, "provider": "oddsapi", "provider_event_id": "1",
           "sport": "baseball", "league": "MLB", "home": "TEX Rangers", "away": "BOS Red Sox",
           "start_utc": "2030-06-01T18:10:00Z", "market": "result2", "period": "full", "stat": "points",
           "entity": "all", "line": None, "selection": "home", "label": "TEX Rangers to Win", "price": 1.6,
           "prob": 0.7, "ev": 0.05, "p_market": 0.6, "sources": {}, "provider_odd_id": ""}
    row.update(kw)
    return row


def test_the_same_selection_on_the_same_match_cannot_be_stored_twice_whatever_the_tier(db):
    import sqlite3
    db.insert_pick(_row())
    with pytest.raises(sqlite3.IntegrityError):
        db.insert_pick(_row(tier="daily", provider="sportsgameodds", provider_event_id="77",
                            home="Texas Rangers", away="Boston Red Sox"))                  # other provider's spelling
    db.insert_pick(_row(tier="daily", market="total", selection="over", line=8.5, label="Over 8.5"))     # other selection: fine
    db.insert_pick(_row(tier="daily", pick_date="2030-06-02"))                                             # other date: fine


def test_result_markets_with_no_line_are_constrained_too(db):
    import sqlite3
    db.insert_pick(_row(line=None))
    with pytest.raises(sqlite3.IntegrityError):
        db.insert_pick(_row(tier="daily", line=None))


def test_migration_adds_and_backfills_fixture_key_and_survives_legacy_duplicates(tmp_path):
    import sqlite3
    from predbot.storage.db import Database
    path = str(tmp_path / "old.db")
    Database(path).close()
    raw = sqlite3.connect(path)
    raw.execute("DROP INDEX IF EXISTS uq_picks_fixture_selection")
    raw.execute("ALTER TABLE picks DROP COLUMN fixture_key")
    cols = ("pick_date,tier,rank,provider,provider_event_id,sport,league,home,away,start_utc,market,period,stat,entity,"
            "line,selection,label,price,prob,ev,created_at,status")
    for tier, pid in (("top", "1"), ("daily", "2"), ("daily", "3")):        # 2 and 3 are legacy duplicates of 1
        raw.execute(f"INSERT INTO picks({cols}) VALUES('2030-06-01',?,1,'p',?,'baseball','MLB','LA Dodgers','San Diego Padres',"
                    "'2030-06-01T02:00:00Z','result2','full','points','all',NULL,'home','x',1.5,0.7,0.05,'t','pending')", (tier, pid))
    raw.execute(f"INSERT INTO picks({cols}) VALUES('2030-06-01','daily',2,'p','9','football','L','A','B','garbage','result3','full',"
                "'points','all',NULL,'home','y',1.5,0.7,0.05,'t','pending')")
    raw.commit()
    raw.close()
    db = Database(path)
    keys = [r["fixture_key"] for r in db.query("SELECT fixture_key FROM picks ORDER BY id")]
    assert all(keys) and len(set(keys)) == 4                                   # every row keyed, legacy dups kept apart
    assert keys[0] == keys[1].split("#")[0] == keys[2].split("#")[0] and "#dup" in keys[1]
    assert db.query("SELECT COUNT(*) AS n FROM picks")[0]["n"] == 4            # nothing deleted
    Database(path).close()                                                     # idempotent


def test_store_picks_skips_a_cross_tier_duplicate_and_warns_instead_of_crashing(db, caplog):
    from predbot.jobs.daily_picks import store_picks
    from predbot.selection.picker import PickSet
    top = sc(_arsenal(), "result3", "home", 0.75, 1.40, sources=3)
    dup = sc(_arsenal("sportsgameodds", pid="2"), "result3", "home", 0.73, 1.38, sources=3)
    over = sc(_arsenal("sportsgameodds", pid="2"), "total", "over", 0.66, 1.55, line=2.5, sources=3)
    with caplog.at_level(logging.WARNING):
        store_picks(PickSet(top=[top], daily=[dup, over]), db, "2030-06-01")
    rows = db.query("SELECT tier, market FROM picks ORDER BY id")
    assert [(r["tier"], r["market"]) for r in rows] == [("top", "result3"), ("daily", "total")]
    assert any("not storing" in r.message for r in caplog.records)
