"""Closing capture for shadow picks: one league-level 1X2 call per league and kickoff window, Pinnacle-led fair.

CLV is defined as SportyBet price at selection divided by the closing fair price (1 / closing fair probability),
minus one. Only picks inside the window before kickoff are captured; nothing is captured after kickoff."""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from predbot.pro_value.fair import primary_fair, sharp_books_1x2
from predbot.pro_value.matching import match_key


def due_groups(rows, now_ts: int, window_min: int) -> Dict[Tuple[str, int], List]:
    """Group pending shadow picks whose kickoff falls within ``window_min`` minutes from now.

    Picks in the same league and the same kickoff hour share one call."""
    groups: Dict[Tuple[str, int], List] = {}
    for r in rows:
        until = r["kickoff_ts"] - now_ts
        if not (0 < until <= window_min * 60):
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
            continue
        book, probs = hit
        fair_prob = probs[p["outcome"]]
        fair_price = 1.0 / fair_prob
        clv = p["sporty_odds"] / fair_price - 1.0
        store.set_closing(p["id"], fair_prob, fair_price, now_ts, clv)
        counts["captured"] += 1
    return counts


def run_closing(client, store, now_ts: int, window_min: int, guard) -> Dict[str, int]:
    """Capture closing prices for all due shadow picks. The guard refuses any call beyond the reserve or the cap."""
    totals = {"calls": 0, "refused": 0, "captured": 0, "not_found": 0}
    rows = store.open_pending_picks(now_ts)
    for (league, _hour), picks in sorted(due_groups(rows, now_ts, window_min).items()):
        if not guard.can_spend(1):
            totals["refused"] += len(picks)
            continue
        counts = capture_group(client, league, picks, store, now_ts)
        for k in totals:
            totals[k] += counts.get(k, 0)
    return totals
