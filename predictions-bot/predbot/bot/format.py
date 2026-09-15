"""Message templates. Deliberately terse: picks, odds, probability, stats."""
from __future__ import annotations

from datetime import datetime
from typing import Dict, Iterable, List, NamedTuple, Optional

from predbot.bot.telegram import esc
from predbot.markets.catalog import short_selection
from predbot.util.timeutil import to_local

BRAND = "SureOdds"
SPORT_ICON = {"football": "⚽", "basketball": "🏀", "american_football": "🏈", "hockey": "🏒",
              "baseball": "⚾", "tennis": "🎾", "mma": "🥊", "handball": "🤾", "other": "🎯"}
SPORT_LABEL = {"football": "Football", "basketball": "Basketball", "american_football": "American Football",
               "hockey": "Hockey", "baseball": "Baseball", "tennis": "Tennis", "mma": "MMA",
               "handball": "Handball", "other": "Other"}
RESULT_ICON = {"won": "✅", "lost": "❌", "push": "↩️", "half_won": "✅½", "half_lost": "❌½",
               "void": "➖", "pending": "⏳"}


class PickLine(NamedTuple):
    """One prediction exactly as the daily post renders it."""

    match: str             # "Chelsea vs Man United"
    outcome: str           # short code, e.g. "1X", "Over 2.5 Goals"
    league: str
    confidence: float      # model probability, 0..1
    when: str              # local kickoff with its zone, e.g. "19:45 WAT"
    price: float


def kickoff_label(start: Optional[datetime], tz: str) -> str:
    """Local kickoff with its zone abbreviation ("19:45 WAT"); "--:--" when unknown."""
    if start is None:
        return "--:--"
    local = to_local(start, tz)
    return f"{local.strftime('%H:%M')} {local.tzname() or tz}"


def combined_odds(prices: Iterable[float]) -> float:
    """Accumulator price: every listed pick has to land for the ticket to land.

    Decimal odds are always MULTIPLIED (1.50 x 2.00 = 3.00), never added.
    Invalid odds (non-numeric, or <= 1.0) raise ValueError - a ticket made of
    bad prices would be meaningless, so it fails loudly instead.
    """
    total = 1.0
    for p in prices:
        try:
            p = float(p)
        except (TypeError, ValueError):
            raise ValueError(f"invalid odds value: {p!r}")
        if not p > 1.0 or p != p:  # also rejects NaN
            raise ValueError(f"odds must be > 1.0, got {p!r}")
        total *= p
    return total


def pick_lines(p: PickLine) -> List[str]:
    """The four lines one pick occupies: match, outcome, confidence | kickoff, odds."""
    match = f"{p.match} ({p.league})" if p.league else p.match
    return [esc(match), esc(p.outcome),
            f"Confidence: {round(p.confidence * 100)}% | {p.when}",
            f"Odds: {p.price:.2f}"]


def picks_card(picks: List[PickLine], date_label: str, stats_line: Optional[str] = None,
               heading: str = "📋 Top Picks for Today") -> str:
    """The daily predictions post: one block per pick, then the combined odds.

    Top picks are listed first, then the rest of the day's selections, so the
    card reads as a single list of up to ``picks`` entries.
    """
    lines = [f"{heading} {date_label}", ""]
    if not picks:
        lines.append("No selection cleared the quality bar today.")
    for p in picks:
        lines.extend(pick_lines(p))
        lines.append("")
    if picks:
        lines.append(f"Combined Odds: {combined_odds([p.price for p in picks]):.4f}")
    if stats_line:
        lines.append("")
        lines.append(stats_line)
    return "\n".join(lines)


def picks_message(top, daily, day: datetime, tz: str, stats_line: Optional[str]) -> str:
    """Channel post for the day's selections: top picks first, then the daily ones."""
    picks: List[PickLine] = []
    for s in list(top) + list(daily):
        e = s.event
        picks.append(PickLine(match=f"{e.home} vs {e.away}",
                              outcome=short_selection(s.line, e.home, e.away, e.sport),
                              league=e.league or sport_name(e.sport),
                              confidence=s.p_model, when=kickoff_label(e.start, tz),
                              price=s.price))
    return picks_card(picks, to_local(day, tz).strftime("%Y-%m-%d"), stats_line)


def _pct(w: int, l: int) -> str:
    n = w + l
    return f"{round(100 * w / n)}%" if n else "–"


def stats_line(s: Dict) -> str:
    seven = s["last7"]
    parts = [f"📊 7d: {seven['won']}W-{seven['lost']}L ({_pct(seven['won'], seven['lost'])}) · ROI {seven['roi']:+.1f}%"]
    y = s.get("yesterday")
    if y and (y["won"] + y["lost"]):
        parts.append(f"Yesterday {y['won']}W-{y['lost']}L")
        if y.get("best_win"):
            parts.append(f"Best win: {esc(y['best_win']['label'])} @ {y['best_win']['price']:.2f}")
    return " · ".join(parts)


def _hit_rate(won: int, lost: int) -> str:
    """One-decimal hit rate; an em dash when nothing is decided yet."""
    n = won + lost
    return f"{100.0 * won / n:.1f}%" if n else "–"


def sport_name(sport: str) -> str:
    return SPORT_LABEL.get(sport, sport.replace("_", " ").title())


def recap_message(day_label: str, sport_breakdown: List[Dict], s: Dict) -> str:
    """Yesterday's results card: record, hit rate, per-sport split, 7-day form, best pick.

    ``sport_breakdown`` is ``predbot.jobs.stats.by_sport()`` output; ``s`` is
    ``predbot.jobs.stats.compute()`` output (or StatsSummary.to_raw(), in which
    case void/push simply render as 0).
    """
    y, seven = s["yesterday"], s["last7"]
    lines = [f"📊 {BRAND.upper()} — {day_label}", ""]
    lines.append(f"✅ Wins: {y['won']}")
    lines.append(f"❌ Losses: {y['lost']}")
    lines.append(f"➖ Void: {y.get('void', 0)}")
    if y.get("push"):
        lines.append(f"↩️ Push: {y['push']}")
    lines.append("")
    lines.append(f"🎯 Hit Rate: {_hit_rate(y['won'], y['lost'])}")
    if sport_breakdown:
        lines.append("")
        for b in sport_breakdown:
            icon = SPORT_ICON.get(b["sport"], SPORT_ICON["other"])
            lines.append(f"{icon} {sport_name(b['sport'])}: {b['won']}/{b['n']}")
    lines.append("")
    lines.append("📈 LAST 7 DAYS")
    lines.append(f"Wins: {seven['won']}")
    lines.append(f"Losses: {seven['lost']}")
    lines.append(f"Hit Rate: {_hit_rate(seven['won'], seven['lost'])}")
    best = y.get("best_win")
    if best:
        lines.append("")
        lines.append("🔥 BEST PICK")
        lines.append(f"{esc(best['label'])} @ {best['price']:.2f} ✅")
    return "\n".join(lines)


def stats_message(s: Dict) -> str:
    seven, thirty, alltime = s["last7"], s["last30"], s["all"]
    lines = ["<b>📊 STATS</b>"]
    for name, blk in (("Last 7 days", seven), ("Last 30 days", thirty), ("All time", alltime)):
        lines.append(f"{name}: {blk['won']}W-{blk['lost']}L" + (f"-{blk['push']}P" if blk['push'] else "")
                     + f" ({_pct(blk['won'], blk['lost'])}) · ROI {blk['roi']:+.1f}%")
    t = s["top_all"]
    lines.append(f"Top picks all time: {t['won']}W-{t['lost']}L ({_pct(t['won'], t['lost'])}) · ROI {t['roi']:+.1f}%")
    if s.get("brier") is not None:
        lines.append(f"Calibration (Brier): {s['brier']:.3f}")
    if s.get("streak"):
        lines.append(f"Streak: {s['streak']}")
    return "\n".join(lines)
