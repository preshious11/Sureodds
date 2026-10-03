"""SportsGameOdds usage control: attribution, billing unit, caching, back-off, cool-down, free-first settlement and
credential hygiene. Nothing here touches the network (fakes only; see also tests/test_sgo_quota.py)."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest

from predbot.providers.base import MatchResult
from predbot.providers.budget import Budget
from predbot.providers.chain import ResultsResolver
from predbot.providers.sgo_meter import SETTLEMENT_BACKOFF_H, SgoMeter, usage_lines, usage_report
from predbot.providers.sportsgameodds import SgoUnavailable, SportsGameOddsProvider
from predbot.util.http import Http, ProviderError
from predbot.util.redact import MASK, RedactingFilter, redact
from predbot.util.timeutil import parse_iso, utcnow
from tests.test_sgo_quota import FakeHttp, FakeProbe, usage

KEY = "sgo_live_0123456789abcdef0123456789abcdef"
LONG_AGO = datetime(2026, 6, 1, 18, 0, tzinfo=timezone.utc)          # long finished
HOME, AWAY = "Cubs", "Dodgers"


def final_event(eid="E1", home=HOME, away=AWAY, h=3, a=2):
    return {"eventID": eid, "sportID": "BASEBALL", "leagueID": "MLB", "status": {"completed": True},
            "teams": {"home": {"names": {"long": home}}, "away": {"names": {"long": away}}},
            "results": {"game": {"home": {"points": h}, "away": {"points": a}}}}


class ResultHttp:
    """/events -> the events given; /leagues; counts every call."""

    def __init__(self, events=(), rate_limited=False):
        self.calls, self.events, self.rate_limited = [], list(events), rate_limited

    def get(self, path, params=None):
        self.calls.append((path, params))
        if self.rate_limited:
            raise ProviderError("429 rate limited", status=429)
        if path == "/leagues":
            return {"data": [{"leagueID": "MLB", "sportID": "BASEBALL"}]}
        return {"success": True, "data": self.events}

    def n(self, path="/events"):
        return len([c for c in self.calls if c[0] == path])


def sgo(db, http=None, probe=None, **kw):
    p = SportsGameOddsProvider(KEY, Budget(db), ["BASEBALL"], ["MLB"], event_limit=50)
    p.http = http or ResultHttp()
    p._probe = probe or FakeProbe(usage(10, 2500))
    for k, v in kw.items():
        setattr(p, k, v)
    return p


# ------------------------------------------------------------------------------------------ billing unit + attribution
def test_billing_unit_is_objects_returned_with_a_minimum_of_one(db):
    p = sgo(db, ResultHttp([final_event("E1"), final_event("E2")]))
    p._call("/events", {}, "generation", league="MLB")
    p.http.events = []
    p._call("/events", {}, "generation", league="MLB")                    # an empty answer still bills 1 object
    rows = db.query("SELECT objects_billed FROM sgo_usage WHERE reason='generation' ORDER BY id")
    assert [r["objects_billed"] for r in rows] == [2, 1]


def test_every_request_is_attributed_without_secrets(db):
    p = sgo(db, ResultHttp([final_event()]))
    p._call("/events", {"eventID": "E1"}, "settlement", sport="BASEBALL", league="MLB", event_id="E1")
    row = db.query("SELECT * FROM sgo_usage")[0]
    assert (row["reason"], row["endpoint"], row["status"], row["objects_billed"]) == ("settlement", "/events", "ok", 1)
    assert (row["sport"], row["league"], row["event_id"], row["ts"]) == ("BASEBALL", "MLB", "E1", row["ts"])
    assert row["note"] == "scheduled"                                    # who drove it: scheduler / manual / diagnostic
    assert KEY not in " ".join(str(v) for v in tuple(row))


def test_manual_and_diagnostic_origins_are_recorded(db, monkeypatch):
    monkeypatch.setenv("PREDBOT_SGO_ORIGIN", "manual")
    p = sgo(db)
    p._call("/events", {}, "generation")
    assert db.query("SELECT note FROM sgo_usage")[0]["note"] == "manual"


def test_the_usage_report_separates_requests_entities_and_retries(db):
    m = SgoMeter(db)
    m.record("generation", "/events", "ok", attempts=1, objects=40)
    m.record("settlement", "/events", "ok", attempts=2, objects=3)       # one retry
    m.record("fallback", "/events", "rate_limited", attempts=1)
    rep = usage_report(db)
    assert rep["entities_consumed"] == 43 and rep["http_requests"] == 4 and rep["retry_requests"] == 1
    assert set(rep["by_reason"]) == {"generation", "settlement", "fallback"}
    text = "\n".join(usage_lines(db))
    assert "sgo_http_requests=4" in text and "sgo_entities_consumed=43" in text and "sgo_retry_requests=1" in text


# ------------------------------------------------------------------------------------------ retries are not multiplicative
def test_a_429_is_never_retried_and_starts_a_cooldown(db):
    http = ResultHttp(rate_limited=True)
    p = sgo(db, http)
    with pytest.raises(ProviderError):
        p._call("/events", {}, "generation")
    assert http.n() == 1                                                  # not 3 attempts
    assert p.meter.in_cooldown()
    with pytest.raises(SgoUnavailable):                                   # and while cooling down: no HTTP at all
        p._call("/events", {}, "generation")
    assert http.n() == 1


def test_a_server_error_is_retried_at_most_once(db):
    class Flaky(ResultHttp):
        def get(self, path, params=None):
            self.calls.append((path, params))
            raise ProviderError("boom", status=503)
    http = Flaky()
    p = sgo(db, http)
    with pytest.raises(ProviderError):
        p._call("/events", {}, "generation")
    assert http.n() == 2                                                  # maximum: 1 try + 1 retry
    assert db.query("SELECT http_attempts, status FROM sgo_usage")[0]["status"] == "error"


def test_a_client_error_is_not_retried(db):
    class Bad(ResultHttp):
        def get(self, path, params=None):
            self.calls.append((path, params))
            raise ProviderError("400 bad league", status=400)
    http = Bad()
    with pytest.raises(ProviderError):
        sgo(db, http)._call("/events", {}, "generation")
    assert http.n() == 1


def test_the_http_layer_does_not_sleep_or_retry_a_429_when_told_not_to(monkeypatch):
    import requests
    sleeps, calls = [], []

    class R:
        status_code, text, headers = 429, "slow down", {}
        def json(self):
            return {}

    monkeypatch.setattr("predbot.util.http.time.sleep", lambda s: sleeps.append(s))
    h = Http("https://x.test", retries=2, sleep_on_429=False)
    monkeypatch.setattr(h.session, "get", lambda *a, **k: (calls.append(1), R())[1])
    with pytest.raises(ProviderError) as e:
        h.get("/p")
    assert len(calls) == 1 and not sleeps and e.value.status == 429


# ------------------------------------------------------------------------------------------ exhausted quota / levels
def test_exhausted_quota_makes_no_http_request_for_any_reason(db):
    http = ResultHttp()
    p = sgo(db, http, FakeProbe(usage(2500, 2500)))
    p.monthly_entities()
    for reason in ("generation", "settlement", "fallback", "manual", "diagnostic"):
        with pytest.raises(SgoUnavailable):
            p._call("/events", {}, reason)
    assert http.calls == []


def test_levels_progressively_switch_off_non_essential_activity(db):
    m = SgoMeter(db)
    m.observe(1250, 2500)                                                 # 50 %
    assert not m.allows("fallback", "diagnostic") and m.allows("generation", "manual")
    m.observe(1900, 2500)                                                 # 76 %: SGO is no longer a stand-in for free sources
    assert not m.allows("settlement", "scheduled", needs_sgo=False)
    assert m.allows("settlement", "scheduled", needs_sgo=True)            # but still grades what nothing else can
    m.observe(2300, 2500)                                                 # 92 %
    assert not m.allows("generation", "manual")
    m.observe(2500, 2500)
    assert not m.allows("settlement", "scheduled", needs_sgo=True)


def test_level_warnings_are_logged_once(db, caplog):
    m = SgoMeter(db)
    with caplog.at_level(logging.WARNING):
        for used in (1250, 1260, 1300, 1900, 1910):
            m.observe(used, 2500)
    msgs = [r.getMessage() for r in caplog.records if "SGO quota at" in r.getMessage()]
    assert [x.split("%")[0].rsplit(" ", 1)[1] for x in msgs] == ["50", "75"]


def test_the_probe_object_is_not_double_counted(db):
    m = SgoMeter(db)
    m.observe(100, 2500)
    m.record("quota_probe", "/account/usage", "ok", objects=1)
    m.record("generation", "/events", "ok", objects=7)
    assert m.reading() == (107, 2500)


# ------------------------------------------------------------------------------------------ settlement: pre-match, dedupe, backoff
def test_a_match_that_cannot_be_over_is_never_looked_up(db):
    http = ResultHttp([final_event()])
    p = sgo(db, http)
    soon = utcnow() + timedelta(hours=1)
    started = utcnow() - timedelta(minutes=30)
    assert p.find_result("baseball", HOME, AWAY, soon) is None
    assert p.find_result("baseball", HOME, AWAY, started) is None
    assert http.calls == []


def test_team_time_lookups_share_one_window_query(db):
    http = ResultHttp([final_event("E1"), final_event("E2", "Mets", "Braves", 1, 0)])
    p = sgo(db, http)
    a = p.find_result("baseball", HOME, AWAY, LONG_AGO)
    b = p.find_result("baseball", "Mets", "Braves", LONG_AGO + timedelta(minutes=20))
    assert a.status == "final" and b.scores["full"] == (1.0, 0.0)
    assert http.n("/events") == 1                                         # one query bought both fixtures


def test_a_fixture_is_asked_again_only_after_the_backoff(db):
    http = ResultHttp([])                                                 # never found
    p = sgo(db, http)
    assert p.find_result("baseball", HOME, AWAY, LONG_AGO) is None
    p.begin_run()
    assert p.find_result("baseball", HOME, AWAY, LONG_AGO) is None        # a new run, same fixture: backed off
    assert http.n("/events") == 1
    # ... and once the backoff has passed it may be asked again
    db.execute("UPDATE sgo_usage SET ts=?", ((utcnow() - timedelta(hours=SETTLEMENT_BACKOFF_H[1] + 1))
                                              .strftime("%Y-%m-%dT%H:%M:%SZ"),))
    p.begin_run()
    p.find_result("baseball", HOME, AWAY, LONG_AGO)
    assert http.n("/events") == 2


def test_backoff_grows_progressively(db):
    m = SgoMeter(db)
    key = "k"
    waits = []
    for n in range(1, 5):
        m.record("fallback", "/events", "ok", event_id=key)
        db.execute("UPDATE sgo_usage SET ts=?", ((utcnow() - timedelta(hours=SETTLEMENT_BACKOFF_H[min(n, 4)] - 0.5))
                                                  .strftime("%Y-%m-%dT%H:%M:%SZ"),))
        waits.append(m.settlement_due(key))
    assert waits == [False] * 4
    assert list(SETTLEMENT_BACKOFF_H) == sorted(SETTLEMENT_BACKOFF_H)


def test_fetch_result_asks_once_per_event_per_run_and_never_for_a_known_final(db):
    http = ResultHttp([final_event("E1")])
    p = sgo(db, http)
    for _ in range(3):
        assert p.fetch_result("E1").status == "final"
    assert http.n("/events") == 1
    assert p.fetch_result("E1", {"_free_alt": True}).status == "final"


def test_with_a_free_alternative_sgo_is_not_called_once_usage_passes_75_percent(db):
    http = ResultHttp([final_event("E1")])
    p = sgo(db, http, probe=FakeProbe(usage(1950, 2500)))
    assert p.fetch_result("E1", {"_free_alt": True}) is None
    assert http.calls == []
    assert p.fetch_result("E1", {}).status == "final"                    # nothing else can grade it: still allowed


# ------------------------------------------------------------------------------------------ resolver: provider priority
class Free:
    """A free result source."""

    def __init__(self, result=None, name="football_data", raises=False):
        self.name, self.result, self.calls, self.raises = name, result, 0, raises

    def find_result(self, sport, home, away, start, league_id="", league=""):
        self.calls += 1
        if self.raises:
            raise ProviderError("down")
        return self.result


class Sgo:
    name = "sportsgameodds"

    def __init__(self, result=None):
        self.result, self.fetches, self.finds, self.hints = result, 0, 0, []

    def fetch_result(self, eid, event_hint=None):
        self.fetches += 1
        self.hints.append(event_hint)
        return self.result

    def find_result(self, *a, event_hint=None, **k):
        self.finds += 1
        self.hints.append(event_hint)
        return self.result


START = parse_iso("2026-09-17T16:45Z")
FINAL = MatchResult("final", {"full": (2.0, 1.0)}, source="football_data")
HINTS = {"league_id": "england_-_premier_league"}


def resolver(free, s):
    return ResultsResolver({"football_data": free, "sportsgameodds": s})


def test_a_free_source_that_can_grade_the_pick_settles_it_without_touching_sgo():
    free, s = Free(FINAL), Sgo(MatchResult("final", {"full": (9.0, 9.0)}, source="sportsgameodds"))
    r = resolver(free, s).resolve("sportsgameodds", "E1", "football", "Arsenal", "Chelsea", START,
                                  hints={**HINTS, "_needs": frozenset({("full", "points")})})
    assert r.source == "football_data" and (s.fetches, s.finds) == (0, 0)


def test_sgo_stays_first_for_markets_only_it_can_grade():
    free, s = Free(FINAL), Sgo(MatchResult("final", {"full": (2.0, 1.0), "1h": (1.0, 0.0)}, source="sportsgameodds"))
    r = resolver(free, s).resolve("sportsgameodds", "E1", "football", "Arsenal", "Chelsea", START,
                                  hints={**HINTS, "_needs": frozenset({("full", "corners")})})
    assert r.source == "sportsgameodds" and s.fetches == 1 and free.calls == 0


def test_sgo_is_a_stand_in_when_the_free_source_has_nothing_and_is_told_so():
    free, s = Free(None), Sgo(MatchResult("final", {"full": (2.0, 1.0)}, source="sportsgameodds"))
    r = resolver(free, s).resolve("sportsgameodds", "E1", "football", "Arsenal", "Chelsea", START,
                                  hints={**HINTS, "_needs": frozenset({("full", "points")})})
    assert r.source == "sportsgameodds" and free.calls == 1
    assert s.hints[0].get("_free_alt") is True


def test_settlement_still_works_with_sgo_disabled_or_exhausted():
    class Dead(Sgo):
        def fetch_result(self, eid, event_hint=None):
            raise SgoUnavailable("quota")
    free = Free(FINAL)
    only_free = ResultsResolver({"football_data": free})
    r = only_free.resolve("sportsgameodds", "E1", "football", "Arsenal", "Chelsea", START, hints=HINTS)
    assert r.status == "final"
    r = ResultsResolver({"football_data": Free(FINAL), "sportsgameodds": Dead()}).resolve(
        "sportsgameodds", "E1", "football", "Arsenal", "Chelsea", START,
        hints={**HINTS, "_needs": frozenset({("full", "points")})})
    assert r.status == "final"


def test_one_fixture_is_resolved_once_however_many_picks_share_it():
    free, s = Free(FINAL), Sgo()
    res, cache = resolver(free, s), {}
    for _ in range(5):
        res.resolve("sportsgameodds", "E1", "football", "Arsenal", "Chelsea", START, cache=cache,
                    hints={**HINTS, "_needs": frozenset({("full", "points")})})
    assert free.calls == 1


# ------------------------------------------------------------------------------------------ generation caching
def test_a_second_generation_in_the_same_window_reuses_the_leagues_it_bought(db):
    import copy
    from tests.conftest import load
    base = load("sgo_event.json")
    base = base["data"][0] if "data" in base else base
    ev = copy.deepcopy(base)
    ev["leagueID"], ev["status"]["startsAt"] = "MLB", "2026-10-03T20:00:00Z"
    http = FakeHttp({"MLB": [ev]})
    p = SportsGameOddsProvider(KEY, Budget(db), [], ["MLB"], event_limit=50)
    p._probe, p.http = FakeProbe(usage(10, 2500)), http
    start, end = parse_iso("2026-10-03T00:00:00Z"), parse_iso("2026-10-04T12:00:00Z")
    first = p.fetch_events(start, end)
    second = p.fetch_events(start, end)
    assert len([c for c in http.calls if c[0] == "/events"]) == 1 and len(first) == len(second)
    assert db.query("SELECT COUNT(*) AS n FROM sgo_usage WHERE reason='generation'")[0]["n"] == 1


def test_the_league_catalogue_is_billed_once_and_then_read_from_the_database(db):
    http = ResultHttp()
    p = sgo(db, http)
    p.available_leagues()
    p2 = sgo(db, http)                                                    # a restart: new object, same database
    p2.available_leagues()
    assert http.n("/leagues") == 1


def test_generation_is_capped_by_the_daily_allowance_across_runs(db):
    m = SgoMeter(db, event_limit=50)
    m.observe(0, 2500)
    a = m.generation_allowance()
    m.record("generation", "/events", "ok", objects=a)
    assert m.generation_allowance() == 0                                  # today's share is spent: no more until tomorrow
    assert a <= 50


# ------------------------------------------------------------------------------------------ credentials are never exposed
@pytest.mark.parametrize("raw", [
    f"GET https://api.sportsgameodds.com/v2/events?apiKey={KEY}&limit=5 failed",
    f"headers {{'X-Api-Key': '{KEY}'}}",
    "Authorization: Bearer abcdef123456",
    "contact omacartt@gmail.com about it",
    "keyID 0f" * 1 + "a" * 62,
    "customer cus_ABCDEFGHIJKL",
    "https://api.telegram.org/bot123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw/sendMessage",
])
def test_redact_masks_credentials_and_identifiers(raw):
    out = redact(raw, known_secrets=[KEY])
    for leak in (KEY, "omacartt@gmail.com", "abcdef123456", "ABCDEFGHIJKL", "AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"):
        assert leak not in out
    assert MASK in out


def test_the_logging_filter_masks_messages_args_and_exceptions():
    rec = logging.LogRecord("x", logging.WARNING, __file__, 1, "failed for %s", (f"?apiKey={KEY}",), None)
    RedactingFilter([KEY]).filter(rec)
    assert KEY not in rec.getMessage()
    try:
        raise RuntimeError(f"url ...?apiKey={KEY}")
    except RuntimeError:
        import sys
        rec = logging.LogRecord("x", logging.ERROR, __file__, 1, "boom", (), sys.exc_info())
    RedactingFilter([KEY]).filter(rec)
    assert KEY not in (rec.exc_text or "") and KEY not in rec.getMessage()


def test_the_usage_probe_never_stores_or_logs_account_identifiers(db, caplog):
    payload = usage(10, 2500)
    payload["data"]["keyID"] = "ab" * 32
    payload["data"]["customerID"] = "cus_SECRETSECRET"
    payload["data"]["email"] = "someone@example.com"
    p = sgo(db, probe=FakeProbe(payload))
    with caplog.at_level(logging.DEBUG):
        p.monthly_entities()
        p.quota_state()
    dump = " ".join(r.getMessage() for r in caplog.records)
    dump += " ".join(str(dict(zip(r.keys(), tuple(r)))) for r in db.query("SELECT * FROM sgo_usage"))
    dump += " ".join(f"{r['k']}={r['v']}" for r in db.query("SELECT k, v FROM kv"))
    for leak in ("ab" * 32, "cus_SECRETSECRET", "someone@example.com", KEY):
        assert leak not in dump
    assert "someone@example.com" not in str(p.quota_state())


def test_http_errors_do_not_echo_the_api_key(monkeypatch):
    import requests

    def boom(*a, **k):
        raise requests.ConnectionError(f"Max retries exceeded with url: /v2/events?apiKey={KEY}")
    h = Http("https://x.test", retries=0)
    monkeypatch.setattr(h.session, "get", boom)
    with pytest.raises(ProviderError) as e:
        h.get("/events", {"apiKey": KEY})
    assert KEY not in str(e.value)


def test_nothing_in_the_subscriber_surface_mentions_sgo_usage():
    import pathlib
    bot = pathlib.Path(__file__).resolve().parent.parent / "predbot" / "bot"
    for f in bot.glob("*.py"):
        if f.name == "lastrun.py":                                        # admin-only (/lastrun)
            continue
        assert "sgo_meter" not in f.read_text(encoding="utf-8"), f.name
