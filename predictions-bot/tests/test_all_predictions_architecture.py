"""Several All Predictions per fixture (one per market family): storage migration, immutability, Top / Daily one-per-fixture
protection, independent settlement of every family, diagnostics."""
from __future__ import annotations

import json
import sqlite3

import pytest

from predbot.config import Config
from predbot.jobs import settle
from predbot.markets.settlement import LOST, PUSH, WON, grade
from predbot.model.elo import Elo
from predbot.providers.base import MarketLine, MatchResult
from predbot.selection.daily import select_daily
from predbot.selection.top import select_top
from predbot.storage.db import Database
from predbot.util.pipeline_report import pipeline_lines
from predbot.util.timeutil import utcnow
from tests.conftest import dt

DAY = "2030-06-01"


def row(event="e1", market="result3", selection="home", line=None, status="pending", **kw):
    base = dict(pick_date=DAY, tier="all", rank=1, provider="p", provider_event_id=event, sport="football", league="L", home="Alpha",
                away="Bravo", start_utc="2030-06-01T12:00:00Z", market=market, period="full", stat="points", entity="all", line=line,
                selection=selection, label=f"{market}:{selection}", price=1.8, prob=0.7, ev=0.0, p_market=0.65, sources={"s": 0.7},
                provider_odd_id="", fixture_key=f"fk-{event}", canonical_event_id=f"evt-{event}", status=status)
    base.update(kw)
    return base


# ------------------------------------------------------------------------------------------------ migration
def _old_database(path):
    """A database as the previous architecture left it: one prediction per (day, canonical event)."""
    db = Database(path)
    db.conn.execute("DROP INDEX IF EXISTS uq_prediction_event_family_day")
    db.conn.execute("CREATE UNIQUE INDEX uq_prediction_event_day ON picks(pick_date,canonical_event_id) "
                    "WHERE canonical_event_id IS NOT NULL AND canonical_event_id<>''")
    db.conn.commit()
    return db


def _index_names(path):
    con = sqlite3.connect(path)
    try:
        return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    finally:
        con.close()


def _dump(db):
    tables = [r["name"] for r in db.query("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    return {t: [dict(r) for r in db.query(f"SELECT * FROM {t}")] for t in tables}


def test_the_migration_replaces_the_per_event_rule_without_touching_any_data(tmp_path):
    path = str(tmp_path / "old.db")
    old = _old_database(path)
    settled = old.store_prediction(row("e1", status="pending"))
    old.settle_pick(settled, "won")
    pending = old.store_prediction(row("e2", market="total", selection="over", line=2.5))
    old.subscriber_add("100", "u", "User", timezone="UTC")
    old.set_kv("keep:me", "yes")
    old.set_rating("football", "alpha", 1612.0, 14)
    old.conn.execute("INSERT INTO provider_cache VALUES ('oddsapi:2030-06-01','2030-06-01','{\"events\": []}','{}','2030-06-02T00:00:00Z',1)")
    old.conn.execute("INSERT INTO prediction_cache VALUES ('k','2030-06-01','UTC','ready','{}','{}','t','t','2030-06-02T00:00:00Z',NULL,1)")
    old.conn.execute("INSERT INTO api_budget VALUES ('oddsapi','2030-06-01',37)")
    old.conn.execute("INSERT INTO results_seen VALUES ('football|alpha|bravo|2030-06-01',2,1,'2030-06-01T20:00:00Z')")
    old.conn.commit()
    before = _dump(old)
    assert "uq_prediction_event_day" in _index_names(path)
    with pytest.raises(sqlite3.IntegrityError):                                 # the OLD rule: one row per event and day
        old.insert_pick(dict(row("e1", market="total", selection="over", line=2.5), tier="all"))
    old.close()

    new = Database(path)                                                        # opening it migrates
    after = _dump(new)
    for table, rows in before.items():                                          # every pre-existing row survives, values intact
        for original in rows:
            assert any(all(r.get(k) == v for k, v in original.items()) for r in after[table]), (table, original)
    assert new.query("SELECT used FROM api_budget WHERE provider='oddsapi'")[0]["used"] == 37       # quota counters
    assert new.query("SELECT COUNT(*) AS n FROM provider_cache")[0]["n"] == 1                        # provider cache
    names = _index_names(path)
    assert "uq_prediction_event_day" not in names and "uq_prediction_event_family_day" in names
    assert new.query("SELECT status FROM picks WHERE id=?", (settled,))[0]["status"] == "won"          # settled rows survive
    assert new.query("SELECT market FROM picks WHERE id=?", (pending,))[0]["market"] == "total"
    assert new.get_kv("keep:me") == "yes" and new.get_rating("football", "alpha") == (1612.0, 14)
    assert new.query("SELECT COUNT(*) AS n FROM subscribers")[0]["n"] == 1
    assert new.query("SELECT COUNT(*) AS n FROM picks")[0]["n"] == 2
    second = new.store_prediction(row("e1", market="total", selection="over", line=2.5))              # a second family now fits
    assert second not in (settled, pending) and new.query("SELECT COUNT(*) AS n FROM picks WHERE canonical_event_id='evt-e1'")[0]["n"] == 2
    new.close()
    Database(path).close()                                                      # idempotent: reopening changes nothing
    assert "uq_prediction_event_family_day" in _index_names(path)


#: the production index exactly as commit 5fbadab ("Cross-tier Phase 3") created it and every release since has kept it
LEGACY_FIXTURE_SELECTION_INDEX = ("CREATE UNIQUE INDEX uq_picks_fixture_selection ON picks("
                                  "pick_date, fixture_key, market, period, stat, entity, COALESCE(line, -999999), selection)")


def _production_legacy_database(path):
    """What the live container holds: both historical unique indexes, and today's rows stored by the old architecture
    BEFORE canonical event ids existed (canonical_event_id NULL), one tier per row."""
    db = _old_database(path)
    db.conn.execute("DROP INDEX IF EXISTS uq_picks_fixture_selection")
    db.conn.execute(LEGACY_FIXTURE_SELECTION_INDEX)
    db.conn.commit()
    ids = [db.insert_pick(dict(row("e1", market="result3", selection="home", canonical_event_id=None), tier="top")),
           db.insert_pick(dict(row("e2", market="total", selection="over", line=2.5, canonical_event_id=None), tier="daily")),
           db.insert_pick(dict(row("e3", market="dnb", selection="away", canonical_event_id=None, status="won"), tier="daily"))]
    db.subscriber_add("100", "u", "User", timezone="UTC")
    db.conn.commit()
    return db, ids


def _sql(path, name):
    con = sqlite3.connect(path)
    try:
        r = con.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (name,)).fetchone()
        return r[0] if r else None
    finally:
        con.close()


def test_a_production_legacy_database_stores_the_new_architecture_without_an_integrity_error(tmp_path):
    """The first live run died with ``UNIQUE constraint failed: index 'uq_picks_fixture_selection'``: today's legacy rows
    have a NULL canonical_event_id, so the canonical-id duplicate check missed them and the insert hit the fixture_key
    guard. The guard is correct and stays; the duplicate check must recognise the same fixture x family."""
    path = str(tmp_path / "prod.db")
    old, ids = _production_legacy_database(path)
    assert _sql(path, "uq_picks_fixture_selection") == LEGACY_FIXTURE_SELECTION_INDEX
    before = [dict(r) for r in old.query("SELECT * FROM picks ORDER BY id")]
    old.close()

    db = Database(path)
    assert _sql(path, "uq_prediction_event_day") is None                         # the old per-event rule is gone
    assert "uq_prediction_event_family_day" in _index_names(path)               # the approved rule exists
    assert "uq_picks_fixture_selection" in _index_names(path)                   # exact-duplicate guard: not one-per-fixture
    after = [dict(r) for r in db.query("SELECT * FROM picks ORDER BY id")]
    assert all({k: v for k, v in b.items()} == {k: a[k] for k in b} for a, b in zip(after, before))   # rows unchanged
    assert db.query("SELECT COUNT(*) AS n FROM subscribers")[0]["n"] == 1

    # the new generation regenerates the very same selection (canonical id now set): first write wins, no exception
    assert db.store_prediction(row("e1", market="result3", selection="home")) == ids[0]
    assert db.store_prediction(row("e1", market="result3", selection="draw", price=9.0)) == ids[0]      # same family, other tip
    assert db.store_prediction(row("e1", market="result3", selection="home", canonical_event_id="evt-another-provider")) == ids[0]
    assert db.store_prediction(row("e3", market="dnb", selection="away")) == ids[2]                      # a settled row is untouched
    # several different families of the SAME fixture and day are allowed
    families = [("total", "over", 2.5), ("btts", "yes", None), ("dc", "home_draw", None), ("handicap", "home", -1.5)]
    new = [db.store_prediction(row("e1", market=m, selection=sel, line=ln)) for m, sel, ln in families]
    assert len(set(new)) == 4 and not set(new) & set(ids)
    assert db.query("SELECT COUNT(*) AS n FROM picks WHERE fixture_key='fk-e1'")[0]["n"] == 5
    # a family the legacy row does not occupy (a team total is not the pool's Over/Under) is not mistaken for it
    team_total = row("e2", market="total", selection="over", line=1.5, entity="home")
    assert db.store_prediction(team_total) not in ids
    assert db.query("SELECT status FROM picks WHERE id=?", (ids[2],))[0]["status"] == "won"
    # uniqueness is not weakened: the indexes themselves still refuse a duplicate of the same fixture/day/family
    with pytest.raises(sqlite3.IntegrityError):
        db.insert_pick(dict(row("e1", market="total", selection="under", line=2.5), tier="all"))
    with pytest.raises(sqlite3.IntegrityError):
        db.insert_pick(dict(row("e2", market="total", selection="over", line=2.5, canonical_event_id=None), tier="all"))
    snapshot = _dump(db)
    db.close()
    again = Database(path)                                                       # reopening changes nothing
    assert _dump(again) == snapshot
    again.close()


def test_two_canonical_ids_for_one_fixture_key_never_raise_on_the_same_family(db):
    first = db.store_prediction(row("e1", market="total", selection="over", line=2.5))
    second = db.store_prediction(row("e1", market="total", selection="over", line=2.5, canonical_event_id="evt-duplicate"))
    assert second == first and db.query("SELECT COUNT(*) AS n FROM picks")[0]["n"] == 1


def test_the_new_rule_is_one_row_per_day_event_and_family(db):
    first = db.store_prediction(row("e1", market="result3", selection="home"))
    other_family = db.store_prediction(row("e1", market="total", selection="over", line=2.5))
    again = db.store_prediction(row("e1", market="result3", selection="away", price=9.9))       # same family: first write wins
    assert other_family != first and again == first
    assert db.query("SELECT COUNT(*) AS n FROM picks WHERE canonical_event_id='evt-e1'")[0]["n"] == 2
    with pytest.raises(sqlite3.IntegrityError):                                  # the index itself forbids a second (event, family)
        db.insert_pick(dict(row("e1", market="result3", selection="draw"), tier="all"))
    assert db.store_prediction(row("e1", market="result3", pick_date="2030-06-02")) not in (first, other_family)   # another day is fine


def test_a_stored_prediction_is_immutable_after_first_write(db):
    pid = db.store_prediction(row("e1", price=1.8, prob=0.7, label="first"))
    db.store_prediction(row("e1", price=3.3, prob=0.2, label="second"))
    stored = db.query("SELECT price, prob, label, selection FROM picks WHERE id=?", (pid,))[0]
    assert (stored["price"], stored["prob"], stored["label"]) == (1.8, 0.7, "first")


def _orphan(db, **kw):
    """A representative row with no 'all' membership, as a partial import or a
    legacy write leaves it: invisible to the membership-gated duplicate
    checks, but still enforced by uq_prediction_event_family_day."""
    import json as _json
    params = row("e9", **kw)
    params["sources"] = _json.dumps(params["sources"])
    params["created_at"] = "2030-06-01T00:00:00Z"
    params.setdefault("status", "pending")
    keys = [k for k in params if k != "id"]
    cols = ",".join(keys)
    cur = db.conn.execute(f"INSERT INTO picks({cols}) VALUES({','.join('?' for _ in keys)})",
                          [params[k] for k in keys])
    db.conn.commit()
    assert db.query("SELECT COUNT(*) AS n FROM prediction_memberships WHERE prediction_id=?",
                    (cur.lastrowid,))[0]["n"] == 0
    return int(cur.lastrowid)


def test_orphan_representative_without_membership_reconciles_without_integrity_error(db, caplog):
    """The October 13 live failure: a stored representative the duplicate
    checks cannot see (no 'all' membership) used to crash the insert with
    UNIQUE constraint failed on (pick_date, canonical_event_id, market)."""
    import logging
    orphan = _orphan(db, market="total", selection="over", line=2.5, price=1.9)
    with caplog.at_level(logging.WARNING, logger="predbot.storage.db"):
        pid = db.store_prediction(row("e9", market="total", selection="under", line=2.5, price=2.1))
    assert pid == orphan                                                  # first write wins
    assert db.query("SELECT COUNT(*) AS n FROM picks")[0]["n"] == 1       # still one row
    stored = db.query("SELECT price, selection FROM picks WHERE id=?", (pid,))[0]
    assert (stored["price"], stored["selection"]) == (1.9, "over")        # never rewritten
    assert any("representative collision kept first write" in r.message for r in caplog.records)
    assert db.query("SELECT COUNT(*) AS n FROM prediction_memberships "
                    "WHERE prediction_id=? AND collection='all'", (pid,))[0]["n"] == 1   # adopted: visible now


def test_identical_outcome_adopts_the_orphan_row(db):
    orphan = _orphan(db, market="total", selection="over", line=2.5, price=1.9)
    pid = db.store_prediction(row("e9", market="total", selection="over", line=2.5, price=2.2))
    assert pid == orphan
    assert db.query("SELECT COUNT(*) AS n FROM picks")[0]["n"] == 1


def test_repeated_generation_keeps_stable_ids_and_memberships(db):
    first = db.store_prediction(row("e1", market="result3", selection="home"))
    second = db.store_prediction(row("e1", market="result3", selection="home"))
    third = db.store_prediction(row("e1", market="total", selection="over", line=2.5))
    assert first == second and third != first
    assert db.query("SELECT COUNT(*) AS n FROM picks")[0]["n"] == 2
    assert db.query("SELECT COUNT(*) AS n FROM prediction_memberships WHERE collection='all'")[0]["n"] == 2


# ------------------------------------------------------------------------------------------------ Top / Daily
def promoted(event, market, prob, price, **kw):
    r = row(event, market=market, prob=prob, price=price, rank_score=prob, books=6, quality="main", ev=0.0, edge_ev=None, id=hash((event, market)) % 10**6, **kw)
    r["sources"] = json.dumps({"_notes": {"evidence": {}}})
    r["fixture_key"], r["canonical_event_id"] = f"fk-{event}", f"evt-{event}"
    return r


NOW = "2030-06-01T08:00:00Z"


def test_top_promotes_at_most_one_prediction_per_fixture():
    rows = [promoted("a", "result3", 0.80, 1.30), promoted("a", "dnb", 0.79, 1.28), promoted("a", "dc", 0.78, 1.25),
            promoted("b", "result3", 0.70, 1.40)]
    chosen, diag = select_top(rows, Config(), NOW)
    events = [r["canonical_event_id"] for r in chosen]
    assert len(events) == len(set(events)) and set(events) == {"evt-a", "evt-b"}
    assert diag["rejections"]["duplicate_event"] >= 1
    assert diag["combined_odds"] == pytest.approx(1.30 * 1.40)                   # the duplicate never inflates the ticket


def test_top_is_never_padded_to_reach_its_target():
    rows = [promoted("a", "result3", 0.80, 1.30), promoted("a", "dnb", 0.79, 1.28)]       # one fixture only
    chosen, diag = select_top(rows, Config(), NOW)
    assert len(chosen) == 1 and diag["combined_odds"] == pytest.approx(1.30) and diag["target_reached"] is False


def test_daily_keeps_one_prediction_per_fixture():
    rows = [promoted("a", "result3", 0.80, 1.30), promoted("a", "dnb", 0.79, 1.28), promoted("b", "result3", 0.70, 1.40)]
    chosen, diag = select_daily(rows, Config(), NOW)
    events = [r["canonical_event_id"] for r in chosen]
    assert len(events) == len(set(events)) and diag["rejections"]["duplicate_event"] == 1


# ------------------------------------------------------------------------------------------------ settlement
def res(h, a):
    return MatchResult(status="final", scores={"full": (float(h), float(a))})


@pytest.mark.parametrize("market,selection,line,score,expected", [
    ("result3", "home", None, (2, 1), WON), ("result3", "draw", None, (2, 1), LOST), ("result3", "away", None, (0, 1), WON),
    ("result2", "home", None, (5, 3), WON), ("result2", "away", None, (5, 3), LOST),
    ("dc", "home_draw", None, (1, 1), WON), ("dc", "away_draw", None, (2, 1), LOST), ("dc", "home_away", None, (1, 1), LOST),
    ("dnb", "home", None, (2, 0), WON), ("dnb", "home", None, (0, 1), LOST), ("dnb", "home", None, (1, 1), PUSH),   # a draw is void
    ("btts", "yes", None, (1, 1), WON), ("btts", "yes", None, (2, 0), LOST), ("btts", "no", None, (2, 0), WON),
    ("total", "over", 2.5, (2, 1), WON), ("total", "over", 2.5, (1, 1), LOST), ("total", "under", 2.5, (1, 1), WON),
    ("total", "over", 3.0, (2, 1), PUSH),
    ("handicap", "home", -1.5, (2, 0), WON), ("handicap", "home", -1.5, (1, 0), LOST), ("handicap", "away", 1.5, (1, 0), WON),
    ("handicap", "home", -1.0, (2, 1), PUSH), ("handicap", "away", 1.0, (1, 0), PUSH)])
def test_every_family_representative_grades_correctly(market, selection, line, score, expected):
    assert grade(MarketLine(market, selection, 1.8, line=line), res(*score)) == expected


def test_each_stored_prediction_of_a_fixture_settles_once_and_independently(db):
    ids = {m: db.store_prediction(row("e1", market=m, selection=s, line=l))
           for m, s, l in (("result3", "home", None), ("total", "over", 2.5), ("btts", "yes", None), ("dnb", "home", None), ("dc", "away_draw", None))}
    elo, counts, now = Elo(db), {"settled": 0, "pending": 0, "void": 0, "errors": 0}, utcnow()
    for r in db.query("SELECT * FROM picks WHERE pick_date=? ORDER BY id", (DAY,)):
        settle.settle_result(dict(r), res(2, 1), db, elo, now, counts)
    status = {m: db.query("SELECT status FROM picks WHERE id=?", (i,))[0]["status"] for m, i in ids.items()}
    assert status == {"result3": "won", "total": "won", "btts": "won", "dnb": "won", "dc": "lost"}      # graded independently
    assert counts["settled"] == 5
    again = {"settled": 0, "pending": 0, "void": 0, "errors": 0}
    for r in db.query("SELECT * FROM picks WHERE pick_date=?", (DAY,)):
        settle.settle_result(dict(r), res(0, 5), db, elo, now, again)                      # a second pass changes nothing
    assert again["settled"] == 0
    assert {m: db.query("SELECT status FROM picks WHERE id=?", (i,))[0]["status"] for m, i in ids.items()} == status
    assert db.query("SELECT COUNT(*) AS n FROM results_seen")[0]["n"] == 1             # the fixture teaches the ratings once


def test_a_void_dnb_and_a_push_are_recorded_without_touching_the_other_predictions(db):
    a = db.store_prediction(row("e1", market="dnb", selection="home"))
    b = db.store_prediction(row("e1", market="result3", selection="draw"))
    counts = {"settled": 0, "pending": 0, "void": 0, "errors": 0}
    for r in db.query("SELECT * FROM picks ORDER BY id"):
        settle.settle_result(dict(r), res(1, 1), db, Elo(db), utcnow(), counts)
    assert db.query("SELECT status FROM picks WHERE id=?", (a,))[0]["status"] == "push"
    assert db.query("SELECT status FROM picks WHERE id=?", (b,))[0]["status"] == "won"


# ------------------------------------------------------------------------------------------------ stats and diagnostics
def test_all_stats_count_predictions_and_fixtures_separately(db):
    from predbot.jobs import stats as stats_mod
    ids = [db.store_prediction(row("e1", market=m, selection=s, line=l)) for m, s, l in
           (("result3", "home", None), ("total", "over", 2.5), ("btts", "yes", None))]
    ids.append(db.store_prediction(row("e2", market="result3", selection="home")))
    for pid, outcome in zip(ids, ("won", "lost", "won", "won")):
        db.settle_pick(pid, outcome)
    block = stats_mod.collection_stats(db, "all", dt(hh=23))["all"]
    assert block["issued"] == 4 and block["fixtures"] == 2                        # four predictions on two fixtures
    assert block["won"] == 3 and block["lost"] == 1                               # every prediction counts once, never as a duplicate


def test_the_funnel_report_separates_fixtures_from_predictions_and_names_omissions():
    text = "\n".join(pipeline_lines({"fixtures_with_predictions": 20, "fixtures_with_valid_candidates": 21, "valid_candidate_lines": 900,
                                     "valid_predictions_generated": 105, "representatives_by_family": {"1X2": 20, "BTTS": 20},
                                     "omitted_families": {"Handicap": {"handicap_line_below_1": 12}}, "stored_all_predictions": 105}))
    assert "Fixtures with a valid prediction: 20 of 21" in text and "valid candidate lines: 900" in text
    assert "one per fixture x market family): 105" in text and "BTTS=20" in text
    assert "Handicap: handicap_line_below_1=12" in text and "All Predictions: 105" in text
