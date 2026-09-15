"""In-memory pagination + inline keyboard construction.

The full day's selection set lives in ONE cache entry (write-once-per-day).
Paging therefore never touches an external API: it slices the cached array and
edits the existing Telegram message via callback buttons.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from predbot.bot.format import SPORT_ICON, stats_message
from predbot.bot.telegram import esc
from predbot.cache.models import (DailySnapshot, PickRecord, VIEW_DAILY, VIEW_STATS,
                                  VIEW_TOP)
from predbot.util.timeutil import parse_iso, to_local

PAGE_SIZE_DEFAULT = 5
CALLBACK_PREFIX = "pg"
CALLBACK_NOOP = "noop"

VIEW_TITLES = {VIEW_TOP: "🏆 TOP PICKS", VIEW_DAILY: "📋 DAILY PICKS", VIEW_STATS: "📊 STATS"}


# --------------------------------------------------------------- payload codec
def encode_callback(view: str, page: int) -> str:
    """callback_data payload (stays far below Telegram's 64-byte limit)."""
    return f"{CALLBACK_PREFIX}|{view}|{max(0, int(page))}"


def decode_callback(data: Optional[str]) -> Optional[Tuple[str, int]]:
    """Parse a button payload; returns None for anything malformed/unknown."""
    if not data:
        return None
    parts = data.split("|")
    if len(parts) != 3 or parts[0] != CALLBACK_PREFIX or parts[1] not in VIEW_TITLES:
        return None
    try:
        page = int(parts[2])
    except ValueError:
        return None
    return parts[1], max(0, page)


# ------------------------------------------------------------------- slicing
def total_pages(item_count: int, size: int) -> int:
    if item_count <= 0 or size <= 0:
        return 1
    return (item_count + size - 1) // size


def clamp_page(page: int, item_count: int, size: int) -> int:
    return min(max(0, page), total_pages(item_count, size) - 1)


def slice_page(items: List[PickRecord], page: int, size: int) -> List[PickRecord]:
    page = clamp_page(page, len(items), size)
    return items[page * size:(page + 1) * size]


# ----------------------------------------------------------------- rendering
def _kickoff_hhmm(pick: PickRecord, tz: str) -> str:
    start = parse_iso(pick.kickoff_utc)
    return to_local(start, tz).strftime("%H:%M") if start else "--:--"


def render_pick(pick: PickRecord, tz: str) -> str:
    icon = SPORT_ICON.get(pick.sport, "🎯")
    trophy = "🏆 " if pick.tier == "top" else ""
    status = "" if pick.status == "pending" else f" {esc(pick.status)}"
    return (f"{trophy}{icon} {_kickoff_hhmm(pick, tz)} "
            f"<b>{esc(pick.home)} vs {esc(pick.away)}</b>\n"
            f"      ▸ <b>{esc(pick.label)}</b> @ {pick.price:.2f} · "
            f"{round(pick.prob * 100)}%{status}")


def build_keyboard(view: str, page: int, pages: int) -> Dict:
    """InlineKeyboardMarkup dict: arrows (when useful) + view-switch row."""
    row: List[Dict[str, str]] = []
    if pages > 1:
        if page > 0:
            row.append({"text": "◀️", "callback_data": encode_callback(view, page - 1)})
        row.append({"text": f"{page + 1}/{pages}", "callback_data": CALLBACK_NOOP})
        if page < pages - 1:
            row.append({"text": "▶️", "callback_data": encode_callback(view, page + 1)})
    switch = [
        {"text": "🏆 Top", "callback_data": encode_callback(VIEW_TOP, 0)},
        {"text": "📋 Daily", "callback_data": encode_callback(VIEW_DAILY, 0)},
        {"text": "📊 Stats", "callback_data": encode_callback(VIEW_STATS, 0)},
    ]
    return {"inline_keyboard": ([row] if row else []) + [switch]}


def render_page(snapshot: DailySnapshot, view: str, page: int, size: int,
                tz: str) -> Tuple[str, Dict]:
    items = snapshot.view_items(view)
    pages = total_pages(len(items), size)
    page = clamp_page(page, len(items), size)
    chunk = slice_page(items, page, size)
    lines = [f"<b>{VIEW_TITLES[view]} · {snapshot.date}</b>",
             f"<i>{len(items)} picks · page {page + 1}/{pages}</i>", ""]
    if not chunk:
        lines.append("No picks available.")
    lines.extend(render_pick(p, tz) for p in chunk)
    return "\n".join(lines), build_keyboard(view, page, pages)


def render_stats(snapshot: DailySnapshot) -> Tuple[str, Dict]:
    if snapshot.stats is None:
        text = "<b>📊 STATS</b>\nNo stats recorded yet."
    else:
        text = stats_message(snapshot.stats.to_raw())
        text += (f"\n\n<i>{snapshot.total_picks()} picks today · snapshot "
                 f"{snapshot.generated_at or 'n/a'}</i>")
    return text, build_keyboard(VIEW_STATS, 0, 1)


def render_view(snapshot: DailySnapshot, view: str, page: int, size: int,
                tz: str) -> Tuple[str, Dict]:
    """Entry point used by both the /today command and callback navigation."""
    if view == VIEW_STATS:
        return render_stats(snapshot)
    return render_page(snapshot, view, page, size, tz)
