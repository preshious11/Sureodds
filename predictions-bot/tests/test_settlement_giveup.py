"""A pick with no authoritative result keeps retrying past its normal settlement_deadline
(12cba67 removed voiding there deliberately), but must not retry forever: GIVE_UP_AFTER
(predbot/jobs/settle.py) is the outer backstop that finally voids it as unresolved -
restoring the "settlement deadline expired" reason jobs/stats.py's UNRESOLVED_REASONS
already expects, and unsticking the Top/Daily ticket it belongs to so its won/lost GIF
reply (workers/src/cron.ts sendReadyResultGifs) can eventually fire."""
from __future__ import annotations

import json

from predbot.config import Config
from predbot.jobs import settle
from predbot.providers.chain import ResultsResolver
from tests.conftest import dt


class NoResultSource:
    """A results source that never finds anything - the permanently-unresolvable case."""

    def find_result(self, sport, home, away, start, league_id="", league=""):
        return None


def _pick(db, i, start_utc):
    db.insert_pick({"pick_date": "2030-06-01", "tier": "daily", "rank": i, "provider": "sharpapi",
                    "provider_event_id": f"e{i}", "sport": "football", "league": "L", "home": f"Home{i}",
                    "away": f"Away{i}", "start_utc": start_utc, "market": "result3", "period": "full",
                    "stat": "points", "entity": "all", "line": None, "selection": "home", "label": "x",
                    "price": 1.5, "prob": 0.7, "ev": 0.05, "p_market": 0.6,
                    "sources": json.dumps({"_league_id": "l", "_sport_key": "", "_alt_ids": {}}),
                    "provider_odd_id": ""})


def _resolver():
    return ResultsResolver({"sharpapi": NoResultSource()})


def test_a_pick_still_unresolved_within_the_give_up_window_stays_pending(db, monkeypatch):
    # football's settlement_deadline is 2.2h (match) + 24h (grace) past kickoff.
    # 3 days later is well past that deadline, but nowhere near the 14-day give-up.
    _pick(db, 0, "2030-06-01T12:00:00Z")
    monkeypatch.setattr(settle, "utcnow", lambda: dt(d=4, hh=12))
    counts = settle.run(_resolver(), db, Config())
    assert counts["void"] == 0 and counts["pending"] == 1
    assert db.query("SELECT status FROM picks")[0]["status"] == "pending"


def test_a_pick_still_unresolved_past_the_give_up_window_is_voided_as_unresolved(db, monkeypatch):
    _pick(db, 0, "2030-06-01T12:00:00Z")
    monkeypatch.setattr(settle, "utcnow", lambda: dt(d=1, m=7, hh=12))   # ~36 days later
    counts = settle.run(_resolver(), db, Config())
    assert counts["void"] == 1 and counts["pending"] == 0
    row = db.query("SELECT status, result_json FROM picks")[0]
    assert row["status"] == "void"
    assert json.loads(row["result_json"])["reason"] == "settlement deadline expired"


def test_a_published_ticket_with_one_permanently_unresolvable_leg_eventually_clears(db, monkeypatch):
    """The exact production scenario: a Top/Daily ticket can never show as fully settled
    (blocking its won/lost GIF forever) while even one leg sits unresolved indefinitely -
    the give-up backstop is what lets that ticket ever reach an all-non-pending state."""
    _pick(db, 0, "2030-06-01T12:00:00Z")
    _pick(db, 1, "2030-06-01T12:00:00Z")
    db.execute("UPDATE picks SET status='won' WHERE rank=1")   # the other leg already settled
    monkeypatch.setattr(settle, "utcnow", lambda: dt(d=1, m=7, hh=12))
    settle.run(_resolver(), db, Config())
    statuses = {r["status"] for r in db.query("SELECT status FROM picks")}
    assert "pending" not in statuses
    assert statuses == {"won", "void"}
