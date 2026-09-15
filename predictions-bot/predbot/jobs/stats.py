"""Win/loss/ROI statistics from the picks table (flat 1-unit stakes)."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, List, Optional

from predbot.model.calibration import Calibrator
from predbot.storage.db import Database

PROFIT = {
    "won": lambda p: p - 1.0, "lost": lambda p: -1.0, "push": lambda p: 0.0, "void": lambda p: 0.0,
    "half_won": lambda p: (p - 1.0) / 2, "half_lost": lambda p: -0.5,
}


def _block(rows: List) -> Dict:
    won = lost = push = 0
    profit = 0.0
    staked = 0
    best: Optional[Dict] = None
    for r in rows:
        st = r["status"]
        if st in ("pending", "void"):
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
    return {"won": won, "lost": lost, "push": push, "n": staked,
            "roi": (100.0 * profit / staked) if staked else 0.0, "profit": round(profit, 2), "best_win": best}


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
    all_rows = db.query("SELECT * FROM picks WHERE status NOT IN ('pending')")
    rows7 = [r for r in all_rows if since7 <= r["pick_date"] < d0.strftime("%Y-%m-%d")]
    rows30 = [r for r in all_rows if since30 <= r["pick_date"] < d0.strftime("%Y-%m-%d")]
    rows_y = [r for r in all_rows if r["pick_date"] == y]
    out = {
        "last7": _block(rows7), "last30": _block(rows30), "all": _block(all_rows),
        "yesterday": _block(rows_y), "yesterday_top": _block([r for r in rows_y if r["tier"] == "top"]),
        "top_all": _block([r for r in all_rows if r["tier"] == "top"]),
        "streak": _streak(all_rows),
    }
    try:
        out["brier"] = Calibrator(db).brier()
    except Exception:  # noqa: BLE001
        out["brier"] = None
    return out


def rows_for_recap(db: Database, date_str: str) -> List[Dict]:
    return [dict(r) for r in db.picks_for_date(date_str)]
