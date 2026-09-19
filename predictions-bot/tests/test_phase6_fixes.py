"""Phase 6: locale detection, /timezone, calibration on raw probabilities, .env parsing,
dead code removal."""
from __future__ import annotations

import pathlib
import sqlite3
from types import SimpleNamespace

import pytest

from predbot.bot.callbacks import _detect_user_timezone
from predbot.bot.commands import CommandLoop
from predbot.config import Config, _parse_env_file
from predbot.jobs import daily_picks
from predbot.model import calibration
from predbot.model.calibration import Calibrator, MIN_SAMPLES
from predbot.model.elo import Elo
from predbot.model.ensemble import EventModel
from predbot.providers.base import Event, MarketLine
from predbot.selection.picker import select
from predbot.storage.db import Database
from predbot.util.timeutil import canonical_tz
from tests.conftest import dt
from tests.test_picker import ev, sc
from tests.test_subscribers_broadcast import FakeTG

ROOT = pathlib.Path(__file__).resolve().parent.parent


# ============================================================ locale detection
@pytest.mark.parametrize("code,expected", [
    ("en_NG", "Africa/Lagos"), ("en-NG", "Africa/Lagos"), ("EN-ng", "Africa/Lagos"),
    ("en-US", "America/New_York"), ("en_us", "America/New_York"), ("pt-br", "America/Sao_Paulo"),
    ("en", "Africa/Lagos"), ("EN", "Africa/Lagos"),
    ("xx", None), ("xx_XX", None), ("", None), (None, None),
])
def test_locale_detection_handles_telegram_style_tags(code, expected):
    assert _detect_user_timezone({"language_code": code}) == expected


def test_locale_detection_without_a_user():
    assert _detect_user_timezone({}) is None and _detect_user_timezone(None) is None


# ================================================================= /timezone
def _loop(db):
    return CommandLoop(FakeTG(), db, Config(timezone="Africa/Lagos", delivery_time="09:30"), {}, service=None)


def test_timezone_command_validates_and_stores_the_canonical_zone(db):
    reply = _loop(db).handle("/timezone", "1", "42", ["america/new_york"])
    assert "America/New_York" in reply and db.subscriber_timezone("42") == "America/New_York"
    assert "09:30" in reply                                   # delivery is one shared instant


@pytest.mark.parametrize("bad", ["Mars/Olympus", "Lagos", "UTC+1", "../../etc", ""])
def test_timezone_command_rejects_unknown_zones(db, bad):
    reply = _loop(db).handle("/timezone", "1", "42", [bad])
    assert "Unknown timezone" in reply and db.subscriber_timezone("42") in ("", None)


def test_timezone_command_without_an_argument_shows_the_current_zone(db):
    db.subscriber_set_timezone("42", "Asia/Tokyo")
    assert "Asia/Tokyo" in _loop(db).handle("/timezone", "1", "42", [])
    assert "Africa/Lagos" in _loop(db).handle("/timezone", "1", "77", [])      # falls back to the bot's zone


def test_canonical_tz():
    assert canonical_tz("europe/london") == "Europe/London" and canonical_tz("Europe/London") == "Europe/London"
    assert canonical_tz("UTC") is None and canonical_tz("nope/nope") is None


# ============================================================ calibration on raw p
def _settled(db, i, prob, raw, status="won"):
    row = {"pick_date": "2030-06-01", "tier": "daily", "rank": i, "provider": "p", "provider_event_id": str(i),
           "sport": "football", "league": "L", "home": "A", "away": "B", "start_utc": "2030-06-01T18:00:00Z",
           "market": "result3", "period": "full", "stat": "points", "entity": "all", "line": None,
           "selection": "home", "label": "x", "price": 1.5, "prob": prob, "ev": 0.05, "p_market": 0.6,
           "sources": {}, "provider_odd_id": "", "status": status}
    if raw is not None:
        row["prob_raw"] = raw
    return db.insert_pick(row)


def test_a_fresh_database_has_the_prob_raw_column(db):
    assert "prob_raw" in {r["name"] for r in db.query("PRAGMA table_info(picks)")}


def test_the_migration_adds_prob_raw_to_an_old_database_without_touching_rows(tmp_path):
    path = str(tmp_path / "old.db")
    fresh = Database(path)
    fresh.close()
    con = sqlite3.connect(path)
    con.execute("ALTER TABLE picks DROP COLUMN prob_raw")
    con.execute("INSERT INTO picks(pick_date,tier,rank,provider,provider_event_id,sport,home,away,start_utc,market,"
                "period,stat,entity,selection,label,price,prob,ev,status,created_at) VALUES "
                "('2030-06-01','daily',1,'p','1','football','A','B','2030-06-01T18:00:00Z','result3','full','points',"
                "'all','home','x',1.5,0.7,0.05,'won','2030-06-01T00:00:00Z')")
    con.commit()
    con.close()

    db = Database(path)                              # opening migrates

    cols = {r["name"] for r in db.query("PRAGMA table_info(picks)")}
    assert "prob_raw" in cols
    row = db.query("SELECT prob, prob_raw FROM picks")[0]
    assert row["prob"] == 0.7 and row["prob_raw"] is None
    db.close()
    Database(path).close()                           # idempotent


def test_refit_trains_on_the_raw_probability_not_the_calibrated_one(db, monkeypatch):
    for i in range(MIN_SAMPLES):
        _settled(db, i, prob=0.90, raw=0.60, status="won" if i % 2 else "lost")
    seen = {}
    monkeypatch.setattr(calibration, "fit", lambda samples, **k: seen.setdefault("s", samples) and (1.0, 0.0))

    Calibrator(db).refit()

    assert {round(p, 2) for p, _ in seen["s"]} == {0.60}


def test_old_rows_without_prob_raw_fall_back_to_prob(db, monkeypatch):
    for i in range(MIN_SAMPLES):
        _settled(db, i, prob=0.70, raw=None, status="won" if i % 2 else "lost")
    seen = {}
    monkeypatch.setattr(calibration, "fit", lambda samples, **k: seen.setdefault("s", samples) and (1.0, 0.0))

    Calibrator(db).refit()

    assert {round(p, 2) for p, _ in seen["s"]} == {0.70}


def test_brier_uses_the_raw_probability(db):
    _settled(db, 1, prob=0.99, raw=0.50, status="won")
    assert Calibrator(db).brier() == pytest.approx(0.25)


def test_the_model_keeps_the_pre_calibration_probability_and_it_is_stored(db):
    e = Event(provider="p", provider_event_id="1", sport="football", league="L", home="Alpha", away="Beta",
              start=dt(hh=20), sources=["p"], markets=[
                  MarketLine("result3", "home", 2.0, n_books=3), MarketLine("result3", "draw", 3.4, n_books=3),
                  MarketLine("result3", "away", 4.0, n_books=3)])
    cal = SimpleNamespace(apply=lambda p: 0.31, n=0)
    model = EventModel(e, Elo(db), cal)

    scored = model.score(e.markets[0])

    assert scored.p_model == 0.31 and scored.p_raw is not None and abs(scored.p_raw - 0.31) > 0.05

    ps = select([sc(ev(1), "result3", "home", 0.75, 1.45), sc(ev(2), "result3", "home", 0.75, 1.45)], __import__("predbot.config", fromlist=["Config"]).Config(), dt())
    for s in ps.all():
        s.p_raw = 0.66
    daily_picks.store_picks(ps, db, "2030-06-01")
    assert {r["prob_raw"] for r in db.picks_for_date("2030-06-01")} == {0.66}


# ================================================================ .env parsing
def _parse(tmp_path, text):
    f = tmp_path / ".env"
    f.write_text(text, encoding="utf-8")
    return _parse_env_file(f)


def test_a_hash_is_a_comment_only_after_whitespace_in_an_unquoted_value(tmp_path):
    env = _parse(tmp_path, "A=abc#def\nB=abc # a comment\nC=  \t# whole value is a comment\nD=x#y # z\n"
                           "E=https://h/p#frag\nF=7 #7\n")
    assert env == {"A": "abc#def", "B": "abc", "C": "", "D": "x#y", "E": "https://h/p#frag", "F": "7"}


def test_a_quoted_value_keeps_its_hash_and_ends_at_the_closing_quote(tmp_path):
    env = _parse(tmp_path, 'A="pa#ss word"\nB=\'q # r\' # note\nC="x"\nD="unterminated\n')
    assert env == {"A": "pa#ss word", "B": "q # r", "C": "x", "D": "unterminated"}


def test_comment_lines_and_blank_lines_are_skipped(tmp_path):
    assert _parse(tmp_path, "# c\n\nK=v\nnot a pair\n") == {"K": "v"}


def test_the_shipped_example_still_parses_to_its_documented_values(tmp_path):
    env = _parse_env_file(ROOT / ".env.example")
    assert env["SGO_SPORTS"] == "" and env["PRICE_MODE"] == "median" and env["DELIVERY_TIME"] == "09:30"
    assert env["SGO_EVENT_LIMIT"] == "50"


# ============================================================== dead code gone
def test_dead_code_is_removed():
    assert not hasattr(Config(), "chain_min_events") and not hasattr(Config, "enabled_providers")
    assert not hasattr(Database, "recent_pick_keys")
    assert "CHAIN_MIN_EVENTS" not in (ROOT / ".env.example").read_text(encoding="utf-8")
