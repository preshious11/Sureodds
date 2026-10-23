"""Closing-price capture for published Top/Daily picks (Part 3, 2026-10-23).

For each Top/Daily pick within ``cfg.closing_price_window_min`` minutes of its own kickoff that has not
already been captured, fetch a fresh odds snapshot and record ``closing_price``/``clv`` - never the
'all' collection (Top/Daily only, per the brief), and never a second time for the same pick (the
``closing_price IS NULL`` guard in ``Database.due_for_closing_price``/``record_closing_price``).

Provider: this engine has exactly ONE provider that ever supplies a sharp-book quote at all - verified
against 71 real Top/Daily picks from the last 10 production days (2026-10-14..23): every row carrying a
``sources._notes.reference_price`` (sharp coverage) had ``oddsapi`` in its ``_providers`` list, and NONE
of the 35 rows sourced from sharpapi/sportsgameodds alone ever did. SharpAPI's free tier is DraftKings +
FanDuel only (its own module docstring); SportsGameOdds is metered per event object and, empirically,
never the sharp source either. So OddsAPI is not just the cheapest provider with a sharp book - it is
the only one, and it only covers football and tennis (its own module docstring). A pick in any other
sport (basketball, baseball, american_football, ...) has no quote source under this job's constraints
at all: it is left pending forever (``unsupported_sport`` in the returned counts), never a fabricated
price - see the "no sharp-book quote available" test for the parallel case where the SPORT is covered
but no sharp book happens to quote this particular market.

Grouping (one request per LEAGUE, never per pick): OddsAPI's own ``/sports/{key}/odds`` endpoint is
billed per request at ``regions x markets`` credits, REGARDLESS of how many fixtures/picks that one
response covers - so every currently-due pick sharing a league is captured from the SAME single fetch,
and only the odds markets (h2h/spreads/totals) actually needed by that group's picks are requested, to
keep the credit cost below what blindly requesting all three would cost. Real volume on the last 10
production days: ~3 distinct (league, kickoff-window) groups/day needing a fetch at all (range 2-4,
after excluding the sports OddsAPI cannot cover regardless) - see predbot/config.py's
``closing_price_max_requests_per_day`` docstring for the full arithmetic.

Reuse, not duplication: the SAME ``OddsApiProvider`` instance generation/settlement already use (from
``build_providers``) is reused here - same ``Budget``, same rate limiter, same HTTP client/cache, same
``can_spend_credits`` gate (so this job can never cannibalize the generation/settlement reserves, and
its own spend is visible to everything else that reads the shared ``oddsapi`` budget bucket). There is
NO existing job that re-fetches odds near kickoff to piggy-back on (checked: live_monitor_d1.py and
settle.py only ever fetch RESULTS - ESPN/football-data/EuroLeague/SportsGameOdds's /scores - never
odds), so there was nothing to reuse there; this job's own per-day cap (``closing_price_max_requests_per_day``,
default 10 - a generous multiple of the real ~3-4/day) is the thing standing between a scheduling bug
and a quota problem, on top of the real OddsAPI budget gate.

Pricing parity with the published price (Part 1 finding - do not re-derive differently): ``apply_price_mode``
(predbot/providers/base.py) already prices every fetched line by ``cfg.price_mode`` inside
``OddsApiProvider._rows_to_events``, exactly as generation does. On top of that, when sharp books are
configured AND this line has both sharp and soft quotes, the PUBLISHED price is re-derived from the
soft books alone (``EventModel._soft_prices``, predbot/model/ensemble.py) - that exact adjustment is
replicated here, read-only (ensemble.py/top.py/daily.py are not imported or modified), as
``_soft_adjusted_price``. The sharp-book fair closing probability is captured the same way
``EventModel._sharp_fair`` does - the same ``shin_devig`` primitive, averaged across whichever
configured sharp books quote the WHOLE market - as ``_sharp_fair_probability``; it is only ever set when
a real sharp quote exists for every selection of that market, never invented. It is stored in
``picks.closing_fair_prob`` (migration 0016), with the snapshot time in ``closing_captured_at``; the job
never writes ``picks.sources``, so a capture cannot trip the D1 sync's settlement guard (see
workers/scripts/d1_sync.py's CLOSING_COLUMNS).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from predbot.config import Config
from predbot.jobs import stats as stats_mod
from predbot.markets.competitions import BY_ID
from predbot.model.devig import shin_devig
from predbot.providers.base import MarketLine
from predbot.providers.budget import Budget
from predbot.providers.chain import build_providers
from predbot.storage.db import Database
from predbot.util.fixtures import fixture_orientation
from predbot.util.timeutil import iso, parse_iso, utcnow

log = logging.getLogger(__name__)

#: Sports OddsAPI can ever quote (its own module docstring: "football (soccer) and tennis only").
#: Anything else has no quote source under this job's provider constraint, by design - never a reason
#: to add a second provider just for this (see module docstring).
ODDSAPI_SPORTS = ("football", "tennis")

#: pick['market'] -> the OddsAPI ``markets=`` value(s) that can quote it. A pick whose market is not
#: here (e.g. a prop) simply cannot be captured via this provider - counted as ``no_quote``.
_MARKET_NEEDS = {
    "result3": ("h2h",), "result2": ("h2h",), "dnb": ("h2h",), "dc": ("h2h",),
    "handicap": ("spreads",), "total": ("totals",), "btts": ("totals", "h2h"),
}

#: A freshly-fetched kickoff differing from the stored one by more than this is treated as "the
#: schedule moved" (postponement/rescheduling) - captured price would not actually be a CLOSING price
#: for the kickoff this pick was published against, so nothing is written (see module docstring's
#: "kickoff moved" edge case, and the matching test).
KICKOFF_MOVE_TOLERANCE_MIN = 5

#: Job-specific Budget bucket (distinct from the real "oddsapi" bucket, which is ALSO charged - see
#: run()) purely for the configurable daily cap on this job's own request count.
_CAP_BUCKET = "closing_price_requests"


def _needed_markets(rows: List[dict]) -> Tuple[str, ...]:
    out: List[str] = []
    for r in rows:
        for m in _MARKET_NEEDS.get(r["market"], ()):
            if m not in out:
                out.append(m)
    return tuple(out) or ("h2h",)


def _group_key(row) -> Tuple[str, str]:
    return (row["sport"], row["canonical_competition_id"] or row["league"] or "")


def _soft_adjusted_price(ml: MarketLine, sharp_books: List[str], price_mode: str) -> Tuple[float, str]:
    """Mirrors ``EventModel._soft_prices`` (predbot/model/ensemble.py) exactly, read-only: when sharp
    books are configured and this line has BOTH sharp and non-sharp quotes, the price a punter can
    actually take is re-derived from the non-sharp ("soft") quotes alone - never averaged in with the
    sharp book as if it were just another offer. ``ml.price`` is otherwise already ``cfg.price_mode``-priced
    across every book by ``apply_price_mode`` (called inside ``_rows_to_events``), so this only changes
    anything when sharp coverage is partial."""
    if not sharp_books or not ml.book_quotes:
        return ml.price, ml.bookmaker
    soft = [p for b, p in ml.book_quotes.items() if b not in sharp_books and p and p > 1.0]
    if not soft or len(soft) >= len(ml.book_quotes):
        return ml.price, ml.bookmaker
    import statistics
    if price_mode == "best":
        return max(soft), ml.bookmaker
    return round(float(statistics.median(soft)), 4), ("median" if len(soft) > 1 else ml.bookmaker)


def _sharp_fair_probability(siblings: List[MarketLine], selection: str,
                            sharp_books: List[str]) -> Tuple[Optional[float], List[str]]:
    """Mirrors ``EventModel._sharp_fair``/``_record_fair`` (predbot/model/ensemble.py) exactly, using
    the SAME ``shin_devig`` primitive: averaged across whichever configured sharp books quote EVERY
    selection of this market (a partial quote cannot be de-vigged). ``siblings`` = every MarketLine of
    the SAME market_key from the freshly-fetched event (so a 3-way result3 gets home+draw+away, not just
    the one selection this pick is on). Returns ``(None, [])`` - never a guess - when no sharp book
    covers the whole market, which is the normal case for most lines (see the "no sharp-book quote
    available" test)."""
    if not sharp_books or not siblings:
        return None, []
    by_selection = {ml.selection: ml for ml in siblings if ml.selection}
    if selection not in by_selection or len(by_selection) != len(siblings):
        return None, []        # a duplicate/ambiguous selection in this market: never guess which is "the" one
    per_book, used = [], []
    for book in sharp_books:
        prices = {sel: ml.book_quotes.get(book) for sel, ml in by_selection.items()}
        if all(p and p > 1.0 for p in prices.values()):
            per_book.append(shin_devig(prices))
            used.append(book)
    if not per_book:
        return None, []
    return sum(pb[selection] for pb in per_book) / len(per_book), used


def _find_line(event_markets: List[MarketLine], row, swapped: bool) -> Optional[MarketLine]:
    """The MarketLine on the freshly-fetched event matching this stored pick's own coordinates -
    mirrored (``MarketLine.mirrored()``, predbot/providers/base.py) when the fetched event lists
    home/away the other way round (``fixture_orientation`` returned "swapped")."""
    target = MarketLine(market=row["market"], selection=row["selection"], price=1.0,
                        period=row["period"] or "full", stat=row["stat"] or "points",
                        entity=row["entity"] or "all",
                        line=float(row["line"]) if row["line"] is not None else None)
    key = target.mirrored().key() if swapped else target.key()
    return next((ml for ml in event_markets if ml.key() == key), None)


def _siblings(event_markets: List[MarketLine], ml: MarketLine) -> List[MarketLine]:
    return [m for m in event_markets if m.market_key() == ml.market_key()]


def _match_event(events, row):
    """``(event, swapped)`` for the fetched event that is this stored pick's real fixture, or ``None``.
    Reuses ``fixture_orientation`` - the SAME fuzzy team/competition/kickoff matcher
    ``build_picks``'s cross-day dedup already relies on - rather than a second, bespoke name matcher."""
    from types import SimpleNamespace
    start = parse_iso(row["start_utc"])
    stand_in = SimpleNamespace(sport=row["sport"], home=row["home"], away=row["away"], start=start)
    for ev in events:
        orientation = fixture_orientation(stand_in, ev, comp_a=row["canonical_competition_id"] or None)
        if orientation is not None:
            return ev, orientation == "swapped"
    return None


def _capture_group(oddsapi, rows: List[dict], db: Database, cfg: Config, now: datetime,
                   counts: Dict[str, int]) -> None:
    sport, _ = _group_key(rows[0])
    sport_key = next((BY_ID[r["canonical_competition_id"]].oddsapi for r in rows
                      if r["canonical_competition_id"] in BY_ID and BY_ID[r["canonical_competition_id"]].oddsapi), "")
    if not sport_key:
        counts["unsupported_sport"] += len(rows)
        return
    markets = _needed_markets(rows)
    credits = len(oddsapi.regions) * len(markets)
    if not oddsapi.can_spend_credits(credits, purpose="closing_price"):
        counts["skipped_budget"] += len(rows)
        return
    try:
        data = oddsapi._get(f"/sports/{sport_key}/odds", {
            "oddsFormat": oddsapi.odds_format, "regions": ",".join(oddsapi.regions), "markets": ",".join(markets)})
    except Exception as e:  # noqa: BLE001 - one league's fetch failing must not stop the others
        log.warning("closing_price: fetch failed for %s (%s): %s", sport_key, sport, e)
        counts["errors"] += len(rows)
        return
    oddsapi.budget.spend(oddsapi.name, credits)
    counts["requests"] += 1
    window_start = min(parse_iso(r["start_utc"]) for r in rows) - timedelta(hours=1)
    window_end = max(parse_iso(r["start_utc"]) for r in rows) + timedelta(hours=1)
    events = oddsapi._rows_to_events([(sport, row) for row in (data if isinstance(data, list) else [])],
                                     window_start, window_end)
    for row in rows:
        _capture_one(row, events, db, cfg, now, counts)


def _capture_one(row, events, db: Database, cfg: Config, now: datetime, counts: Dict[str, int]) -> None:
    matched = _match_event(events, row)
    if matched is None:
        counts["no_quote"] += 1
        return
    event, swapped = matched
    stored_start = parse_iso(row["start_utc"])
    if stored_start and event.start and abs((event.start - stored_start).total_seconds()) > KICKOFF_MOVE_TOLERANCE_MIN * 60:
        counts["kickoff_moved"] += 1
        return                                     # not a closing price for the kickoff this pick was published against
    ml = _find_line(event.markets, row, swapped)
    if ml is None:
        counts["no_quote"] += 1
        return
    price, bookmaker = _soft_adjusted_price(ml, cfg.sharp_books, cfg.price_mode)
    siblings = _siblings(event.markets, ml)
    p_fair, fair_books = _sharp_fair_probability(siblings, ml.selection, cfg.sharp_books)
    clv = stats_mod.clv(row["price"], price)
    fair = None if p_fair is None else round(p_fair, 4)
    log.info("closing price | pick=%s price=%s bookmaker=%s books=%s swapped=%s sharp_fair=%s sharp_books=%s",
             row["id"], price, bookmaker, ml.distinct_books(), swapped, fair, fair_books)
    if db.record_closing_price(row["id"], price, clv, fair, iso(now)):
        counts["captured"] += 1
    else:
        counts["already_captured"] += 1            # idempotency guard fired - another run beat this one to it


def run(db: Database, cfg: Config, now: Optional[datetime] = None, providers: Optional[Dict] = None) -> Dict[str, int]:
    """Entry point (see module docstring). ``providers`` lets a caller that already built the provider
    map for this cycle (e.g. a combined settlement+closing-price run) pass it in instead of paying for a
    second ``build_providers`` construction; defaults to building one."""
    now = now or utcnow()
    counts = {"captured": 0, "already_captured": 0, "no_quote": 0, "kickoff_moved": 0, "unsupported_sport": 0,
             "skipped_budget": 0, "skipped_cap": 0, "errors": 0, "requests": 0}
    if not cfg.closing_price_enabled:
        counts["skipped"] = 1
        return counts
    due = db.due_for_closing_price(iso(now), cfg.closing_price_window_min)
    if not due:
        return counts
    providers = providers if providers is not None else build_providers(cfg, db)
    oddsapi = providers.get("oddsapi")
    if oddsapi is None:
        counts["unsupported_sport"] = len(due)       # no provider configured at all: nothing is capturable
        return counts
    cap_budget = Budget(db)
    groups: Dict[Tuple[str, str], List[dict]] = {}
    unsupported = []
    for row in due:
        if row["sport"] not in ODDSAPI_SPORTS:
            unsupported.append(row)
            continue
        groups.setdefault(_group_key(row), []).append(dict(row))
    counts["unsupported_sport"] += len(unsupported)
    for key, rows in groups.items():
        if not cap_budget.can_spend(_CAP_BUCKET, 1, daily_limit=cfg.closing_price_max_requests_per_day):
            counts["skipped_cap"] += len(rows)
            continue
        before = counts["requests"]
        _capture_group(oddsapi, rows, db, cfg, now, counts)
        if counts["requests"] > before:
            cap_budget.spend(_CAP_BUCKET, 1)
    return counts
