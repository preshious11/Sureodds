"""Historical results: canonical keys, idempotent ingestion, a fetch ledger, strict time filters, canonical-id Elo."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from predbot.history import ingest
from predbot.history.store import HistoryStore, season_of
from predbot.model.elo import Elo
from predbot.storage.identity import IdentityRegistry

NOW = datetime(2030, 3, 1, 12, tzinfo=timezone.utc)
CSV = """Div,Date,Time,HomeTeam,AwayTeam,FTHG,FTAG,PSH,PSD,PSA,B365H,B365D,B365A
E0,10/08/2029,15:00,Arsenal,Chelsea,2,1,2.10,3.40,3.60,2.05,3.40,3.70
E0,17/08/2029,15:00,Chelsea,Everton,1,1,1.60,4.00,5.50,1.62,3.9,5.5
E0,24/08/2029,15:00,Everton,Arsenal,0,3,5.0,3.9,1.65,5.1,3.8,1.66
,25/08/2029,15:00,Bad,Row,x,y,,,,,,
"""


def _fd_match(mid, home, away, hid, aid, score=(2, 1), day="2029-08-10T15:00:00Z", duration="REGULAR", status="FINISHED"):
    sc = {"duration": duration, "fullTime": {"home": score[0], "away": score[1]}, "halfTime": {"home": 1, "away": 0}}
    if duration != "REGULAR":
        sc["fullTime"] = {"home": score[0] + 1, "away": score[1] + 2}          # includes extra time / penalties
        sc["regularTime"] = {"home": score[0], "away": score[1]}
    return {"id": mid, "utcDate": day, "status": status, "score": sc,
            "homeTeam": {"id": hid, "name": home, "shortName": home.split()[0], "tla": home[:3].upper()},
            "awayTeam": {"id": aid, "name": away, "shortName": away.split()[0], "tla": away[:3].upper()}}


def test_csv_import_is_idempotent_and_keyed_canonically(db):
    first = ingest.ingest_csv(db, CSV, now=NOW)
    again = ingest.ingest_csv(db, CSV, now=NOW)
    assert first["inserted"] == 3 and first["unusable"] == 1
    assert again["inserted"] == 0 and again["duplicate"] == 3
    store = HistoryStore(db)
    rows = store.results(competition_id="epl")
    assert len(rows) == 3 and all(r.home_id != r.away_id for r in rows)
    assert {r.season for r in rows} == {"2029"}
    # Arsenal is ONE canonical participant across its three matches, and odds were stored for the market baseline
    arsenal = {rows[0].home_id, rows[2].away_id}
    assert len(arsenal) == 1
    assert set(store.odds(rows[0].id)) == {"pinnacle", "bet365"}
    cov = store.coverage()["epl"]
    assert cov["matches"] == 3 and cov["teams"] == 3 and cov["with_odds"] == 3 and cov["seasons"] == ["2029"]


def test_the_same_real_match_from_a_second_source_is_stored_once(db):
    ingest.ingest_csv(db, CSV, now=NOW)
    store = HistoryStore(db)
    r = store.results(competition_id="epl")[0]
    outcome = store.add_result(sport="football", competition_id="epl", kickoff=r.kickoff + timedelta(hours=1),
                               home_id=r.home_id, away_id=r.away_id, home_goals=2, away_goals=1, source="espn",
                               source_match_id="espn-1")
    assert outcome == "duplicate" and len(store.results(competition_id="epl")) == 3


def test_unusable_rows_are_rejected_not_stored(db):
    store, reg = HistoryStore(db), IdentityRegistry(db)
    h, a = reg.participant("t", "football", "Alpha"), reg.participant("t", "football", "Bravo")
    for bad in (dict(away_id=h), dict(home_goals=-1)):
        row = dict(sport="football", competition_id="epl", kickoff=NOW, home_id=h, away_id=a, home_goals=1, away_goals=0,
                   source="t", source_match_id="x")
        row.update(bad)
        with pytest.raises(ValueError):
            store.add_result(**row)
    with pytest.raises(ValueError):
        store.add_result(sport="football", competition_id="epl", kickoff=datetime(2030, 1, 1), home_id=h, away_id=a,
                         home_goals=1, away_goals=0, source="t", source_match_id="naive")
    assert store.results() == []


def test_football_data_backfill_is_cached_by_the_ledger_and_never_refetches_a_past_season(db):
    calls = []

    def get(path, params):
        calls.append((path, params["season"]))
        return {"matches": [_fd_match(1, "Arsenal FC", "Chelsea FC", 57, 61),
                            _fd_match(2, "Chelsea FC", "Everton FC", 61, 62, day="2029-08-17T15:00:00Z", duration="EXTRA_TIME"),
                            _fd_match(3, "Everton FC", "Arsenal FC", 62, 57, status="SCHEDULED", day="2029-08-24T15:00:00Z")]}

    report = ingest.ingest_football_data(db, get, ["PL"], ["2028"], now=NOW)       # a past season
    assert report["PL:2028"] == {"unusable": 1, "inserted": 2}
    rows = HistoryStore(db).results(competition_id="epl")
    assert (rows[1].home_goals, rows[1].away_goals) == (2, 1)            # extra-time match: the 90-minute score only
    again = ingest.ingest_football_data(db, get, ["PL"], ["2028"], now=NOW + timedelta(days=90))
    assert again["PL:2028"] == {"skipped": "already fetched"}
    assert len(calls) == 1
    plan = ingest.plan_football_data(db, ["PL", "PD"], ["2028", "2030"], now=NOW)
    assert plan == [("PL", "2030"), ("PD", "2028"), ("PD", "2030")]


def test_the_current_season_refreshes_at_most_daily_and_a_failure_backs_off(db):
    store = HistoryStore(db)
    now_season = season_of(NOW)
    state = {"fail": True, "calls": 0}

    def get(path, params):
        state["calls"] += 1
        if state["fail"]:
            raise RuntimeError("boom")
        return {"matches": []}

    ingest.ingest_football_data(db, get, ["PL"], [now_season], now=NOW)
    assert store.fetch_state("football_data", "epl", now_season)["status"] == "failed"
    ingest.ingest_football_data(db, get, ["PL"], [now_season], now=NOW + timedelta(minutes=10))
    assert state["calls"] == 1                                            # backed off
    state["fail"] = False
    ingest.ingest_football_data(db, get, ["PL"], [now_season], now=NOW + timedelta(hours=2))
    ingest.ingest_football_data(db, get, ["PL"], [now_season], now=NOW + timedelta(hours=3))
    assert state["calls"] == 2                                            # fetched once, then cached
    ingest.ingest_football_data(db, get, ["PL"], [now_season], now=NOW + timedelta(hours=30))
    assert state["calls"] == 3                                            # a day later the current season refreshes


def test_results_before_is_strict_so_a_prediction_never_sees_its_own_kickoff(db):
    ingest.ingest_csv(db, CSV, now=NOW)
    store = HistoryStore(db)
    allrows = store.results(competition_id="epl")
    before = store.results(competition_id="epl", before=allrows[1].kickoff)
    assert [r.kickoff for r in before] == [allrows[0].kickoff]


def test_elo_is_keyed_by_canonical_participant_id_and_learns_each_result_once(db):
    ingest.ingest_csv(db, CSV, now=NOW)
    store = HistoryStore(db)
    assert store.apply_pending_elo() == 3
    assert store.apply_pending_elo() == 0                                  # idempotent
    rows = store.results(competition_id="epl")
    ratings = {r["team"]: (r["rating"], r["games"]) for r in db.query("SELECT * FROM ratings WHERE sport='football'")}
    assert set(ratings) == {rows[0].home_id, rows[0].away_id, rows[1].away_id}     # ids, not display names
    arsenal = rows[0].home_id
    assert ratings[arsenal][1] == 2 and ratings[arsenal][0] > 1500                  # two matches, both won


def test_a_legacy_name_keyed_rating_migrates_to_the_canonical_id_without_overwriting(db):
    db.set_rating("football", "arsenal", 1620.0, 12)                       # the old display-name key
    pid = IdentityRegistry(db).participant("odds", "football", "Arsenal")
    elo = Elo(db)
    assert elo.get("football", "Arsenal", pid) == (1620.0, 12)
    assert db.get_rating("football", pid) == (1620.0, 12)
    db.set_rating("football", pid, 1700.0, 13)
    assert elo.get("football", "Arsenal", pid) == (1700.0, 13)             # an existing id rating is never overwritten
    assert elo.get("football", "Arsenal") == (1620.0, 12)                  # the legacy lookup still works


def test_a_score_that_contradicts_its_own_half_time_is_rejected_as_unusable(db):
    def get(path, params):
        return {"matches": [_fd_match(1, "Arsenal FC", "Chelsea FC", 57, 61, score=(0, 0)) | {"score": {
            "duration": "REGULAR", "fullTime": {"home": 0, "away": 0}, "halfTime": {"home": 1, "away": 1}}}]}

    report = ingest.ingest_football_data(db, get, ["PL"], ["2028"], now=NOW)
    assert report["PL:2028"] == {"rejected": 1}
    assert HistoryStore(db).results() == []
