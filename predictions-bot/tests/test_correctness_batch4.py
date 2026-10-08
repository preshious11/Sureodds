"""Batch 4 - settlement, stats and data correctness.

Accuracy by collection membership (M6), regulation vs overtime grading (M7), unresolved vs void reporting (M8), the
Daily end-game search (M12), the Top combination search (M13), diversity never relaxed (M14), mirrored composite
selections (M17), quarter-line result totals, half-score names, Elo idempotency, strict team matching for results
and orientation, and the extended identity-repair report. Deterministic fixtures only; no network."""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from predbot.config import Config
from predbot.jobs import settle, stats
from predbot.jobs.live_results import orient_result, same_orientation
from predbot.markets.settlement import HALF_LOST, HALF_WON, LOST, PENDING, PUSH, WON, grade
from predbot.model.elo import Elo
from predbot.providers.base import MarketLine, MatchResult
from predbot.selection.daily import select_daily
from predbot.selection.top import select_top
from predbot.util.names import distinct_participants, same_team
from tests.test_all_predictions_architecture import NOW, promoted, row

START = datetime(2030, 6, 1, 18, 0, tzinfo=timezone.utc)


def ml(market, selection, line=None, period="full", entity="all", stat="points"):
    return MarketLine(market=market, selection=selection, price=1.9, period=period, entity=entity, stat=stat, line=line)


# ============================================================================== M7 regulation vs overtime
def ot_game(reg=(100, 100), full=(112, 105), overtime=True, with_reg=True):
    scores = {"full": full}
    if with_reg:
        scores["reg"] = reg
    return MatchResult("final", scores, overtime=overtime)


def test_overtime_changes_the_winner_only_for_the_full_game_market():
    game = ot_game()
    assert grade(ml("result2", "home"), game, "basketball") == WON              # full game includes OT
    assert grade(ml("result2", "home", period="reg"), game, "basketball") == PUSH    # regulation ended level
    assert grade(ml("result3", "draw", period="reg"), game, "basketball") == WON


def test_overtime_changes_the_total():
    game = ot_game()
    assert grade(ml("total", "under", 201.5, period="reg"), game, "basketball") == WON     # 200 in regulation
    assert grade(ml("total", "under", 201.5), game, "basketball") == LOST                  # 217 with overtime


def test_without_overtime_regulation_is_the_final_score():
    game = MatchResult("final", {"full": (101, 99)}, overtime=False)
    assert grade(ml("result2", "home", period="reg"), game, "basketball") == WON
    assert grade(ml("total", "over", 199.5, period="reg"), game, "basketball") == WON


def test_regulation_is_never_guessed_from_an_overtime_inclusive_score():
    game = MatchResult("final", {"full": (112, 105)})                       # source did not say whether OT happened
    assert grade(ml("result2", "home", period="reg"), game, "american_football") == PENDING
    assert grade(ml("total", "over", 200.5, period="reg"), game, "basketball") == PENDING
    assert grade(ml("result2", "home"), game, "basketball") == WON           # the full-game bet is unaffected


def test_full_game_from_regulation_only_when_regulation_was_decided():
    decided = MatchResult("final", {"reg": (98.0, 95.0)})
    level = MatchResult("final", {"reg": (100.0, 100.0)})
    assert grade(ml("result2", "home"), decided, "basketball") == WON        # no overtime is possible after a decision
    assert grade(ml("result2", "home"), level, "basketball") == PENDING      # overtime decided it: unknown


def test_football_regulation_still_means_the_ninety_minutes():
    game = MatchResult("final", {"full": (1.0, 1.0)})
    assert grade(ml("result3", "draw", period="reg"), game, "football") == WON


def test_espn_linescores_give_regulation_and_the_overtime_flag():
    from predbot.providers.espn import ESPNProvider
    nba = SimpleNamespace(sport="basketball", id="nba")
    r = MatchResult("final", {"full": (112.0, 105.0)})
    ESPNProvider._add_periods(r, nba, [25.0, 25, 25, 25, 12], [30.0, 20, 25, 25, 5], 112.0, 105.0)
    assert r.overtime is True and r.scores["reg"] == (100.0, 100.0)
    no_ot = MatchResult("final", {"full": (100.0, 95.0)})
    ESPNProvider._add_periods(no_ot, nba, [25.0, 25, 25, 25], [20.0, 25, 25, 25], 100.0, 95.0)
    assert no_ot.overtime is False and no_ot.scores["reg"] == (100.0, 95.0)
    bare = MatchResult("final", {"full": (100.0, 95.0)})
    ESPNProvider._add_periods(bare, nba, [], [], 100.0, 95.0)
    assert bare.overtime is None and "reg" not in bare.scores                # no linescores: regulation unknown


def test_euroleague_quarters_give_regulation():
    from predbot.providers.euroleague import EuroLeagueProvider
    p = EuroLeagueProvider.__new__(EuroLeagueProvider)
    side = lambda score, q: {"score": score, "partials": {f"partials{i + 1}": v for i, v in enumerate(q)}}
    r = p._to_result({"played": True}, side(90, [20, 20, 20, 20]), side(88, [20, 20, 20, 20]))
    assert r.overtime is True and r.scores["reg"] == (80.0, 80.0)
    assert grade(ml("result2", "home", period="reg"), r, "basketball") == PUSH


def test_sgo_reports_regulation_and_game_separately():
    from predbot.providers.sportsgameodds import SportsGameOddsProvider
    ev = {"sportID": "BASKETBALL", "status": {"completed": True},
          "results": {"reg": {"home": {"points": 100}, "away": {"points": 100}},
                      "game": {"home": {"points": 110}, "away": {"points": 104}}}}
    r = SportsGameOddsProvider._parse_result(ev)
    assert r.overtime is True and r.score("reg") == (100.0, 100.0) and r.score("full") == (110.0, 104.0)
    only_reg = {"sportID": "BASKETBALL", "status": {"completed": True},
                "results": {"reg": {"home": {"points": 100}, "away": {"points": 100}}}}
    assert SportsGameOddsProvider._parse_result(only_reg).score("full") is None       # was copied from reg


def test_the_overtime_flag_survives_storage(db):
    from predbot.jobs.live_results import _restore
    db.save_live_result("2030-06-01", "fk", ot_game(), "2030-06-01T22:00:00Z")
    restored = _restore(db.live_state("2030-06-01", "fk"))
    assert restored.overtime is True and restored.score("reg") == (100.0, 100.0)


# ============================================================================== quarter-line result totals
@pytest.mark.parametrize("score,selection,line,expected", [
    ((2, 0), "home_over", 2.25, HALF_LOST),     # 2 goals: push on 2.0, lost on 2.5
    ((3, 0), "home_over", 2.25, WON),
    ((2, 1), "home_over", 2.75, HALF_WON),      # 3 goals: won on 2.5, push on 3.0
    ((2, 1), "home_under", 2.75, HALF_LOST),
    ((2, 0), "home_under", 2.25, HALF_WON),
    ((1, 0), "home_under", 2.25, WON),
    ((1, 2), "home_over", 2.75, LOST),          # result wrong: lost whatever the total
    ((2, 0), "home_over", 2.0, PUSH),
])
def test_result_total_grades_every_quarter_line_outcome(score, selection, line, expected):
    res = MatchResult("final", {"full": score})
    assert grade(ml("result_total", selection, line), res, "football") == expected


@pytest.mark.parametrize("total,side,line,expected", [
    (2, "over", 2.25, HALF_LOST), (3, "over", 2.25, WON), (2, "under", 2.25, HALF_WON), (3, "under", 2.75, HALF_LOST),
    (3, "over", 2.75, HALF_WON), (2, "over", 2.75, LOST), (3, "under", 2.25, LOST), (2, "under", 2.75, WON),
])
def test_total_quarter_lines(total, side, line, expected):
    assert grade(ml("total", side, line), MatchResult("final", {"full": (total, 0)}), "football") == expected


def test_half_outcomes_pay_half_stakes_and_count_as_their_side():
    rows = [dict(status="half_won", price=2.0), dict(status="half_lost", price=2.0)]
    block = stats._block(rows)
    assert block["won"] == 1 and block["lost"] == 1 and block["profit"] == 0.0     # +0.5 and -0.5


# ============================================================================== half-score names
def test_history_half_time_scores_use_the_graders_period_names(db):
    from predbot.history.store import HistoryStore
    HistoryStore(db).add_result(sport="football", competition_id="epl", kickoff=START, home_id="h-id", away_id="a-id",
                                home_goals=1, away_goals=2, source="fd", source_match_id="1", home_ht=1, away_ht=0)
    pid = db.insert_pick(row("e9", sport="football", start_utc="2030-06-01T18:00:00Z", canonical_competition_id="epl",
                             home_id="h-id", away_id="a-id", fixture_key="fk-e9"))
    r = db.picks_where("p.id=?", (pid,))[0]
    res = settle.stored_result(r, db)
    assert res.score("1h") == (1, 0) and res.score("2h") == (0, 2) and res.score("full") == (1, 2)
    assert grade(ml("result3", "home", period="1h"), res) == WON          # ahead at half time
    assert grade(ml("result3", "away", period="2h"), res) == WON          # won the second half 0-2
    assert grade(ml("result3", "away"), res) == WON                       # and the match


# ============================================================================== M17 mirrored selections
@pytest.mark.parametrize("market,selection,entity,line,mirrored_sel,mirrored_entity,mirrored_line", [
    ("result3", "home", "all", None, "away", "all", None),
    ("result3", "draw", "all", None, "draw", "all", None),
    ("result2", "away", "all", None, "home", "all", None),
    ("dnb", "home", "all", None, "away", "all", None),
    ("dc", "home_draw", "all", None, "away_draw", "all", None),
    ("dc", "home_away", "all", None, "home_away", "all", None),
    ("handicap", "home", "all", -1.5, "away", "all", -1.5),        # the line follows the selected TEAM
    ("total", "over", "all", 2.5, "over", "all", 2.5),
    ("total", "over", "home", 1.5, "over", "away", 1.5),           # a team total follows its team
    ("result_total", "home_over", "all", 2.5, "away_over", "all", 2.5),
    ("result_total", "draw_under", "all", 2.5, "draw_under", "all", 2.5),
    ("result_btts", "away_yes", "all", None, "home_yes", "all", None),
    ("ht_ft", "home/draw", "all", None, "away/draw", "all", None),
    ("winning_margin", "home_1-2", "all", None, "away_1-2", "all", None),
    ("correct_score", "2-1", "all", None, "1-2", "all", None),
])
def test_mirroring_keeps_every_selection_the_same_bet(market, selection, entity, line, mirrored_sel, mirrored_entity,
                                                      mirrored_line):
    original = ml(market, selection, line, entity=entity)
    flipped = original.mirrored()
    assert (flipped.selection, flipped.entity, flipped.line) == (mirrored_sel, mirrored_entity, mirrored_line)
    assert flipped.mirrored().selection == original.selection             # round trip
    # the same real result graded from either orientation gives the same outcome
    res, swapped = MatchResult("final", {"full": (2, 1), "1h": (0, 0)}), MatchResult("final", {"full": (1, 2), "1h": (0, 0)})
    assert grade(original, res) == grade(flipped, swapped)


def test_a_reversed_providers_composite_line_merges_as_the_same_bet():
    from predbot.providers.base import Event
    from predbot.providers.chain import _merge_markets
    ours = Event("a", "1", "football", "L", "Alpha", "Bravo", START, markets=[ml("result_total", "home_over", 2.5)])
    theirs = Event("b", "2", "football", "L", "Bravo", "Alpha", START, markets=[ml("result_total", "away_over", 2.5)])
    _merge_markets(ours, theirs, swapped=True)
    assert [m.selection for m in ours.markets] == ["home_over"]               # one bet, not two opposite ones


# ============================================================================== same_team / orientation
@pytest.mark.parametrize("a,b,sport", [
    ("Kentucky", "Western Kentucky", "basketball"), ("Michigan", "Michigan State", "american_football"),
    ("Texas", "Texas A&M", "american_football"), ("Nigeria", "Niger", "football"), ("Austria", "Australia", "football"),
    ("Real Madrid", "Real Madrid Castilla", "football"), ("Barcelona", "Barcelona B", "football"),
])
def test_different_teams_are_never_the_same_team(a, b, sport):
    assert not same_team(a, b, sport=sport) and not same_team(b, a, sport=sport)
    assert distinct_participants(a, b, sport)


@pytest.mark.parametrize("a,b,sport", [
    ("Rangers", "Texas Rangers", "baseball"), ("Los Angeles Lakers", "LA Lakers", "basketball"),
    ("St Etienne", "Saint-Etienne", "football"), ("Olympiakos", "Olympiacos", "football"),
    ("Brasil", "Brazil", "football"), ("Bayern Munich", "Bayern Muenchen", "football"),
    ("Athletics", "Oakland Athletics", "baseball"),
])
def test_legitimate_spellings_still_match(a, b, sport):
    assert same_team(a, b, sport=sport)


def test_orientation_is_decided_by_canonical_ids_first():
    ref = {"home": "Kentucky", "away": "Western Kentucky", "home_id": "K", "away_id": "WK", "sport": "basketball"}
    leg = {"home": "Western Kentucky", "away": "Kentucky", "home_id": "WK", "away_id": "K", "sport": "basketball"}
    assert same_orientation(ref, leg) is False
    swapped = orient_result(MatchResult("final", {"full": (70.0, 60.0)}), ref, leg)
    assert swapped.score("full") == (60.0, 70.0)


def test_unproven_orientation_leaves_the_leg_unresolved():
    ref = {"home": "Alpha", "away": "Bravo", "sport": "football"}
    leg = {"home": "Gamma", "away": "Delta", "sport": "football"}
    assert same_orientation(ref, leg) is None
    assert orient_result(MatchResult("final", {"full": (1.0, 0.0)}), ref, leg).status == "unknown"


# ============================================================================== Elo idempotency
def _pick(db, event, market, selection, home="Boston Celtics", away="Miami Heat", hid="bos", aid="mia"):
    pid = db.insert_pick(row(event, market=market, selection=selection, sport="basketball", home=home, away=away,
                             home_id=hid, away_id=aid, start_utc="2030-06-01T18:00:00Z", fixture_key="fk-nba-1"))
    return db.picks_where("p.id=?", (pid,))[0]


def test_one_game_updates_elo_once_whatever_the_route(db):
    final = MatchResult("final", {"full": (110.0, 100.0)}, overtime=False)
    counts = {"settled": 0, "pending": 0, "void": 0, "errors": 0}
    now = START + timedelta(hours=4)
    settle._apply_result(_pick(db, "nba1", "result2", "home"), final, db, Elo(db), now, counts, False)
    settle._apply_result(_pick(db, "nba2", "total", "over", line=None) if False else
                         _pick(db, "nba2", "dnb", "home"), final, db, Elo(db), now, counts, False)   # 2nd market
    settle._apply_result(_pick(db, "nba1b", "result2", "away"), final, db, Elo(db), now, counts, False)  # retry/restart
    # the ESPN scoreboard harvest names the teams differently, same canonical participants
    assert not settle.first_sighting(db, settle.result_keys("basketball", "Celtics", "Heat", START, "bos", "mia"),
                                     (110.0, 100.0))
    assert not settle.first_sighting(db, settle.result_keys("basketball", "Heat", "Celtics", START, "mia", "bos"),
                                     (100.0, 110.0))                      # either orientation
    assert Elo(db).get("basketball", "", "bos")[1] == 1 and Elo(db).get("basketball", "", "mia")[1] == 1


def test_a_new_game_is_still_learned(db):
    assert settle.first_sighting(db, settle.result_keys("basketball", "A", "B", START, "a", "b"), (1, 0))
    assert settle.first_sighting(db, settle.result_keys("basketball", "A", "B", START + timedelta(days=1), "a", "b"),
                                 (1, 0))


def test_an_unproven_orientation_never_teaches_elo(db):
    final = MatchResult("final", {"full": (110.0, 100.0)}, overtime=False)
    counts = {"settled": 0, "pending": 0, "void": 0, "errors": 0}
    first = _pick(db, "x1", "result2", "home", home="Alpha", away="Bravo", hid="", aid="")
    other = _pick(db, "x2", "dnb", "home", home="Gamma", away="Delta", hid="", aid="")
    db.execute("UPDATE picks SET fixture_key='fk-x' WHERE id IN (?,?)", (first["id"], other["id"]))
    other = db.picks_where("p.id=?", (other["id"],))[0]
    settle._apply_result(other, final, db, Elo(db), START + timedelta(hours=4), counts, False)
    assert db.query("SELECT COUNT(*) AS n FROM ratings")[0]["n"] == 0


# ============================================================================== M6 accuracy by membership
def test_accuracy_reads_top_and_daily_from_memberships(db):
    def stored(event, status, collections):
        pid = db.store_prediction(row(event))
        for c in collections:
            db.add_membership(pid, c, 1)
        db.settle_pick(pid, status, {})
    stored("t1", "won", ("all", "top", "daily"))          # one prediction in BOTH collections
    stored("d1", "lost", ("all", "daily"))
    stored("a1", "won", ("all",))
    rep = stats.accuracy_report(db)
    assert rep["legs"]["decided"] == 3
    assert (rep["by_tier"]["top"]["decided"], rep["by_tier"]["top"]["wins"]) == (1, 1)
    assert (rep["by_tier"]["daily"]["decided"], rep["by_tier"]["daily"]["wins"]) == (2, 1)
    assert rep["tickets"]["top"]["days"] == 1 and rep["tickets"]["top"]["hit"] == 1
    assert rep["tickets"]["daily"]["days"] == 1 and rep["tickets"]["daily"]["hit"] == 0
    assert "top: legs 1/1" in stats.accuracy_text(rep)


# ============================================================================== M8 unresolved vs void
def _expired(db, event):
    pid = db.store_prediction(row(event))
    db.add_membership(pid, "daily", 1)
    db.settle_pick(pid, "void", {"reason": "settlement deadline expired", "deadline": "2030-06-03T00:00:00Z"})
    return pid


def test_unresolved_is_counted_apart_from_win_loss_and_genuine_void(db):
    _expired(db, "u1")
    cancelled = db.store_prediction(row("v1"))
    db.add_membership(cancelled, "daily", 1)
    db.settle_pick(cancelled, "void", {"status": "cancelled"})
    won = db.store_prediction(row("w1", price=2.0))
    db.add_membership(won, "daily", 1)
    db.settle_pick(won, "won", {})
    rows = db.picks_where("1=1", published_only=True)
    block = stats.day_block(rows)
    assert (block["won"], block["lost"], block["void"], block["unresolved"]) == (1, 0, 1, 1)
    assert block["hit_rate"] == 100.0 and block["n"] == 1 and block["profit"] == 1.0      # neither in the denominator
    legs = stats.accuracy_report(db)["legs"]
    assert (legs["decided"], legs["void"], legs["unresolved"]) == (1, 1, 1)
    from predbot.bot.format import counts_line
    assert counts_line(block) == "✅ 1  ❌ 0  ➖ 1  ❔ 1 unresolved"


def test_a_ticket_with_an_unresolved_leg_is_neither_a_hit_nor_a_miss(db):
    _expired(db, "u2")
    won = db.store_prediction(row("w2"))
    db.add_membership(won, "daily", 1)
    db.settle_pick(won, "won", {})
    tickets = stats.accuracy_report(db)["tickets"]["daily"]
    assert tickets["days"] == 0 and tickets["unresolved_days"] == 1 and tickets["hit_rate"] is None


def test_a_genuine_void_is_still_a_void(db):
    pid = db.store_prediction(row("g1"))
    db.settle_pick(pid, "void", {"status": "postponed"})
    assert stats.outcome(db.picks_where("p.id=?", (pid,))[0]) == "void"


# ============================================================================== M13 Top combinations
def test_top_two_legs_beat_a_poor_greedy_ticket():
    rows = [promoted("a", "result3", 0.82, 1.35), promoted("b", "result3", 0.80, 1.40),
            promoted("c", "result3", 0.75, 1.45), promoted("d", "result3", 0.70, 1.60)]
    chosen, diag = select_top(rows, Config(), NOW)
    assert [r["canonical_event_id"] for r in chosen] == ["evt-b", "evt-c"]          # 1.40 x 1.45 = 2.03
    assert diag["combined_odds"] == pytest.approx(2.03) and diag["target_reached"] and diag["search"] == "combination"


def test_top_a_strong_single_near_the_target_is_enough():
    rows = [promoted("a", "result3", 0.70, 2.05), promoted("b", "result3", 0.65, 1.30), promoted("c", "result3", 0.64, 1.25)]
    chosen, _ = select_top(rows, Config(), NOW)
    assert [r["canonical_event_id"] for r in chosen] == ["evt-a"]


def test_top_uses_three_legs_only_when_needed():
    rows = [promoted("a", "result3", 0.90, 1.20), promoted("b", "result3", 0.88, 1.25), promoted("c", "result3", 0.86, 1.35)]
    chosen, diag = select_top(rows, Config(), NOW)
    assert len(chosen) == 3 and Config().top_odds_target <= diag["combined_odds"] <= Config().top_odds_max


def test_top_never_overshoots_and_never_forces_a_ticket():
    chosen, diag = select_top([promoted("a", "result3", 0.80, 2.80)], Config(), NOW)
    assert chosen == [] and not diag["target_reached"]                    # 2.80 > TOP_ODDS_MAX, even alone
    chosen, _ = select_top([promoted("a", "result3", 0.50, 2.00)], Config(), NOW)   # below TOP_MIN_PROB
    assert chosen == []


def test_top_never_combines_two_markets_of_one_fixture():
    rows = [promoted("a", "result3", 0.80, 1.40), promoted("a", "dnb", 0.79, 1.45), promoted("b", "result3", 0.60, 1.10)]
    chosen, _ = select_top(rows, Config(), NOW)
    assert len({r["canonical_event_id"] for r in chosen}) == len(chosen)


def test_top_below_target_falls_back_to_the_best_first_build():
    rows = [promoted("a", "result3", 0.80, 1.30), promoted("a", "dnb", 0.79, 1.28)]
    chosen, diag = select_top(rows, Config(), NOW)
    assert len(chosen) == 1 and diag["combined_odds"] == pytest.approx(1.30) and diag["target_reached"] is False
    assert "search" not in diag


# ============================================================================== M12 / M14 Daily
MARKETS = ("result3", "dnb", "dc", "result2", "handicap", "btts", "total", "odd_even")


def daily_leg(i, prob, price):
    return promoted(f"d{i}", MARKETS[i % len(MARKETS)], prob, price, league=f"L{i}", canonical_competition_id=f"c{i}",
                    sport=("football", "basketball")[i % 2])


def test_daily_end_game_search_reaches_the_target_where_greedy_stalls():
    best = [daily_leg(i, 0.80 - i * 0.01, p) for i, p in enumerate((1.6, 1.6, 1.6, 1.6, 1.5))]     # 9.83
    rest = [daily_leg(10 + i, 0.70 - i * 0.01, p) for i, p in enumerate((1.25, 1.30, 1.70))]       # each overshoots 12
    chosen, diag = select_daily(best + rest, Config(), NOW)
    odds = math.prod(r["price"] for r in chosen)
    assert 10.0 <= odds <= 12.0 and diag["target_reached"] and diag["search"] == "end_game"
    assert [r["canonical_event_id"] for r in chosen[:4]] == [r["canonical_event_id"] for r in best[:4]]  # quality kept
    assert len({r["canonical_event_id"] for r in chosen}) == len(chosen)


def test_daily_never_pads_with_unqualified_legs_or_lowers_a_gate():
    best = [daily_leg(i, 0.80, p) for i, p in enumerate((1.6, 1.6, 1.6, 1.6, 1.5))]
    weak = [daily_leg(20, 0.40, 1.65)]                                        # below DAILY_MIN_PROB: never used
    chosen, diag = select_daily(best + weak, Config(), NOW)
    assert all(r["prob"] >= Config().daily_min_prob for r in chosen) and not diag["target_reached"]


def test_daily_diversity_quotas_are_never_relaxed():
    legs = [promoted(f"s{i}", "result3", 0.80, 1.35, league="Serie A", canonical_competition_id="sa") for i in range(9)]
    chosen, diag = select_daily(legs, Config(), NOW)
    assert len(chosen) <= Config().daily_max_per_league and "diversity_relaxed" not in diag
    assert not diag["target_reached"]                                         # posted below target instead


def test_daily_that_already_reaches_the_target_is_unchanged():
    legs = [daily_leg(i, 0.80 - i * 0.01, 1.8) for i in range(6)]             # 1.8^4 = 10.5
    chosen, diag = select_daily(legs, Config(), NOW)
    assert len(chosen) == 4 and "search" not in diag


# ============================================================================== identity-repair report
def test_identity_repair_reports_deterministically_contaminated_ratings_without_names(db):
    from predbot.storage.identity_repair import audit
    db.execute("INSERT INTO canonical_participants VALUES('bk:participant:ky','basketball','Kentucky Wildcats')")
    db.execute("INSERT INTO canonical_participants VALUES('bk:participant:duke','basketball','Duke Blue Devils')")
    # a legacy pick attributed "Arizona Wildcats" to Kentucky's id, and it was settled: Kentucky's rating learned it
    pid = db.insert_pick(row("w1", sport="basketball", home="Arizona Wildcats", away="Duke Blue Devils",
                             home_id="bk:participant:ky", away_id="bk:participant:duke"))
    db.settle_pick(pid, "won", {})
    Elo(db).update("basketball", "", "", 80, 70, home_id="bk:participant:ky", away_id="bk:participant:duke")
    found = audit(db)
    rep, ids = found["report"], found["ids"]
    assert rep["misattributed_settled_picks"] == 1 and rep["ratings_deterministically_contaminated"] == 1
    assert ids["ratings_deterministically_contaminated_ids"] == ["bk:participant:ky"]
    assert "duke" not in json.dumps(rep).lower() and "Wildcats" not in json.dumps(found["ids"])
