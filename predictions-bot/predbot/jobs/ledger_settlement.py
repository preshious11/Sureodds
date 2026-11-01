"""Bridges the Worker-side issued-pick ledger (workers/migrations/0014,
workers/src/ledger.ts) to predbot's EXISTING, already-tested settlement
grader. This file never grades anything itself - "avoid creating a second
contradictory grader" - it only reshapes a ledger leg row into the exact
MarketLine shape ``grade()`` already expects (predbot/jobs/settle.py's own
``_row_to_line`` does the equivalent reshaping for the ``picks`` table;
this is the same idea for ``issued_ticket_legs``) and calls the SAME
function Top/Daily settlement already calls.

Scheduled by the full settlement and live-monitor jobs; the relay applies
monotonic settlement updates to the same authoritative Oracle database.
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


def run(resolver, db):
    """Settle issued contracts only from an exact fixture mapping and final evidence."""
    import logging
    from predbot.jobs.settle import stored_result, _resolve_args
    from predbot.util.timeutil import parse_iso, utcnow, iso
    from predbot.util.duration import expected_end

    if not db.query("SELECT 1 FROM sqlite_master WHERE name='issued_ticket_legs'"):
        return {'settled_legs': 0, 'unresolved_legs': 0, 'errors': 0}
    legs=db.query("SELECT l.*,t.prediction_date FROM issued_ticket_legs l JOIN issued_tickets t "
                  "ON t.id=l.ticket_id WHERE l.settlement_status='pending'")
    counts={'settled_legs':0,'unresolved_legs':0,'errors':0}
    candidates={};cache={}
    for raw in legs:
        leg=dict(raw)
        try:
            rows=db.query('SELECT * FROM picks WHERE canonical_event_id=? AND sport=? AND home=? AND away=?',
                          (leg['canonical_event_id'],leg['sport'],leg['home'],leg['away']))
            if not rows:
                day=leg['prediction_date']
                if day not in candidates: candidates[day]=db.ticket_candidate_rows(day)
                rows=[r for r in candidates[day] if all(r.get(k)==leg[k] for k in ('canonical_event_id','sport','home','away'))]
            if not rows:
                counts['unresolved_legs']+=1;continue
            row=dict(rows[0]);start=parse_iso(row['start_utc'])
            if start is None or expected_end(start,leg['sport'])>utcnow():
                counts['unresolved_legs']+=1;continue
            # Use the ISSUED contract, never today's representative market.
            for k in ('market','period','stat','entity','selection','line','price'): row[k]=leg[k]
            result=stored_result(row,db)
            if result is None:
                alt,hints=_resolve_args(row)
                result=resolver.resolve(row['provider'],row['provider_event_id'],leg['sport'],leg['home'],leg['away'],
                                        start,alt_ids=alt,cache=cache,hints=hints)
            if result is None or result.status not in ('final','cancelled','postponed'):
                counts['unresolved_legs']+=1;continue
            outcome=grade_ledger_leg(leg,result,leg['sport'])
            if outcome=='pending': counts['unresolved_legs']+=1;continue
            price=float(leg['price'])
            payout={'won':price,'lost':0.,'push':1.,'void':1.,'half_won':(1+price)/2,'half_lost':.5}[outcome]
            with db.transaction():
                db.execute("UPDATE issued_ticket_legs SET settlement_status=?,payout_multiplier=?,settled_at=? "
                           "WHERE ticket_id=? AND leg_index=? AND settlement_status='pending'",
                           (outcome,payout,iso(utcnow()),leg['ticket_id'],leg['leg_index']))
                db.execute("UPDATE issued_tickets SET status='settled' WHERE id=? AND NOT EXISTS "
                           "(SELECT 1 FROM issued_ticket_legs WHERE ticket_id=? AND settlement_status='pending')",
                           (leg['ticket_id'],leg['ticket_id']))
            counts['settled_legs']+=1
        except Exception:
            logging.getLogger(__name__).exception('issued leg settlement failed')
            counts['errors']+=1
    return counts
