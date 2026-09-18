"""Regression tests: explicit outcomes, combined-odds correctness, canonical summary.

Covers the three recurring bugs:
  1. prediction cards showing fragmented/raw outcomes ("Batting_Hits",
     "Hits Over/Under", a bare "Over") instead of the actual recommended result;
  2. combined odds that could disagree with the displayed selections;
  3. summary cards that could fall back to old copy.

Every test renders from cached/fixture data: ZERO provider calls.
"""
from __future__ import annotations

import math
from types import SimpleNamespace

import pytest

from predbot.bot import pagination
from predbot.bot.broadcast import picks_text
from predbot.bot.callbacks import handle_callback, handle_command
from predbot.bot.format import (DAILY_SECTION_TITLE, SECTION_COMBINED, SECTION_TOTAL,
                                SUMMARY_TITLE, TOP_SECTION_TITLE, combined_odds,
                                format_outcome, outcome_label, pick_line_from_record,
                                render_section, summary_card)
from predbot.cache.fetcher import UnifiedFetcher
from predbot.cache.models import DailySnapshot, PickRecord, VIEW_DAILY, VIEW_TOP
from predbot.config import Config
from predbot.markets.catalog import prop_outcome, readable_stat, selection_label
from predbot.providers.base import MarketLine
from predbot.storage.db import Database
from predbot.util.timeutil import to_local, utcnow
from tests.test_subscribers_broadcast import FakeTG, _cb

WAT = "Africa/Lagos"


def rec(pick_id=1, tier="top", sport="football", home="Chelsea", away="Arsenal",
        label="", market="result3", selection="home", line=None, stat="points",
        entity="all", price=1.85, prob=0.7, kickoff="2030-06-01T18:00:00Z"):
    return PickRecord(pick_id=pick_id, tier=tier, rank=pick_id, sport=sport,
                      league="Premier League" if sport == "football" else "MLB",
                      home=home, away=away, kickoff_utc=kickoff, label=label,
                      market=market, selection=selection, line=line, stat=stat,
                      entity=entity, price=price, prob=prob, ev=0.05,
                      pick_date="2030-06-01")


# ============================================================ 1-6: outcome wording
def test_baseball_player_prop_names_player_side_line_and_stat():
    r = rec(sport="baseball", home="Phillies", away="Mets", label="Alec Bohm Hits Over/Under",
            market="prop", selection="over", line=1.5, stat="hits", entity="Alec Bohm")
    assert format_outcome(r) == "Alec Bohm Over 1.5 Hits"


def test_baseball_player_prop_total_bases():
    r = rec(sport="baseball", home="Phillies", away="Mets",
            label="Sam Antonacci Total Bases Over/Under",
            market="prop", selection="over", line=1.5, stat="Total_Bases",
            entity="Sam Antonacci")
    assert format_outcome(r) == "Sam Antonacci Over 1.5 Total Bases"


def test_baseball_team_prop_names_team_and_stat():
    r = rec(sport="baseball", home="Los Angeles Dodgers", away="Giants",
            label="Los Angeles Dodgers Over 8.5", market="prop", selection="over",
            line=8.5, stat="Batting_Hits", entity="home")
    assert format_outcome(r) == "Los Angeles Dodgers Over 8.5 Team Hits"


def test_baseball_match_total_uses_runs():
    r = rec(sport="baseball", home="Guardians", away="Tigers", label="Over 8.5",
            market="total", selection="over", line=8.5)
    assert format_outcome(r) == "Over 8.5 Total Runs"


def test_football_total_uses_goals():
    r = rec(market="total", selection="over", line=2.5)
    assert format_outcome(r) == "Over 2.5 Total Goals"


def test_football_corners_and_team_totals():
    r = rec(market="total", selection="over", line=8.5, stat="corners")
    assert format_outcome(r) == "Over 8.5 Total Corners"
    team = rec(market="total", selection="over", line=1.5, stat="points", entity="home")
    assert format_outcome(team) == "Chelsea Over 1.5 Goals"


def test_win_market_says_to_win():
    r = rec(home="Chelsea", away="Arsenal")
    assert format_outcome(r) == "Chelsea to Win"


def test_btts_keeps_explicit_side():
    r = rec(market="btts", selection="yes", stat="points")
    assert format_outcome(r) == "BTTS Yes"


def test_basketball_prop_and_team_total():
    p = rec(sport="basketball", home="Lakers", away="Celtics", market="prop",
            selection="over", line=25.5, stat="points", entity="LeBron James")
    assert format_outcome(p) == "LeBron James Over 25.5 Points"
    t = rec(sport="basketball", home="Lakers", away="Celtics", market="total",
            selection="over", line=110.5)
    assert format_outcome(t) == "Over 110.5 Total Points"


# ================================================================ 7: ambiguity
def test_ambiguous_props_are_never_rendered():
    # provider label present but NO line/stat/entity context -> '' (skipped)
    r = rec(market="prop", selection="over", line=None, stat="", entity="", label="Over")
    assert format_outcome(r) == ""
    r2 = rec(market="prop", selection="over", line=None, stat="hits",
             entity="Alec Bohm", label="Over")
    assert format_outcome(r2) == ""            # over WHAT line? never guessed


def test_ambiguous_labels_fall_through_to_canonical_fields():
    r = rec(label="Over 1.5", market="total", selection="over", line=1.5)
    assert format_outcome(r) == "Over 1.5 Total Goals"   # unit restored from canonical fields
    bad = rec(label="Over/Under", market="prop", selection="", line=None, stat="",
              entity="")
    assert format_outcome(bad) == ""


def test_outcome_label_never_returns_a_bare_fragment():
    for label in ("Over", "Under", "Over/Under", "Win", "Yes", "No", "", "result3:home"):
        assert outcome_label(label) in ("", "Home to Win")   # never the raw fragment


# ================================================== 8/9: card structure, no raw ids
def _summary_snapshot():
    top = [rec(pick_id=1, tier="top", label="", market="prop", selection="over",
               line=8.5, stat="Batting_Hits", entity="home",
               home="Los Angeles Dodgers", away="Giants", sport="baseball", price=2.67),
           rec(pick_id=2, tier="top", label="", market="prop", selection="over",
               line=1.5, stat="hits", entity="Sam Antonacci",
               home="Cardinals", away="Cubs", sport="baseball", price=1.87)]
    daily = [rec(pick_id=3, tier="daily", label="", market="total", selection="over",
                 line=1.5, price=1.86),
             rec(pick_id=4, tier="daily", label="", market="btts", selection="yes",
                 stat="points", price=1.85)]
    return DailySnapshot(date="2030-06-01", top_picks=top, daily_picks=daily)


def test_every_card_has_league_match_outcome_confidence_time_and_odds():
    snap = _summary_snapshot()
    for p in snap.view_items(VIEW_DAILY):
        block = pagination.render_pick(p, WAT).splitlines()
        assert len(block) >= 6                               # sport, league, match, market, prediction, odds, confidence
        assert block[0] in ("Football", "Baseball")          # sport
        assert block[1]                                      # league
        assert " vs " in block[2]                            # match
        assert block[3] and " " in block[3]                  # market or prediction
        # Find odds line (should be present)
        odds_line = [ln for ln in block if ln.startswith("Odds: ")]
        assert odds_line                                     # individual odds must exist
        # Find confidence line (should be present)
        confidence_line = [ln for ln in block if ln.startswith("Confidence level: <b>")]
        assert confidence_line                               # confidence + time must exist


def test_no_raw_identifiers_anywhere_in_user_copy():
    snap = _summary_snapshot()
    card = picks_text(snap, WAT)
    views = [pagination.render_page(snap, v, 0, 20, WAT)[0] for v in (VIEW_TOP, VIEW_DAILY)]
    for text in [card, *views]:
        for bad in ("Batting_Hits", "batting_hits", "result3", "player_hits_over_under",
                    "team_total_batting_hits", "total_bases_over_under", "Total_Bases",
                    "Over/Under", "_"):
            assert bad not in text



# ==================================================== 10-13: combined odds maths
def test_combined_odds_is_the_exact_product_of_the_displayed_picks():
    prices = [2.67, 1.87, 1.86, 1.85]
    total = combined_odds(prices)
    assert total == pytest.approx(math.prod(prices), rel=1e-12)
    assert total == pytest.approx(17.1805, rel=1e-4)
    assert total != sum(prices)                       # multiplied, never added


def test_section_totals_use_only_that_sections_picks():
    snap = _summary_snapshot()
    card = picks_text(snap, WAT)
    top_prod = math.prod(p.price for p in snap.top_picks)
    daily_prod = math.prod(p.price for p in snap.daily_picks)
    assert f"{TOP_SECTION_TITLE}\n{SECTION_COMBINED} {top_prod:.2f}\n{SECTION_TOTAL} 2" in card
    assert f"{DAILY_SECTION_TITLE}\n{SECTION_COMBINED} {daily_prod:.2f}\n{SECTION_TOTAL} 2" in card


def test_render_section_totals_match_rendered_cards():
    snap = _summary_snapshot()
    top_lines = [pick_line_from_record(p, WAT) for p in snap.top_picks]
    rendered = render_section(TOP_SECTION_TITLE, top_lines)
    odds_lines = [ln for ln in rendered if ln.startswith(SECTION_COMBINED)]
    cards = [ln for ln in rendered if ln.startswith("Odds: ")]
    assert len(cards) == 2                            # Total Picks == number of cards
    expected = f"{SECTION_COMBINED} {combined_odds(p.price for p in top_lines):.2f}"
    assert odds_lines == [expected]


def test_view_combined_odds_come_from_the_same_list_as_the_cards():
    snap = _summary_snapshot()
    for view, items in ((VIEW_TOP, snap.top_picks),
                        (VIEW_DAILY, snap.top_picks + snap.daily_picks)):
        text, _ = pagination.render_page(snap, view, 0, 20, WAT)
        expected = combined_odds(p.price for p in items if format_outcome(p))
        assert f"Combined Odds: {expected:.2f}" in text


# ================================================ 14/15: command == button parity
def test_top_and_daily_command_and_button_render_identically(db):
    snap = _summary_snapshot()
    service = SimpleNamespace(get_daily_snapshot=lambda: snap)
    for view in (VIEW_TOP, VIEW_DAILY):
        tg = FakeTG()
        handle_command('/' + view, "42", service=service, tg=tg,
                       cfg=Config(timezone="UTC"), db=db)
        handle_callback(_cb(f"pg|{view}|0"), service=service, tg=tg,
                        cfg=Config(timezone="UTC"), db=db)
        assert tg.sends[0]["text"] == tg.edits[0]["text"]
        assert tg.sends[0]["reply_markup"] == tg.edits[0]["reply_markup"]


# ================================================ 16/17: canonical summary copy
def test_all_summary_paths_share_one_canonical_copy():
    snap = _summary_snapshot()
    broadcast = picks_text(snap, WAT)
    # the broadcast card IS the canonical summary, built from the same pick cards
    top = [pick_line_from_record(p, WAT) for p in snap.top_picks]
    daily = [pick_line_from_record(p, WAT) for p in snap.daily_picks]
    assert broadcast == summary_card(top, daily)
    assert SUMMARY_TITLE in broadcast
    # /yesterday and the recap job share the recap formatter (see test_recap_format)


@pytest.mark.parametrize("stale", ["predictions——", "Total odds", "Combined odds:",
                                   "Over/Under", "Batting_Hits"])
def test_old_and_stale_copy_cannot_appear_from_any_active_formatter(db, stale):
    snap = _summary_snapshot()
    service = SimpleNamespace(get_daily_snapshot=lambda: snap)
    tg = FakeTG()
    handle_command('/daily', "42", service=service, tg=tg, cfg=Config(timezone="UTC"), db=db)
    handle_callback(_cb("pg|top|0"), service=service, tg=tg,
                    cfg=Config(timezone="UTC"), db=db)
    for text in (picks_text(snap, WAT), tg.sends[0]["text"], tg.edits[0]["text"]):
        assert stale not in text



# ================================== 18/19: cache roundtrip and restart, zero APIs
def _seed(db_path, snapshot):
    db = Database(db_path)
    cfg = Config(timezone=WAT)
    service = UnifiedFetcher(None, db, cfg)      # chain=None: any API call would explode
    day = to_local(utcnow(), cfg.timezone).date().isoformat()
    snapshot.date = day
    service._store(snapshot, utcnow())
    db.close()
    return day


def test_cache_roundtrip_renders_the_same_corrected_copy(tmp_path):
    snap = _summary_snapshot()
    day = _seed(str(tmp_path / "bot.db"), snap)

    db = Database(str(tmp_path / "bot.db"))
    service = UnifiedFetcher(None, db, Config(timezone=WAT))
    restored = service.get_daily_snapshot()
    assert restored.date == day and restored.total_picks() == 4
    assert picks_text(restored, WAT) == picks_text(snap, WAT)   # data, not stale copy
    db.close()


def test_restart_from_existing_sqlite_needs_no_provider_calls(tmp_path):
    snap = _summary_snapshot()
    path = str(tmp_path / "bot.db")
    _seed(path, snap)

    db = Database(path)                          # fresh process, same file
    service = UnifiedFetcher(None, db, Config(timezone=WAT))
    second = service.get_daily_snapshot()
    assert picks_text(second, WAT) == picks_text(snap, WAT)
    text, _ = pagination.render_page(second, VIEW_DAILY, 0, 20, WAT)
    assert "Alec Bohm" not in text and "Over 1.5 Total Goals" in text
    db.close()


# ===================================================== 20: sports format coverage
@pytest.mark.parametrize("sport,market,stat,entity,selection,line,expected", [
    ("football", "total", "corners", "all", "under", 9.5, "Under 9.5 Total Corners"),
    ("football", "total", "cards", "all", "over", 3.5, "Over 3.5 Total Cards"),
    ("football", "total", "shots", "away", "over", 4.5, "Arsenal Over 4.5 Shots"),
    ("baseball", "total", "points", "all", "under", 7.5, "Under 7.5 Total Runs"),
    ("baseball", "prop", "rbis", "Bryce Harper", "over", 0.5, "Bryce Harper Over 0.5 RBIs"),
    ("baseball", "prop", "homeRuns", "Bryce Harper", "under", 1.5,
     "Bryce Harper Under 1.5 Home Runs"),
    ("basketball", "prop", "rebounds", "Joel Embiid", "over", 10.5,
     "Joel Embiid Over 10.5 Rebounds"),
    ("basketball", "prop", "three_pointers", "Joel Embiid", "over", 2.5,
     "Joel Embiid Over 2.5 Three-Pointers"),
])
def test_sport_specific_units(sport, market, stat, entity, selection, line, expected):
    r = rec(sport=sport, market=market, stat=stat, entity=entity, selection=selection,
            line=line, label="")
    assert format_outcome(r) == expected


# ------------------------------------------------------ catalog-level guarantees
def test_readable_stat_never_leaks_raw_identifiers():
    for raw in ("Batting_Hits", "totalBases", "player_hits_over_under", "threePointers"):
        out = readable_stat(raw)
        assert "_" not in out


def test_prop_outcome_requires_full_context():
    ml = MarketLine("prop", "over", 1.9, stat="hits", entity="Alec Bohm", line=None)
    assert prop_outcome(ml) == ""                 # no line -> never "Alec Bohm Over Hits"
    ml = MarketLine("prop", "over", 1.9, stat="hits", entity="", line=1.5)
    assert prop_outcome(ml) == ""                 # no entity -> whose hits?
    ml = MarketLine("prop", "over", 1.9, stat="hits", entity="Alec Bohm", line=1.5)
    assert prop_outcome(ml) == "Alec Bohm Over 1.5 Hits"


def test_selection_label_stores_explicit_outcomes_too():
    ml = MarketLine("prop", "over", 1.9, stat="hits", entity="Alec Bohm", line=1.5,
                    label="Hits Over/Under")
    assert selection_label(ml, "Phillies", "Mets", "baseball") == "Alec Bohm Over 1.5 Hits"

