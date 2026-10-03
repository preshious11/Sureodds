"""Live lifecycle against real SQLite and provider/resolver contracts; no network."""
from datetime import timedelta
from unittest.mock import Mock

import pytest

from predbot.bot import broadcast
from predbot.bot.format import live_match_message
from predbot.config import Config
from predbot.jobs import live_results, recap, settle, stats
from predbot.markets.competitions import lookup
from predbot.model.elo import Elo
from predbot.providers.base import MatchResult
from predbot.providers.chain import ResultsResolver
from predbot.providers.espn import ESPNProvider
from predbot.providers.football_data import FootballDataProvider
from predbot.storage.db import Database
from predbot.util.timeutil import iso, parse_iso
from tests.test_results_espn import _espn, _first

NOW = parse_iso("2030-06-01T15:04:00Z")
DAY = "2030-06-01"


def pick(db, **changes):
    row = dict(pick_date=DAY, tier="top", rank=1, provider="sharpapi", provider_event_id="event",
               sport="football", league="English Premier League", home="Chelsea", away="Arsenal",
               start_utc="2030-06-01T15:00:00Z", market="result3", selection="home", period="full",
               stat="points", entity="all", line=None, label="Chelsea to Win", price=1.8, prob=.7,
               ev=.1, sources={"_league_id": "soccer_epl", "_sport_key": "soccer_epl"})
    row.update(changes)
    return db.insert_pick(row)


def received(db, chat="1", tiers=("top",)):
    db.subscriber_add(chat)
    for tier in tiers:
        db.mark_delivery(f"picks_{tier}", DAY, chat)


class Provider:
    def __init__(self, status="scheduled", source="espn"):
        self.calls = self.runs = 0
        self.result = MatchResult(status, {"full": (2., 1.)} if status == "final" else {}, source=source)

    def begin_run(self):
        self.runs += 1

    def find_result(self, *args, **kwargs):
        self.calls += 1
        return self.result


class Telegram:
    token = "test"

    def __init__(self):
        self.messages = []
        self.fail = set()

    def send(self, text, chat_id=None, **kwargs):
        if chat_id in self.fail:
            return None
        self.messages.append((chat_id, text))
        return "sent"

    def enabled(self):
        return True


@pytest.fixture
def env(db, monkeypatch):
    monkeypatch.setattr(broadcast.time, "sleep", lambda _: None)
    provider = Provider()
    # Any paid call or prediction fetch fails the test, including begin_run.
    forbidden = Mock()
    for method in ("begin_run", "fetch_result", "find_result", "fetch_events"):
        getattr(forbidden, method).side_effect = AssertionError("paid/prediction call during live polling")
    resolver = ResultsResolver({"espn": provider, "oddsapi": forbidden, "sportsgameodds": forbidden})
    cfg = Config(live_updates_enabled=True, live_poll_minutes=5)
    return provider, resolver, cfg, Telegram()


def poll(db, env, now=NOW):
    _, resolver, cfg, tg = env
    live_results.run(resolver, db, cfg, tg, now=now)


def final(env):
    env[0].result = MatchResult("final", {"full": (2., 1.)}, source="espn")


def test_scheduled_espn_after_kickoff_never_starts_or_settles(db, env):
    pick(db)
    received(db)
    env[0].result = ESPNProvider()._to_result(lookup("soccer_epl"),
        {"status": {"type": {"name": "STATUS_SCHEDULED", "state": "pre", "completed": False}}}, {}, {})
    poll(db, env)
    assert not env[3].messages
    assert db.pending_picks()[0]["status"] == "pending"
    assert db.query("SELECT status FROM live_fixture_state")[0]["status"] == "scheduled"


def test_start_final_multiple_tiers_repeated_polls_and_restart(db, env):
    pick(db)
    pick(db, tier="daily", market="total", selection="over", line=3.5, label="Over 3.5 Total Goals")
    received(db, tiers=("top", "daily"))
    poll(db, env)
    # Restart before kickoff transition; the scheduled state is durable.
    path = db.path
    db.close()
    db = Database(path)
    env[0].result = MatchResult("in_progress", source="espn")
    poll(db, env)
    poll(db, env)
    db.close()
    db = Database(path)
    poll(db, env)
    assert len(env[3].messages) == 1
    assert "Chelsea to Win" in env[3].messages[0][1] and "Over 3.5" in env[3].messages[0][1]
    assert env[0].calls == 4  # once per fixture, not per tier
    final(env)
    poll(db, env)
    assert {r["status"] for r in db.picks_for_date(DAY)} == {"won", "lost"}
    assert len(env[3].messages) == 2
    assert "Chelsea 2–1 Arsenal" in env[3].messages[-1][1]
    assert "— WON" in env[3].messages[-1][1] and "— LOST" in env[3].messages[-1][1]
    before = [dict(r) for r in db.picks_for_date(DAY)]
    db.close()
    db = Database(path)
    poll(db, env)
    assert env[0].calls == 5 and len(env[3].messages) == 2
    assert [dict(r) for r in db.picks_for_date(DAY)] == before
    assert Elo(db).get("football", "Chelsea")[1] == 1
    assert len(db.query("SELECT * FROM results_seen")) == 1
    db.close()


def test_final_without_start_settles_before_duration_and_keeps_fallback_gate(db, env, monkeypatch):
    pick(db)
    received(db)
    final(env)
    monkeypatch.setattr(settle, "utcnow", lambda: NOW)
    fallback = ResultsResolver({"espn": env[0]})
    assert settle.run(fallback, db, env[2])["pending"] == 1
    assert env[0].calls == 0  # 4 minutes is less than ordinary football duration
    refit = Mock()
    monkeypatch.setattr(live_results.Calibrator, "refit", refit)
    poll(db, env)
    assert db.picks_for_date(DAY)[0]["status"] == "won"
    assert len(env[3].messages) == 1 and "Match Ended" in env[3].messages[0][1]
    refit.assert_called_once()
    assert settle.run(fallback, db, env[2])["settled"] == 0


def test_recipient_eligibility_and_personalized_predictions(db, env):
    pick(db)
    pick(db, tier="daily", market="total", selection="over", line=2.5, label="Over 2.5 Total Goals")
    pick(db, tier="daily", home="Liverpool", away="Everton", provider_event_id="other")
    received(db, "top", ("top",))
    received(db, "daily", ("daily",))
    received(db, "both", ("top", "daily"))
    received(db, "late", ())
    received(db, "inactive", ("top",))
    db.subscriber_remove("inactive")
    env[0].result = MatchResult("in_progress", source="espn")
    poll(db, env)
    assert env[0].calls == 2
    top = [text for chat, text in env[3].messages if chat == "top"]
    assert len(top) == 1 and "Chelsea to Win" in top[0] and "Over 2.5" not in top[0]
    assert len([1 for chat, _ in env[3].messages if chat == "daily"]) == 2
    assert not any(chat in ("late", "inactive") for chat, _ in env[3].messages)


def test_failed_end_delivery_retries_only_missing_after_restart_without_refetch(db, env):
    pick(db)
    received(db, "1")
    received(db, "2")
    final(env)
    env[3].fail.add("2")
    poll(db, env)
    settled_at = db.picks_for_date(DAY)[0]["settled_at"]
    assert [c for c, _ in env[3].messages] == ["1"]
    path = db.path
    db.close()
    db = Database(path)
    env[3].fail.clear()
    # Delivery can retry even after the stale tracking cutoff.
    poll(db, env, NOW + timedelta(days=6))
    assert env[0].calls == 1
    assert [c for c, _ in env[3].messages] == ["1", "2"]
    assert db.picks_for_date(DAY)[0]["settled_at"] == settled_at
    assert Elo(db).get("football", "Chelsea")[1] == 1
    db.close()


def test_crash_after_persisted_final_recovers_without_lookup(db, env, monkeypatch):
    pick(db)
    received(db)
    final(env)
    original = live_results._settle_fixture
    monkeypatch.setattr(live_results, "_settle_fixture", Mock(side_effect=RuntimeError("crash")))
    with pytest.raises(RuntimeError):
        poll(db, env)
    assert db.pending_picks()
    path = db.path
    db.close()
    db = Database(path)
    monkeypatch.setattr(live_results, "_settle_fixture", original)
    poll(db, env)
    assert not db.pending_picks() and env[0].calls == 1
    assert len(env[3].messages) == 1
    db.close()


def test_crash_between_settlement_and_send_does_not_repeat_grading(db, env, monkeypatch):
    pick(db)
    received(db)
    final(env)
    deliver = live_results.deliver_live
    monkeypatch.setattr(live_results, "deliver_live", Mock(side_effect=RuntimeError("crash")))
    with pytest.raises(RuntimeError):
        poll(db, env)
    assert not db.pending_picks() and not env[3].messages
    path = db.path
    db.close()
    db = Database(path)
    monkeypatch.setattr(live_results, "deliver_live", deliver)
    monkeypatch.setattr(live_results, "_settle_fixture", Mock(side_effect=AssertionError("settled twice")))
    poll(db, env)
    assert env[0].calls == 1 and len(env[3].messages) == 1
    assert Elo(db).get("football", "Chelsea")[1] == 1
    db.close()


@pytest.mark.parametrize("status", ["postponed", "cancelled"])
def test_postponed_cancelled_void_without_false_notification(db, env, status):
    pick(db)
    received(db)
    env[0].result = MatchResult(status, source="espn")
    poll(db, env)
    poll(db, env)
    assert db.picks_for_date(DAY)[0]["status"] == "void"
    assert not env[3].messages and env[0].calls == 1


def test_no_early_or_stale_or_unknown_competition_polling(db, env):
    pick(db, start_utc=iso(NOW + timedelta(hours=2)))
    pick(db, start_utc=iso(NOW - timedelta(days=5)), provider_event_id="old")
    pick(db, league="Unknown League", sources={}, provider_event_id="unsupported")
    poll(db, env)
    assert env[0].calls == 0 and not env[3].messages and len(db.pending_picks()) == 3


def test_subscribers_never_multiply_result_calls(db, env):
    pick(db)
    for i in range(100):
        received(db, str(i))
    env[0].result = MatchResult("in_progress", source="espn")
    poll(db, env)
    assert len(env[3].messages) == 100 and env[0].calls == 1
    poll(db, env)
    assert len(env[3].messages) == 100 and env[0].calls == 2


def test_espn_scoreboard_shared_between_fixtures_in_one_poll(db, env):
    espn = _espn("espn_soccer.json")
    env[1].providers = {"espn": espn}
    for i in range(2):
        home, away, start = _first("espn_soccer.json", i)
        pick(db, home=home, away=away, start_utc=iso(start), provider_event_id=str(i))
    poll(db, env, start + timedelta(minutes=5))
    assert len(espn.http.calls) == 1
    assert not db.pending_picks()


def test_free_source_priority_football_data_fallback_and_euroleague_final_only(db, env):
    pick(db)
    received(db)
    fd = Provider("in_progress", "football_data")
    env[1].providers["football_data"] = fd
    poll(db, env)
    assert fd.calls == 0  # ESPN scheduled is authoritative
    env[0].result = None
    poll(db, env)
    assert fd.calls == 1 and len(env[3].messages) == 1
    pick(db, sport="basketball", league="EuroLeague", sources={"_league_id": "euroleague"},
         home="Olympiacos", away="Real Madrid", provider_event_id="euro")
    euro = Provider("in_progress", "euroleague")
    env[1].providers["euroleague"] = euro
    poll(db, env)
    assert euro.calls == 1 and len(env[3].messages) == 1
    euro.result = MatchResult("final", {"full": (92., 85.)}, source="euroleague")
    poll(db, env)
    assert len(env[3].messages) == 2 and "Match Ended" in env[3].messages[-1][1]


def test_unknown_football_competition_does_not_query_football_data(db, env):
    pick(db, league="Unknown", sources={})
    fd = Provider("in_progress", "football_data")
    env[1].providers = {"football_data": fd}
    poll(db, env)
    assert fd.calls == 0


@pytest.mark.parametrize("available", [True, False])
def test_richer_final_path_once_and_missing_stat_stays_pending(db, env, available):
    pick(db, stat="corners", market="total", selection="over", line=5.5, label="Over 5.5 Corners")
    pick(db, tier="daily", stat="corners", market="total", selection="over", line=6.5,
         label="Over 6.5 Corners")
    received(db, tiers=("top", "daily"))
    final(env)
    richer = Provider("final", "football_data")
    if available:
        richer.result.stats[("corners", "full")] = (5., 3.)
    env[1].providers = {"espn": env[0], "football_data": richer}
    poll(db, env)
    poll(db, env)
    assert env[0].calls == richer.calls == 1
    assert {p["status"] for p in db.picks_for_date(DAY)} == ({"won"} if available else {"pending"})
    assert "— WON" in env[3].messages[0][1] if available else "— PENDING" in env[3].messages[0][1]


def test_paid_result_lookup_only_on_ungradable_final_and_not_on_later_polls(db, env):
    pick(db, provider="sportsgameodds", stat="corners", market="total", selection="over", line=5.5)
    paid = Mock()
    paid.fetch_result.return_value = MatchResult("final", stats={("corners", "full"): (5., 3.)}, source="sportsgameodds")
    env[1].providers["sportsgameodds"] = paid
    poll(db, env)
    paid.fetch_result.assert_not_called()
    final(env)
    poll(db, env)
    poll(db, env)
    paid.fetch_result.assert_called_once()
    paid.begin_run.assert_called_once()
    assert db.picks_for_date(DAY)[0]["status"] == "won"
    assert Elo(db).get("football", "Chelsea")[1] == 1


def test_next_day_recap_reads_live_result_without_resettling(db, env, monkeypatch):
    pick(db)
    final(env)
    poll(db, env)
    before = dict(db.picks_for_date(DAY)[0])
    tomorrow = NOW + timedelta(days=1)
    monkeypatch.setattr(settle, "utcnow", lambda: tomorrow)
    monkeypatch.setattr(recap, "utcnow", lambda: tomorrow)
    assert settle.run(ResultsResolver({"espn": env[0]}), db, env[2])["settled"] == 0
    text = recap.run(db, env[2], None)
    assert text and stats.rows_for_recap(db, DAY)[0]["status"] == "won"
    assert dict(db.picks_for_date(DAY)[0]) == before
    assert Elo(db).get("football", "Chelsea")[1] == 1


def test_live_format_escaping_and_all_outcomes(db):
    pick(db, league="League <&>", home="Chelsea <&>", label="Chelsea <&> to Win")
    row = dict(db.picks_for_date(DAY)[0])
    for status in ("won", "lost", "push", "half_won", "half_lost", "void", "pending"):
        row["status"] = status
        text = live_match_message([row], MatchResult("final", {"full": (2., 1.)}), True)
        assert status.replace("_", " ").upper() in text
        assert "<&>" not in text and "&lt;&amp;&gt;" in text
        assert "soccer_epl" not in text and "fixture_key" not in text and "2030" not in text


def test_disabled_and_invalid_config(db, env):
    pick(db)
    env[2].live_updates_enabled = False
    poll(db, env)
    assert env[0].calls == 0
    with pytest.raises(ValueError, match="LIVE_POLL_MINUTES"):
        Config(live_poll_minutes=0)


@pytest.mark.parametrize("status", ["SCHEDULED", "TIMED"])
def test_football_data_scheduled_is_not_live(status):
    assert FootballDataProvider._to_result({"status": status}).status == "scheduled"


def test_reversed_fixture_legs_keep_score_orientation_and_learn_once(db, env):
    pick(db)
    pick(db, tier="daily", home="Arsenal", away="Chelsea", provider="oddsapi",
         provider_event_id="other-id", market="result3", selection="away", label="Chelsea to Win")
    assert len({r["fixture_key"] for r in db.picks_for_date(DAY)}) == 1
    received(db, "top", ("top",))
    received(db, "daily", ("daily",))
    final(env)
    poll(db, env)
    assert env[0].calls == 1
    assert {r["status"] for r in db.picks_for_date(DAY)} == {"won"}
    assert Elo(db).get("football", "Chelsea")[1] == 1
    assert len(db.query("SELECT * FROM results_seen")) == 1
    assert "Chelsea 2–1 Arsenal" in next(t for c, t in env[3].messages if c == "top")
    assert "Arsenal 1–2 Chelsea" in next(t for c, t in env[3].messages if c == "daily")


def test_normal_and_live_grading_match_and_stale_row_cannot_settle_twice(db, env, monkeypatch):
    pick(db)
    row = db.picks_for_date(DAY)[0]
    final(env)
    poll(db, env)
    early = dict(db.picks_for_date(DAY)[0])
    counts = dict(settled=0, pending=0, void=0, errors=0)
    settle.settle_result(row, env[0].result, db, Elo(db), NOW, counts)
    assert counts["settled"] == 0
    assert dict(db.picks_for_date(DAY)[0]) == early
    # Another day's ordinary fallback grades the same bet/score identically.
    pick(db, pick_date="2030-05-31", start_utc="2030-05-31T15:00:00Z", provider_event_id="yesterday")
    monkeypatch.setattr(settle, "utcnow", lambda: NOW)
    counts = settle.run(ResultsResolver({"espn": env[0]}), db, env[2])
    assert counts["settled"] == 1
    assert db.picks_for_date("2030-05-31")[0]["status"] == early["status"]


def test_fallback_settled_an_active_watch_still_gets_end_alert(db, env, monkeypatch):
    pick(db)
    received(db)
    env[0].result = MatchResult("in_progress", source="espn")
    poll(db, env)
    final(env)
    monkeypatch.setattr(settle, "utcnow", lambda: NOW + timedelta(hours=4))
    assert settle.run(ResultsResolver({"espn": env[0]}), db, env[2])["settled"] == 1
    poll(db, env, NOW + timedelta(hours=4))
    assert len(env[3].messages) == 2
    assert "Match Ended" in env[3].messages[-1][1]
    assert Elo(db).get("football", "Chelsea")[1] == 1


def test_additive_schema_keeps_existing_data(db):
    pick(db)
    received(db)
    db.set_kv("recap_text", "existing recap")
    db.execute("INSERT INTO provider_cache VALUES (?,?,?,?,?,?)",
               ("provider", DAY, "{}", "{}", "2031-01-01", 1))
    db.execute("INSERT INTO prediction_cache VALUES (?,?,?,?,?,?,?,?,?,?,?)",
               ("prediction", DAY, "UTC", "complete", "{}", "{}", iso(NOW), iso(NOW),
                "2031-01-01", None, 1))
    # Recreate the state of a pre-feature DB, then run the real startup migration.
    before = {table: [dict(r) for r in db.query(f"SELECT * FROM {table}")]
              for table in ("picks", "subscribers", "deliveries", "prediction_cache", "provider_cache", "kv")}
    db.execute("DROP TABLE live_fixture_state")
    path = db.path
    db.close()
    reopened = Database(path)
    assert reopened.query("SELECT * FROM live_fixture_state") == []
    for table, rows in before.items():
        assert [dict(r) for r in reopened.query(f"SELECT * FROM {table}")] == rows
    reopened.close()


@pytest.mark.parametrize("enabled", [True, False])
def test_scheduler_installs_optional_interval_without_startup_poll(db, env, monkeypatch, enabled):
    import main
    cfg = Config(live_updates_enabled=enabled, live_poll_minutes=3, enable_commands=False)
    monkeypatch.setattr(main, "load_config", lambda: cfg)
    monkeypatch.setattr(main, "setup_logging", lambda *args, **kwargs: None)
    monkeypatch.setattr(main, "require_provider_keys", lambda *args: None)
    monkeypatch.setattr(main, "Database", lambda _: db)
    monkeypatch.setattr(main, "build_providers", lambda *args: {})
    monkeypatch.setattr(main, "log_coverage", lambda *args: None)
    monkeypatch.setattr(main, "UnifiedFetcher", Mock())
    run = Mock()
    monkeypatch.setattr(main.live_results, "run", run)
    jobs = []
    monkeypatch.setattr(main, "loop", lambda items, **kwargs: jobs.extend(items))
    main.main()
    run.assert_not_called()
    assert any(isinstance(j, main.DynamicJob) and j.name == "settle" for j in jobs)
    assert any(j.name == "recap" for j in jobs)
    live = [j for j in jobs if j.name == "live_results"]
    assert len(live) == int(enabled)
    if enabled:
        assert isinstance(live[0], main.IntervalJob) and live[0].every == timedelta(minutes=3)


def test_overlapping_poll_is_skipped(db, env):
    from threading import Event, Thread
    pick(db)
    entered, release = Event(), Event()

    def hold():
        with env[1].run_lock:
            entered.set()
            release.wait(5)

    thread = Thread(target=hold)
    thread.start()
    try:
        assert entered.wait(2)
        poll(db, env)
        assert env[0].calls == 0
    finally:
        release.set()
        thread.join(2)
