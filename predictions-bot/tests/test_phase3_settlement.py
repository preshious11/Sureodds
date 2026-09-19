"""Phase 3: results paths for what we now pick (football-data.org, Odds API /scores),
the event-level results gate, the resolver order and settle.run never raising."""
from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from predbot.bot.commands import CommandLoop
from predbot.config import Config
from predbot.jobs import daily_picks, settle
from predbot.markets.settlement import make_results_gate, results_path
from predbot.providers.base import Event, MatchResult
from predbot.providers.budget import Budget
from predbot.providers.chain import ResultsResolver, build_providers
from predbot.providers.football_data import FootballDataProvider, competition_for
from predbot.providers.oddsapi import OddsApiProvider
from predbot.selection.picker import select
from predbot.util.http import ProviderError
from tests.conftest import dt, load
from tests.test_picker import ev, sc


# ============================================================ 3a football-data.org
class _FdHttp:
    def __init__(self, payload=None, error=None):
        self.payload, self.error, self.calls = payload if payload is not None else load("football_data_matches.json"), error, []

    def get(self, path, params=None):
        self.calls.append((path, params))
        if self.error:
            raise ProviderError(self.error)
        return self.payload


def _fd(**kw):
    p = FootballDataProvider("k")
    p.http = _FdHttp(**kw)
    return p


START = dt(hh=19)


def test_a_finished_match_gives_full_first_half_and_derived_second_half():
    r = _fd().find_result("football", "Manchester City", "Newcastle United", START, league_id="england_-_premier_league")
    assert r.status == "final" and r.source == "football_data"
    assert r.scores == {"full": (3.0, 0.0), "1h": (1.0, 0.0), "2h": (2.0, 0.0)}


def test_extra_time_uses_the_regular_time_score_not_the_full_time_one():
    """Football markets settle on 90 minutes; v4 fullTime includes extra time."""
    r = _fd().find_result("football", "Paris Saint-Germain", "Liverpool", START, league_id="uefa_-_champions_league")
    assert r.scores["full"] == (2.0, 2.0) and r.scores["2h"] == (1.0, 1.0)


def test_beyond_regulation_without_regular_time_is_not_guessed():
    r = _fd().find_result("football", "FC Porto", "Sporting CP", START, league_id="uefa_-_champions_league")
    assert r.status == "unknown" and r.scores == {}


@pytest.mark.parametrize("home,away,status", [("Everton", "Fulham", "postponed"), ("Aston Villa", "Brentford", "in_progress")])
def test_non_final_statuses_are_mapped_without_scores(home, away, status):
    r = _fd().find_result("football", home, away, START, league_id="england_-_premier_league")
    assert r.status == status and r.scores == {}


def test_team_names_match_fuzzily_and_by_short_name():
    assert _fd().find_result("football", "Man City", "Newcastle", START, league_id="soccer_epl").status == "final"


def test_a_regular_match_without_regular_time_settles_on_full_time():
    """duration REGULAR carries no regularTime key; fullTime is the 90 minutes."""
    match = next(m for m in load("football_data_matches.json")["matches"] if m["id"] == 1006)
    assert match["score"]["duration"] == "REGULAR" and "regularTime" not in match["score"]
    r = _fd().find_result("football", "Tottenham Hotspur", "Brighton & Hove Albion", START, league_id="england_-_premier_league")
    assert r.status == "final" and r.scores == {"full": (2.0, 2.0), "1h": (1.0, 0.0), "2h": (1.0, 2.0)}


def test_teams_also_match_on_the_three_letter_code():
    r = _fd().find_result("football", "TOT", "BHA", START, league_id="england_-_premier_league")
    assert r is not None and r.status == "final"


def test_no_match_or_wrong_kickoff_returns_none():
    p = _fd()
    assert p.find_result("football", "Nobody FC", "Ghost United", START, league_id="soccer_epl") is None
    assert p.find_result("football", "Manchester City", "Newcastle United", dt(hh=19) .replace(day=20),
                         league_id="soccer_epl") is None


def test_only_football_and_only_with_a_key():
    assert _fd().find_result("basketball", "A", "B", START) is None
    assert FootballDataProvider("").find_result("football", "A", "B", START) is None


def test_a_competition_and_date_is_fetched_once_per_run_then_again_after_begin_run():
    p = _fd()
    p.find_result("football", "Manchester City", "Newcastle United", START, league_id="soccer_epl")
    p.find_result("football", "Everton", "Fulham", START, league_id="england_-_premier_league")
    assert len(p.http.calls) == 1
    path, params = p.http.calls[0]
    assert path == "/competitions/PL/matches"
    assert params == {"dateFrom": "2030-05-31", "dateTo": "2030-06-02"}          # UTC date +-1 day
    p.begin_run()
    p.find_result("football", "Everton", "Fulham", START, league_id="soccer_epl")
    assert len(p.http.calls) == 2


def test_an_unmapped_league_searches_all_competitions_in_one_call():
    p = _fd()
    p.find_result("football", "Manchester City", "Newcastle United", START, league_id="turkey_-_super_lig")
    assert p.http.calls[0][0] == "/matches"


def test_league_mapping_covers_sharpapi_slugs_and_odds_api_keys():
    assert competition_for("england_-_premier_league") == "PL" == competition_for("soccer_epl")
    assert competition_for("uefa_-_champions_league") == "CL" == competition_for("soccer_uefa_champs_league")
    assert competition_for("turkey_-_super_lig") is None and competition_for("") is None


def test_it_authenticates_by_header_is_rate_limited_and_never_supplies_events():
    p = FootballDataProvider("secret")
    assert p.http._headers["X-Auth-Token"] == "secret"
    assert p.http.rate.min_interval == 6.5 and p.http.base_url == "https://api.football-data.org/v4"
    assert p.fetch_events(dt(), dt(hh=23)) == []


def test_provider_errors_propagate_so_the_resolver_can_skip_them():
    with pytest.raises(ProviderError):
        _fd(error="429").find_result("football", "A", "B", START, league_id="soccer_epl")


def test_it_is_registered_only_when_a_key_is_configured(db):
    assert "football_data" not in build_providers(Config(football_data_key="", sharpapi_key="s"), db)
    assert isinstance(build_providers(Config(football_data_key="k"), db)["football_data"], FootballDataProvider)


# ============================================================= 3b Odds API /scores
SCORES = [
    {"id": "e1", "sport_key": "soccer_epl", "completed": True, "home_team": "Arsenal", "away_team": "Chelsea",
     "scores": [{"name": "Arsenal", "score": "2"}, {"name": "Chelsea", "score": "1"}]},
    {"id": "e2", "sport_key": "soccer_epl", "completed": False, "home_team": "Leeds", "away_team": "Wolves",
     "scores": [{"name": "Leeds", "score": "0"}, {"name": "Wolves", "score": "0"}]},
    {"id": "e3", "sport_key": "soccer_epl", "completed": False, "home_team": "Spurs", "away_team": "Villa",
     "scores": None},
]


def _oa(db, remaining=400, reserve=80):
    p = OddsApiProvider("k", Budget(db), reserve_credits=reserve)
    p.requests_remaining = remaining
    p._get = Mock(return_value=SCORES)
    return p


def test_a_completed_score_becomes_a_final_result(db):
    r = _oa(db).fetch_result("e1", event_hint={"sport_key": "soccer_epl"})
    assert r.status == "final" and r.scores["full"] == (2.0, 1.0) and r.source == "oddsapi"


def test_incomplete_and_missing_scores_are_not_final(db):
    p = _oa(db)
    assert p.fetch_result("e2", {"sport_key": "soccer_epl"}).status == "in_progress"
    assert p.fetch_result("e3", {"sport_key": "soccer_epl"}).status == "unknown"
    assert p.fetch_result("nope", {"sport_key": "soccer_epl"}) is None


def test_scores_cost_two_credits_and_one_call_per_key_per_run(db):
    p = _oa(db)
    p.fetch_result("e1", {"sport_key": "soccer_epl"})
    p.fetch_result("e2", {"sport_key": "soccer_epl"})
    assert p._get.call_count == 1
    assert p._get.call_args[0][0] == "/sports/soccer_epl/scores" and p._get.call_args[0][1]["daysFrom"] == 3
    assert Budget(db).used_month("oddsapi") == 2


def test_a_second_settle_run_the_same_day_spends_nothing(db):
    p = _oa(db)
    p.fetch_result("e1", {"sport_key": "soccer_epl"})
    p.begin_run()                                     # next settlement run
    assert p.fetch_result("e1", {"sport_key": "soccer_epl"}).status == "final"
    fresh = _oa(db)                                   # ... and a restarted process
    assert fresh.fetch_result("e1", {"sport_key": "soccer_epl"}).scores["full"] == (2.0, 1.0)
    fresh._get.assert_not_called()
    assert Budget(db).used_month("oddsapi") == 2
    fresh.fetch_result("e1", {"sport_key": "soccer_efl_champ"})       # another key is a new payload
    assert fresh._get.call_count == 1


def test_a_payload_stored_on_an_earlier_day_is_not_reused(db):
    p = _oa(db)
    p.fetch_result("e1", {"sport_key": "soccer_epl"})
    db.execute("UPDATE provider_cache SET local_date='2000-01-01' WHERE cache_key LIKE 'oddsapi:scores:%'")
    fresh = _oa(db)
    fresh.fetch_result("e1", {"sport_key": "soccer_epl"})
    assert fresh._get.call_count == 1


def test_no_sport_key_means_no_call(db):
    p = _oa(db)
    assert p.fetch_result("e1") is None and p.fetch_result("e1", {"sport_key": ""}) is None
    p._get.assert_not_called()


def test_scores_are_not_fetched_when_the_credit_gate_refuses(db):
    p = _oa(db, remaining=81, reserve=80)               # 81 - 2 < 80
    assert p.fetch_result("e1", {"sport_key": "soccer_epl"}) is None
    p._get.assert_not_called()


def test_the_sport_key_is_stored_on_events_and_persisted_with_the_pick(db):
    p = OddsApiProvider("k", Budget(db))
    row = {"id": "e1", "sport_key": "soccer_epl", "sport_title": "EPL", "commence_time": "2030-06-01T19:00:00Z",
           "home_team": "Arsenal", "away_team": "Chelsea",
           "bookmakers": [{"key": "bk", "title": "Bk", "markets": [{"key": "h2h", "outcomes": [
               {"name": "Arsenal", "price": 1.9}, {"name": "Chelsea", "price": 4.0}, {"name": "Draw", "price": 3.5}]}]}]}
    events = p._rows_to_events([("football", row)], dt(hh=12), dt(hh=23))
    assert events and events[0].extra["sport_key"] == "soccer_epl"

    e = ev(1)
    e.extra["sport_key"] = "soccer_epl"
    e.league_id = "england_-_premier_league"
    ps = select([sc(e, "result3", "home", 0.75, 1.45), sc(ev(2), "result3", "home", 0.75, 1.45)], Config(), dt())
    daily_picks.store_picks(ps, db, "2030-06-01")
    stored = [json.loads(r["sources"]) for r in db.picks_for_date("2030-06-01")]
    assert any(s["_sport_key"] == "soccer_epl" and s["_league_id"] == "england_-_premier_league" for s in stored)


# ============================================================ 3c results-path gate
def _event(provider="sharpapi", sport="football", league_id="england_-_premier_league", sources=None, extra=None):
    return SimpleNamespace(provider=provider, sport=sport, league="L", league_id=league_id,
                           sources=sources or [provider], extra=extra or {})


def _providers(**names):
    return {n: Mock() for n in names}


def test_sportsgameodds_events_have_a_path():
    assert results_path(_event("sportsgameodds", league_id="NBA", sport="basketball"), Config(),
                        _providers(sportsgameodds=1)) == "sportsgameodds"


def test_oddsapi_events_need_a_scores_covered_sport_key():
    prov = _providers(oddsapi=1)
    assert results_path(_event("oddsapi", extra={"sport_key": "soccer_epl"}), Config(), prov) == "oddsapi"
    assert results_path(_event("oddsapi", extra={"sport_key": "tennis_atp_us_open"}, sport="tennis",
                               league_id=""), Config(), prov) is None
    assert results_path(_event("oddsapi", league_id=""), Config(), prov) is None            # no key stored


def test_football_needs_football_data_and_a_mapped_league():
    assert results_path(_event(), Config(), _providers(football_data=1)) == "football_data"
    assert results_path(_event(), Config(), _providers(oddsapi=1)) is None                   # no key -> no path
    assert results_path(_event(league_id="turkey_-_super_lig"), Config(), _providers(football_data=1)) is None
    assert results_path(_event(sport="tennis", league_id="atp"), Config(), _providers(football_data=1)) is None


def test_a_league_sportsgameodds_covers_is_settled_through_find_result():
    sgo = SimpleNamespace(_available_leagues={"NBA", "MLB"}, leagues=["NBA"])
    e = _event("sharpapi", sport="basketball", league_id="nba")
    assert results_path(e, Config(), {"sportsgameodds": sgo}) == "sportsgameodds"
    assert results_path(_event("sharpapi", sport="basketball", league_id="euroleague"), Config(),
                        {"sportsgameodds": sgo}) is None
    assert results_path(_event("sharpapi", sport="tennis", league_id="atp"), Config(), {"sportsgameodds": sgo}) is None


def test_the_gate_logs_each_league_without_a_path_once(caplog):
    gate = make_results_gate(Config(), {})
    with caplog.at_level(logging.INFO):
        for _ in range(3):
            assert gate(_event(league_id="turkey_-_super_lig")) is None
        gate(_event(league_id="nigeria_-_premier_league"))
    lines = [r.message for r in caplog.records if "NO_RESULTS_PATH" in r.message]
    assert len(lines) == 2 and "turkey_-_super_lig" in lines[0]


def test_the_picker_rejects_events_without_a_results_path_and_counts_them_per_league():
    e_ok, e_no, e_no2 = ev(1), ev(2), ev(3)
    e_ok.league_id, e_no.league_id, e_no2.league_id = "england_-_premier_league", "turkey_-_super_lig", "turkey_-_super_lig"
    pool = [sc(e, "result3", "home", 0.75, 1.45) for e in (e_ok, e_no, e_no2)] + [sc(e_no, "total", "over", 0.75, 1.45, line=2.5)]
    gate = lambda event: "football_data" if event.league_id == "england_-_premier_league" else None

    ps = select(pool, Config(), dt(), results_gate=gate)

    assert ps.diagnostics["selection_rejections"]["NO_RESULTS_PATH"] == 3          # three candidates ...
    assert ps.diagnostics["no_results_path_by_league"] == {"turkey_-_super_lig": 2}   # ... on two events
    assert all(s.event is e_ok for s in ps.all())


def test_build_picks_wires_the_gate_from_the_chain_providers(db, monkeypatch):
    from predbot.providers.chain import ProviderChain
    fd = Mock(); fd.enabled.return_value = True; fd.fetch_events.return_value = []
    src = Mock(); src.enabled.return_value = True
    a, b = ev(1), ev(2)
    a.league_id, b.league_id = "england_-_premier_league", "turkey_-_super_lig"
    a.provider = b.provider = "sharpapi"
    src.fetch_events.return_value = [a, b]
    chain = ProviderChain({"sharpapi": src, "football_data": fd}, ["sharpapi"], 1)
    chain.db, chain.cfg = None, None
    monkeypatch.setattr(daily_picks, "score_events",
                        lambda events, *x: [sc(e, "result3", "home", 0.75, 1.45) for e in events])

    ps = daily_picks.build_picks(chain, db, Config(timezone="UTC", provider_order=["sharpapi"]), dt())

    assert ps.diagnostics["no_results_path_by_league"] == {"turkey_-_super_lig": 1}


# ============================================================ 3d resolver order
def _res(status="final", source="x"):
    return MatchResult(status=status, scores={"full": (1.0, 0.0)}, source=source)


def _prov(**kw):
    p = Mock()
    p.fetch_result.return_value = kw.get("fetch")
    p.find_result.return_value = kw.get("find")
    return p


def test_resolver_order_is_origin_then_alt_ids_then_football_data_then_sgo_for_uncovered_leagues():
    origin, alt, fd, sgo = _prov(), _prov(), _prov(), _prov(find=_res(source="sgo"))
    fd.find_result.return_value = None
    resolver = ResultsResolver({"sharpapi": origin, "oddsapi": alt, "football_data": fd, "sportsgameodds": sgo})
    calls = []
    for name, p in (("origin", origin), ("alt", alt), ("fd", fd), ("sgo", sgo)):
        p.fetch_result.side_effect = lambda *a, _n=name, **k: calls.append(_n)
        p.find_result.side_effect = lambda *a, _n=name, _p=p, **k: (calls.append(_n), _p.find_result.return_value)[1]

    r = resolver.resolve("sharpapi", "1", "football", "A", "B", dt(), alt_ids={"oddsapi": "9"},
                         hints={"league_id": "soccer_uefa_europa_league"})

    assert r.source == "sgo" and calls == ["origin", "alt", "fd", "sgo"]
    assert fd.find_result.call_args.kwargs["league_id"] == "soccer_uefa_europa_league"


@pytest.mark.parametrize("league_id", ["soccer_epl", "england_-_premier_league"])
def test_a_league_football_data_covers_skips_the_paid_scores_call(league_id):
    origin, alt, fd = _prov(fetch=_res(source="oddsapi")), _prov(fetch=_res(source="oddsapi")), _prov(find=_res(source="fd"))
    resolver = ResultsResolver({"oddsapi": origin, "football_data": fd, "sharpapi": alt})

    r = resolver.resolve("oddsapi", "e1", "football", "A", "B", dt(), alt_ids={"sharpapi": "9"},
                         hints={"league_id": league_id, "sport_key": league_id})

    assert r.source == "fd"
    origin.fetch_result.assert_not_called()
    alt.fetch_result.assert_not_called()


def test_a_covered_league_falls_back_to_scores_when_football_data_has_no_answer():
    origin, fd = _prov(fetch=_res(source="oddsapi")), _prov()
    fd.find_result.return_value = None
    resolver = ResultsResolver({"oddsapi": origin, "football_data": fd})

    r = resolver.resolve("oddsapi", "e1", "football", "A", "B", dt(), hints={"league_id": "soccer_epl"})

    assert r.source == "oddsapi" and fd.find_result.call_count == 1          # not asked twice


def test_scores_stay_first_for_keys_football_data_does_not_cover():
    origin, fd = _prov(fetch=_res(source="oddsapi")), _prov(find=_res(source="fd"))
    resolver = ResultsResolver({"oddsapi": origin, "football_data": fd})
    for key in ("soccer_uefa_europa_league", "soccer_turkey_super_league"):
        assert resolver.resolve("oddsapi", key, "football", "A", "B", dt(), hints={"league_id": key}).source == "oddsapi"
    fd.find_result.assert_not_called()


def test_football_data_is_only_asked_about_football_and_stops_the_chain_when_it_answers():
    fd, sgo = _prov(find=_res(source="fd")), _prov(find=_res(source="sgo"))
    resolver = ResultsResolver({"football_data": fd, "sportsgameodds": sgo})
    assert resolver.resolve("sharpapi", "1", "football", "A", "B", dt()).source == "fd"
    sgo.find_result.assert_not_called()
    resolver.resolve("sharpapi", "2", "basketball", "A", "B", dt())
    assert fd.find_result.call_count == 1                                         # not asked about basketball


@pytest.mark.parametrize("failing", ["origin", "alt", "football_data", "sportsgameodds"])
def test_every_provider_call_in_the_resolver_swallows_provider_errors(failing):
    def boom(*a, **k):
        raise ProviderError("down")
    provs = {"sharpapi": _prov(), "oddsapi": _prov(), "football_data": _prov(), "sportsgameodds": _prov()}
    key = {"origin": "sharpapi", "alt": "oddsapi"}.get(failing, failing)
    provs[key].fetch_result.side_effect = boom
    provs[key].find_result.side_effect = boom
    resolver = ResultsResolver(provs)

    assert resolver.resolve("sharpapi", "1", "football", "A", "B", dt(), alt_ids={"oddsapi": "9"}) is None


# ============================================================ 3e never raises
def _pick(db, i, **kw):
    row = {"pick_date": "2030-06-01", "tier": "daily", "rank": i, "provider": "sharpapi", "provider_event_id": str(i),
           "sport": "football", "league": "L", "home": f"Alpha{i}x", "away": f"Beta{i}y",
           "start_utc": "2030-06-01T10:00:00Z", "market": "result3", "period": "full", "stat": "points",
           "entity": "all", "line": None, "selection": "home", "label": "x", "price": 1.5, "prob": 0.7, "ev": 0.05,
           "p_market": 0.68, "sources": {}, "provider_odd_id": ""}
    row.update(kw)
    return db.insert_pick(row)


class _Resolver:
    providers = {}

    def __init__(self, fail_ids):
        self.fail_ids = fail_ids

    def resolve(self, provider, event_id, *a, **k):
        if event_id in self.fail_ids:
            raise self.fail_ids[event_id]
        return _res()


@pytest.mark.parametrize("error", [ProviderError("x"), ValueError("x"), KeyError("x"), TypeError("x"), AttributeError("x")])
def test_one_failing_pick_is_counted_and_the_rest_are_still_settled(db, monkeypatch, error):
    monkeypatch.setattr(settle, "utcnow", lambda: dt(hh=23).replace(day=3))
    for i in (1, 2, 3):
        _pick(db, i)

    counts = settle.run(_Resolver({"2": error}), db, Config())

    assert counts["errors"] == 1 and counts["settled"] == 2


def test_an_error_after_resolution_does_not_stop_the_run_either(db, monkeypatch):
    monkeypatch.setattr(settle, "utcnow", lambda: dt(hh=23).replace(day=3))
    for i in (1, 2):
        _pick(db, i)
    real = db.settle_pick
    monkeypatch.setattr(db, "settle_pick", lambda pid, *a, **k: (_ for _ in ()).throw(TypeError("bad")) if pid == 1 else real(pid, *a, **k))

    counts = settle.run(_Resolver({}), db, Config())

    assert counts["errors"] == 1 and counts["settled"] == 1


def test_run_starts_each_provider_run_cache(db, monkeypatch):
    monkeypatch.setattr(settle, "utcnow", lambda: dt(hh=23).replace(day=3))
    resolver = _Resolver({})
    resolver.providers = {"oddsapi": Mock(), "football_data": Mock(spec=["begin_run"]), "sharpapi": Mock(spec=[])}
    settle.run(resolver, db, Config())
    resolver.providers["oddsapi"].begin_run.assert_called_once()
    resolver.providers["football_data"].begin_run.assert_called_once()


def test_a_poll_loop_survives_an_action_that_raises(db, caplog):
    class TG:
        token = "t"

        def __init__(self, loop_holder):
            self.holder, self.sent, self.calls = loop_holder, [], 0

        def get_updates(self, offset, timeout=0):
            self.calls += 1
            if self.calls == 1:
                mk = lambda uid, text: {"update_id": uid, "message": {"text": text, "chat": {"id": 1}, "from": {"id": 111}}}
                return [mk(1, "/settle"), mk(2, "/help")]
            self.holder[0].stop()
            return []

        def send(self, *a, **k):
            self.sent.append(a)
            return True

    holder = []
    tg = TG(holder)

    def explode():
        raise RuntimeError("provider blew up")

    loop = CommandLoop(tg, db, Config(telegram_admin_ids=["111"]), {"/settle": explode})
    holder.append(loop)
    with caplog.at_level(logging.ERROR):
        loop._poll()

    assert loop.offset == 3                                        # both updates consumed, loop survived
    assert any("update 1 failed" in r.message and r.exc_info for r in caplog.records)
