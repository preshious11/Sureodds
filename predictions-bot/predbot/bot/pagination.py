"""In-memory pagination + inline keyboard construction.

The full day's selection set lives in ONE cache entry (write-once-per-day).
Paging therefore never touches an external API: it slices the cached array and
edits the existing Telegram message via callback buttons.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from predbot.bot.format import (HEADING_DAILY, HEADING_TOP, PickLine, outcome_label,
                                pick_lines, picks_card, sport_name, stats_message)
from predbot.providers.base import MarketLine
from predbot.cache.models import (DailySnapshot, PickRecord, VIEW_DAILY, VIEW_STATS,
                                  VIEW_TOP)
from predbot.util.timeutil import parse_iso, to_local, utcnow

log = logging.getLogger(__name__)

PAGE_SIZE_DEFAULT = 5
CALLBACK_PREFIX = "pg"
CALLBACK_NOOP = "noop"

VIEW_TITLES = {VIEW_TOP: "🏆 TOP PICKS", VIEW_DAILY: "📋 DAILY PICKS", VIEW_STATS: "📊 STATS"}

#: The three interactive views: emoji, button label, the command that opens the
#: same view, and a one-line description.  One definition shared by the /start
#: menu, the welcome screen and the pagination switch row, so a tap and a type
#: are guaranteed to be the same action.
VIEW_ICON = {VIEW_TOP: "🏆", VIEW_DAILY: "📋", VIEW_STATS: "📊"}
VIEW_LABEL = {VIEW_TOP: "Top Picks", VIEW_DAILY: "Daily Picks", VIEW_STATS: "Stats"}
VIEW_COMMAND = {VIEW_TOP: "/top", VIEW_DAILY: "/daily", VIEW_STATS: "/stats"}
VIEW_DESC = {VIEW_TOP: "Top picks of the day",
             VIEW_DAILY: "Daily picks · Accumulator slip",
             VIEW_STATS: "See prediction results"}


def view_button_label(view: str) -> str:
    """Inline-button label for a view."""
    return f"{VIEW_ICON[view]} {VIEW_LABEL[view]}"


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
def _kickoff_hhmm(pick: PickRecord, tz: str, user_tz: Optional[str] = None) -> str:
    """Kickoff in the subscriber's own timezone, falling back to the server's."""
    start = parse_iso(pick.kickoff_utc)
    if not start:
        return "--:--"
    display_tz = user_tz or tz
    local = to_local(start, display_tz)
    tz_abbr = local.tzname() or display_tz
    return f"{local.strftime('%I:%M %p').lstrip('0')} {tz_abbr}"


def _pick_line(pick: PickRecord, tz: str, user_tz: Optional[str] = None) -> PickLine:
    """A cached pick as the shared PickLine the broadcast card also renders.

    Nothing is escaped here - ``format.pick_lines`` escapes every field once.
    """
    ml = MarketLine(pick.market, pick.selection, pick.price, line=pick.line)
    return PickLine(sport=sport_name(pick.sport),
                    match=f"{pick.home} vs {pick.away}",
                    outcome=outcome_label(pick.label, ml, pick.home, pick.away, pick.sport),
                    league=pick.league or sport_name(pick.sport),
                    confidence=pick.prob,
                    when=_kickoff_hhmm(pick, tz, user_tz),
                    price=pick.price)


def render_pick(pick: PickRecord, tz: str, user_tz: Optional[str] = None) -> str:
    """One pick's six-line block - the same renderer the broadcast card uses."""
    return "\n".join(pick_lines(_pick_line(pick, tz, user_tz)))


def build_keyboard(view: str, page: int, pages: int) -> Dict:
    """InlineKeyboardMarkup dict: arrows (when useful) + view-switch row."""
    row: List[Dict[str, str]] = []
    if pages > 1:
        if page > 0:
            row.append({"text": "◀️", "callback_data": encode_callback(view, page - 1)})
        row.append({"text": f"{page + 1}/{pages}", "callback_data": CALLBACK_NOOP})
        if page < pages - 1:
            row.append({"text": "▶️", "callback_data": encode_callback(view, page + 1)})
    switch = [{"text": view_button_label(v), "callback_data": encode_callback(v, 0)}
              for v in (VIEW_TOP, VIEW_DAILY, VIEW_STATS)]
    return {"inline_keyboard": ([row] if row else []) + [switch]}


def render_page(snapshot: DailySnapshot, view: str, page: int, size: int,
                tz: str, user_tz: Optional[str] = None) -> Tuple[str, Dict]:
    items = snapshot.view_items(view)
    if not items and snapshot.status == 'PARTIAL':
        return ('Predictions are temporarily unavailable. Please try again later.',
                build_keyboard(view, 0, 1))
    pages = total_pages(len(items), size)
    page = clamp_page(page, len(items), size)
    chunk = slice_page(items, page, size)

    text = picks_card([_pick_line(p, tz, user_tz) for p in chunk], snapshot.date,
                      heading=HEADING_TOP if view == VIEW_TOP else HEADING_DAILY,
                      total_prices=[p.price for p in items])

    if pages > 1:
        text += f"\n<i>Page {page + 1}/{pages}</i>"

    return text, build_keyboard(view, page, pages)


def render_stats(snapshot: DailySnapshot, db=None, tz: str = "UTC",
                 now: Optional[datetime] = None,
                 user_tz: Optional[str] = None) -> Tuple[str, Dict]:
    """The 📊 /stats card - the rolling record the /stats command sends.

    One renderer for the command and the button: with a Database handle the
    record is computed fresh (7/30-day and all-time W-L, ROI, best picks,
    calibration, streak), otherwise it falls back to the cached snapshot's
    stats.  The richer ``SUREODDS — YESTERDAY`` card stays a broadcast (the
    scheduled recap) instead of becoming a second /stats implementation.
    """
    raw = None
    display_tz = user_tz or tz          # "today" has to mean the subscriber's today
    if db is not None:
        try:
            from predbot.jobs import stats as stats_mod

            raw = stats_mod.compute(db, to_local(now or utcnow(), display_tz))
        except Exception as e:  # noqa: BLE001
            log.warning("stats view DB render failed, using snapshot: %s", e)
    if raw is None:
        if snapshot.stats is None:
            return "📊 STATS\nNo settled picks yet.", build_keyboard(VIEW_STATS, 0, 1)
        raw = snapshot.stats.to_raw()
    return stats_message(raw), build_keyboard(VIEW_STATS, 0, 1)


def render_view(snapshot: DailySnapshot, view: str, page: int, size: int,
                tz: str, db=None, user_tz: Optional[str] = None) -> Tuple[str, Dict]:
    """Entry point used by both the /daily command and callback navigation."""
    if view == VIEW_STATS:
        return render_stats(snapshot, db=db, tz=tz, user_tz=user_tz)
    return render_page(snapshot, view, page, size, tz, user_tz)
