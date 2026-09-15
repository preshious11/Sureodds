"""Message templates. Deliberately terse: picks, odds, probability, stats."""
from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional

from predbot.bot.telegram import esc
from predbot.util.timeutil import to_local

SPORT_ICON = {"football": "⚽", "basketball": "🏀", "american_football": "🏈", "hockey": "🏒",
              "baseball": "⚾", "tennis": "🎾", "mma": "🥊", "handball": "🤾", "other": "🎯"}
RESULT_ICON = {"won": "✅", "lost": "❌", "push": "↩️", "half_won": "✅½", "half_lost": "❌½",
               "void": "⚪", "pending": "⏳"}


def _hm(dt: datetime, tz: str) -> str:
    return to_local(dt, tz).strftime("%H:%M")


def _date(dt: datetime, tz: str) -> str:
    return to_local(dt, tz).strftime("%a %d %b")


def picks_message(top, daily, day: datetime, tz: str, stats_line: Optional[str]) -> str:
    lines: List[str] = []
    lines.append(f"<b>🏆 TOP PICKS · {_date(day, tz)}</b>")
    if not top:
        lines.append("No selection met the top-pick bar today.")
    for i, s in enumerate(top, 1):
        e = s.event
        lines.append(f"{i}. {SPORT_ICON.get(e.sport, '🎯')} <b>{esc(e.home)} vs {esc(e.away)}</b> · {esc(e.league)} · {_hm(e.start, tz)}")
        lines.append(f"   ▸ <b>{esc(s.label)}</b> @ {s.price:.2f} · {round(s.p_model * 100)}%")
    lines.append("")
    lines.append(f"<b>📋 DAILY PICKS ({len(daily)}) · {_date(day, tz)}</b>")
    if not daily:
        lines.append("No further selections cleared the quality bar today.")
    for s in daily:
        e = s.event
        lines.append(f"{SPORT_ICON.get(e.sport, '🎯')} {_hm(e.start, tz)} {esc(e.home)} vs {esc(e.away)} · "
                     f"<b>{esc(s.label)}</b> @ {s.price:.2f} · {round(s.p_model * 100)}%")
    if stats_line:
        lines.append("")
        lines.append(stats_line)
    return "\n".join(lines)


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


def recap_message(day_label: str, rows: List, s: Dict) -> str:
    lines = [f"<b>📊 RESULTS · {day_label}</b>"]
    t, d = s["yesterday_top"], s["yesterday"]
    lines.append(f"Top picks: {t['won']}W-{t['lost']}L" + (f"-{t['push']}P" if t['push'] else ""))
    lines.append(f"Daily picks: {d['won'] - t['won']}W-{d['lost'] - t['lost']}L" + (f"-{d['push'] - t['push']}P" if (d['push'] - t['push']) else ""))
    if d.get("best_win"):
        lines.append(f"Best win: {esc(d['best_win']['label'])} @ {d['best_win']['price']:.2f}")
    seven = s["last7"]
    lines.append(f"Last 7 days: {seven['won']}W-{seven['lost']}L ({_pct(seven['won'], seven['lost'])}) · ROI {seven['roi']:+.1f}%")
    if s.get("streak"):
        lines.append(f"Streak: {s['streak']}")
    lines.append("")
    for r in rows:
        icon = RESULT_ICON.get(r["status"], "⏳")
        tag = "🏆 " if r["tier"] == "top" else ""
        lines.append(f"{icon} {tag}{esc(r['home'])} vs {esc(r['away'])} · {esc(r['label'])} @ {r['price']:.2f}")
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
