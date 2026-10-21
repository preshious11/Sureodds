"""grade_ledger_leg must produce IDENTICAL outcomes to calling predbot's
existing settlement.grade() directly - it is a reshape, never a second
grader. Reference match mirrors test_settlement.py's own fixture.
"""
from __future__ import annotations

from predbot.jobs.ledger_settlement import grade_ledger_leg, grade_pending_legs
from predbot.markets.settlement import grade
from predbot.providers.base import MarketLine, MatchResult


def res():
    return MatchResult(status="final", scores={"full": (2.0, 1.0), "reg": (2.0, 1.0), "1h": (1.0, 1.0)})


def draw():
    return MatchResult(status="final", scores={"full": (1.0, 1.0)})


def leg(market, selection, price=1.9, line=None, period="full", stat="points", entity="all"):
    return {"market": market, "selection": selection, "price": price, "line": line,
            "period": period, "stat": stat, "entity": entity}


def test_matches_direct_grade_call_exactly_for_a_straight_win():
    l = leg("result3", "home", price=2.0)
    direct = grade(MarketLine(l["market"], l["selection"], l["price"], line=l["line"], period=l["period"], stat=l["stat"], entity=l["entity"]), res())
    assert grade_ledger_leg(l, res(), "football") == direct == "won"


def test_matches_direct_grade_call_exactly_for_a_total_push():
    l = leg("total", "over", price=1.9, line=3.0)
    direct = grade(MarketLine(l["market"], l["selection"], l["price"], line=l["line"], period=l["period"], stat=l["stat"], entity=l["entity"]), res())
    assert grade_ledger_leg(l, res(), "football") == direct == "push"


def test_matches_direct_grade_call_exactly_for_dnb_push_on_draw():
    l = leg("dnb", "home", price=1.9)
    assert grade_ledger_leg(l, draw(), "football") == grade(MarketLine("dnb", "home", 1.9), draw()) == "push"


def test_matches_direct_grade_call_exactly_for_a_quarter_line_half_result():
    l = leg("total", "over", price=1.9, line=2.75)
    direct = grade(MarketLine(l["market"], l["selection"], l["price"], line=l["line"], period=l["period"], stat=l["stat"], entity=l["entity"]), res())
    outcome = grade_ledger_leg(l, res(), "football")
    assert outcome == direct
    assert outcome in ("half_won", "half_lost")  # 3 goals vs a 2.75 quarter line


def test_grade_pending_legs_only_grades_fixtures_with_a_known_result_never_guesses():
    legs = [leg("result3", "home", price=2.0), leg("result3", "away", price=4.0)]
    legs[0]["canonical_event_id"] = "fx-known"
    legs[1]["canonical_event_id"] = "fx-unknown"
    graded = grade_pending_legs(legs, {"fx-known": res(), "fx-unknown": None}, {"fx-known": "football"})
    assert graded == {0: "won"}  # index 1 (fx-unknown) is NEVER in the output - no result, no guess


def test_grade_pending_legs_never_returns_a_pending_outcome():
    legs = [leg("total", "over", price=1.9, line=50.5)]  # miles above any real score -> PENDING-safe, actually LOST here
    legs[0]["canonical_event_id"] = "fx-1"
    graded = grade_pending_legs(legs, {"fx-1": res()}, {"fx-1": "football"})
    assert graded[0] in ("lost",)  # a real, decisive grade - never "pending" leaking into the ledger
