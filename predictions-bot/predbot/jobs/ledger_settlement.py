"""Bridges the Worker-side issued-pick ledger (workers/migrations/0014,
workers/src/ledger.ts) to predbot's EXISTING, already-tested settlement
grader. This file never grades anything itself - "avoid creating a second
contradictory grader" - it only reshapes a ledger leg row into the exact
MarketLine shape ``grade()`` already expects (predbot/jobs/settle.py's own
``_row_to_line`` does the equivalent reshaping for the ``picks`` table;
this is the same idea for ``issued_ticket_legs``) and calls the SAME
function Top/Daily settlement already calls.

NOT wired into a scheduled job yet. The remaining integration step -
reading pending issued_ticket_legs rows out of production D1, resolving
each one's MatchResult (reusing whatever live_monitor_d1.py/settle.py
already use for that lookup), and writing the graded outcome back via
workers/scripts/d1_sync.py's existing plan/apply pattern - touches the
already-complex, live-data-writing stage8 job directly and deserves its
own careful pass rather than a rushed addition today. This module is the
tested, reusable piece that step will call into.
"""
from __future__ import annotations

from typing import Optional

from predbot.markets.settlement import grade
from predbot.providers.base import MarketLine, MatchResult


def grade_ledger_leg(leg: dict, result: MatchResult, sport: str) -> str:
    """``leg`` is one row's fields from issued_ticket_legs (market, selection,
    line, period, stat, entity, price) - the same shape recorded by
    workers/src/ledger.ts's recordIssuedTicket(). Returns one of predbot's
    own outcome vocabulary (won|lost|push|half_won|half_lost|void|pending) -
    the EXACT SAME strings workers/src/ledger.ts's SettlementStatus type
    and migrations/0014's CHECK constraint already use, so the result can
    be written straight through with no translation layer to drift out of
    sync."""
    ml = MarketLine(
        market=leg["market"], selection=leg["selection"], price=float(leg["price"]),
        period=leg["period"], stat=leg["stat"], entity=leg["entity"],
        line=float(leg["line"]) if leg.get("line") is not None else None,
    )
    return grade(ml, result, sport)


def grade_pending_legs(legs: list[dict], results_by_event: dict[str, Optional[MatchResult]], sports_by_event: dict[str, str]) -> dict[int, str]:
    """Grades every leg whose fixture has a known result; a leg whose
    fixture has none yet (``results_by_event.get(event_id)`` is None) is
    simply omitted from the returned dict - never guessed at, never forced
    to a default outcome. Keyed by the leg's own index in ``legs`` (the
    caller already knows how to map that back to a (ticket_id, leg_index)
    pair for settleIssuedLeg)."""
    graded: dict[int, str] = {}
    for i, leg in enumerate(legs):
        result = results_by_event.get(leg["canonical_event_id"])
        if result is None:
            continue
        sport = sports_by_event.get(leg["canonical_event_id"], leg.get("sport", "football"))
        outcome = grade_ledger_leg(leg, result, sport)
        if outcome != "pending":
            graded[i] = outcome
    return graded
