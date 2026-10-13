"""Win/loss/ROI statistics from the picks table (flat 1-unit stakes).

Outcomes are counted apart, never folded into each other:

* won / lost (a half win or half loss counts as its side for the hit rate, at half stake for ROI);
* push - stake returned;
* void - the bet itself was void (match cancelled or postponed, a void market): stake returned;
* unresolved - stored as VOID by the bounded settlement lifecycle because no result could be confirmed before the
  settlement deadline (``settle._void_expired``). It is NOT a win, NOT a loss and NOT a genuine void: it is shown as
  its own count (❔) so a gap in grading is visible instead of quietly shrinking the sample;
* pending - not settled yet.

Hit rate = won / (won + lost). ROI = profit / stakes on won, lost and push. Void and unresolved legs are in neither.
A ticket day with an unresolved leg cannot be called a hit or a miss and is left out of ticket hit rates (reported
as ``unresolved_days``).
"""
from __future__ import annotations

import json
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


#: ``result_json`` reasons of a VOID that means "never graded", not "the bet was void"
UNRESOLVED_REASONS = frozenset({"settlement deadline expired"})


def is_unresolved(row) -> bool:
    """A VOID stored because no result could be confirmed in time (see the module docstring)."""
    try:
        if str(row["status"]) != "void":
            return False
        payload = row["result_json"]
    except (KeyError, IndexError, TypeError):
        return False
    if not payload:
        return False
    try:
        data = json.loads(payload) if isinstance(payload, str) else payload
    except (TypeError, ValueError):
        return False
    return isinstance(data, dict) and data.get("reason") in UNRESOLVED_REASONS


def _block(rows: List) -> Dict:
    won = lost = push = void = unresolved = 0
    profit = 0.0
    staked = 0
    best: Optional[Dict] = None
    for r in rows:
        st = r["status"]
        if st in ("pending", "void"):
            if st == "void":
                if is_unresolved(r):
                    unresolved += 1
                else:
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
    return {"won": won, "lost": lost, "push": push, "void": void, "unresolved": unresolved, "n": staked,
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
    """won | lost | void | push | unresolved, or None while the pick is unsettled."""
    kind = OUTCOME.get(str(row["status"]))
    return "unresolved" if kind == "void" and is_unresolved(row) else kind


def in_collection(row, tier: str) -> bool:
    """Whether a prediction belongs to ``tier`` (top / daily): its memberships when the row carries them (one
    prediction may be in several collections), else the legacy ``tier`` column."""
    try:
        cols = row["collections"]
    except (KeyError, IndexError, TypeError):
        cols = None
    if cols:
        return tier in cols
    return str(row["tier"]) == tier


def ordered(rows) -> List:
    """The order a day's picks are listed in everywhere (/yesterday, the per-day strip): Top legs first, then
    Daily, each in kickoff order."""
    return sorted(rows, key=lambda r: (0 if r["tier"] == "top" else 1, str(r["start_utc"]), int(r["rank"] or 0), int(r["id"] or 0)))


def day_block(rows: List) -> Dict:
    """Everything the stats card, the recap and /yesterday say about ONE day's picks.

    A day is ``final`` once every pick issued that day is settled; hit rate and ROI (level stakes) exist only
    then, so a half-settled day can never advertise a flattering percentage. ``best_win`` is the won pick with
    the highest price (tie: the higher model probability)."""
    counts = {"won": 0, "lost": 0, "void": 0, "push": 0, "unresolved": 0}
    profit, staked, best = 0.0, 0, None
    for r in rows:
        kind = outcome(r)
        if kind is None:
            continue
        counts[kind] += 1
        if kind not in ("void", "unresolved"):
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
            "best_win": ({"label": best[1]["label"], "home": best[1]["home"], "away": best[1]["away"], "sport": best[1]["sport"],
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
        if in_collection(r, "top"):
            by_day.setdefault(str(r["pick_date"]), []).append(r)
    hit = days = unresolved_days = 0
    for legs in by_day.values():
        kinds = [outcome(r) for r in legs]
        if None in kinds:
            continue
        if "unresolved" in kinds:
            unresolved_days += 1                 # cannot be called a hit or a miss
            continue
        days += 1
        hit += "lost" not in kinds and "won" in kinds
    return {"hit": hit, "days": days, "unresolved_days": unresolved_days}


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

    # Legacy "picks" figures describe what was PUBLISHED (Top + Daily legs); collection_stats covers All Predictions.
    all_rows = db.picks_where("p.status NOT IN ('pending')", published_only=True)
    recent = db.picks_where("p.pick_date >= ? AND p.pick_date < ?", (since7, today_str), published_only=True)
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
        out["brier"] = Calibrator(db, scope="settlement_v2").brier()
    except (ValueError, KeyError, TypeError):
        out["brier"] = None
    return out


def _fixture_id(row):
    """The fixture a prediction belongs to: its canonical event, else its fixture key (legacy rows)."""
    return row['canonical_event_id'] or row['fixture_key']


def collection_stats(db, collection, today_local):
    """Each stored prediction counts once within its collection, including today's settled results. A fixture may hold
    several predictions (one per market family), so ``issued`` counts predictions and ``fixtures`` counts the distinct
    fixtures behind them; both are always reported apart."""
    rows = db.collection_predictions(collection)
    day = today_local.date()
    out = {}
    for label, days in (('today', 1), ('last7', 7), ('last30', 30), ('all', None)):
        since = (day - timedelta(days=days - 1)).isoformat() if days else ''
        subset = [r for r in rows if since <= r['pick_date'] <= day.isoformat()]
        block = _block(subset)
        decided = block['won'] + block['lost']
        block.update(issued=len(subset), fixtures=len({_fixture_id(r) for r in subset}),
                     pending=sum(r['status'] == 'pending' for r in subset),
                     hit_rate=100 * block['won'] / decided if decided else None, decided=decided)
        out[label] = block
    return out


def rows_for_recap(db: Database, date_str: str) -> List[Dict]:
    return [dict(r) for r in db.picks_for_date(date_str, published_only=True)]


# ------------------------------------------------------------------ prediction accuracy
# Individual-selection accuracy is NOT ticket win rate: a 10-leg Daily that loses because ONE leg failed is a 90% leg
# hit rate, not a failed model. These numbers keep the two apart, and compare the model against the market's own
# probability so "is the model adding anything?" has an answer.
SETTLED = ("won", "lost", "half_won", "half_lost")
#: the outcome a half result counts as for SCORING (hit rate counts a half as the side it landed on)
_Y = {"won": 1.0, "half_won": 0.75, "half_lost": 0.25, "lost": 0.0}
#: Brier / calibration claims need this many decided legs; below it they are reported as None, never as a number
MIN_BRIER_SAMPLES = 30
CONFIDENCE_BUCKETS = ((0.0, 0.55), (0.55, 0.65), (0.65, 0.75), (0.75, 0.85), (0.85, 1.01))
BOOK_BUCKETS = ((1, 1), (2, 2), (3, 4), (5, 99))


def clv(price_taken: float, closing_price: float) -> Optional[float]:
    """Closing line value: ``price_taken / closing_price - 1`` (positive = the price beat the closing line).
    ``None`` without both prices. Filled into ``picks.clv`` once a closing price is captured."""
    try:
        taken, close = float(price_taken), float(closing_price)
    except (TypeError, ValueError):
        return None
    return taken / close - 1.0 if taken > 1.0 and close > 1.0 else None


def _num(row, key):
    try:
        v = row[key]
    except (KeyError, IndexError):
        return None
    return float(v) if v is not None else None


def _leg_block(rows: List) -> Dict:
    """Counts and calibration of individual legs. ``hit_rate`` is won / (won + lost) in percent with a half result
    counted as the side it landed on (as everywhere else in the bot); pushes and voids are in neither."""
    cnt = {k: 0 for k in ("won", "lost", "half_won", "half_lost", "push", "void", "unresolved")}
    probs: List[float] = []
    pm: List[float] = []
    ys: List[float] = []
    raws: List[float] = []
    edges: List[float] = []
    books: List[float] = []
    for r in rows:
        st = str(r["status"])
        if st == "void" and is_unresolved(r):
            cnt["unresolved"] += 1
        elif st in cnt:
            cnt[st] += 1
        try:
            meta = (json.loads(r['sources'] or '{}').get('_notes') or {})
        except (KeyError, IndexError, TypeError, ValueError):
            meta = {}
        strict = meta.get('probability_basis') == 'strict_win_v2'
        if st not in SETTLED and not (strict and st == 'push'):
            continue
        p = _num(r, "prob")
        if p is not None:
            probs.append(p)
            ys.append(float(st == 'won') if strict else _Y[st])
            market = (meta.get('settlement') or {}).get('market_strict_win') if strict else _num(r, "p_market")
            raw = _num(r, "prob_raw")
            pm.append(market if market is not None else p)
            raws.append(raw if raw is not None else p)
        if _num(r, "edge_ev") is not None:
            edges.append(_num(r, "edge_ev"))
        if _num(r, "books") is not None:
            books.append(_num(r, "books"))
    won, lost = cnt["won"] + cnt["half_won"], cnt["lost"] + cnt["half_lost"]
    decided = won + lost
    enough = len(probs) >= MIN_BRIER_SAMPLES

    def brier(ps):
        return sum((p - y) ** 2 for p, y in zip(ps, ys)) / len(ys)

    return {**cnt, "settled": decided + cnt["push"], "decided": decided, "wins": won, "losses": lost,
            "hit_rate": (100.0 * won / decided) if decided else None,
            "avg_predicted_prob": (100.0 * sum(probs) / len(probs)) if probs else None,
            "brier": brier(probs) if enough else None,
            "brier_raw": brier(raws) if enough else None,
            "brier_market": brier(pm) if enough else None,          # the baseline: does the model beat the market's own p?
            "avg_edge_ev": (sum(edges) / len(edges)) if edges else None, "with_sharp_edge": len(edges),
            "avg_books": (sum(books) / len(books)) if books else None}


def _grouped(rows: List, key) -> List[Dict]:
    groups: Dict[str, List] = {}
    for r in rows:
        groups.setdefault(key(r), []).append(r)
    out = [{"key": k, **_leg_block(v)} for k, v in groups.items()]
    return sorted([b for b in out if b["decided"]], key=lambda b: (-b["decided"], str(b["key"])))


def ticket_results(rows: List, tier: str) -> Dict:
    """Per day, did ``tier``'s ticket land? Only days whose legs are ALL settled count; a ticket hits when no leg lost
    and at least one won. ``one_leg_short`` counts days lost by exactly ONE leg - the accumulator effect, not a bad
    model; ``avg_legs`` / ``avg_legs_won`` show the per-leg picture behind the ticket result."""
    by_day: Dict[str, List] = {}
    for r in rows:
        if in_collection(r, tier):
            by_day.setdefault(str(r["pick_date"]), []).append(r)
    days = hit = one_short = unresolved_days = 0
    legs = won_legs = 0
    for day_rows in by_day.values():
        kinds = [outcome(r) for r in day_rows]
        if None in kinds:
            continue
        if "unresolved" in kinds:
            unresolved_days += 1
            continue
        days += 1
        lost = kinds.count("lost")
        hit += lost == 0 and "won" in kinds
        one_short += lost == 1
        legs += len(kinds)
        won_legs += kinds.count("won")
    return {"days": days, "hit": hit, "hit_rate": (100.0 * hit / days) if days else None, "one_leg_short": one_short,
            "unresolved_days": unresolved_days,
            "avg_legs": (legs / days) if days else None, "avg_legs_won": (won_legs / days) if days else None}


def _confidence_bucket(p: float) -> str:
    return next((f"{int(lo * 100)}-{min(int(hi * 100), 100)}%" for lo, hi in CONFIDENCE_BUCKETS if lo <= p < hi), "?")


def _book_bucket(n: float) -> str:
    return next((f"{lo}" if lo == hi else f"{lo}+" if hi == 99 else f"{lo}-{hi}"
                 for lo, hi in BOOK_BUCKETS if lo <= n <= hi), "?")


def accuracy(rows: List) -> Dict:
    """Prediction-accuracy report over picks rows (any status; unsettled ones are ignored where they cannot count)."""
    settled = [r for r in rows if str(r["status"]) in SETTLED + ("push", "void")]
    clvs = [v for v in (_num(r, "clv") for r in settled) if v is not None]
    with_prob = [r for r in settled if _num(r, "prob") is not None]
    with_books = [r for r in settled if _num(r, "books") is not None]

    def quality(r):
        try:
            return str(r["quality"] or "unrecorded")
        except (KeyError, IndexError):
            return "unrecorded"

    return {
        "legs": _leg_block(settled),
        "by_tier": {t: _leg_block([r for r in settled if in_collection(r, t)]) for t in ("top", "daily")},
        "tickets": {t: ticket_results(rows, t) for t in ("top", "daily")},
        "by_sport": _grouped(settled, lambda r: str(r["sport"])),
        "by_market": _grouped(settled, lambda r: f"{r['market']}/{r['stat']}"),
        "by_quality": _grouped(settled, quality),
        "by_confidence": sorted(_grouped(with_prob, lambda r: _confidence_bucket(_num(r, "prob"))), key=lambda b: b["key"]),
        "by_books": _grouped(with_books, lambda r: _book_bucket(_num(r, "books"))),
        "clv": {"n": len(clvs), "mean": (sum(clvs) / len(clvs)) if clvs else None},
    }


def accuracy_report(db: Database) -> Dict:
    """Every settled prediction, with its Top / Daily MEMBERSHIPS (the stored ``tier`` column is 'all' for every
    prediction written since collections exist, so reading it reported no Top or Daily at all)."""
    return accuracy(db.picks_where("p.status NOT IN ('pending')"))


def accuracy_summary(rep: Dict) -> Dict:
    """The few numbers worth a log line."""
    legs = rep["legs"]

    def r(v, n):
        return None if v is None else round(v, n)

    return {"decided": legs["decided"], "hit_rate": r(legs["hit_rate"], 1), "avg_predicted": r(legs["avg_predicted_prob"], 1),
            "brier": r(legs["brier"], 4), "brier_market": r(legs["brier_market"], 4),
            "top_tickets": rep["tickets"]["top"], "daily_tickets": rep["tickets"]["daily"]}


def _pct(v) -> str:
    return "n/a" if v is None else f"{v:.1f}%"


def accuracy_text(rep: Dict) -> str:
    """Plain-text report for ``cli.py accuracy``."""
    legs = rep["legs"]
    lines = ["PREDICTION ACCURACY (individual selections, flat stakes; ticket results are separate)",
             f"  settled legs: {legs['decided']} decided ({legs['wins']} won / {legs['losses']} lost), "
             f"{legs['push']} push, {legs['void']} void, {legs['unresolved']} unresolved (no result confirmed in time)",
             f"  per-leg hit rate: {_pct(legs['hit_rate'])}   average predicted probability: {_pct(legs['avg_predicted_prob'])}"]
    if legs["brier"] is None:
        lines.append(f"  Brier: not reported until {MIN_BRIER_SAMPLES} legs have settled")
    else:
        lines.append(f"  Brier: model {legs['brier']:.4f}  raw {legs['brier_raw']:.4f}  market {legs['brier_market']:.4f} "
                     f"(lower is better; the model only adds value if it is below the market)")
    if legs["avg_edge_ev"] is not None:
        lines.append(f"  edge vs sharp at selection: {legs['avg_edge_ev']:+.3f} EV over {legs['with_sharp_edge']} legs")
    for tier in ("top", "daily"):
        t, b = rep["tickets"][tier], rep["by_tier"][tier]
        lines.append(f"  {tier}: legs {b['wins']}/{b['decided']} ({_pct(b['hit_rate'])}) | tickets {t['hit']}/{t['days']} "
                     f"({_pct(t['hit_rate'])}), {t['one_leg_short']} lost by exactly one leg"
                     + (f", {t['unresolved_days']} day(s) not countable (unresolved leg)" if t["unresolved_days"] else ""))
    for title, key in (("by sport", "by_sport"), ("by market", "by_market"), ("by quality", "by_quality"),
                       ("by confidence", "by_confidence"), ("by books", "by_books")):
        if rep[key]:
            lines.append(f"  {title}:")
            for b in rep[key]:
                lines.append(f"    {b['key']:<22} {b['wins']}/{b['decided']}  hit {_pct(b['hit_rate'])}  "
                             f"predicted {_pct(b['avg_predicted_prob'])}")
    if rep["clv"]["n"]:
        lines.append(f"  closing line value: {rep['clv']['mean']:+.3f} over {rep['clv']['n']} legs")
    else:
        lines.append("  closing line value: no closing prices captured yet (picks.closing_price / picks.clv are ready)")
    return "\n".join(lines)
