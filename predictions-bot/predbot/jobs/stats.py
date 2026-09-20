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


# ------------------------------------------------------------------ one day, one source of truth
#: status -> the outcome a reader sees. ``pending`` (anything else) is not settled and has no mark.
OUTCOME = {"won": "won", "half_won": "won", "lost": "lost", "half_lost": "lost", "void": "void", "push": "push"}


def outcome(row) -> Optional[str]:
    """won | lost | void | push, or None while the pick is unsettled."""
    return OUTCOME.get(str(row["status"]))


def ordered(rows) -> List:
    """The order a day's picks are listed in everywhere (/yesterday, the per-day strip): Top legs first, then
    Daily, each in kickoff order."""
    return sorted(rows, key=lambda r: (0 if r["tier"] == "top" else 1, str(r["start_utc"]), int(r["rank"] or 0), int(r["id"] or 0)))


def day_block(rows: List) -> Dict:
    """Everything the stats card, the recap and /yesterday say about ONE day's picks.

    A day is ``final`` once every pick issued that day is settled; hit rate and ROI (level stakes) exist only
    then, so a half-settled day can never advertise a flattering percentage. ``best_win`` is the won pick with
    the highest price (tie: the higher model probability)."""
    counts = {"won": 0, "lost": 0, "void": 0, "push": 0}
    profit, staked, best = 0.0, 0, None
    for r in rows:
        kind = outcome(r)
        if kind is None:
            continue
        counts[kind] += 1
        if kind != "void":
            staked += 1
            profit += PROFIT[str(r["status"])](float(r["price"]))
        if str(r["status"]) == "won":
            key = (float(r["price"]), float(r["prob"] or 0.0))
            if best is None or key > best[0]:
                best = (key, r)
    issued = len(rows)
    settled = sum(counts.values())
    final = issued > 0 and settled == issued
    decided = counts["won"] + counts["lost"]
    return {**counts, "issued": issued, "settled": settled, "final": final,
            "n": staked, "profit": round(profit, 2),
            "hit_rate": (100.0 * counts["won"] / decided) if final and decided else None,
            "roi": (100.0 * profit / staked) if final and staked else None,
            "best_win": ({"label": best[1]["label"], "home": best[1]["home"], "away": best[1]["away"],
                          "price": float(best[1]["price"]), "outcome": pick_outcome(best[1])} if best else None)}


def pick_outcome(row) -> str:
    """The explicit prediction of a stored pick ('Over 2.5 Total Goals', 'Toulouse (Draw No Bet)'), as the cards word it."""
    from types import SimpleNamespace
    from predbot.bot.format import format_outcome
    data = dict(row)
    return format_outcome(SimpleNamespace(**data)) or str(data.get("label") or "")


def sports_day(rows: List) -> List[Dict]:
    """Per sport: picks issued, won and settled that day. A sport whose picks are all unsettled is still listed
    (``settled`` = 0). Most picks first, then most settled, then alphabetical."""
    agg: Dict[str, Dict[str, int]] = {}
    for r in rows:
        b = agg.setdefault(str(r["sport"]), {"issued": 0, "won": 0, "settled": 0})
        b["issued"] += 1
        kind = outcome(r)
        if kind is not None:
            b["settled"] += 1
            b["won"] += kind == "won"
    return sorted(({"sport": k, **v} for k, v in agg.items()), key=lambda b: (-b["issued"], -b["settled"], b["sport"]))


def top_ticket(rows: List) -> Dict:
    """Per day, does the Top ticket land? Only days whose Top legs are ALL settled count; a ticket hits when no leg
    lost and at least one won (a void or pushed leg drops out, as on any accumulator)."""
    by_day: Dict[str, List] = {}
    for r in rows:
        if r["tier"] == "top":
            by_day.setdefault(str(r["pick_date"]), []).append(r)
    hit = days = 0
    for legs in by_day.values():
        kinds = [outcome(r) for r in legs]
        if None in kinds:
            continue
        days += 1
        hit += "lost" not in kinds and "won" in kinds
    return {"hit": hit, "days": days}


def _streak_of(rows: List) -> str:
    seq = [outcome(r) for r in sorted(rows, key=lambda r: (str(r["pick_date"]), str(r["start_utc"])))]
    seq = [k for k in seq if k in ("won", "lost")]
    if not seq:
        return ""
    last, n = seq[-1], 0
    for k in reversed(seq):
        if k != last:
            break
        n += 1
    return f"{'W' if last == 'won' else 'L'}{n}"


def last_days(rows: List, window_days: int) -> Dict:
    """The last-7-days block: settled results only, hit rate on settled, the current streak, and a per-day strip
    (most recent first) of settled outcomes in listing order; a day with nothing settled is left out."""
    settled = [r for r in rows if outcome(r)]
    block = _block(settled)
    days: Dict[str, List] = {}
    for r in settled:
        days.setdefault(str(r["pick_date"]), []).append(r)
    strip = [{"date": d, "marks": [outcome(r) for r in ordered(days[d])]} for d in sorted(days, reverse=True)]
    decided = block["won"] + block["lost"]
    return {**block, "hit_rate": (100.0 * block["won"] / decided) if decided else None,
            "streak": _streak_of(settled), "strip": strip, "window_days": window_days}


def compute(db: Database, today_local: datetime) -> Dict:
    """Every number the stats card, the recap and /yesterday show, keyed on the READER's local dates:
    ``today_local`` is 'now' in the reader's zone and picks are looked up by their ``pick_date``."""
    d0 = today_local.date()
    y = (d0 - timedelta(days=1)).strftime("%Y-%m-%d")
    since7 = (d0 - timedelta(days=7)).strftime("%Y-%m-%d")
    since30 = (d0 - timedelta(days=30)).strftime("%Y-%m-%d")
    today_str = d0.strftime("%Y-%m-%d")

    all_rows = db.query("SELECT * FROM picks WHERE status NOT IN ('pending')")
    recent = db.query("SELECT * FROM picks WHERE pick_date >= ? AND pick_date < ?", (since7, today_str))
    rows7 = [r for r in recent if since7 <= r["pick_date"] < today_str]
    rows30 = [r for r in all_rows if since30 <= r["pick_date"] < today_str]
    rows_y_all = [r for r in recent if r["pick_date"] == y]
    rows_y_top = [r for r in all_rows if r["pick_date"] == y and r["tier"] == "top"]
    top_all = [r for r in all_rows if r["tier"] == "top"]

    first = db.query("SELECT MIN(pick_date) AS d FROM picks")[0]["d"]
    window = 0
    if first:
        try:
            window = max(0, min(7, (d0 - datetime.strptime(str(first), "%Y-%m-%d").date()).days))
        except ValueError:
            window = 0

    yesterday = {**day_block(rows_y_all), "date": y}
    out = {
        "today": today_str, "settled_ever": len(all_rows),
        "last7": last_days(rows7, window), "last30": _block(rows30), "all": _block(all_rows),
        "yesterday": yesterday, "yesterday_top": _block(rows_y_top),
        "by_sport_yesterday": sports_day(rows_y_all), "top_ticket_7d": top_ticket(rows7),
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
