"""Bounded result lookups with real SQLite/resolver paths and no network calls."""
from datetime import timedelta
import json
from unittest.mock import Mock

import pytest

from predbot.bot import pagination
from predbot.bot.format import recap_message, stats_message
from predbot.history.store import HistoryStore
from predbot.jobs import live_results, settle, stats
from predbot.model.elo import Elo
from predbot.providers.base import MatchResult
from predbot.providers.chain import ResultsResolver
from predbot.storage.db import Database
from predbot.util.duration import expected_end, live_cutoff, settlement_deadline
from predbot.util.timeutil import iso, parse_iso
from tests.test_live_results import Provider, Telegram, pick
from predbot.config import Config


START = parse_iso("2030-06-01T15:00:00Z")
SPORTS = ("football", "tennis", "basketball", "baseball", "american_football", "mma", "other")
LEAGUES = {"football": "soccer_epl", "tennis": "atp", "basketball": "nba",
           "baseball": "mlb", "american_football": "nfl"}


@pytest.fixture
def lifecycle(db, monkeypatch):
    clock = [START]
    monkeypatch.setattr(settle, "utcnow", lambda: clock[0])
    provider = Provider("unknown")
    paid = Mock(spec=["begin_run", "fetch_result", "find_result"])
    paid.fetch_result.side_effect = AssertionError("unexpected paid lookup")
    paid.find_result.side_effect = AssertionError("unexpected paid lookup")
    resolver = ResultsResolver({"espn": provider, "oddsapi": paid, "sportsgameodds": paid})
    cfg = Config(timezone="UTC", live_updates_enabled=True, subscriber_delivery_enabled=False)
    return clock, provider, resolver, cfg, Telegram()


def event(db, sport="football", **changes):
    row = dict(sport=sport, start_utc=iso(START), sources={"_league_id": LEAGUES.get(sport, "")},
               league=LEAGUES.get(sport, ""), market="result3" if sport == "football" else "result2")
    row.update(changes)
    return pick(db, **row)


def status(db, pid):
    return db.query("SELECT status FROM picks WHERE id=?", (pid,))[0]["status"]


def live(db, lifecycle):
    clock, _, resolver, cfg, tg = lifecycle
    live_results.run(resolver, db, cfg, tg, now=clock[0])


def fallback(db, lifecycle):
    _, _, resolver, cfg, _ = lifecycle
    return settle.run(resolver, db, cfg)


@pytest.mark.parametrize("sport,duration,live_grace,settlement_grace", [
    ("football", 2.2, 2, 24), ("tennis", 3, 6, 48), ("basketball", 2.8, 3, 24),
    ("baseball", 3.6, 6, 48), ("american_football", 3.6, 4, 24),
    ("mma", 3, 3, 24), ("other", 3, 3, 24),
])
def test_exact_sport_deadlines(sport, duration, live_grace, settlement_grace):
    assert expected_end(START, sport) == START + timedelta(hours=duration)
    assert live_cutoff(START, sport) == START + timedelta(hours=duration + live_grace)
    assert settlement_deadline(START, sport) == START + timedelta(hours=duration + settlement_grace)
    assert live_cutoff(START, sport) < settlement_deadline(START, sport)


@pytest.mark.parametrize("sport", LEAGUES)
def test_active_event_and_single_unknown_allow_live_without_voiding(db, lifecycle, sport):
    pid = event(db, sport)
    lifecycle[0][0] = START + timedelta(minutes=5)
    live(db, lifecycle)
    assert lifecycle[1].calls == 1 and status(db, pid) == "pending"
    assert db.live_state("2030-06-01", db.picks_for_date("2030-06-01")[0]["fixture_key"])["status"] == "unknown"


@pytest.mark.parametrize("sport", LEAGUES)
def test_live_cutoff_stops_old_polls_but_allows_bounded_settlement(db, lifecycle, sport):
    pid = event(db, sport)
    lifecycle[0][0] = live_cutoff(START, sport) - timedelta(seconds=1)
    live(db, lifecycle)
    assert lifecycle[1].calls == 1
    lifecycle[0][0] = live_cutoff(START, sport)
    live(db, lifecycle)
    live(db, lifecycle)
    assert lifecycle[1].calls == 1 and status(db, pid) == "pending"
    assert fallback(db, lifecycle)["pending"] == 1
    assert lifecycle[1].calls == 2 and status(db, pid) == "pending"


@pytest.mark.parametrize("sport", LEAGUES)
def test_future_event_never_calls_live_providers(db, lifecycle, sport):
    pid = event(db, sport)
    lifecycle[0][0] = START - timedelta(seconds=1)
    live(db, lifecycle)
    assert lifecycle[1].calls == 0 and status(db, pid) == "pending"


@pytest.mark.parametrize("sport", SPORTS)
@pytest.mark.parametrize("job", ["live", "settle"])
def test_deadline_voids_before_all_provider_calls_and_never_queries_again(db, lifecycle, sport, job):
    pid = event(db, sport)
    # A durable unknown/in-progress watcher must not resurrect an expired fixture.
    row = db.picks_for_date("2030-06-01")[0]
    db.save_live_result(row["pick_date"], row["fixture_key"], MatchResult("unknown"), iso(START))
    lifecycle[0][0] = settlement_deadline(START, sport)
    resolver = lifecycle[2]
    resolver.resolve = Mock(wraps=resolver.resolve)
    resolver._ask = Mock(wraps=resolver._ask)
    if job == "live":
        live(db, lifecycle)
    else:
        assert fallback(db, lifecycle)["void"] == 1
    assert status(db, pid) == "void"
    persisted = dict(db.query("SELECT * FROM picks WHERE id=?", (pid,))[0])
    assert persisted["settled_at"] and json.loads(persisted["result_json"])["reason"] == "settlement deadline expired"
    assert db.pending_picks() == []
    lifecycle[0][0] += timedelta(days=20)
    live(db, lifecycle)
    assert fallback(db, lifecycle)["void"] == 0
    assert dict(db.query("SELECT * FROM picks WHERE id=?", (pid,))[0]) == persisted
    assert lifecycle[1].calls == 0
    resolver.resolve.assert_not_called()
    resolver._ask.assert_not_called()


def test_restart_does_not_resurrect_expired_void(db, lifecycle):
    pid = event(db)
    lifecycle[0][0] = settlement_deadline(START, "football")
    live(db, lifecycle)
    path = db.path
    db.close()
    reopened = Database(path)
    try:
        live(reopened, lifecycle)
        fallback(reopened, lifecycle)
        assert status(reopened, pid) == "void" and lifecycle[1].calls == 0
    finally:
        reopened.close()


@pytest.mark.parametrize("sport", LEAGUES)
@pytest.mark.parametrize("scores,outcome", [((2., 1.), "won"), ((1., 2.), "lost")])
def test_recently_finished_event_settles_normally_before_deadline(db, lifecycle, sport, scores, outcome):
    pid = event(db, sport)
    lifecycle[0][0] = expected_end(START, sport) + timedelta(minutes=15)
    lifecycle[1].result = MatchResult("final", {"full": scores}, source="espn")
    counts = fallback(db, lifecycle)
    assert counts["settled"] == 1 and counts["void"] == 0
    assert status(db, pid) == outcome and lifecycle[1].calls == 1
    fallback(db, lifecycle)
    assert lifecycle[1].calls == 1


@pytest.mark.parametrize("answer", [None, MatchResult("unknown"), MatchResult("in_progress"),
                                  MatchResult("final", {}, source="espn")])
def test_unavailable_or_ungradable_result_does_not_void_early(db, lifecycle, answer):
    pid = event(db)
    # Restrict to the free result path to exercise None without a paid fallback.
    lifecycle[2].providers = {"espn": lifecycle[1]}
    lifecycle[1].result = answer
    lifecycle[0][0] = expected_end(START, "football") + timedelta(minutes=15)
    assert fallback(db, lifecycle)["pending"] == 1
    assert status(db, pid) == "pending" and lifecycle[1].calls == 1
    lifecycle[0][0] = settlement_deadline(START, "football")
    assert fallback(db, lifecycle)["void"] == 1
    assert status(db, pid) == "void" and lifecycle[1].calls == 1


@pytest.mark.parametrize("after_deadline", [False, True])
def test_saved_live_final_is_used_before_any_provider(db, lifecycle, after_deadline):
    pid = event(db)
    row = db.picks_for_date("2030-06-01")[0]
    db.save_live_result(row["pick_date"], row["fixture_key"],
                        MatchResult("final", {"full": (2., 1.)}, source="espn"), iso(START))
    lifecycle[0][0] = settlement_deadline(START, "football") if after_deadline else expected_end(START, "football")
    assert fallback(db, lifecycle)["settled"] == 1
    assert status(db, pid) == "won" and lifecycle[1].calls == 0


def test_local_historical_final_grades_without_provider_at_deadline(db, lifecycle):
    pid = event(db, canonical_competition_id="epl", home_id="chelsea-id", away_id="arsenal-id")
    row = db.picks_for_date("2030-06-01")[0]
    assert row["canonical_competition_id"] and row["home_id"] and row["away_id"]
    HistoryStore(db).add_result(sport="football", competition_id=row["canonical_competition_id"], kickoff=START,
                               home_id=row["home_id"], away_id=row["away_id"], home_goals=2, away_goals=1,
                               source="test-history", source_match_id="known-final")
    lifecycle[0][0] = settlement_deadline(START, "football")
    assert fallback(db, lifecycle)["settled"] == 1
    assert status(db, pid) == "won" and lifecycle[1].calls == 0


def test_missing_stat_on_persisted_final_expires_without_richer_provider_lookup(db, lifecycle):
    pid = event(db, market="total", stat="corners", selection="over", line=5.5)
    row = db.picks_for_date("2030-06-01")[0]
    db.save_live_result(row["pick_date"], row["fixture_key"],
                        MatchResult("final", {"full": (2., 1.)}, source="espn"), iso(START))
    lifecycle[0][0] = settlement_deadline(START, "football")
    live(db, lifecycle)
    assert status(db, pid) == "void" and lifecycle[1].calls == 0
    assert db.live_state(row["pick_date"], row["fixture_key"])["notifications_done"] == 1


def test_void_excluded_from_win_loss_roi_and_learning_and_visible_in_history(db, lifecycle):
    pid = event(db)
    lifecycle[0][0] = settlement_deadline(START, "football")
    fallback(db, lifecycle)
    rows = db.picks_for_date("2030-06-01", published_only=True)
    block = stats.day_block(rows)
    assert block["won"] == block["lost"] == block["n"] == 0
    assert block["void"] == 1 and block["final"] and block["profit"] == 0
    assert block["roi"] is None and block["hit_rate"] is None
    assert db.all_settled_for_calibration() == [] and db.query("SELECT * FROM results_seen") == []
    assert Elo(db).get("football", "Chelsea")[1] == 0
    raw = stats.compute(db, START + timedelta(days=1))
    assert raw["yesterday"]["void"] == 1 and raw["last7"]["won"] == raw["last7"]["lost"] == 0
    for text in (recap_message(raw), stats_message(raw),
                 pagination.render_yesterday(db, day="2030-06-01")[0],
                 pagination.render_collection_results(db, "top", 0, 5, "2030-06-01")[0]):
        assert "➖" in text  # the existing VOID result mark, retained in all history views
    assert status(db, pid) == "void" and len(rows) == 1


def test_expired_rows_do_not_enter_parallel_prefetch(db, lifecycle):
    old = event(db)
    event(db, sport="baseball", provider_event_id="fresh", start_utc=iso(START + timedelta(days=1)),
          pick_date="2030-06-02")
    lifecycle[0][0] = settlement_deadline(START, "football")
    lifecycle[1].result = MatchResult("final", {"full": (3., 1.)}, source="espn")
    resolver = lifecycle[2]
    resolver.resolve = Mock(wraps=resolver.resolve)
    counts = fallback(db, lifecycle)
    assert counts["void"] == 1 and counts["pending"] == 1
    assert status(db, old) == "void"
    resolver.resolve.assert_not_called()
    assert lifecycle[1].calls == 0


def test_parallel_prefetch_excludes_expired_paid_fixture_but_settles_due_sports(db, lifecycle):
    old = event(db, provider="sportsgameodds", provider_event_id="expired-paid")
    lifecycle[0][0] = settlement_deadline(START, "football")
    fresh_start = lifecycle[0][0] - timedelta(hours=5)
    event(db, home="New Home", provider_event_id="fresh-football", pick_date="2030-06-02",
          start_utc=iso(fresh_start))
    event(db, "baseball", home="New US Home", provider_event_id="fresh-baseball", pick_date="2030-06-02",
          start_utc=iso(fresh_start))
    free = Provider("final", "football_data")
    lifecycle[2].providers["football_data"] = free
    lifecycle[1].result = MatchResult("final", {"full": (3., 1.)}, source="espn")
    resolver = lifecycle[2]
    resolver.resolve = Mock(wraps=resolver.resolve)
    counts = fallback(db, lifecycle)
    assert counts["settled"] == 2 and counts["void"] == 1 and status(db, old) == "void"
    assert free.calls == lifecycle[1].calls == 1
    assert all(call.args[1] != "expired-paid" for call in resolver.resolve.call_args_list)
    resolver.providers["sportsgameodds"].fetch_result.assert_not_called()


def test_settled_leg_reuses_persisted_rich_result_with_reversed_orientation(db, lifecycle):
    winner = event(db)
    corners = event(db, tier="daily", market="total", stat="corners", selection="over", line=5.5,
                    home="Arsenal", away="Chelsea", provider_event_id="reversed-leg")
    row = db.query("SELECT * FROM picks WHERE id=?", (winner,))[0]
    result = MatchResult("final", {"full": (2., 1.)}, stats={("corners", "full"): (5., 3.)}, source="espn")
    counts = dict(settled=0, pending=0, void=0, errors=0)
    settle.settle_result(row, result, db, Elo(db), START, counts)
    lifecycle[0][0] = expected_end(START, "football")
    assert fallback(db, lifecycle)["settled"] == 1
    assert status(db, corners) == "won" and lifecycle[1].calls == 0
    assert json.loads(db.query("SELECT result_json FROM picks WHERE id=?", (corners,))[0][0])["stats"] == [
        ["corners", "full", [3., 5.]]]


def test_void_from_cancelled_fixture_never_restarts_nonterminal_watcher(db, lifecycle):
    pid = event(db)
    row = db.picks_for_date("2030-06-01")[0]
    db.save_live_result(row["pick_date"], row["fixture_key"], MatchResult("in_progress"), iso(START))
    db.settle_pick(pid, "void", {"status": "cancelled"})
    lifecycle[0][0] = START + timedelta(minutes=10)
    live(db, lifecycle)
    fallback(db, lifecycle)
    assert lifecycle[1].calls == 0 and status(db, pid) == "void"
    assert db.live_state(row["pick_date"], row["fixture_key"])["notifications_done"] == 1


def test_provider_fallback_rechecks_deadline_after_slow_unknown(db, lifecycle):
    pid = event(db)
    deadline = settlement_deadline(START, "football")
    lifecycle[0][0] = deadline - timedelta(seconds=1)
    free = lifecycle[1]
    original = free.find_result

    def crosses_deadline(*args, **kwargs):
        result = original(*args, **kwargs)
        lifecycle[0][0] = deadline
        return result

    free.find_result = crosses_deadline
    counts = fallback(db, lifecycle)
    assert free.calls == 1 and counts["void"] == 1 and status(db, pid) == "void"
    lifecycle[2].providers["oddsapi"].fetch_result.assert_not_called()
    lifecycle[2].providers["sportsgameodds"].find_result.assert_not_called()


def test_mixed_sports_schedule_uses_latest_expected_completion_not_latest_start(db):
    event(db, "baseball", start_utc=iso(START), provider_event_id="long")
    event(db, "football", start_utc=iso(START + timedelta(hours=1)), provider_event_id="short")
    assert settle.compute_settlement_deadline(db, "2030-06-01") == expected_end(START, "baseball") + timedelta(minutes=15)
