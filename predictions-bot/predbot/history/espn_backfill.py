"""Bounded, idempotent ESPN backfill for the football competitions football-data.org and the CSV importer
cannot reach: UEL, UECL, Super Lig, MLS - any competition in ``predbot.markets.competitions.COMPETITIONS`` with
an ``espn`` slug but no ``football_data`` code (and no ``CSV_DIVISIONS`` entry).

LOCAL / OFFLINE operation only, same discipline as ``predbot.history.ingest`` (see its own module docstring):
nothing here runs from the bot's normal paths, and nothing here ever touches production D1 - the caller supplies
a ``Database`` (sqlite) directly, and that is the only thing this module writes to.

Design, reusing what already exists rather than inventing a second version of it:

* The scoreboard fetch, its per-(league, day) cache and its rate limiter (``ESPNProvider._scoreboard``, default
  1 request/second) are reused AS IS - this module drives the same provider instance ``predbot/jobs/settle.py``
  and ``predbot/providers/espn.py`` already use for live settlement, it does not reimplement HTTP.
* Status interpretation is reused AS IS: ``ESPNProvider._to_result`` already distinguishes
  final / postponed / cancelled / scheduled / in-progress / unknown (see ``espn.py``'s own FINAL /
  BEYOND_REGULATION / POSTPONED / CANCELLED vocabulary) - a row is stored only when that call reports
  ``status == "final"`` with a usable ``scores["full"]``; postponed/abandoned/incomplete fixtures are rejected
  by the SAME logic settlement already trusts, not a second guess at ESPN's status strings.
* Identity is reused AS IS: ``IdentityRegistry.participant`` (the identity system every other ingester and
  ``settle.py``'s own ESPN Elo harvest already use), keyed by ESPN's own numeric team id so a team is the same
  canonical participant here as it is everywhere else results flow through.
* The write path is reused AS IS: ``HistoryStore.add_result``, idempotent via ``UNIQUE(source,
  source_match_id)`` and the ``(competition_id, home_id, away_id, date)`` unique index - a rerun over the same
  (or an overlapping) date range inserts nothing twice, and a result that a later CLI/API ingester or
  settlement's own harvest later brings in from a different source collapses onto the same row.
* Request accounting goes through the same ``Budget`` ledger the odds providers use (``predbot/providers
  /budget.py``), under provider name ``"espn"``, so a backfill's usage is visible next to everything else that
  already reports there - no second accounting mechanism.
"""
from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, Iterator, List, Optional

from predbot.history.store import HistoryStore
from predbot.markets.competitions import Competition
from predbot.providers.espn import ESPNProvider, _by_side, _names
from predbot.providers.budget import Budget
from predbot.storage.identity import IdentityRegistry
from predbot.util.http import ProviderError
from predbot.util.timeutil import parse_iso, utcnow

log = logging.getLogger(__name__)

SPORT = "football"


def date_range(start: datetime, end: datetime) -> Iterator[datetime]:
    """Every UTC day in ``[start, end]`` inclusive, as midnight-UTC datetimes. ``end`` before ``start`` yields
    nothing rather than raising - a caller's bad range is a zero-day plan, not a crash."""
    day = start.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    last = end.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    while day <= last:
        yield day
        day += timedelta(days=1)


def plan(competitions: Iterable[Competition], start: datetime, end: datetime) -> List[tuple]:
    """The (competition, day) pairs a backfill over this range would request - what a dry run reports before
    anything is fetched. One request per pair (ESPN's ``dates`` parameter accepts exactly one day)."""
    days = list(date_range(start, end))
    return [(c, d) for c in competitions if c.espn for d in days]


def _final_rows(espn: ESPNProvider, comp: Competition, events: List[Dict]) -> Iterator[Dict]:
    """Every FINAL match on one already-fetched scoreboard page, as a dict ready for ``HistoryStore.add_result``
    (minus the identity fields, filled in by the caller) - ``None`` fields mean "ESPN gave no usable score",
    never a guess."""
    for c in espn._candidates(comp, events):
        pair = _by_side(c.get("competitors") or [])
        if pair is None:
            continue
        home, away = pair
        result = espn._to_result(comp, c, home, away)
        if result.status != "final" or "full" not in result.scores:
            continue                                        # postponed/cancelled/unknown/incomplete: rejected, not guessed
        when = parse_iso(str(c.get("date") or ""))
        if when is None:
            continue
        hg, ag = result.scores["full"]
        ht = result.scores.get("1h")
        yield {
            "kickoff": when, "home_goals": int(round(hg)), "away_goals": int(round(ag)),
            "home_ht": int(round(ht[0])) if ht else None, "away_ht": int(round(ht[1])) if ht else None,
            "home": home, "away": away,
            "source_match_id": f"{comp.espn}:{c.get('id') or (when.strftime('%Y%m%d') + ':' + str(home.get('team', {}).get('id')) + ':' + str(away.get('team', {}).get('id')))}",
        }


def backfill(db, espn: ESPNProvider, competitions: Iterable[Competition], start: datetime, end: datetime,
             budget: Optional[Budget] = None, now: Optional[datetime] = None) -> Dict[str, dict]:
    """Backfill real completed-match results for ``competitions`` over ``[start, end]`` into ``db``'s
    ``match_results`` table. ``db`` MUST be a local sqlite ``Database`` - this function has no notion of D1 and
    never should; the caller decides what ``db`` is, exactly as every other history ingester does.

    Returns one report entry per (competition, day): ``{"finals": n, "inserted": n, "duplicate": n,
    "rejected_participant": n}`` on success, or ``{"error": "<ProviderError message>"}`` on a failed request -
    a failure on one day never stops the rest (same discipline as ``ingest_football_data``'s per-season
    try/except). A top-level ``"_summary"`` key totals requests made, rows inserted, and days with no finals."""
    now = now or utcnow()
    store, registry = HistoryStore(db), IdentityRegistry(db)
    report: Dict[str, dict] = {}
    requests_made = 0
    totals = Counter()
    espn_competitions = [c for c in competitions if c.espn]
    for comp in espn_competitions:
        for day in date_range(start, end):
            key = f"{comp.id}:{day.strftime('%Y-%m-%d')}"
            cache_key = (comp.espn, day.strftime("%Y%m%d"), comp.espn_groups)
            was_cached = cache_key in espn._days
            try:
                events = espn._scoreboard(comp, day)
            except ProviderError as exc:
                report[key] = {"error": str(exc)}
                totals["request_errors"] += 1
                continue
            if not was_cached:
                requests_made += 1
                if budget is not None:
                    budget.spend("espn", 1)
            counts: Counter = Counter()
            for row in _final_rows(espn, comp, events):
                try:
                    home_names = _names(row["home"])
                    away_names = _names(row["away"])
                    home_id = registry.participant("espn", SPORT, home_names[0],
                                                    str(row["home"].get("team", {}).get("id") or ""), comp.id)
                    away_id = registry.participant("espn", SPORT, away_names[0],
                                                    str(row["away"].get("team", {}).get("id") or ""), comp.id)
                except (ValueError, IndexError):
                    counts["rejected_participant"] += 1
                    continue
                try:
                    outcome = store.add_result(
                        sport=SPORT, competition_id=comp.id, kickoff=row["kickoff"], home_id=home_id,
                        away_id=away_id, home_goals=row["home_goals"], away_goals=row["away_goals"],
                        home_ht=row["home_ht"], away_ht=row["away_ht"], source="espn",
                        source_match_id=row["source_match_id"], now=now)
                except ValueError:
                    counts["rejected_participant"] += 1
                    continue
                counts[outcome] += 1
                counts["finals"] += 1
            report[key] = dict(counts) if counts else {"finals": 0}
            totals.update(counts)
            if not counts:
                totals["empty_days"] += 1
    report["_summary"] = {
        "competitions": [c.id for c in espn_competitions], "days": len(list(date_range(start, end))),
        "requests_made": requests_made, "finals_seen": totals.get("finals", 0),
        "inserted": totals.get("inserted", 0), "duplicate": totals.get("duplicate", 0),
        "rejected_participant": totals.get("rejected_participant", 0),
        "empty_days": totals.get("empty_days", 0), "request_errors": totals.get("request_errors", 0),
    }
    return report
