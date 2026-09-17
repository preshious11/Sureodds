"""Message templates. Deliberately terse: picks, odds, probability, stats."""
from __future__ import annotations

import re
from datetime import datetime
from typing import Dict, Iterable, List, NamedTuple, Optional

from predbot.bot.telegram import esc
from predbot.markets.catalog import short_selection
from predbot.providers.base import MarketLine
from predbot.util.timeutil import parse_iso, to_local

BRAND = "SureOdds"
SPORT_ICON = {"football": "⚽", "basketball": "🏀", "american_football": "🏈", "hockey": "🏒",
              "baseball": "⚾", "tennis": "🎾", "mma": "🥊", "handball": "🤾", "other": "🎯"}
SPORT_LABEL = {"football": "Football", "basketball": "Basketball", "american_football": "American Football",
               "hockey": "Hockey", "baseball": "Baseball", "tennis": "Tennis", "mma": "MMA",
               "handball": "Handball", "other": "Other"}
MARKET_LABEL = {"result3": "Match Result", "result2": "Moneyline", "dnb": "Draw No Bet",
                "dc": "Double Chance", "total": "Totals", "handicap": "Handicap",
                "btts": "Both Teams to Score", "odd_even": "Odd/Even", "ht_ft": "Halftime/Fulltime",
                "correct_score": "Correct Score", "result_total": "Result & Totals",
                "result_btts": "Result & BTTS", "clean_sheet": "Clean Sheet", "win_to_nil": "Win To Nil",
                "team_to_score": "Team To Score", "winning_margin": "Winning Margin", "prop": "Prop"}
RESULT_ICON = {"won": "✅", "lost": "❌", "push": "↩️", "half_won": "✅½", "half_lost": "❌½",
               "void": "➖", "pending": "⏳"}
# The two card headings. Defined once here so the broadcast card, the /top and
# /daily commands and their inline buttons cannot drift apart again.
HEADING_TOP = "🏆 Top Picks for Today"
HEADING_DAILY = "📋 Daily Picks for Today"
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
    when: str              # local kickoff with its zone, e.g. "19:45 WAT"
    price: float
    country: str = ""      # "England", "Kazakhstan", etc.
    market_type: str = ""  # "Over/Under", "Match Result", etc.


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


def pick_line_from_record(pick, tz: str, user_tz: Optional[str] = None) -> PickLine:
    """THE canonical pick-card builder (``format_pick_card`` for a cached pick).

    The ONLY place a cached ``PickRecord`` becomes display text: the /top and
    /daily views, the broadcast summary and every other path render through
    this, so a pick cannot show one card in one view and another card elsewhere.
    The outcome is always explicit (entity + side + line + stat for props);
    ambiguous picks come back with outcome='' and are skipped by the renderers.
    """
    return PickLine(sport=sport_name(pick.sport),
                    match=f"{pick.home} vs {pick.away}",
                    outcome=format_outcome(pick),
                    league=pick.league or sport_name(pick.sport),
                    confidence=pick.prob,
                    when=kickoff_label(parse_iso(pick.kickoff_utc), tz, user_tz),
                    price=pick.price,
                    country=getattr(pick, "country", ""),
                    market_type=MARKET_LABEL.get(pick.market, pick.market))


def kickoff_label(start: Optional[datetime], tz: str, user_tz: Optional[str] = None) -> str:
    """Local kickoff with its zone abbreviation ("7:45 PM WAT"); "--:--" when unknown."""
    if start is None:
        return "--:--"
    display_tz = user_tz or tz
    local = to_local(start, display_tz)
    return f"{local.strftime('%I:%M %p').lstrip('0')} {local.tzname() or display_tz}"


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
    """Pick display: sport, league+country, fixture, market type, prediction, odds, confidence, kickoff."""
    league_label = esc(p.league)
    if hasattr(p, 'country') and p.country:
        league_label = f"{esc(p.country)} • {league_label}" if league_label else esc(p.country)

    market_label = getattr(p, 'market_type', '')
    lines = [esc(p.sport), league_label, esc(p.match)]

    if market_label:
        lines.append(f"<b>Market:</b> {market_label}")

    lines.extend([
        f"<b>Prediction:</b> {esc(p.outcome)}",
        f"Odds: {p.price:.2f}",
        f"Confidence level: <b>{round(p.confidence * 100)}%</b> | {p.when}"
    ])
    return lines


def picks_card(picks: List[PickLine], date_label: str, stats_line: Optional[str] = None,
               heading: str = HEADING_DAILY,
               total_prices: Optional[Iterable[float]] = None) -> str:
    """The daily predictions post: one block per pick, then the ticket totals.

    ``total_prices`` lets a caller price a different collection than the cards
    shown (legacy callers only); when omitted the combined odds are the product
    of exactly the picks listed here - displayed picks and combined odds can
    never come from different lists.
    """
    lines = [f"{heading} {date_label}", ""]
    if not picks:
        lines.append("⏳ <i>Predictions being analyzed...</i>")
        lines.append("")
        lines.append("Our model is carefully reviewing today's fixtures to find the best value selections. "
                    "We only recommend picks that meet our strict quality standards.")
        lines.append("")
        lines.append("✅ Check back soon! Predictions typically available by 09:00 AM Lagos time.")
    for p in picks:
        lines.extend(pick_lines(p))
        lines.append("")
    if picks:
        prices = list(total_prices) if total_prices is not None else [p.price for p in picks]
        lines.append(f"Total Picks: {len(picks)}")
        lines.append(f"Combined Odds: {combined_odds(prices):.4f}")
    if stats_line:
        lines.append("")
        lines.append(stats_line)
    return "\n".join(lines)


def picks_message(top, daily, day: datetime, tz: str, stats_line: Optional[str]) -> str:
    """Channel post for the day's selections: top picks first, then the daily ones."""
    picks: List[PickLine] = []
    for s in list(top) + list(daily):
        e = s.event
        picks.append(PickLine(sport=sport_name(e.sport),
                              match=f"{e.home} vs {e.away}",
                              outcome=outcome_label(s.label, s.line, e.home, e.away, e.sport),
                              league=e.league or sport_name(e.sport),
                              confidence=s.p_model, when=kickoff_label(e.start, tz),
                              price=s.price,
                              country=str(e.extra.get("country") or ""),
                              market_type=MARKET_LABEL.get(s.line.market, s.line.market)))
    picks = [p for p in picks if p.outcome]
    return picks_card(picks, to_local(day, tz).strftime("%Y-%m-%d"), stats_line)


# ------------------------------------------------------------ the summary card
#: The corrected, canonical summary copy. Every summary/recap path renders THIS
#: structure; any other wording found at runtime is a stale-path regression.
SUMMARY_TITLE = "📊 TODAY'S RESULTS"
TOP_SECTION_TITLE = "🔥 TOP PICKS"
DAILY_SECTION_TITLE = "📋 DAILY PICKS"
SECTION_COMBINED = "Combined Odds:"
SECTION_TOTAL = "Total Picks:"


def render_section(title: str, picks: List[PickLine]) -> List[str]:
    """ONE canonical section renderer: title, Combined Odds, Total Picks, then
    the pick blocks. Combined odds and Total Picks are computed from EXACTLY
    the picks rendered here - never from another collection."""
    lines = [title, f"{SECTION_COMBINED} {combined_odds(p.price for p in picks):.2f}",
             f"{SECTION_TOTAL} {len(picks)}"]
    for p in picks:
        lines.extend(pick_lines(p))
        lines.append("")
    return lines


def summary_card(top: List[PickLine], daily: List[PickLine]) -> str:
    """THE canonical results/summary card (one and only one renderer).

        📊 TODAY'S RESULTS
        🔥 TOP PICKS
        Combined Odds: X.XX
        Total Picks: X
        <pick blocks...>
        📋 DAILY PICKS
        Combined Odds: X.XX
        Total Picks: X
        <pick blocks...>

    The Daily section's odds/picks cover only the Daily selections; the Top
    section's only the Top ones. An outcome that cannot be stated explicitly is
    skipped before anything is computed, so the totals always match the cards.
    """
    top = [p for p in top if p.outcome]
    daily = [p for p in daily if p.outcome]
    lines = [SUMMARY_TITLE]
    if top:
        lines.append("")
        lines.extend(render_section(TOP_SECTION_TITLE, top))
    if daily:
        lines.append("")
        lines.extend(render_section(DAILY_SECTION_TITLE, daily))
    return "\n".join(lines).rstrip()


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


def stats_message(s: Dict, sport_breakdown: Optional[List[Dict]] = None) -> str:
    """Yesterday's results card formatted like broadcast recap.

    Uses the same format as recap_message but for the /stats command view.
    If sport_breakdown is None, computes it from the stats dict.
    """
    y, seven = s.get("yesterday", {}), s.get("last7", {})
    if not y:
        y = {"won": 0, "lost": 0, "void": 0, "push": 0, "best_win": None}

    lines = [f"📊 {BRAND.upper()} RESULTS — YESTERDAY", ""]
    lines.append(f"✅ Wins: {y.get('won', 0)}")
    lines.append(f"❌ Losses: {y.get('lost', 0)}")
    lines.append(f"➖ Void: {y.get('void', 0)}")
    if y.get("push"):
        lines.append(f"↩️ Push: {y['push']}")
    lines.append("")
    lines.append(f"🎯 Hit Rate: {_hit_rate(y.get('won', 0), y.get('lost', 0))}")

    if sport_breakdown:
        lines.append("")
        for b in sport_breakdown:
            icon = SPORT_ICON.get(b["sport"], SPORT_ICON["other"])
            lines.append(f"{icon} {sport_name(b['sport'])}: {b['won']}/{b['n']}")

    lines.append("")
    lines.append("📈 LAST 7 DAYS")
    lines.append(f"Wins: {seven.get('won', 0)}")
    lines.append(f"Losses: {seven.get('lost', 0)}")
    lines.append(f"Hit Rate: {_hit_rate(seven.get('won', 0), seven.get('lost', 0))}")

    best = y.get("best_win")
    if best:
        lines.append("")
        lines.append("🔥 BEST PICK")
        lines.append(f"{esc(best['label'])} @ {best['price']:.2f} ✅")

    return "\n".join(lines)
