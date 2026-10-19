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

from predbot.bot.format import (DAILY_TITLE, NO_RESULTS_YET, SPORT_ICON, SPORT_LABEL, TOP_TITLE, PickLine,
                                by_kickoff, collection_stats_message, day_label, fixture_block, format_outcome, pick_line_from_record, pick_lines,
                                picks_card, stats_message, yesterday_header, yesterday_pick_line,
                                zone_label)
from predbot.cache.models import (DailySnapshot, PickRecord, SPORT_VIEW_PREFIX, VIEW_DAILY, VIEW_STATS,
                                  VIEW_ALL, VIEW_TOP, VIEW_YESTERDAY, sport_bucket)
from predbot.selection.representatives import FAMILY_ORDER
from predbot.util.timeutil import parse_iso, to_local, utcnow

log = logging.getLogger(__name__)

PAGE_SIZE_DEFAULT = 5
CALLBACK_PREFIX = "pg"
CALLBACK_NOOP = "noop"

VIEW_TITLES = {VIEW_TOP: "🔥 BET OF THE DAY", VIEW_DAILY: "📋 DAILY PICKS", VIEW_STATS: "📊 STATS",
               VIEW_YESTERDAY: "🗓 YESTERDAY"}

#: The three interactive views: emoji, button label, the command that opens the
#: same view, and a one-line description.  One definition shared by the /start
#: menu, the welcome screen and the pagination switch row, so a tap and a type
#: are guaranteed to be the same action.
VIEW_ICON = {VIEW_TOP: "🔥", VIEW_DAILY: "📋", VIEW_STATS: "📊", VIEW_YESTERDAY: "🗓"}
VIEW_LABEL = {VIEW_TOP: "Bet of the Day", VIEW_DAILY: "Daily Picks", VIEW_STATS: "Stats", VIEW_YESTERDAY: "Yesterday"}
VIEW_COMMAND = {VIEW_TOP: "/top", VIEW_DAILY: "/daily", VIEW_STATS: "/stats", VIEW_YESTERDAY: "/yesterday"}
VIEW_DESC = {VIEW_TOP: "Bet of the Day",
             VIEW_DAILY: "Daily picks · Accumulator slip",
             VIEW_STATS: "See prediction results",
             VIEW_YESTERDAY: "Every pick from yesterday, with its result"}
#: the buttons under every card, two to a row
SWITCH_ROWS = ((VIEW_TOP, VIEW_DAILY), (VIEW_STATS, VIEW_YESTERDAY))
ALL_TITLE = '📚 ALL PREDICTIONS'               # the Top / Daily title style: icon, caps, then the day
VIEW_TITLES[VIEW_ALL] = 'ALL PREDICTIONS'
VIEW_ICON[VIEW_ALL], VIEW_LABEL[VIEW_ALL], VIEW_COMMAND[VIEW_ALL] = '📚', 'All Predictions', '/all'
VIEW_DESC[VIEW_ALL] = 'Every model prediction, by match'
SWITCH_ROWS = ((VIEW_ALL,), (VIEW_TOP, VIEW_DAILY), (VIEW_STATS, VIEW_YESTERDAY))
for _sport, _name in SPORT_LABEL.items():
    _view = SPORT_VIEW_PREFIX + _sport
    VIEW_TITLES[_view] = 'ALL PREDICTIONS'
    VIEW_LABEL[_view], VIEW_ICON[_view] = _name, SPORT_ICON.get(_sport, SPORT_ICON['other'])
for _collection in ('all', 'top', 'daily'):
    for _prefix in ('stats_', 'results_'):
        _view = _prefix + _collection
        VIEW_TITLES[_view] = VIEW_LABEL[_collection]
        VIEW_LABEL[_view], VIEW_ICON[_view] = VIEW_LABEL[_collection], '📊'


def is_prediction_view(view: str) -> bool:
    """Views that read the day's stored predictions (page turns and sport taps never reach a provider)."""
    return view in (VIEW_ALL, VIEW_TOP, VIEW_DAILY) or view.startswith(SPORT_VIEW_PREFIX)


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
# pick_line_from_record is imported from predbot.bot.format above - the ONE
# canonical pick-card builder, shared by every view and the broadcast card.


def _pick_line(pick: PickRecord, tz: str, user_tz: Optional[str] = None) -> PickLine:
    """Backwards-compatible alias of format.pick_line_from_record."""
    return pick_line_from_record(pick, tz, user_tz)


def render_pick(pick: PickRecord, tz: str, user_tz: Optional[str] = None) -> str:
    """One pick's six-line block - the same renderer the broadcast card uses."""
    return "\n".join(pick_lines(pick_line_from_record(pick, tz, user_tz)))


def build_keyboard(view: str, page: int, pages: int, day: Optional[str] = None, extra_rows=()) -> Dict:
    """InlineKeyboardMarkup dict: arrows (when useful), any ``extra_rows`` (the sport menu / back button) and the
    view-switch rows. The arrows carry ``day``."""
    row: List[Dict[str, str]] = []
    if pages > 1:
        if page > 0:
            row.append({"text": "◀️", "callback_data": encode_callback(view, page - 1, day)})
        row.append({"text": f"{page + 1}/{pages}", "callback_data": CALLBACK_NOOP})
        if page < pages - 1:
            row.append({"text": "▶️", "callback_data": encode_callback(view, page + 1, day)})
    switch = [[{"text": view_button_label(v), "callback_data": encode_callback(v, 0)} for v in views]
              for views in SWITCH_ROWS]
    return {"inline_keyboard": ([row] if row else []) + [list(r) for r in extra_rows] + switch}

def _retry_hint(snapshot: DailySnapshot, tz: str) -> str:
    """' Retrying around 11:00 Africa/Lagos.' when a partial refresh is scheduled."""
    raw = (snapshot.generation or {}).get("retry_after")
    at = parse_iso(raw) if raw else None
    return f" Retrying around {to_local(at, tz).strftime('%H:%M')} {tz}." if at else ""


def _retry_until(snapshot: DailySnapshot) -> str:
    until = (snapshot.generation or {}).get("retry_until")
    return f" until {until}" if until else ""


def predictions_evaluated(snapshot: DailySnapshot) -> bool:
    """A completed evaluation can have partial provider coverage and zero Top picks."""
    generation = snapshot.generation or {}
    if generation.get('reason') in ('before_post_time', 'generating', 'awaiting scheduled fetch'):
        return False
    return snapshot.status == 'COMPLETE' or bool(snapshot.total_picks()) or 'top' in generation


def empty_view_message(snapshot: DailySnapshot, view: str, tz: str = "UTC") -> str:
    if (snapshot.generation or {}).get("reason") == "before_post_time":
        return f"📋 Today's picks are ready at {snapshot.generation.get('ready_at', 'the scheduled time')}."
    if (snapshot.generation or {}).get("reason") == "generating":
        return "📋 Today's picks are being generated — try again in a minute."
    if (snapshot.generation or {}).get('reason') == 'awaiting scheduled fetch':
        return "📋 Today's predictions have not been generated yet."
    if view == VIEW_TOP and predictions_evaluated(snapshot):
        return picks_card([], snapshot.date, TOP_TITLE)
    if snapshot.all_predictions and view != VIEW_ALL:
        if view == VIEW_TOP:
            return f'There is no Bet of the Day for today. {len(snapshot.all_predictions)} All Predictions are available in /all.'
        return f'There are no {VIEW_LABEL[view]} for today. {len(snapshot.all_predictions)} All Predictions are available in /all.'
    if snapshot.status == 'COMPLETE' and not snapshot.total_picks():
        return 'No valid model predictions were generated today.'
    """Why a view has nothing to show - the real reasons, kept distinct.

    * an empty tier of an otherwise fine card: top picks are a quality bar, not a
      right, so the card's own "no selection cleared" line is the honest answer
    * provider data is incomplete: say that (with the retry time) instead of the
      old "temporarily unavailable" that implied the bot itself was down
    * a PARTIAL day with zero picks (e.g. every provider quota-blocked) gets the
      assembly message, not the quality-bar line
    """
    if snapshot.status == 'PARTIAL' and not snapshot.total_picks():
        return ("📋 <b>Today's predictions are still being assembled</b> - a data "
                f"source did not answer.{_retry_hint(snapshot, tz)}\n"
                f"The bot retries automatically{_retry_until(snapshot)}; /daily shows the card as "
                "soon as it is complete.")
    return picks_card([], snapshot.date, TOP_TITLE if view == VIEW_TOP else DAILY_TITLE,
                      zone=zone_label(tz, snapshot.date))



def _sports_back_row(day: str):
    return ([{"text": "◀️ Sports", "callback_data": encode_callback(VIEW_ALL, 0, day)}],)


def _valid(items: List[PickRecord]) -> List[PickRecord]:
    return [p for p in items if format_outcome(p) and math.isfinite(p.price) and p.price > 1.0]


def fixture_of(p: PickRecord) -> str:
    """The fixture a stored prediction belongs to (several predictions - one per market family - share one)."""
    return p.fixture_id or f"{p.sport}|{p.home}|{p.away}|{p.kickoff_utc}"


def group_by_fixture(items: List[PickRecord]) -> List[List[PickRecord]]:
    """All Predictions grouped by fixture: fixtures in kick-off order, each one's predictions in the stable market-family
    order (1X2 / Moneyline, Double Chance, DNB, Handicap, Over/Under, BTTS). A fixture is never split."""
    groups: Dict[str, List[PickRecord]] = {}
    for p in items:
        groups.setdefault(fixture_of(p), []).append(p)
    far = datetime.max.replace(tzinfo=None)

    def kickoff(g: List[PickRecord]):
        t = parse_iso(g[0].kickoff_utc)
        return (t is None, t.replace(tzinfo=None) if t else far)

    for g in groups.values():
        g.sort(key=lambda p: (FAMILY_ORDER.get(p.market, len(FAMILY_ORDER)), p.rank))
    return sorted(groups.values(), key=kickoff)


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def render_sport_menu(snapshot: DailySnapshot) -> Tuple[str, Dict]:
    """All Predictions, step one: only the sports that have a prediction today, each with its prediction and match counts.
    Reads the stored snapshot; a tap opens that sport's pages (``all_<sport>``) with no provider call."""
    items = _valid(snapshot.all_predictions)
    if not items:
        return empty_view_message(snapshot, VIEW_ALL), build_keyboard(VIEW_ALL, 0, 1)
    counts: Dict[str, int] = {}
    matches: Dict[str, set] = {}
    for p in items:
        b = sport_bucket(p.sport)
        counts[b] = counts.get(b, 0) + 1
        matches.setdefault(b, set()).add(fixture_of(p))
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    fixtures = len({fixture_of(p) for p in items})
    text = '\n'.join([f'{ALL_TITLE} · {day_label(snapshot.date)}',
                      f'{_plural(len(items), "prediction", "predictions")} · {_plural(fixtures, "match", "matches")}',
                      '', 'Pick a sport:'])
    buttons = [{"text": f"{SPORT_ICON.get(s, SPORT_ICON['other'])} {SPORT_LABEL.get(s, s)} ({n} · {_plural(len(matches[s]), 'match', 'matches')})",
                "callback_data": encode_callback(SPORT_VIEW_PREFIX + s, 0, snapshot.date)} for s, n in ordered]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    return text, build_keyboard(VIEW_ALL, 0, 1, snapshot.date, extra_rows=rows)


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
    if view == VIEW_ALL:
        return render_sport_menu(snapshot)
    items = snapshot.view_items(view)
    # A pick with unusable odds (<=1.0, non-finite) would make combined_odds()
    # raise and blank the whole page; drop it the same way an ambiguous
    # outcome is dropped, instead of failing the entire view.
    items = [p for p in items if format_outcome(p) and math.isfinite(p.price) and p.price > 1.0]
    if not items:
        if view.startswith(SPORT_VIEW_PREFIX):
            return (f'No {VIEW_LABEL[view]} predictions today.',
                    build_keyboard(view, 0, 1, snapshot.date, extra_rows=_sports_back_row(snapshot.date)))
        return empty_view_message(snapshot, view, tz), build_keyboard(view, 0, 1)
    display_tz = user_tz or tz
    if view.startswith(SPORT_VIEW_PREFIX):
        # one block per FIXTURE (its predictions together), ``size`` fixtures per page, a fixture never split
        sport = view[len(SPORT_VIEW_PREFIX):]
        groups = group_by_fixture(items)
        pages = total_pages(len(groups), size)
        page = clamp_page(page, len(groups), size)
        blocks = ['\n'.join(fixture_block([pick_line_from_record(p, tz, user_tz, now) for p in g]))
                  for g in groups[page * size:(page + 1) * size]]
        zone = zone_label(display_tz, snapshot.date)
        head = [f'{ALL_TITLE} · {day_label(snapshot.date)}',
                f'{SPORT_ICON.get(sport, SPORT_ICON["other"])} {VIEW_LABEL[view]} · {_plural(len(items), "prediction", "predictions")}'
                f' · {_plural(len(groups), "match", "matches")}' + (f' · 🕘 times in {zone}' if zone else '')]
        text = '\n\n'.join(['\n'.join(head), *blocks] + ([f'Page {page + 1} of {pages}'] if pages > 1 else []))
        return text, build_keyboard(view, page, pages, snapshot.date, extra_rows=_sports_back_row(snapshot.date))
    lines = by_kickoff(pick_line_from_record(p, tz, user_tz, now) for p in items)     # kickoff order, then paged
    pages = total_pages(len(lines), size)
    page = clamp_page(page, len(lines), size)
    # the header (odds AND count) prices the whole ticket - every page of it - never the page in hand
    text = picks_card(lines[page * size:(page + 1) * size], snapshot.date,
                      title=TOP_TITLE if view == VIEW_TOP else DAILY_TITLE,
                      total_prices=[p.price for p in lines], zone=zone_label(display_tz, snapshot.date),
                      page=page + 1, pages=pages)

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
    return stats_message(raw), build_keyboard(VIEW_STATS, 0, 1, extra_rows=_collection_stats_row() if db is not None else ())


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
    rows = stats_mod.ordered(db.picks_for_date(day, published_only=True))
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
        return render_stats(snapshot, db=db, tz=tz, now=now, user_tz=user_tz)
    if view.startswith('stats_'):
        return render_collection_stats(db, view[6:], user_tz or tz, now)
    if view.startswith('results_'):
        return render_collection_results(db, view[8:], page, size, day or snapshot.date)
    if view == VIEW_YESTERDAY:
        return render_yesterday(db, tz, page, size, day, now, user_tz)
    return render_page(snapshot, view, page, size, tz, user_tz, now)


def _collection_stats_row():
    """The one addition to the /stats card: a way into the per-collection numbers (Top, Daily, All Predictions)."""
    return ([{'text': f'{VIEW_ICON[v]} {name} stats', 'callback_data': encode_callback('stats_' + k, 0)}
             for k, v, name in (('top', VIEW_TOP, 'Top'), ('daily', VIEW_DAILY, 'Daily'), ('all', VIEW_ALL, 'All'))],)


def render_collection_stats(db, collection, tz, now=None):
    """One collection's results (Top / Daily / All Predictions), worded like the /stats card (see format.collection_stats_message)."""
    from predbot.jobs.stats import collection_stats
    if db is None:
        return NO_RESULTS_YET, build_keyboard(VIEW_STATS, 0, 1)
    local = to_local(now or utcnow(), tz)
    values = collection_stats(db, collection, local)
    view = {'top': VIEW_TOP, 'daily': VIEW_DAILY, 'all': VIEW_ALL}[collection]
    text = collection_stats_message(f"{VIEW_ICON[view]} {VIEW_LABEL[view]}", local.date().isoformat(), values,
                                    with_matches=collection == 'all')
    results = [{'text': '📊 View Results', 'callback_data': encode_callback('results_' + collection, 0, local.date().isoformat())}]
    return text, build_keyboard('stats_' + collection, 0, 1, extra_rows=(results, *_collection_stats_row()))


def render_collection_results(db, collection, page, size, day):
    from predbot.jobs import stats as stats_mod
    rows = [] if db is None else [r for r in db.collection_predictions(collection, settled_only=True)
                                  if r['pick_date'] <= day]
    rows.sort(key=lambda r: (r['pick_date'], r['start_utc'], r['id']), reverse=True)
    pages = total_pages(len(rows), size)
    page = clamp_page(page, len(rows), size)
    label = VIEW_LABEL[{'top': VIEW_TOP, 'daily': VIEW_DAILY, 'all': VIEW_ALL}[collection]]
    text = [f"🗓 {label.upper()} · RESULTS"]
    text += [yesterday_pick_line(r, stats_mod.outcome(r)) for r in rows[page * size:(page + 1) * size]]
    if not rows:
        text.append('No settled predictions yet.')
    elif pages > 1:
        text += ['', f'Page {page + 1} of {pages}']
    return '\n'.join(text), build_keyboard('results_' + collection, page, pages, day)
