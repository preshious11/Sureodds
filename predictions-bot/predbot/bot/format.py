"""Message templates. Deliberately terse: picks, odds, probability, stats."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, NamedTuple, Optional, Tuple

from predbot.bot.telegram import esc
from predbot.markets.catalog import short_selection
from predbot.providers.base import MarketLine
from predbot.util.timeutil import parse_iso, to_local, utcnow
from predbot.util.duration import DURATION_H

log = logging.getLogger(__name__)

BRAND = "SureOdds"
SPORT_ICON = {"football": "⚽", "basketball": "🏀", "american_football": "🏈",
              "baseball": "⚾", "tennis": "🎾", "mma": "🥊", "other": "🎯"}
SPORT_LABEL = {"football": "Football", "basketball": "Basketball", "american_football": "American Football",
               "baseball": "Baseball", "tennis": "Tennis", "mma": "MMA", "other": "Other"}
MARKET_LABEL = {"result3": "Match Result", "result2": "Moneyline", "dnb": "Draw No Bet",
                "dc": "Double Chance", "total": "Totals", "handicap": "Handicap",
                "btts": "Both Teams to Score", "odd_even": "Odd/Even", "ht_ft": "Halftime/Fulltime",
                "correct_score": "Correct Score", "result_total": "Result & Totals",
                "result_btts": "Result & BTTS", "clean_sheet": "Clean Sheet", "win_to_nil": "Win To Nil",
                "team_to_score": "Team To Score", "winning_margin": "Winning Margin", "prop": "Prop"}
RESULT_ICON = {"won": "✅", "lost": "❌", "push": "↩️", "half_won": "✅½", "half_lost": "❌½",
               "void": "➖", "pending": "⏳"}
# Bare codes a punter should never have to decode; mapped to plain English.
_TERSE_CODES = {"1": "{home} to Win", "2": "{away} to Win", "X": "Draw",
                "1X": "{home} or Draw", "X2": "{away} or Draw", "12": "{home} or {away}",
                "1 DNB": "{home} (Draw No Bet)", "2 DNB": "{away} (Draw No Bet)"}


class PickLine(NamedTuple):
    """One prediction exactly as the daily post renders it."""

    sport: str             # "Football", "Basketball", etc.
    match: str             # "Chelsea vs Man United"
    outcome: str           # explicit outcome, e.g. "Over 2.5 Goals", "Chelsea to Win"
    league: str            # "Premier League", "La Liga", etc.
    confidence: float      # model probability, 0..1
    when: str              # local kickoff time in the reader's zone, e.g. "7:45 PM" (the zone is in the card header)
    price: float
    country: str = ""      # "England", "Kazakhstan", etc.
    market_type: str = ""  # "Over/Under", "Match Result", etc.
    state: str = ""        # "", "started" or "finished" - see leg_state()
    start: Optional[datetime] = None   # kickoff instant: cards are listed earliest first
    sport_key: str = ""    # "football", "tennis"...: picks the sport icon


def _looks_internal(text: str) -> bool:
    """True for raw market keys like 'result3:home' that must never reach a user."""
    return " " not in text and (":" in text or "_" in text)


#: Outcomes a punter cannot act on without more context. A bare "Over", an
#: "Over/Under" without a side, an "Over 1.5" without a unit, or bare line
#: values like "1.5" or "2.0" - all ambiguous.
_BARE_OUTCOMES = {"over", "under", "over/under", "over-under", "win", "lose",
                  "yes", "no", "draw", "selection", "even", "odd"}
_BARE_TOTAL_RE = re.compile(r"(?:over|under)\s+[\d.]+")
_OVER_UNDER_RE = re.compile(r"\bover\s*/\s*under\b")
_BARE_NUMBER_RE = re.compile(r"^[\d.]+$")  # just a number with no context


def ambiguous_outcome(text: str) -> bool:
    """True when ``text`` does not state an explicit, complete outcome."""
    t = " ".join(str(text or "").strip().lower().split())
    if not t:
        return True
    if t in _BARE_OUTCOMES:
        return True
    if _looks_internal(t):
        return True
    if _OVER_UNDER_RE.search(t):
        return True
    if _BARE_TOTAL_RE.fullmatch(t):        # "Over 1.5" - over WHAT?
        return True
    if _BARE_NUMBER_RE.fullmatch(t):       # "1.5" or "2.0" - no context
        return True
    return False


def outcome_label(label: Optional[str] = None, ml=None, home: str = "",
                  away: str = "", sport: str = "football") -> str:
    """ONE canonical outcome formatter: the explicit full prediction, never a
    bare code or a raw market name. Returns '' when the outcome cannot be
    stated explicitly - callers skip such candidates instead of guessing.

    Prop markets are ALWAYS rebuilt from the canonical fields (entity, side,
    line, stat): a provider label like 'Batting_Hits' or 'Hits Over/Under' is
    never shown. Ambiguous stored labels ('Over 1.5' with no unit, 'Over/Under')
    fall through to the canonical derivation, and if that fails too the outcome
    is '' (the pick is hidden) rather than a misleading fragment.
    """
    text = " ".join(str(label or "").split())
    derived = (short_selection(ml, home, away, sport) or "").strip() if ml is not None else ""
    if ml is not None and ml.market == "prop":
        if derived and not ambiguous_outcome(derived):
            return derived
        if text and not ambiguous_outcome(text):
            return text
        return ""
    if text and text not in _TERSE_CODES and not ambiguous_outcome(text):
        return text
    if derived:
        if derived not in _TERSE_CODES and not ambiguous_outcome(derived):
            return derived
        named = _TERSE_CODES.get(derived)
        if named:
            return named.format(home=home or "Home", away=away or "Away")
    named = _TERSE_CODES.get(text)
    if named:
        return named.format(home=home or "Home", away=away or "Away")
    return ""


def format_outcome(pick, home: str = "", away: str = "") -> str:
    """The outcome of a cached pick (``PickRecord``-like object) as one explicit
    prediction, via :func:`outcome_label`. '' when it cannot be represented."""
    ml = MarketLine(
        market=str(getattr(pick, "market", "") or ""),
        selection=str(getattr(pick, "selection", "") or ""),
        price=float(getattr(pick, "price", 0.0) or 0.0),
        period=str(getattr(pick, "period", "full") or "full"),
        stat=str(getattr(pick, "stat", "") or ""),
        entity=str(getattr(pick, "entity", "") or ""),
        line=getattr(pick, "line", None),
        label=str(getattr(pick, "label", "") or ""),
        market_name=str(getattr(pick, "label", "") or ""),
    )
    return outcome_label(ml.label, ml, home or getattr(pick, "home", ""),
                         away or getattr(pick, "away", ""),
                         getattr(pick, "sport", "football"))


#: A match that has kicked off carries 🔴 after its time; 🏁 is not part of the card vocabulary, so a match whose
#: typical length has passed just says so in words.
STARTED_MARK = " 🔴"
FINISHED_MARK = " · Finished"


def leg_state(start: Optional[datetime], sport: str, now: Optional[datetime] = None) -> str:
    """"" before kickoff, "started" from kickoff, "finished" once the sport's
    typical match length has passed. "finished" is an ESTIMATE from the same
    duration table settlement uses - there is no live result feed behind it.

    A card is built once per day but read at every subscriber's own 9:30 and at
    every /top and /daily tap; a leg that has already kicked off must say so
    instead of looking like a live tip."""
    if start is None:
        return ""
    now = now or utcnow()
    if now < start:
        return ""
    hours = DURATION_H.get(sport, DURATION_H["other"])
    return "finished" if now >= start + timedelta(hours=hours) else "started"


def day_label(day) -> str:
    """'Sat 19 Sep' for a YYYY-MM-DD string (or a date): the date every card carries in its header."""
    if isinstance(day, str):
        day = datetime.strptime(day[:10], "%Y-%m-%d")
    return f"{day.strftime('%a')} {day.day} {day.strftime('%b')}"


def zone_label(tz: str, day: str = "") -> str:
    """The reader's zone as it is written on a card: 'WAT', 'BST', 'EDT' (the abbreviation in force on the card's
    day, so a summer card says BST); the IANA name when the platform only knows a numeric one ('+01')."""
    try:
        ref = datetime.strptime(day[:10], "%Y-%m-%d").replace(hour=12, tzinfo=timezone.utc) if day else utcnow()
    except ValueError:
        ref = utcnow()
    name = (to_local(ref, tz).tzname() or "").strip()
    return name if name and not name[0] in "+-" and not name.isdigit() else tz


def clock_label(start: Optional[datetime], tz: str) -> str:
    """Kickoff time in ``tz`` without the zone ('7:45 PM'); '--:--' when unknown. The zone is written once, in the
    card's header."""
    if start is None:
        return "--:--"
    return to_local(start, tz).strftime("%I:%M %p").lstrip("0")


def kickoff_label(start: Optional[datetime], tz: str, user_tz: Optional[str] = None) -> str:
    """Local kickoff with its zone abbreviation ("7:45 PM WAT"); "--:--" when unknown."""
    if start is None:
        return "--:--"
    display_tz = user_tz or tz
    local = to_local(start, display_tz)
    return f"{local.strftime('%I:%M %p').lstrip('0')} {local.tzname() or display_tz}"


def pick_line_from_record(pick, tz: str, user_tz: Optional[str] = None,
                          now: Optional[datetime] = None) -> PickLine:
    """THE canonical pick-card builder (``format_pick_card`` for a cached pick).

    The ONLY place a cached ``PickRecord`` becomes display text: the /top and
    /daily views, the broadcast summary and every other path render through
    this, so a pick cannot show one card in one view and another card elsewhere.
    The outcome is always explicit (entity + side + line + stat for props);
    ambiguous picks come back with outcome='' and are skipped by the renderers.
    """
    start = parse_iso(pick.kickoff_utc)
    return PickLine(sport=sport_name(pick.sport),
                    match=f"{pick.home} vs {pick.away}",
                    outcome=format_outcome(pick),
                    league=pick.league or sport_name(pick.sport),
                    confidence=pick.prob,
                    when=clock_label(start, user_tz or tz),
                    state=leg_state(start, pick.sport, now),
                    price=pick.price,
                    country=getattr(pick, "country", ""),
                    market_type=MARKET_LABEL.get(pick.market, pick.market),
                    start=start, sport_key=pick.sport)


def by_kickoff(lines: Iterable[PickLine]) -> List[PickLine]:
    """Display order on every card: earliest kickoff first (unknown times last; equal times keep slate order).
    Only the rendering is sorted - the slate and the stored picks are untouched."""
    return sorted(lines, key=lambda p: (p.start is None, p.start or datetime.max.replace(tzinfo=timezone.utc)))


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


def _icon(p: PickLine) -> str:
    key = p.sport_key or next((k for k, v in SPORT_LABEL.items() if v == p.sport), "other")
    return SPORT_ICON.get(key, SPORT_ICON["other"])


def pick_lines(p: PickLine) -> List[str]:
    """One pick block:

        ⚽ England • Premier League · Chelsea vs Man United
        🎯 Chelsea to Win
        💰 1.31  ·  📊 76%  ·  🕘 12:30 AM 🔴
    """
    league = esc(p.league)
    if p.country:
        league = f"{esc(p.country)} • {league}" if league else esc(p.country)
    mark = STARTED_MARK if p.state == "started" else FINISHED_MARK if p.state == "finished" else ""
    return [f"{_icon(p)} {league} · {esc(p.match)}",
            f"🎯 {esc(p.outcome)}",
            f"💰 {p.price:.2f}  ·  📊 {round(p.confidence * 100)}%  ·  🕘 {esc(p.when)}{mark}"]


TOP_TITLE = "🔥 TOP PICKS"
DAILY_TITLE = "📋 DAILY PICKS"
NO_SELECTION = "No selection cleared the quality bar today."


def n_picks(n: int) -> str:
    return f"{n} pick" + ("" if n == 1 else "s")


def card_header(title: str, day: str, prices: List[float], zone: str = "") -> List[str]:
    """'🔥 TOP PICKS · Sun 20 Sep' / '🎟 Combined odds 2.01 · 2 picks · 🕘 times in WAT'. The odds and the count
    are this card's OWN: the whole ticket (every page of it), never the page in hand or another tier."""
    lines = [f"{title} · {day_label(day)}" if day else title]
    if prices:
        odds = f"🎟 Combined odds {combined_odds(prices):.2f} · {n_picks(len(prices))}"
        lines.append(odds + (f" · 🕘 times in {zone}" if zone else ""))
    return lines


def picks_card(picks: List[PickLine], day: str, title: str = DAILY_TITLE,
               total_prices: Optional[Iterable[float]] = None, zone: str = "",
               page: Optional[int] = None, pages: Optional[int] = None,
               stats_line: Optional[str] = None) -> str:
    """One ticket card: header, then one block per pick in kickoff order.

    ``total_prices`` prices the WHOLE ticket when ``picks`` is only one page of it (the count is then the whole
    ticket's too); when omitted the odds are the product of exactly the picks listed. ``page``/``pages`` add the
    only per-page line, the footer 'Page 2 of 3'."""
    picks = by_kickoff(picks)
    prices = list(total_prices) if total_prices is not None else [p.price for p in picks]
    lines = card_header(title, day, prices if picks else [], zone)
    if not picks:
        lines.append(NO_SELECTION)
    for p in picks:
        lines += ["", *pick_lines(p)]
    if pages and pages > 1 and page:
        lines += ["", f"Page {page} of {pages}"]
    if stats_line:
        lines += ["", stats_line]
    return "\n".join(lines)


def picks_message(top, daily, day: datetime, tz: str, stats_line: Optional[str]) -> str:
    """Channel post for the day's selections, built from scored picks: the Top ticket, then the Daily one."""
    def lines(items):
        out = []
        for s in items:
            e = s.event
            out.append(PickLine(sport=sport_name(e.sport), match=f"{e.home} vs {e.away}",
                                outcome=outcome_label(s.label, s.line, e.home, e.away, e.sport),
                                league=e.league or sport_name(e.sport), confidence=s.p_model,
                                when=clock_label(e.start, tz), price=s.price,
                                country=str(e.extra.get("country") or ""),
                                market_type=MARKET_LABEL.get(s.line.market, s.line.market),
                                start=e.start, sport_key=e.sport))
        return out
    top_lines, daily_lines = lines(top), lines(daily)
    for match, outcome in duplicate_pick_lines([p for p in top_lines if p.outcome], [p for p in daily_lines if p.outcome]):
        log.warning("card renders the same pick in Top and Daily: %s - %s", match, outcome)
    day_str = to_local(day, tz).strftime("%Y-%m-%d")
    card = summary_card(top_lines, daily_lines, day_str, zone_label(tz, day_str)) if (top_lines or daily_lines) else \
        picks_card([], day_str, DAILY_TITLE)
    return f"{card}\n\n{stats_line}" if stats_line else card


# ------------------------------------------------------------ the summary card
def duplicate_pick_lines(top: Iterable[PickLine], daily: Iterable[PickLine]) -> List[Tuple[str, str]]:
    """(match, outcome) of every Daily block whose text repeats a Top block: the same outcome on the same
    two teams (in either order). The cross-tier rule makes this impossible; the card checks anyway."""
    def ident(p: PickLine):
        teams = sorted(t.strip().lower() for t in re.split(r"\s+vs\.?\s+", p.match or "", maxsplit=1))
        return (p.outcome.strip().lower(), tuple(teams))
    seen = {ident(p) for p in top if p.outcome}
    return [(p.match, p.outcome) for p in daily if p.outcome and ident(p) in seen]


def summary_card(top: List[PickLine], daily: List[PickLine], day: str = "", zone: str = "") -> str:
    """THE canonical delivered/channel card (one and only one renderer): the Top ticket, then the Daily ticket,
    each with its own header (own combined odds, own pick count) and its picks in kickoff order.

    An outcome that cannot be stated explicitly is skipped before anything is computed, so the totals always match
    the blocks shown."""
    top = by_kickoff(p for p in top if p.outcome)
    daily = by_kickoff(p for p in daily if p.outcome)
    for match, outcome in duplicate_pick_lines(top, daily):
        log.warning("card renders the same pick in Top and Daily: %s - %s", match, outcome)
    sections = []
    for title, picks in ((TOP_TITLE, top), (DAILY_TITLE, daily)):
        if picks:
            lines = card_header(title, day, [p.price for p in picks], zone)
            for p in picks:
                lines += ["", *pick_lines(p)]
            sections.append("\n".join(lines))
    return "\n\n".join(sections)


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


def sport_name(sport: str) -> str:
    return SPORT_LABEL.get(sport, sport.replace("_", " ").title())


# ------------------------------------------------------------------ results cards (stats, recap, /yesterday)
#: The settled outcome marks. An unsettled pick has none.
MARK = {"won": "✅", "lost": "❌", "void": "➖", "push": "↩️"}
NO_RESULTS_YET = "📊 No results yet — the first recap arrives the morning after the first slate."


def counts_line(b: Dict, sep: str = "  ") -> str:
    """'✅ 14  ❌ 9  ➖ 2' (plus '↩️ n' when something pushed): ONE helper for /stats, the recap and /yesterday, so the
    numbers on the card and the marks in the audit trail can never drift apart."""
    parts = [f"{MARK['won']} {b.get('won', 0)}", f"{MARK['lost']} {b.get('lost', 0)}", f"{MARK['void']} {b.get('void', 0)}"]
    if b.get("push"):
        parts.append(f"{MARK['push']} {b['push']}")
    return sep.join(parts)


def _norm(s: Dict) -> Dict:
    """A stats dict from an older writer (a cached snapshot's ``StatsSummary.to_raw()``) has no issued/final/strip:
    read it as a fully settled day with no per-sport split, so nothing invents a completeness claim."""
    y = dict(s.get("yesterday") or {})
    settled = sum(int(y.get(k, 0) or 0) for k in ("won", "lost", "void", "push"))
    y["unknown"] = "issued" not in y and not settled            # nothing recorded: say nothing, not "no picks were issued"
    y.setdefault("issued", settled)
    y.setdefault("settled", settled)
    y.setdefault("final", True)
    y.setdefault("hit_rate", (100.0 * y.get("won", 0) / (y.get("won", 0) + y.get("lost", 0)))
                 if y.get("won", 0) + y.get("lost", 0) else None)
    y.setdefault("roi", None)
    return {**s, "yesterday": y, "last7": dict(s.get("last7") or {}), "by_sport_yesterday": s.get("by_sport_yesterday") or []}


def _best_line(best: Optional[Dict], final: bool) -> List[str]:
    if not best:
        return []
    what = esc(best.get("outcome") or best.get("label") or "")
    match = f" · {esc(best['home'])} vs {esc(best['away'])}" if best.get("home") and best.get("away") else ""
    return [f"🏆 {'Best' if final else 'Best so far'}: {what}{match} · @{float(best['price']):.2f}"]


def yesterday_lines(y: Dict, day: str) -> List[str]:
    """The 'Yesterday' block. Settled results only - no pending marks, no counters - but a day that is not final
    says so ONCE, and withholds hit rate and ROI until the last result is in."""
    head = "🗓 Yesterday" + (f" · {day_label(day)}" if day else "")
    if not y.get("issued"):
        return [f"{head} — no picks were issued."]
    head += f" · {n_picks(y['issued'])}"
    if not y.get("settled"):
        return [f"{head} — results come in after the last match ends."]
    lines = [head, counts_line(y)]
    if y.get("final"):
        hit = f"🎯 {round(y['hit_rate'])}%" if y.get("hit_rate") is not None else "🎯 –"
        lines.append(f"{hit}  ·  💹 ROI {y['roi']:+.0f}%" if y.get("roi") is not None else hit)
    else:
        lines.append(f"{y['settled']} of {y['issued']} settled · final card after the last result")
    return lines + _best_line(y.get("best_win"), bool(y.get("final")))


def by_sport_lines(rows: List[Dict]) -> List[str]:
    if not rows:
        return []
    items = []
    for b in rows:
        label = f"{SPORT_ICON.get(b['sport'], SPORT_ICON['other'])} {sport_name(b['sport'])}"
        items.append(f"{label} {b['won']}/{b['settled']}" if b.get("settled") else f"{label} —")
    return ["🏅 By sport", "  ·  ".join(items)]


def last_days_lines(seven: Dict, top: Optional[Dict]) -> List[str]:
    window = int(seven.get("window_days", 7) or 0)
    if not window:
        return []
    lines = ["📈 Last 7 days" if window >= 7 else f"📈 Last {window} day" + ("" if window == 1 else "s")]
    parts = [counts_line(seven)]
    if seven.get("hit_rate") is not None:
        parts.append(f"🎯 {round(seven['hit_rate'])}%")
    streak = str(seven.get("streak") or "")
    if streak:
        parts.append(f"🔥 {streak}" if streak.startswith("W") else streak)
    lines.append("  ·  ".join(parts))
    strip = ["%s %s" % (datetime.strptime(d["date"], "%Y-%m-%d").strftime("%a"), "".join(MARK[m] for m in d["marks"]))
             for d in seven.get("strip", [])]
    if strip:
        lines.append("  ".join(strip))
    if top and top.get("days"):
        span = "the last 7 days" if top["days"] >= 7 else f"{top['days']} settled day" + ("" if top["days"] == 1 else "s")
        lines += ["", f"🎟 Top ticket hit {top['hit']} of {span}"]
    return lines


def _results_body(s: Dict) -> List[str]:
    y = s["yesterday"]
    blocks = [] if y.get("unknown") else [yesterday_lines(y, y.get("date") or "")]
    if y.get("settled"):
        blocks.append(by_sport_lines(s["by_sport_yesterday"]))
    blocks.append(last_days_lines(s["last7"], s.get("top_ticket_7d")))
    out: List[str] = []
    for block in (b for b in blocks if b):
        out += ([""] if out else []) + block
    return out


def _results_card(title: str, s: Dict) -> str:
    s = _norm(s)
    if not s.get("settled_ever", 1) and not s["yesterday"]["issued"]:
        return NO_RESULTS_YET
    date = f" · {day_label(s['today'])}" if s.get("today") else ""
    return "\n".join([f"{title}{date}", ""] + _results_body(s))


def recap_message(s: Dict) -> str:
    """Yesterday's results card (the channel post and every delivered recap): the same helpers, and the same
    settled-only rule, as the /stats card."""
    return _results_card("📊 RESULTS", s)


def stats_message(s: Dict) -> str:
    """The /stats card."""
    return _results_card("📊 STATS", s)


def yesterday_header(day: str, y: Dict) -> List[str]:
    """The /yesterday header: its numbers ARE the /stats 'Yesterday' numbers (one helper), so the owner can count the
    marks in the list against the card."""
    if not y.get("issued"):
        return [f"🗓 YESTERDAY · {day_label(day)}", "No picks were issued."]
    return [f"🗓 YESTERDAY · {day_label(day)}", f"{n_picks(y['issued'])} · {counts_line(y, ' ')}"]


def yesterday_pick_line(row, kind: Optional[str]) -> str:
    """'✅ 🎯 Over 2.5 goals · Venezia vs Lazio · @1.69'; an unsettled pick has no mark."""
    from predbot.jobs.stats import pick_outcome
    mark = MARK[kind] if kind else "   "
    return (f"{mark} 🎯 {esc(pick_outcome(row))} · {esc(row['home'])} vs {esc(row['away'])} · @{float(row['price']):.2f}")
