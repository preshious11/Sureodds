"""Pro value gates and selection. Pure: takes already-matched, already-priced events and returns decisions.

Each candidate is one outcome of one matched fixture. Gates (all configurable through Gates):
  1. a sharp fair price exists (Pinnacle, then Matchbook, then Betfair Exchange) - otherwise no candidate at all
  2. SportyBet price inside [odds_min, odds_max]
  3. SportyBet and sharp prices from the same run: |sporty_fetched - sharp_fetched| <= max_price_age
  4. edge >= edge_min -> selected, unless edge > review_edge, which also needs a second sharp book giving an
     edge >= second_book_edge on the same outcome; otherwise needs_review (never selected)
  5. edges in [near_miss_min, edge_min) are stored as near misses only
  6. at most one pick per match (best edge), top_n by edge
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from predbot.pro_value.fair import OUTCOMES, edge, primary_fair, sharp_books_1x2
from predbot.pro_value.matching import match_key


@dataclass(frozen=True)
class Gates:
    edge_min: float = 0.02
    odds_min: float = 1.5
    odds_max: float = 4.0
    review_edge: float = 0.06
    second_book_edge: float = 0.02
    max_price_age_s: int = 30 * 60
    top_n: int = 3
    near_miss_min: float = 0.0

    @classmethod
    def from_config(cls, cfg) -> "Gates":
        return cls(edge_min=cfg.pro_value_edge_min, odds_min=cfg.pro_value_odds_min,
                   odds_max=cfg.pro_value_odds_max, review_edge=cfg.pro_value_review_edge,
                   second_book_edge=cfg.pro_value_second_book_edge,
                   max_price_age_s=cfg.pro_value_max_price_age_min * 60, top_n=cfg.pro_value_top_n,
                   near_miss_min=cfg.pro_value_near_miss_min)


def evaluate_match(matched: Dict, gates: Gates) -> List[Dict]:
    """Evaluate the three outcomes of one matched pair. ``matched`` is {odds, sporty, sharp_books, ...} where
    odds.bookmakers maps book key -> {Home, Draw, Away} and sharp_books are the books' own fetch timestamps."""
    odds, sporty = matched["odds"], matched["sporty"]
    fairs = sharp_books_1x2({k: v for k, v in odds["bookmakers"].items()})
    primary = primary_fair(fairs)
    rows: List[Dict] = []
    if primary is None:
        return rows                                    # no sharp price: no pick, no near miss
    primary_book, primary_probs = primary
    sharp_at = odds["fetched_at"]
    key = match_key(odds["home_team"], odds["away_team"], odds["commence_ts"])
    for outcome in OUTCOMES:
        row = {"match_key": key, "league": odds.get("sport_key"), "home": odds["home_team"],
               "away": odds["away_team"], "kickoff_ts": odds["commence_ts"], "outcome": outcome,
               "sporty_odds": float(sporty["odds"][outcome]), "sporty_fetched_at": sporty["fetched_at"],
               "fair_probability": primary_probs[outcome], "fair_book": primary_book,
               "sharp_fetched_at": sharp_at, "edge": None, "second_book": None, "second_edge": None,
               "status": None, "reason": None}
        row["edge"] = edge(row["sporty_odds"], row["fair_probability"])
        if not (gates.odds_min <= row["sporty_odds"] <= gates.odds_max):
            row.update(status="rejected", reason="odds_out_of_band")
        elif abs(row["sporty_fetched_at"] - sharp_at) > gates.max_price_age_s:
            row.update(status="rejected", reason="stale_prices")
        elif row["edge"] >= gates.edge_min:
            if row["edge"] > gates.review_edge:
                second = next(((b, f) for b, f in fairs.items() if b != primary_book), None)
                if second is None:
                    row.update(status="needs_review", reason="no_second_sharp_book")
                else:
                    row["second_book"] = second[0]
                    row["second_edge"] = edge(row["sporty_odds"], second[1][outcome])
                    if row["second_edge"] >= gates.second_book_edge:
                        row.update(status="selected", reason="ok")
                    else:
                        row.update(status="needs_review", reason="second_book_edge_low")
            else:
                row.update(status="selected", reason="ok")
        elif row["edge"] >= gates.near_miss_min:
            row.update(status="near_miss", reason="edge_below_min")
        else:
            row.update(status="rejected", reason="edge_negative")
        rows.append(row)
    return rows


@dataclass
class Selection:
    picks: List[Dict]
    near_misses: List[Dict]
    rejected: List[Dict]
    no_pick_reason: Optional[str]


def select_pro(matched_events: List[Dict], gates: Gates) -> Selection:
    """Run every gate over every matched event, keep one pick per match and the top_n by edge."""
    all_rows: List[Dict] = []
    for m in matched_events:
        all_rows.extend(evaluate_match(m, gates))
    selected = [r for r in all_rows if r["status"] == "selected"]
    best: Dict[str, Dict] = {}
    for r in sorted(selected, key=lambda r: -r["edge"]):
        best.setdefault(r["match_key"], r)                 # one pick per match: its best outcome
    picks = sorted(best.values(), key=lambda r: -r["edge"])[:gates.top_n]
    for rank, r in enumerate(picks, 1):
        r["rank"] = rank
    picked_ids = {id(r) for r in picks}
    for r in selected:
        if id(r) not in picked_ids and r["status"] == "selected":
            r["status"], r["reason"] = "not_top", "lost_to_better_outcome_or_top_n"
    near = [r for r in all_rows if r["status"] == "near_miss"]
    review = [r for r in all_rows if r["status"] == "needs_review"]
    rejected = [r for r in all_rows if r["status"] not in ("selected", "near_miss", "needs_review")]
    return Selection(picks=picks, near_misses=near, rejected=review + rejected, no_pick_reason=None if picks else "no Pro pick today")
