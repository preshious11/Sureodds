"""predbot.history.espn_backfill: idempotent, bounded ESPN result backfill for competitions football-data.org
and the CSV importer cannot reach (UEL, UECL, Super Lig, MLS, ...). No network: every test preloads
``ESPNProvider._days`` directly or stubs ``http.get``, so this proves the backfill's own logic (parsing,
identity, idempotency, request accounting, status rejection) without depending on ESPN actually being up."""
from __future__ import annotations

from datetime import datetime, timezone

from predbot.history import espn_backfill
from predbot.history.store import HistoryStore
from predbot.markets.competitions import BY_ID
from predbot.providers.budget import Budget
from predbot.providers.espn import ESPNProvider

UEL = BY_ID["uel"]
START = datetime(2026, 4, 30, tzinfo=timezone.utc)
END = datetime(2026, 4, 30, tzinfo=timezone.utc)


def _event(day_iso, home_name, home_id, home_score, away_name, away_id, away_score, status="STATUS_FULL_TIME"):
    completed = status in ("STATUS_FULL_TIME", "STATUS_FINAL")
    return {
        "date": day_iso,
        "competitions": [{
            "date": day_iso,
            "status": {"type": {"name": status, "state": "post" if completed else "pre", "completed": completed}},
            "competitors": [
                {"homeAway": "home", "winner": home_score > away_score, "score": str(home_score),
                 "team": {"id": home_id, "displayName": home_name}},
                {"homeAway": "away", "winner": away_score > home_score, "score": str(away_score),
                 "team": {"id": away_id, "displayName": away_name}},
            ],
        }],
    }


def _cache_key(comp, day):
    return (comp.espn, day.strftime("%Y%m%d"), comp.espn_groups)


def test_finals_are_ingested_and_a_rerun_is_idempotent(db):
    espn = ESPNProvider()
    espn._days[_cache_key(UEL, START)] = [
        _event("2026-04-30T19:00Z", "Rayo Vallecano", "101", 1, "Strasbourg", "202", 0),
        _event("2026-04-30T19:00Z", "Shakhtar Donetsk", "303", 1, "Crystal Palace", "404", 3),
    ]
    report = espn_backfill.backfill(db, espn, [UEL], START, END)
    assert report["_summary"]["inserted"] == 2
    assert report["_summary"]["requests_made"] == 0          # the page was already cached: no HTTP call

    rows = HistoryStore(db).results(competition_id="uel")
    assert len(rows) == 2
    assert {(r.home_goals, r.away_goals) for r in rows} == {(1, 0), (1, 3)}

    again = espn_backfill.backfill(db, espn, [UEL], START, END)
    assert again["_summary"]["inserted"] == 0
    assert again["_summary"]["duplicate"] == 2
    assert len(HistoryStore(db).results(competition_id="uel")) == 2       # no duplicate rows on rerun


def test_postponed_and_cancelled_fixtures_are_rejected_not_guessed(db):
    espn = ESPNProvider()
    espn._days[_cache_key(UEL, START)] = [
        _event("2026-04-30T19:00Z", "Rayo Vallecano", "101", 0, "Strasbourg", "202", 0, status="STATUS_POSTPONED"),
        _event("2026-04-30T19:00Z", "Shakhtar Donetsk", "303", 0, "Crystal Palace", "404", 0, status="STATUS_CANCELED"),
        _event("2026-04-30T19:00Z", "Celtic", "505", 0, "Braga", "606", 0, status="STATUS_SCHEDULED"),
    ]
    report = espn_backfill.backfill(db, espn, [UEL], START, END)
    assert report["_summary"]["inserted"] == 0
    assert report["_summary"]["finals_seen"] == 0
    assert HistoryStore(db).results(competition_id="uel") == []


def test_request_is_made_once_per_uncached_day_and_counted_in_budget(db):
    calls = []

    def fake_get(path, params):
        calls.append((path, dict(params)))
        return {"events": [_event("2026-04-30T19:00Z", "Rayo Vallecano", "101", 2, "Strasbourg", "202", 1)]}

    espn = ESPNProvider()
    espn.http.get = fake_get
    budget = Budget(db)

    report = espn_backfill.backfill(db, espn, [UEL], START, END, budget=budget)
    assert len(calls) == 1                                    # one (competition, day) pair requested once
    assert report["_summary"]["requests_made"] == 1
    assert budget.used_today("espn") == 1

    # rerunning over the SAME provider instance hits its own cache: zero further requests
    report2 = espn_backfill.backfill(db, espn, [UEL], START, END, budget=budget)
    assert len(calls) == 1
    assert report2["_summary"]["requests_made"] == 0
    assert budget.used_today("espn") == 1


def test_a_failed_request_is_reported_per_day_and_does_not_abort_the_run(db):
    from predbot.util.http import ProviderError

    other = BY_ID["uecl"]

    def flaky_get(path, params):
        raise ProviderError("espn", 503, "temporary")

    espn = ESPNProvider()
    espn.http.get = flaky_get

    report = espn_backfill.backfill(db, espn, [other], START, END)
    key = f"uecl:{START.strftime('%Y-%m-%d')}"
    assert "error" in report[key]
    assert report["_summary"]["request_errors"] == 1


def test_plan_lists_one_competition_day_pair_per_day_without_fetching_anything(db):
    pairs = espn_backfill.plan([UEL], START, END)
    assert pairs == [(UEL, START)]
