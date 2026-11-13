"""Closing capture for shadow picks: one league-level 1X2 call per league and kickoff window, Pinnacle-led fair.

CLV is defined as SportyBet price at selection divided by the closing fair price (1 / closing fair probability),
minus one. Only picks inside the window before kickoff are captured; nothing is captured after kickoff."""
from __future__ import annotations

from typing import Dict, List, Tuple

from predbot.pro_value.fair import primary_fair, sharp_books_1x2
from predbot.pro_value.matching import match_key


#: Scheduled capture: a pick is captured only while its kickoff is 30-60 minutes away. With the 30-minute schedule
#: every pick falls in exactly one run's window, so it is captured once, and never after kickoff.
CAPTURE_LO_MIN = 30
CAPTURE_HI_MIN = 60


def due_groups(rows, now_ts: int, window_min: int, lo_min: int = 0) -> Dict[Tuple[str, int], List]:
    """Group pending shadow picks whose kickoff is between ``lo_min`` and ``window_min`` minutes from now.

    Picks in the same league and the same kickoff hour share one call."""
    groups: Dict[Tuple[str, int], List] = {}
    for r in rows:
        until = r["kickoff_ts"] - now_ts
        if not (lo_min * 60 < until <= window_min * 60):
            continue
        key = (r["league"], int(r["kickoff_ts"]) // 3600)
        groups.setdefault(key, []).append(r)
    return groups


def capture_group(client, league: str, picks: List, store, now_ts: int) -> Dict[str, int]:
    """One call for the league; then the closing fair for each pick that is present in the response."""
    counts = {"calls": 0, "refused": 0, "captured": 0, "not_found": 0}
    events = client.fetch_league_h2h(league, now_ts)
    if events is None:
        counts["refused"] = len(picks)
        return counts
    counts["calls"] = 1
    by_key = {}
    for ev in events:
        fair = primary_fair(sharp_books_1x2(ev["bookmakers"]))
        if fair is not None:
            by_key[match_key(ev["home_team"], ev["away_team"], ev["commence_ts"])] = fair
    for p in picks:
        hit = by_key.get(p["match_key"])
        if hit is None:
            counts["not_found"] += 1
            store.log_mismatch("closing_not_found", p["match_key"], league, now_ts)
            continue
        book, probs = hit
        if p["outcome"] not in probs:
            counts["not_found"] += 1
            store.log_mismatch("closing_outcome_missing", p["match_key"], f"{league}:{p['outcome']}", now_ts)
            continue
        fair_prob = probs[p["outcome"]]
        fair_price = 1.0 / fair_prob
        clv = p["sporty_odds"] / fair_price - 1.0
        store.set_closing(p["id"], fair_prob, fair_price, now_ts, clv)
        counts["captured"] += 1
    return counts


def run_closing(client, store, now_ts: int, window_min: int, guard, lo_min: int = 0) -> Dict[str, int]:
    """Capture closing prices for all due shadow picks. The guard refuses any call beyond the reserve or the cap."""
    totals = {"calls": 0, "refused": 0, "captured": 0, "not_found": 0}
    rows = store.open_pending_picks(now_ts)
    for (league, _hour), picks in sorted(due_groups(rows, now_ts, window_min, lo_min).items()):
        if not guard.can_spend(1):
            totals["refused"] += len(picks)
            continue
        counts = capture_group(client, league, picks, store, now_ts)
        for k in totals:
            totals[k] += counts.get(k, 0)
    return totals
