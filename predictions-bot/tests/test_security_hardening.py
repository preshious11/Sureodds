"""Batch 2 - security and API-cost protection.

Leader lease (one worker per database), SGO transport errors that must not disable SGO for the day, a provider
diagnostic that is read-only unless --live, HTTP-layer safety (split timeouts, no credential forwarding across hosts,
body cap, Retry-After, no sleep after the last attempt), identifier redaction, budgets net of today's spend, the
Odds API settlement reserve and repository / build-context protection. No test makes a real network request: every
transport is a fake, and the diagnostic test makes any real one raise."""
from __future__ import annotations

import contextlib
import io
import logging
import re
import socket
import threading
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests
from requests.structures import CaseInsensitiveDict

from predbot.providers.budget import Budget
from predbot.providers.sportsgameodds import SportsGameOddsProvider
from predbot.storage.db import Database
from predbot.storage.lease import LeaderLease, wait_for_leadership
from predbot.util.http import Http, ProviderError, retry_after_seconds

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
START = datetime(2030, 6, 1, 12, 0, tzinfo=timezone.utc)
END = datetime(2030, 6, 1, 23, 0, tzinfo=timezone.utc)


# ============================================================================== leader lease
class Clock:
    """Wall clock and monotonic clock under the test's control."""

    def __init__(self, t: float = 1_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def lease(db, owner, clock, **kw):
    return LeaderLease(db, owner=owner, clock=clock, monotonic=clock, ttl=kw.pop("ttl", 120.0),
                       renew_every=kw.pop("renew_every", 30.0), margin=kw.pop("margin", 40.0), **kw)


def row(db):
    rows = db.query("SELECT owner, token, expires_at FROM leader_lease WHERE name='worker'")
    return dict(rows[0]) if rows else None


def test_first_owner_acquires_and_is_the_leader(db):
    clock = Clock()
    a = lease(db, "host-a:1", clock)
    assert a.acquire() and a.held()
    assert row(db)["owner"] == "host-a:1" and row(db)["expires_at"] == clock.t + 120.0


def test_second_owner_is_rejected_while_the_first_is_healthy(db):
    clock = Clock()
    a, b = lease(db, "host-a:1", clock), lease(db, "host-b:1", clock)
    assert a.acquire()
    clock.t += 100                                     # still inside the TTL
    assert not b.acquire() and not b.held()
    assert row(db)["owner"] == "host-a:1"


def test_owner_renews_and_pushes_the_expiry_out(db):
    clock = Clock()
    a = lease(db, "host-a:1", clock)
    a.acquire()
    clock.t += 30
    assert a.renew() and a.held()
    assert row(db)["expires_at"] == clock.t + 120.0
    clock.t += 100                                     # 130 s after acquiring, 100 s after the renewal
    assert not lease(db, "host-b:1", clock).acquire()


def test_an_expired_lease_can_be_taken_over(db):
    clock = Clock()
    a, b = lease(db, "host-a:1", clock), lease(db, "host-b:1", clock)
    a.acquire()
    clock.t += 121                                     # a crashed: never renewed
    assert b.acquire() and b.held()
    assert row(db)["owner"] == "host-b:1"


def test_a_stale_owner_cannot_renew_after_a_takeover_and_knows_it_lost(db):
    clock = Clock()
    a, b = lease(db, "host-a:1", clock), lease(db, "host-b:1", clock)
    a.acquire()
    clock.t += 121
    b.acquire()
    assert not a.renew()
    assert a.lost and not a.held()
    assert not a.acquire()                             # lost is permanent for that process
    assert row(db)["owner"] == "host-b:1"              # the successor's row is untouched
    a.release()                                        # a late release must not evict the successor
    assert row(db)["owner"] == "host-b:1"


def test_a_holder_that_cannot_renew_fences_itself_before_anyone_may_take_over(db):
    clock = Clock()
    a, b = lease(db, "host-a:1", clock), lease(db, "host-b:1", clock)
    a.acquire()
    clock.t += 79
    assert a.held()                                    # renewals failing (db locked...) but still inside the fence
    clock.t += 2                                       # 81 s: past ttl - margin (80 s)
    assert not a.held()
    assert not b.acquire()                             # ...and nobody can take over until 120 s: no overlap
    clock.t += 40
    assert b.acquire()
    assert not a.renew() and a.lost


def test_clean_release_lets_the_next_process_start_at_once(db):
    clock = Clock()
    a, b = lease(db, "host-a:1", clock), lease(db, "host-b:1", clock)
    a.acquire()
    a.release()
    assert row(db) is None and not a.held()
    assert b.acquire()


def test_restart_after_a_crash_waits_for_expiry_and_after_a_clean_stop_does_not(tmp_path):
    path = str(tmp_path / "shared.db")
    clock = Clock()
    first = lease(Database(path), "host-a:1:aaaa", clock)
    assert first.acquire()
    # the process dies without releasing; the restarted process has a new pid and a new random owner id
    restarted = lease(Database(path), "host-a:7:bbbb", clock)
    assert not restarted.acquire()
    clock.t += 121
    assert restarted.acquire()
    # a clean stop releases, so the next restart is immediate
    restarted.release()
    assert lease(Database(path), "host-a:9:cccc", clock).acquire()


def test_concurrent_acquisition_has_exactly_one_winner(tmp_path):
    path = str(tmp_path / "shared.db")
    Database(path).close()
    n = 8
    barrier, results = threading.Barrier(n), []

    def contend(i):
        own = LeaderLease(Database(path), owner=f"host-{i}:1")       # own connection, like another process
        barrier.wait()
        results.append(own.acquire())

    threads = [threading.Thread(target=contend, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert sorted(results) == [False] * (n - 1) + [True]


def test_heartbeat_reports_the_loss_when_another_owner_takes_over(db):
    a = LeaderLease(db, owner="host-a:1", ttl=3.0, renew_every=0.05, margin=1.0)
    assert a.acquire()
    lost = threading.Event()
    a.start_heartbeat(on_lost=lost.set, tick=0.02)
    db.execute("UPDATE leader_lease SET owner='host-b:1', token='other'")      # a successor holds it now
    assert lost.wait(5)
    assert a.lost and not a.held()
    a.release()
    assert row(db)["owner"] == "host-b:1"


def test_standby_waits_without_working_and_takes_over_when_the_holder_leaves(db):
    holder = LeaderLease(db, owner="host-a:1")
    holder.acquire()
    standby = LeaderLease(db, owner="host-b:1", ttl=3.0, renew_every=0.05, margin=1.0)
    stop = threading.Event()
    result = []
    t = threading.Thread(target=lambda: result.append(wait_for_leadership(standby, stop)))
    t.start()
    t.join(0.3)
    assert t.is_alive() and not standby.held()          # still waiting: the holder is healthy
    holder.release()
    t.join(5)
    assert result == [True] and standby.held()
    stop.set()


def test_invalid_lease_timing_is_refused(db):
    with pytest.raises(ValueError):
        LeaderLease(db, ttl=60, renew_every=30, margin=40)


# ------------------------------------------------------------------------------ the worker and the lease
def _patch_main(monkeypatch, db, cfg):
    import main
    monkeypatch.setattr(main, "load_config", lambda: cfg)
    monkeypatch.setattr(main, "setup_logging", lambda *a, **k: None)
    monkeypatch.setattr(main, "require_provider_keys", lambda *a: None)
    monkeypatch.setattr(main, "Database", lambda _: db)
    monkeypatch.setattr(main, "build_providers", lambda *a: {})
    monkeypatch.setattr(main, "log_coverage", lambda *a: None)
    monkeypatch.setattr(main, "UnifiedFetcher", Mock())
    return main


def test_a_second_worker_on_the_same_database_starts_no_scheduler_and_no_polling(db, monkeypatch):
    from predbot.config import Config
    main = _patch_main(monkeypatch, db, Config(enable_commands=True, telegram_token="1:fake"))
    LeaderLease(db, owner="production:1").acquire()
    attempts, loop, commands = [], Mock(), Mock()
    monkeypatch.setattr(main, "loop", loop)
    monkeypatch.setattr(main, "CommandLoop", commands)
    monkeypatch.setattr(main, "wait_for_leadership", lambda lease, stop: attempts.append(lease.acquire()) or False)
    main.main()
    assert attempts == [False]
    loop.assert_not_called()
    commands.assert_not_called()
    assert row(db)["owner"] == "production:1"


def test_the_leader_releases_on_a_clean_stop(db, monkeypatch):
    from predbot.config import Config
    main = _patch_main(monkeypatch, db, Config(enable_commands=False))
    seen = {}

    def fake_loop(jobs, **kw):
        seen.update(kw)
        assert kw["leader_check"]() and row(db) is not None

    monkeypatch.setattr(main, "loop", fake_loop)
    main.main()
    assert row(db) is None                              # released for the successor
    assert isinstance(seen["stop_event"], threading.Event)


def test_a_leader_that_loses_the_lease_stops_and_exits_without_touching_the_successor(db, monkeypatch):
    from predbot.config import Config
    main = _patch_main(monkeypatch, db, Config(enable_commands=False))

    def fake_loop(jobs, leader_check, stop_event, **kw):
        db.execute("UPDATE leader_lease SET owner='successor:1', token='t'")
        leader_check.__self__.renew()                   # what the heartbeat does
        assert not leader_check()

    monkeypatch.setattr(main, "loop", fake_loop)
    with pytest.raises(SystemExit) as exc:
        main.main()
    assert exc.value.code == main.EXIT_LEASE_LOST
    assert row(db)["owner"] == "successor:1"


def test_the_scheduler_dispatches_nothing_once_leadership_is_gone(monkeypatch):
    from predbot.jobs import scheduler
    monkeypatch.setattr(scheduler.signal, "signal", lambda *a: None)
    job = Mock()
    job.due.return_value = True
    scheduler.loop([job], poll_seconds=0, leader_check=lambda: False, stop_event=threading.Event())
    job.run.assert_not_called()


def test_the_scheduler_stops_mid_tick_when_leadership_ends(monkeypatch):
    from predbot.jobs import scheduler
    monkeypatch.setattr(scheduler.signal, "signal", lambda *a: None)
    answers = iter([True, True, False])
    first, second = Mock(), Mock()
    first.due.return_value = second.due.return_value = True
    scheduler.loop([first, second], poll_seconds=0, leader_check=lambda: next(answers, False),
                   stop_event=threading.Event())
    first.run.assert_called_once()
    second.run.assert_not_called()


def _poller(leader_answers, updates):
    from predbot.bot.commands import CommandLoop
    loop = CommandLoop.__new__(CommandLoop)
    loop._stop_event, loop._admin_threads, loop.offset = threading.Event(), [], None
    answers = iter(leader_answers)
    loop.leader_check = lambda: next(answers, False)
    loop.tg = Mock()
    loop.tg.interactive = contextlib.nullcontext
    loop.tg.get_updates.return_value = updates
    loop.db = Mock()
    loop.handled = []
    loop._handle_update = loop.handled.append
    return loop


def test_telegram_polling_never_starts_without_leadership():
    loop = _poller([False], [{"update_id": 1}])
    loop._poll()
    loop.tg.get_updates.assert_not_called()


def test_telegram_polling_stops_and_leaves_the_rest_of_a_batch_to_the_new_leader():
    loop = _poller([True, True, False], [{"update_id": 5}, {"update_id": 6}])
    loop._poll()
    assert [u["update_id"] for u in loop.handled] == [5]
    assert loop.offset == 6 and loop._stop_event.is_set()


# ============================================================================== SGO: network errors
class TransportFailure(Http):
    """The real HTTP layer with a transport that cannot connect (its error text carries the URL, leagueID included)."""

    def __init__(self, exc=requests.ConnectionError):
        super().__init__("https://api.sportsgameodds.com/v2", headers={"X-Api-Key": "fake-key"}, retries=0,
                         sleep_on_429=False)
        self.attempts = 0
        self.exc = exc

        def get(url, params=None, **kw):
            self.attempts += 1
            raise self.exc(f"HTTPSConnectionPool(host='api.sportsgameodds.com'): Max retries exceeded with url: "
                           f"/v2/events?leagueID={(params or {}).get('leagueID', '')}&type=match")
        self._session = Mock(get=get)


def _sgo(db, http, leagues=("NBA",)):
    p = SportsGameOddsProvider("fake-key", Budget(db), sports=["BASKETBALL"], leagues=list(leagues))
    p.http = http
    return p


@pytest.mark.parametrize("exc", [requests.ConnectionError, requests.Timeout, requests.exceptions.SSLError])
def test_a_transport_failure_leaves_sgo_eligible_for_the_next_run(db, exc, monkeypatch):
    monkeypatch.setattr("predbot.util.http.time.sleep", lambda s: None)
    http = TransportFailure(exc)
    p = _sgo(db, http)
    with pytest.raises(ProviderError):
        p.fetch_events(START, END)
    assert not Budget(db).is_exhausted("sportsgameodds")
    first = http.attempts
    assert first > 0
    with pytest.raises(ProviderError):
        p.fetch_events(START, END)                      # a later retry really asks SGO again
    assert http.attempts > first


def test_transport_errors_are_classified_as_such():
    http = TransportFailure(requests.Timeout)
    with pytest.raises(ProviderError) as e:
        http.get("/events", {"leagueID": "NBA"})
    assert e.value.kind == "timeout" and e.value.status is None
    assert not SportsGameOddsProvider._is_league_config_error(e.value)
    assert not SportsGameOddsProvider._is_429(e.value)


@pytest.mark.parametrize("error,disables", [
    (ProviderError('400 from /events: {"error":"specify either a leagueID or an eventID at this tier"}', status=400,
                   body='{"error":"specify either a leagueID or an eventID at this tier"}'), True),
    (ProviderError("404 from /events: not found", status=404, body="not found"), False),
    (ProviderError("503 from /events", status=503), False),
    (ProviderError("429 from /events: rate limited", status=429), False),
    (ProviderError("request failed: leagueID=NBA", kind="transport"), False),
    (ProviderError("request failed: leagueID=NBA timed out", kind="timeout"), False),
])
def test_only_a_provider_answer_about_the_league_disables_sgo(error, disables):
    assert SportsGameOddsProvider._is_league_config_error(error) is disables


def test_a_server_error_mentioning_leagueid_does_not_disable_sgo(db):
    class FiveHundred(Http):
        def __init__(self):
            super().__init__("https://fake")

        def get(self, path, params=None):
            raise ProviderError("502 from /events: leagueID gateway", status=502)
    p = _sgo(db, FiveHundred())
    with pytest.raises(ProviderError):
        p.fetch_events(START, END)
    assert not Budget(db).is_exhausted("sportsgameodds")


def test_auto_discovery_that_cannot_reach_the_catalogue_does_not_disable_sgo(db, monkeypatch):
    monkeypatch.setattr("predbot.util.http.time.sleep", lambda s: None)
    p = _sgo(db, TransportFailure(), leagues=())
    assert p.fetch_events(START, END) == []
    assert not Budget(db).is_exhausted("sportsgameodds")


# ============================================================================== diagnostics: read-only by default
def test_the_provider_diagnostic_makes_no_network_request_by_default(tmp_path, monkeypatch, capsys):
    import json
    import diagnose_providers

    from predbot import config as config_mod

    attempts = []

    def no_network(*a, **k):            # recorded as well as raised: the script catches provider exceptions
        attempts.append(a[1:2])
        raise AssertionError("diagnose_providers made a network request in its default mode")

    monkeypatch.setattr(requests.Session, "request", no_network)
    monkeypatch.setattr(requests.Session, "send", no_network)
    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(config_mod, "default_env_path", lambda: tmp_path / "absent.env")
    path = tmp_path / "diag.db"
    seeded = Database(str(path))
    seeded.set_kv("sgo:leagues", json.dumps({"at": "2030-06-01T00:00:00Z", "ids": ["NBA", "MLB"], "sport": {}}))
    seeded.close()
    for name, value in (("DB_PATH", str(path)), ("SPORTSGAMEODDS_KEY", "fake-sgo-key-000000"),
                        ("SHARPAPI_KEY", "fake-sharp-key-000000"), ("ODDS_API_KEY", "fake-odds-key-000000"),
                        ("PROVIDER_ORDER", "sharpapi,sportsgameodds,oddsapi")):
        monkeypatch.setenv(name, value)
    diagnose_providers.main([])
    diagnose_providers.main(["--trace", "--report"])
    out = capsys.readouterr().out
    assert attempts == []
    assert "zero provider requests" in out
    assert "['MLB', 'NBA']" in out                          # the cached SGO catalogue, not a billed /leagues call
    assert "fake-sgo-key" not in out and "fake-sharp-key" not in out


def test_the_provider_diagnostic_builds_clients_only_with_live(monkeypatch, tmp_path):
    import diagnose_providers
    from predbot.config import Config
    built = []
    monkeypatch.setattr("predbot.providers.chain.build_providers", lambda cfg, db: built.append(1) or {})
    cfg = Config(sharpapi_key="fake", sgo_key="", odds_api_key="", provider_order=["sharpapi"])
    db = Database(str(tmp_path / "d.db"))
    diagnose_providers.get_provider_info(cfg, db)
    assert built == []
    diagnose_providers.get_provider_info(cfg, db, live=True)
    assert built == [1]


# ============================================================================== HTTP safety
class Resp:
    def __init__(self, status=200, payload=None, headers=None, text=""):
        self.status_code, self.payload, self.headers, self.text = status, payload, headers or {}, text
        self.closed = False

    def json(self):
        return self.payload

    def close(self):
        self.closed = True


class Session:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def get(self, url, params=None, **kw):
        self.calls.append((url, params, kw))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def http_with(session, **kw):
    h = Http("https://api.example.test/v1", **kw)
    h._session = session
    return h


def test_connect_and_read_timeouts_are_separate():
    s = Session(Resp(payload={"ok": 1}))
    h = http_with(s, timeout=40)
    assert h.get("/x") == {"ok": 1}
    assert s.calls[0][2]["timeout"] == (5.0, 40.0)
    assert http_with(Session(), timeout=3).timeout == (3.0, 3.0)


@pytest.mark.parametrize("headers,params", [({"X-Api-Key": "fake"}, None), ({"X-Auth-Token": "fake"}, None),
                                             ({}, {"apiKey": "fake"})])
def test_a_keyed_request_never_follows_a_redirect_to_another_host(headers, params):
    s = Session(Resp(302, headers={"Location": "https://collector.evil.test/steal"}), Resp(payload={"x": 1}))
    h = http_with(s, headers=headers, retries=2)
    with pytest.raises(ProviderError) as e:
        h.get("/odds", params)
    assert e.value.kind == "redirect" and len(s.calls) == 1        # the second host was never contacted
    assert all(c[2]["allow_redirects"] is False for c in s.calls)


@pytest.mark.parametrize("location", ["http://api.example.test/v1/next", "https://api.example.test:8443/v1/next"])
def test_a_keyed_request_refuses_a_scheme_or_port_change(location):
    s = Session(Resp(301, headers={"Location": location}))
    with pytest.raises(ProviderError):
        http_with(s, headers={"X-Api-Key": "fake"}).get("/odds")
    assert len(s.calls) == 1


def test_a_keyed_same_origin_redirect_is_followed_without_the_original_query():
    s = Session(Resp(302, headers={"Location": "/v1/odds?page=2"}), Resp(payload={"page": 2}))
    h = http_with(s, headers={"X-Api-Key": "fake"})
    assert h.get("/odds", {"apiKey": "fake"}) == {"page": 2}
    assert s.calls[1][0] == "https://api.example.test/v1/odds?page=2" and s.calls[1][1] is None


def test_an_unkeyed_request_still_follows_a_cross_host_redirect():
    s = Session(Resp(302, headers={"Location": "https://cdn.example.org/data.json"}), Resp(payload=[1]))
    assert http_with(s).get("/public") == [1]
    assert s.calls[1][0] == "https://cdn.example.org/data.json"


def test_redirect_loops_are_bounded():
    s = Session(*[Resp(302, headers={"Location": "/v1/again"}) for _ in range(10)])
    with pytest.raises(ProviderError):
        http_with(s, headers={"X-Api-Key": "fake"}, retries=0).get("/loop")
    assert len(s.calls) <= 4


def test_no_sleep_after_the_final_failed_attempt(monkeypatch):
    sleeps = []
    monkeypatch.setattr("predbot.util.http.time.sleep", sleeps.append)
    s = Session(requests.ConnectionError("down"), requests.ConnectionError("down"))
    with pytest.raises(ProviderError) as e:
        http_with(s, retries=1).get("/x")
    assert len(sleeps) == 1 and e.value.kind == "transport"
    sleeps.clear()
    with pytest.raises(ProviderError):
        http_with(Session(Resp(503), Resp(503), Resp(503)), retries=2).get("/x")
    assert len(sleeps) == 2
    sleeps.clear()
    with pytest.raises(ProviderError):
        http_with(Session(requests.Timeout("slow")), retries=0).get("/x")
    assert sleeps == []


def test_a_final_429_is_reported_without_sleeping(monkeypatch):
    sleeps = []
    monkeypatch.setattr("predbot.util.http.time.sleep", sleeps.append)
    s = Session(Resp(429, headers={"Retry-After": "5"}), Resp(429, headers={"Retry-After": "5"}))
    with pytest.raises(ProviderError) as e:
        http_with(s, retries=1).get("/x")
    assert sleeps == [5.0] and e.value.status == 429


def test_numeric_retry_after_is_honoured_and_capped(monkeypatch):
    sleeps = []
    monkeypatch.setattr("predbot.util.http.time.sleep", sleeps.append)
    s = Session(Resp(429, headers={"Retry-After": "7"}), Resp(payload={"ok": True}))
    assert http_with(s, retries=1).get("/x") == {"ok": True}
    assert sleeps == [7.0]
    sleeps.clear()
    s = Session(Resp(429, headers={"Retry-After": "3600"}), Resp(payload={"ok": True}))
    http_with(s, retries=1).get("/x")
    assert sleeps == [60.0]


def test_http_date_retry_after_is_understood():
    now = datetime(2030, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    later = format_datetime(now + timedelta(seconds=42), usegmt=True)
    assert retry_after_seconds(later, now=now) == 42.0
    assert retry_after_seconds(format_datetime(now - timedelta(seconds=5), usegmt=True), now=now) == 0.0
    assert retry_after_seconds("12", now=now) == 12.0
    assert retry_after_seconds("soon", now=now) is None and retry_after_seconds(None) is None


def _real_response(body: bytes, length=None, status=200):
    r = requests.Response()
    r.status_code = status
    r.headers = CaseInsensitiveDict({"Content-Type": "application/json"})
    if length is not None:
        r.headers["Content-Length"] = str(length)
    r.raw = io.BytesIO(body)
    r.url = "https://api.example.test/v1/x"
    return r


def test_response_bodies_are_capped():
    big = b'{"data": "' + b"x" * 5000 + b'"}'
    with pytest.raises(ProviderError) as e:
        http_with(Session(_real_response(big)), max_bytes=1000).get("/x")
    assert e.value.kind == "too_large"
    with pytest.raises(ProviderError):
        http_with(Session(_real_response(b"{}", length=10_000_000)), max_bytes=1000).get("/x")
    assert http_with(Session(_real_response(b'{"ok": 1}')), max_bytes=1000).get("/x") == {"ok": 1}


def test_error_bodies_are_read_with_a_small_cap_and_redacted():
    body = b'{"error":"bad league leagueID apiKey=fake-secret-123"}' + b" " * 200_000
    with pytest.raises(ProviderError) as e:
        http_with(Session(_real_response(body, status=400))).get("/x")
    assert e.value.status == 400 and "leagueID" in e.value.body
    assert "fake-secret-123" not in str(e.value) and len(e.value.body) <= 300


def test_the_requests_floor_excludes_the_vulnerable_releases():
    line = next(l for l in (ROOT / "requirements.txt").read_text().splitlines() if l.startswith("requests"))
    floor = re.search(r">=\s*([\d.]+)", line).group(1)
    assert tuple(int(x) for x in floor.split(".")) >= (2, 33)


# ============================================================================== redaction
def test_startup_lines_never_show_chat_or_admin_ids(tmp_path, monkeypatch):
    from predbot.config import load_config, startup_lines
    env = tmp_path / ".env"
    env.write_text("TELEGRAM_CHAT_ID=-1009876543210\nTELEGRAM_ADMIN_IDS=111222333,444555666\n"
                   "TELEGRAM_BOT_TOKEN=123456:fake-token-value-abcdefghij\nDAILY_PICKS_TARGET=11\n")
    for k in ("TELEGRAM_CHAT_ID", "TELEGRAM_ADMIN_IDS", "TELEGRAM_BOT_TOKEN", "DAILY_PICKS_TARGET"):
        monkeypatch.delenv(k, raising=False)
    try:
        text = "\n".join(startup_lines(load_config(env_file=str(env))))
    finally:
        for k in ("TELEGRAM_CHAT_ID", "TELEGRAM_ADMIN_IDS", "TELEGRAM_BOT_TOKEN", "DAILY_PICKS_TARGET"):
            monkeypatch.delenv(k, raising=False)
    for value in ("9876543210", "111222333", "444555666", "fake-token-value"):
        assert value not in text
    assert "TELEGRAM_CHAT_ID=***" in text and "TELEGRAM_ADMIN_IDS=***" in text
    assert "DAILY_PICKS_TARGET=11" in text                 # ordinary settings stay readable


def test_identifier_masking_does_not_hide_ordinary_settings():
    from predbot.config import is_secret
    assert all(is_secret(k) for k in ("TELEGRAM_CHAT_ID", "TELEGRAM_ADMIN_IDS", "JUSTRUN_USER_IDENTITY",
                                       "SOME_AUTH_HEADER"))
    assert not any(is_secret(k) for k in ("PROVIDER_ORDER", "ODDSAPI_RESERVE_CREDITS", "DAILY_PICKS_TARGET",
                                           "SGO_LEAGUES"))


@pytest.mark.parametrize("text,secret", [
    ("headers={'X-Api-Key': 'fake-api-key-1'}", "fake-api-key-1"),
    ('{"X-Auth-Token": "fake-auth-token-2"}', "fake-auth-token-2"),
    ("X-User-Identity: fake-identity-3", "fake-identity-3"),
    ("{'X-User-Identity': 'fake-identity-4'}", "fake-identity-4"),
    ("Authorization: Bearer fake.jwt.5", "fake.jwt.5"),
    ("Proxy-Authorization: Basic ZmFrZTpmYWtl", "ZmFrZTpmYWtl"),
    ("Cookie: session=fake-cookie-6", "fake-cookie-6"),
    ("GET /odds?apiKey=fake-query-7&regions=eu", "fake-query-7"),
    ("community menu posted | chat_id=-1001234567890 | message_id=12", "1234567890"),
    ('{"chat_id": 987654321, "text": "hi"}', "987654321"),
    ("admin_ids=[111222333, 444555666]", "111222333"),
])
def test_redaction_covers_header_query_and_identifier_shapes(text, secret):
    from predbot.util.redact import redact
    assert secret not in redact(text)


def test_redaction_keeps_ordinary_log_lines_readable():
    from predbot.util.redact import redact
    line = "picks posted | events=42 | providers=sharpapi,sportsgameodds | message_id=12 | status=200"
    assert redact(line) == line


def test_the_log_filter_masks_identifiers_in_every_record(caplog):
    from predbot.util.redact import RedactingFilter
    logger = logging.getLogger("test.redaction")
    handler = logging.Handler()
    seen = []
    handler.emit = lambda record: seen.append(record.getMessage())
    handler.addFilter(RedactingFilter(["fake-known-secret"]))
    logger.addHandler(handler)
    try:
        logger.warning("community menu failed | chat_id=%s | key=%s", -1005555555555, "fake-known-secret")
    finally:
        logger.removeHandler(handler)
    assert seen and "5555555555" not in seen[0] and "fake-known-secret" not in seen[0]


# ============================================================================== budgets
def test_the_daily_allowance_is_net_of_what_today_already_spent(db):
    b = Budget(db)
    first = b.monthly_daily_allowance("sharpapi", 3000, reserve_frac=0.10)
    assert first > 0
    b.spend("sharpapi", 10)
    assert b.monthly_daily_allowance("sharpapi", 3000, reserve_frac=0.10) == first - 10
    b.spend("sharpapi", first)
    assert b.monthly_daily_allowance("sharpapi", 3000, reserve_frac=0.10) == 0


def test_a_retry_on_the_same_day_does_not_get_a_fresh_sharpapi_share(db):
    from predbot.providers.sharpapi import SharpApiProvider
    b = Budget(db)
    p = SharpApiProvider("fake", b, leagues=["nba"], monthly_limit=3000, max_requests_per_generation=1000)
    full = p.request_budget()
    b.spend("sharpapi", full)
    assert p.request_budget() == 0


def test_sgo_pre_probe_generation_is_net_of_todays_spend(db):
    b = Budget(db)
    p = SportsGameOddsProvider("fake-key", b, sports=["BASKETBALL"], leagues=["NBA"])
    share = b.monthly_daily_allowance("sportsgameodds", p.monthly_limit)
    b.spend("sportsgameodds", share)
    calls = []

    class Recorder(Http):
        def __init__(self):
            super().__init__("https://fake")

        def get(self, path, params=None):
            calls.append(path)
            return {"data": []}
    p.http = Recorder()
    assert p.fetch_events(START, END) == []
    assert "/events" not in calls                        # today's share is already spent


def _odds(budget, remaining, settlement_reserve=0):
    from predbot.providers.oddsapi import OddsApiProvider
    p = OddsApiProvider("fake", budget, reserve_credits=80, settlement_reserve_credits=settlement_reserve)
    p.requests_remaining = remaining
    return p


def test_the_odds_api_settlement_reserve_is_off_by_default(budget):
    p = _odds(budget, 89)
    assert p.can_spend_credits(9) and p.can_spend_credits(2, purpose="settlement")
    assert not _odds(budget, 88).can_spend_credits(9)                    # the shared 80-credit floor, as before


def test_the_odds_api_settlement_reserve_is_kept_from_generation_only(budget):
    p = _odds(budget, 95, settlement_reserve=10)
    assert not p.can_spend_credits(9)                    # 95 - 9 = 86 < 80 + 10
    assert p.can_spend_credits(2, purpose="settlement")  # 93 >= 80: settlement may use it
    assert not _odds(budget, 81, settlement_reserve=10).can_spend_credits(2, purpose="settlement")


def test_settlement_scores_use_the_settlement_purpose(budget, monkeypatch):
    p = _odds(budget, 85, settlement_reserve=10)
    p.http = Mock()
    p.http.get.return_value = []
    p.http.last_headers = {}
    monkeypatch.setattr(p, "_stored_scores", lambda key: None)
    assert p._scores_for("soccer_epl") == {}
    p.http.get.assert_called_once()


# ============================================================================== repository / build context
def _docker_excluded(path: str, patterns) -> bool:
    """Docker's .dockerignore rules (last match wins, ``!`` re-includes, ``**`` spans directories, a match on a
    directory excludes everything under it)."""
    def rx(p):
        out, i = "", 0
        while i < len(p):
            if p.startswith("**/", i):
                out, i = out + "(?:.*/)?", i + 3
            elif p.startswith("**", i):
                out, i = out + ".*", i + 2
            elif p[i] == "*":
                out, i = out + "[^/]*", i + 1
            elif p[i] == "?":
                out, i = out + "[^/]", i + 1
            else:
                out, i = out + re.escape(p[i]), i + 1
        return re.compile(out + "(?:/.*)?$")
    excluded = False
    for raw in patterns:
        neg = raw.startswith("!")
        if rx(raw[1:] if neg else raw).match(path):
            excluded = not neg
    return excluded


def _dockerignore():
    return [l.strip() for l in (REPO / ".dockerignore").read_text().splitlines() if l.strip() and not l.startswith("#")]


@pytest.mark.parametrize("path", [
    "predictions-bot/.env", "predictions-bot/.env.production", "predictions-bot/.vscode/mcp.json", ".git/config",
    "predictions-bot/.venv/Lib/site-packages/x.py", "data/predbot.db", "data/predbot.db-wal",
    "predictions-bot/data/booking_diagnostic.sqlite3", "predictions-bot/_top_current.db-shm", "predictions-bot/bot.log",
    "predictions-bot/.kilo/worktrees/x/main.py", ".kilo/worktrees/y/Dockerfile", "predictions-bot/.claude/settings.local.json",
    "predictions-bot/__pycache__/cli.cpython-311.pyc", "predictions-bot/.pytest_cache/v/x",
    "predictions-bot/_top_full_tests.txt", "predictions-bot/prediction_diagnostic.json",
    "predictions-bot/tests/fixtures/replay/replay.db",
])
def test_the_docker_build_context_excludes_local_and_credential_files(path):
    assert _docker_excluded(path, _dockerignore()), path


@pytest.mark.parametrize("path", [
    "Dockerfile", "predictions-bot/requirements.txt", "predictions-bot/main.py", "predictions-bot/cli.py",
    "predictions-bot/predbot/__init__.py", "predictions-bot/predbot/storage/lease.py",
    "predictions-bot/predbot/bot/__init__.py", "predictions-bot/tools/replay.py",
])
def test_the_docker_build_context_keeps_what_the_image_needs(path):
    assert not _docker_excluded(path, _dockerignore()), path


def test_editor_and_agent_workspaces_are_ignored_by_git():
    for ignore in (REPO / ".gitignore", ROOT / ".gitignore"):
        rules = ignore.read_text().splitlines()
        for rule in (".vscode/", ".kilo/", ".env", "*.db", "*.sqlite3", ".venv/"):
            assert rule in rules, (ignore, rule)


def test_legacy_deployment_files_are_gone():
    assert not (ROOT / "Procfile").exists() and not (ROOT / "railway.json").exists()
