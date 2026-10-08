"""API-usage guarantees of the stored-prediction architecture.

1. Reading is storage-only: page turns, view switches and stats callbacks never reach a provider or regenerate.
2. One generation serves every reader: two users cause exactly one provider collection.
3. Predictions are first-write-wins: re-running generation never rewrites a stored prediction or its settlement.
4. db.reset_prediction_day clears only the unpublished, still-pending, future pool (+ the snapshot key)."""
from __future__ import annotations

import pytest

from predbot.bot.callbacks import handle_callback, handle_command
from predbot.cache.fetcher import UnifiedFetcher
from predbot.config import Config
from predbot.jobs import daily_picks
from tests.test_picker import sc
from tests.test_sqlite_prediction_cache import clock, service  # noqa: F401  (fixtures/helpers)
from tests.test_subscribers_broadcast import FakeTG, _cb

DAY = "2030-06-01"
TZ = "Africa/Lagos"


class ExplodingChain:
    """A provider chain that fails the test if anything tries to collect or fetch."""
    providers = {}

    def __init__(self):
        self.calls = 0

    def collect(self, *a, **k):
        self.calls += 1
        raise AssertionError("a read path reached the providers")

    fetch_events = fetch = collect


def _generated(db, monkeypatch):
    app, football, baseball = service(db, monkeypatch)
    snap = app.get_daily_snapshot(scheduled=True)
    assert snap.status == "COMPLETE" and snap.all_predictions and snap.top_picks and snap.daily_picks
    return app, snap


def _rows(db):
    """The stored predictions themselves (collection membership is a view over them and may be re-derived)."""
    view = {"tier", "rank", "collections"}
    return {r["canonical_event_id"]: {k: v for k, v in dict(r).items() if k not in view}
            for r in db.picks_for_date(DAY)}


# ------------------------------------------------------------------ 1. reads never touch providers
def test_pagination_and_stats_callbacks_perform_zero_provider_calls_and_never_regenerate(db, monkeypatch, clock):
    _generated(db, monkeypatch)
    before = _rows(db)
    chain = ExplodingChain()
    reader = UnifiedFetcher(chain, db, Config(timezone=TZ, cache_retry_seconds=900))
    tg = FakeTG()
    cfg = reader.cfg

    for data in ("pg|all|0", "pg|top|0", "pg|daily|0", "pg|all|1|20300601", "pg|daily|1|20300601",
                 "pg|stats|0", "pg|stats_all|0", "pg|stats_top|0", "pg|stats_daily|0",
                 "pg|results_all|0|20300601", "pg|results_top|0|20300601", "pg|results_daily|0|20300601",
                 "pg|yesterday|0"):
        handle_callback(_cb(data), service=reader, tg=tg, db=db, cfg=cfg)
    for cmd in ("/all", "/top", "/daily", "/stats", "/yesterday"):
        handle_command(cmd, "42", service=reader, tg=tg, cfg=cfg, db=db)

    assert tg.edits and tg.sends                       # the callbacks and commands really rendered something
    assert chain.calls == 0 and reader.api_batches == 0
    assert _rows(db) == before                         # and nothing was regenerated or rewritten


def test_reads_with_no_snapshot_do_not_generate_either(db):
    chain = ExplodingChain()
    reader = UnifiedFetcher(chain, db, Config(timezone=TZ))
    tg = FakeTG()

    # page turns (they carry their day), stats and results are storage-only even when nothing is stored
    for data in ("pg|all|1|20300601", "pg|top|1|20300601", "pg|stats|0", "pg|stats_all|0",
                 "pg|results_daily|0|20300601", "pg|yesterday|0"):
        handle_callback(_cb(data), service=reader, tg=tg, db=db, cfg=reader.cfg)

    assert chain.calls == 0 and reader.api_batches == 0 and not db.has_picks_for_date(DAY)


# ------------------------------------------------------------------ 2. one generation serves every reader
def test_two_users_reading_today_after_one_generation_cause_exactly_one_collection(db, monkeypatch, clock):
    app, football, baseball = service(db, monkeypatch)
    db.subscriber_add("1", timezone="Africa/Lagos")
    db.subscriber_add("2", timezone="America/New_York")
    tg = FakeTG()

    # M4: a reader never generates; the scheduled job does, once, and every reader is served from it
    handle_command("/top", "1", service=app, tg=tg, cfg=app.cfg, db=db)           # cold cache: "not generated yet"
    assert app.api_batches == 0 and football.fetch_events.call_count == 0
    assert "not been generated yet" in tg.sends[-1]["text"]
    tg.sends.clear()
    app.get_daily_snapshot(scheduled=True)                                        # the POST_TIME job
    handle_command("/top", "1", service=app, tg=tg, cfg=app.cfg, db=db)
    handle_command("/all", "2", service=app, tg=tg, cfg=app.cfg, db=db)
    handle_command("/daily", "2", service=app, tg=tg, cfg=app.cfg, db=db)
    handle_callback(_cb("pg|all|0"), service=app, tg=tg, db=db, cfg=app.cfg)

    assert app.api_batches == 1
    assert football.fetch_events.call_count == baseball.fetch_events.call_count == 1
    assert len(tg.sends) == 3 and len(tg.edits) == 1


# ------------------------------------------------------------------ 3. first write wins
def test_regeneration_with_different_probabilities_never_changes_a_stored_prediction_or_its_settlement(db, monkeypatch, clock):
    app, snap = _generated(db, monkeypatch)
    before = _rows(db)
    settled_id = next(iter(before.values()))["id"]
    db.settle_pick(settled_id, "won", {})
    before = _rows(db)

    def changed(events, *a):                                   # a model that now disagrees with itself
        return [sc(e, "result3" if e.sport == "football" else "result2", "away", .97, 3.9) for e in events]

    monkeypatch.setattr(daily_picks, "score_events", changed)
    again = app.get_daily_snapshot(force_refresh=True, scheduled=True)

    after = _rows(db)
    assert again.all_predictions
    assert after == before                                     # id, prob, price, selection, status: all untouched
    settled = next(r for r in after.values() if r["id"] == settled_id)
    assert settled["status"] == "won" and settled["settled_at"]
    assert len(after) == len(before)                           # and no second prediction per event


# ------------------------------------------------------------------ 4. safe reset
def test_reset_removes_only_the_unpublished_pending_future_pool_and_the_snapshot_key(db, monkeypatch, clock):
    app, snap = _generated(db, monkeypatch)
    key = app._key(DAY)
    ids = sorted(r["id"] for r in db.picks_for_date(DAY))
    settled, started, other_day = ids[0], ids[1], ids[2]
    removable = set(ids[3:])
    assert len(removable) >= 3

    db.settle_pick(settled, "won", {})
    db.execute("UPDATE picks SET start_utc='2030-06-01T10:00:00Z' WHERE id=?", (started,))      # already kicked off
    db.execute("UPDATE picks SET pick_date='2030-05-30' WHERE id=?", (other_day,))              # history, another day
    db.set_kv("history:marker", "keep")
    provider_rows = db.query("SELECT cache_key,payload_json FROM provider_cache ORDER BY cache_key")
    assert provider_rows and db.get_prediction_cache(key) is not None

    result = db.reset_prediction_day(DAY, TZ, "2030-06-01T18:00:00Z")

    left = {r["id"] for r in db.query("SELECT id FROM picks")}
    assert left == {settled, started, other_day}               # the pending future pool (and only it) is gone
    assert result["pending_predictions_removed"] == len(removable)
    assert db.get_prediction_cache(key) is None                # the shared snapshot key is invalidated
    assert db.query("SELECT cache_key,payload_json FROM provider_cache ORDER BY cache_key") == provider_rows
    assert db.get_kv("history:marker") == "keep"
    orphans = db.query("SELECT 1 FROM prediction_memberships WHERE prediction_id NOT IN (SELECT id FROM picks)")
    assert not orphans                                         # removed rows left no memberships behind
    assert db.query("SELECT status FROM picks WHERE id=?", (settled,))[0]["status"] == "won"


@pytest.mark.parametrize("how", ["delivery", "channel_post_top", "channel_post_daily"])
def test_reset_refuses_a_published_day(db, monkeypatch, clock, how):
    app, _ = _generated(db, monkeypatch)
    count = len(db.picks_for_date(DAY))
    key = app._key(DAY)
    if how == "delivery":
        db.mark_delivery("picks_top", DAY, "42")
    else:
        db.mark_post("picks_" + how.rsplit("_", 1)[1], DAY, "99")

    with pytest.raises(ValueError, match="published"):
        db.reset_prediction_day(DAY, TZ, "2030-06-01T00:00:00Z")

    assert len(db.picks_for_date(DAY)) == count and db.get_prediction_cache(key) is not None
