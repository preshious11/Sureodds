"""Win/loss/ROI statistics from the picks table (flat 1-unit stakes)."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, List, Optional

from predbot.model.calibration import Calibrator
from predbot.storage.db import Database

# "void" has no entry: _block() always `continue`s past void rows before
# ever computing profit, so a void multiplier would never be reached.
PROFIT = {
    "won": lambda p: p - 1.0, "lost": lambda p: -1.0, "push": lambda p: 0.0,
    "half_won": lambda p: (p - 1.0) / 2, "half_lost": lambda p: -0.5,
}


def _block(rows: List) -> Dict:
    won = lost = push = void = 0
    profit = 0.0
    staked = 0
    best: Optional[Dict] = None
    for r in rows:
        st = r["status"]
        if st in ("pending", "void"):
            if st == "void":
                void += 1
            continue
        staked += 1
        profit += PROFIT[st](float(r["price"]))
        if st in ("won", "half_won"):
            won += 1
            if st == "won" and (best is None or float(r["price"]) > best["price"]):
                best = {"label": r["label"], "price": float(r["price"]), "home": r["home"], "away": r["away"]}
        elif st in ("lost", "half_lost"):
            lost += 1
        else:
            push += 1
    return {"won": won, "lost": lost, "push": push, "void": void, "n": staked,
            "roi": (100.0 * profit / staked) if staked else 0.0, "profit": round(profit, 2), "best_win": best}


def by_sport(rows: List) -> List[Dict]:
    """Decided results per sport (most picks first, ties alphabetical).

    ``n`` is the hit-rate denominator (won + lost): pending and void picks are
    excluded, halves count as their full win/loss side, pushes count in neither.
    """
    agg: Dict[str, Dict[str, int]] = {}
    for r in rows:
        st = r["status"]
        if st in ("pending", "void", "push"):
            continue
        if st not in PROFIT:
            continue
        blk = agg.setdefault(str(r["sport"]), {"won": 0, "lost": 0})
        blk["won" if st in ("won", "half_won") else "lost"] += 1
    out = [{"sport": sport, "won": b["won"], "lost": b["lost"], "n": b["won"] + b["lost"]}
           for sport, b in agg.items()]
    return sorted([b for b in out if b["n"]], key=lambda b: (-b["n"], b["sport"]))


def _streak(rows: List) -> str:
    seq = [r["status"] for r in sorted(rows, key=lambda r: (r["pick_date"], r["start_utc"])) if r["status"] in ("won", "lost")]
    if not seq:
        return ""
    last = seq[-1]
    n = 0
    for s in reversed(seq):
        if s != last:
            break
        n += 1
    return f"{'W' if last == 'won' else 'L'}{n}"


def compute(db: Database, today_local: datetime) -> Dict:
    d0 = today_local.date()
    y = (d0 - timedelta(days=1)).strftime("%Y-%m-%d")
    since7 = (d0 - timedelta(days=7)).strftime("%Y-%m-%d")
    since30 = (d0 - timedelta(days=30)).strftime("%Y-%m-%d")
    today_str = d0.strftime("%Y-%m-%d")

    all_rows = db.query("SELECT * FROM picks WHERE status NOT IN ('pending')")
    rows7 = []
    rows30 = []
    rows_y = []
    rows_y_top = []
    top_all = []

    for r in all_rows:
        if r["pick_date"] == y:
            rows_y.append(r)
            if r["tier"] == "top":
                rows_y_top.append(r)
        if since7 <= r["pick_date"] < today_str:
            rows7.append(r)
        if since30 <= r["pick_date"] < today_str:
            rows30.append(r)
        if r["tier"] == "top":
            top_all.append(r)

    out = {
        "last7": _block(rows7), "last30": _block(rows30), "all": _block(all_rows),
        "yesterday": _block(rows_y), "yesterday_top": _block(rows_y_top),
        "top_all": _block(top_all),
        "streak": _streak(all_rows),
    }
    try:
        out["brier"] = Calibrator(db).brier()
    except (ValueError, KeyError, TypeError):
        out["brier"] = None
    return out


def rows_for_recap(db: Database, date_str: str) -> List[Dict]:
    return [dict(r) for r in db.picks_for_date(date_str)]
