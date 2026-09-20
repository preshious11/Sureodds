"""In-memory pagination + inline keyboard construction.

The full day's selection set lives in ONE cache entry (write-once-per-day).
Paging therefore never touches an external API: it slices the cached array and
edits the existing Telegram message via callback buttons.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from predbot.bot.format import (HEADING_DAILY, HEADING_TOP, NO_RESULTS_YET, PickLine,
                                format_outcome, generated_line, odds_as_of_label,
                                pick_line_from_record, pick_lines, picks_card, stats_message,
                                yesterday_header, yesterday_pick_line)
from predbot.cache.models import (DailySnapshot, PickRecord, VIEW_DAILY, VIEW_STATS,
                                  VIEW_TOP, VIEW_YESTERDAY)
from predbot.util.timeutil import parse_iso, to_local, utcnow

log = logging.getLogger(__name__)

PAGE_SIZE_DEFAULT = 5
CALLBACK_PREFIX = "pg"
CALLBACK_NOOP = "noop"

VIEW_TITLES = {VIEW_TOP: "🏆 TOP PICKS", VIEW_DAILY: "📋 DAILY PICKS", VIEW_STATS: "📊 STATS",
               VIEW_YESTERDAY: "🗓 YESTERDAY"}

#: The three interactive views: emoji, button label, the command that opens the
#: same view, and a one-line description.  One definition shared by the /start
#: menu, the welcome screen and the pagination switch row, so a tap and a type
#: are guaranteed to be the same action.
VIEW_ICON = {VIEW_TOP: "🏆", VIEW_DAILY: "📋", VIEW_STATS: "📊", VIEW_YESTERDAY: "🗓"}
VIEW_LABEL = {VIEW_TOP: "Top Picks", VIEW_DAILY: "Daily Picks", VIEW_STATS: "Stats", VIEW_YESTERDAY: "Yesterday"}
VIEW_COMMAND = {VIEW_TOP: "/top", VIEW_DAILY: "/daily", VIEW_STATS: "/stats", VIEW_YESTERDAY: "/yesterday"}
VIEW_DESC = {VIEW_TOP: "Top picks of the day",
             VIEW_DAILY: "Daily picks · Accumulator slip",
             VIEW_STATS: "See prediction results",
             VIEW_YESTERDAY: "Every pick from yesterday, with its result"}
#: the buttons under every card, two to a row
SWITCH_ROWS = ((VIEW_TOP, VIEW_DAILY), (VIEW_STATS, VIEW_YESTERDAY))


def view_button_label(view: str) -> str:
    """Inline-button label for a view."""
    return f"{VIEW_ICON[view]} {VIEW_LABEL[view]}"


# --------------------------------------------------------------- payload codec
def encode_callback(view: str, page: int, day: Optional[str] = None) -> str:
    """callback_data payload (far below Telegram's 64-byte limit). A page-turn button carries the day of the
    card it sits on (``pg|daily|1|20260920``), so an old message keeps turning ITS day's pages; a view-switch
    button carries none (``pg|daily|0``) and opens the current day."""
    base = f"{CALLBACK_PREFIX}|{view}|{max(0, int(page))}"
    return f"{base}|{str(day).replace('-', '')}" if day else base


def parse_callback(data: Optional[str]) -> Optional[Tuple[str, int, Optional[str]]]:
    """Parse a button payload into (view, page, day 'YYYY-MM-DD' or None); None for anything malformed/unknown."""
    if not data:
        return None
    parts = data.split("|")
    if len(parts) not in (3, 4) or parts[0] != CALLBACK_PREFIX or parts[1] not in VIEW_TITLES:
        return None
    try:
        page = int(parts[2])
    except ValueError:
        return None
    day = None
    if len(parts) == 4:
        raw = parts[3]
        if len(raw) != 8 or not raw.isdigit():
            return None
        day = f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    return parts[1], max(0, page), day


def decode_callback(data: Optional[str]) -> Optional[Tuple[str, int]]:
    """(view, page) of a button payload; None for anything malformed/unknown."""
    parsed = parse_callback(data)
    return (parsed[0], parsed[1]) if parsed else None


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


# pick_line_from_record is imported from predbot.bot.format above - the ONE
# canonical pick-card builder, shared by every view and the broadcast card.


def _pick_line(pick: PickRecord, tz: str, user_tz: Optional[str] = None) -> PickLine:
    """Backwards-compatible alias of format.pick_line_from_record."""
    return pick_line_from_record(pick, tz, user_tz)


def render_pick(pick: PickRecord, tz: str, user_tz: Optional[str] = None) -> str:
    """One pick's six-line block - the same renderer the broadcast card uses."""
    return "\n".join(pick_lines(pick_line_from_record(pick, tz, user_tz)))


def build_keyboard(view: str, page: int, pages: int, day: Optional[str] = None) -> Dict:
    """InlineKeyboardMarkup dict: arrows (when useful) + view-switch row. The arrows carry ``day``."""
    row: List[Dict[str, str]] = []
    if pages > 1:
        if page > 0:
            row.append({"text": "◀️", "callback_data": encode_callback(view, page - 1, day)})
        row.append({"text": f"{page + 1}/{pages}", "callback_data": CALLBACK_NOOP})
        if page < pages - 1:
            row.append({"text": "▶️", "callback_data": encode_callback(view, page + 1, day)})
    switch = [[{"text": view_button_label(v), "callback_data": encode_callback(v, 0)} for v in views]
              for views in SWITCH_ROWS]
    return {"inline_keyboard": ([row] if row else []) + switch}

def _retry_hint(snapshot: DailySnapshot, tz: str) -> str:
    """' Retrying around 11:00 Africa/Lagos.' when a partial refresh is scheduled."""
    raw = (snapshot.generation or {}).get("retry_after")
    at = parse_iso(raw) if raw else None
    return f" Retrying around {to_local(at, tz).strftime('%H:%M')} {tz}." if at else ""


def _retry_until(snapshot: DailySnapshot) -> str:
    until = (snapshot.generation or {}).get("retry_until")
    return f" until {until}" if until else ""


def empty_view_message(snapshot: DailySnapshot, view: str, tz: str = "UTC") -> str:
    if (snapshot.generation or {}).get("reason") == "before_post_time":
        return f"Today's picks are ready at {snapshot.generation.get('ready_at', 'the scheduled time')}."
    if (snapshot.generation or {}).get("reason") == "generating":
        return "Today's picks are being generated — try again in a minute."
    """Why a view has nothing to show - the real reasons, kept distinct.

    * an empty tier of an otherwise fine card: top picks are a quality bar, not a
      right, so the card's own "no selection cleared" line is the honest answer
    * provider data is incomplete: say that (with the retry time) instead of the
      old "temporarily unavailable" that implied the bot itself was down
    * a PARTIAL day with zero picks (e.g. every provider quota-blocked) gets the
      assembly message, not the quality-bar line
    """
    if snapshot.status == 'PARTIAL' and not snapshot.total_picks():
        return ("<b>Today's predictions are still being assembled</b> - a data "
                f"source did not answer.{_retry_hint(snapshot, tz)}\n"
                f"The bot retries automatically{_retry_until(snapshot)}; /daily shows the card as "
                "soon as it is complete.")
    return picks_card([], snapshot.date, heading="Top" if view == VIEW_TOP else "Today's")



def render_page(snapshot: DailySnapshot, view: str, page: int, size: int,
                tz: str, user_tz: Optional[str] = None,
                now: Optional[datetime] = None) -> Tuple[str, Dict]:
    """One page of a ticket view: cards for the visible picks, then totals for
    the section those picks come from (Top or the whole Daily ticket).

    ONE source of truth: ``items`` is the section's selection list, the cards
    are its visible slice, and the combined odds are the product of EXACTLY
    ``items`` - the same list the cards were sliced from. Nothing is priced
    from a second, hidden collection.
    """
    items = snapshot.view_items(view)
    # A pick with unusable odds (<=1.0, non-finite) would make combined_odds()
    # raise and blank the whole page; drop it the same way an ambiguous
    # outcome is dropped, instead of failing the entire view.
    items = [p for p in items if format_outcome(p) and math.isfinite(p.price) and p.price > 1.0]
    if not items:
        return empty_view_message(snapshot, view, tz), build_keyboard(view, 0, 1)
    pages = total_pages(len(items), size)
    page = clamp_page(page, len(items), size)
    chunk = slice_page(items, page, size)

    text = picks_card([pick_line_from_record(p, tz, user_tz, now) for p in chunk],
                      snapshot.date,
                      heading=HEADING_TOP if view == VIEW_TOP else HEADING_DAILY,
                      total_prices=[p.price for p in items],
                      generated=generated_line(snapshot.generated_at, user_tz or tz),
                      odds_as_of=odds_as_of_label(snapshot.generated_at, user_tz or tz))

    if pages > 1:
        text += f"\n<i>Page {page + 1}/{pages}</i>"

    return text, build_keyboard(view, page, pages, snapshot.date)


def render_stats(snapshot: DailySnapshot, db=None, tz: str = "UTC",
                 now: Optional[datetime] = None,
                 user_tz: Optional[str] = None) -> Tuple[str, Dict]:
    """The stats card - the one the /stats command and the Stats button send.

    With a Database handle the numbers are computed fresh, on the READER's local dates; without one the cached
    snapshot's stats are used (older shape: read as a settled day)."""
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
            return NO_RESULTS_YET, build_keyboard(VIEW_STATS, 0, 1)
        raw = snapshot.stats.to_raw()
    return stats_message(raw), build_keyboard(VIEW_STATS, 0, 1)


def render_yesterday(db=None, tz: str = "UTC", page: int = 0, size: int = PAGE_SIZE_DEFAULT,
                     day: Optional[str] = None, now: Optional[datetime] = None,
                     user_tz: Optional[str] = None) -> Tuple[str, Dict]:
    """/yesterday - the audit trail: EVERY pick issued that day, Top legs first under 🔥 then Daily under 📋, in
    the order the cards used, each with its result mark (none while unsettled). Its header numbers come from the
    same helper as the /stats 'Yesterday' block, so marks can be counted against the card."""
    from predbot.jobs import stats as stats_mod

    if db is None:
        return NO_RESULTS_YET, build_keyboard(VIEW_YESTERDAY, 0, 1)
    display_tz = user_tz or tz
    day = day or (to_local(now or utcnow(), display_tz) - timedelta(days=1)).strftime("%Y-%m-%d")
    rows = stats_mod.ordered(db.picks_for_date(day))
    header = yesterday_header(day, stats_mod.day_block(rows))
    if not rows:
        return "\n".join(header), build_keyboard(VIEW_YESTERDAY, 0, 1, day)
    pages = total_pages(len(rows), size)
    page = clamp_page(page, len(rows), size)
    lines = list(header)
    for tier, title in (("top", "🔥 Top"), ("daily", "📋 Daily")):
        chunk = [r for r in rows[page * size:(page + 1) * size] if (r["tier"] == "top") == (tier == "top")]
        if chunk:
            lines += ["", title] + [yesterday_pick_line(r, stats_mod.outcome(r)) for r in chunk]
    if pages > 1:
        lines += ["", f"Page {page + 1} of {pages}"]
    return "\n".join(lines), build_keyboard(VIEW_YESTERDAY, page, pages, day)


def render_view(snapshot: DailySnapshot, view: str, page: int, size: int,
                tz: str, db=None, user_tz: Optional[str] = None,
                now: Optional[datetime] = None, day: Optional[str] = None) -> Tuple[str, Dict]:
    """Entry point used by both the /daily command and callback navigation."""
    if view == VIEW_STATS:
        return render_stats(snapshot, db=db, tz=tz, user_tz=user_tz)
    if view == VIEW_YESTERDAY:
        return render_yesterday(db, tz, page, size, day, now, user_tz)
    return render_page(snapshot, view, page, size, tz, user_tz, now)
