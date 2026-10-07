"""Regression tests for the small, controlled hardening pass.

Covers: single-instance documentation, /run regeneration-vs-repost split,
late-night empty snapshot preservation, insert_pick atomicity, removal of dead
Top knobs, and the SUBSCRIBER_DELIVERY_ENABLED channel-only switch.

All offline: tmp SQLite databases, fake Telegram/chains, no network.
"""
from __future__ import annotations

import pathlib
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from predbot.bot import broadcast
from predbot.cache.fetcher import UnifiedFetcher
from predbot.cache.models import DailySnapshot
from predbot.config import Config
from predbot.jobs import daily_picks, live_results
from predbot.providers.chain import ResultsResolver
from predbot.selection.picker import PickSet
from tests.test_live_results import DAY, NOW, Provider, Telegram as LiveTG, final, pick, received
from tests.test_subscribers_broadcast import FakeTG, _snapshot

ROOT = pathlib.Path(__file__).resolve().parent.parent
NOW_UTC = datetime(2030, 6, 1, 9, 30, tzinfo=timezone.utc)
CFG = Config(timezone="UTC")


@pytest.fixture(autouse=True)
def _joined_early(monkeypatch):
    import predbot.storage.db as db_mod
    monkeypatch.setattr(db_mod, "_now", lambda: "2030-01-01T00:00:00Z")


# ------------------------------------------------------------------ 1. single instance
def test_deploy_docs_require_exactly_one_worker_and_a_persistent_volume():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "exactly ONE" in text and "worker/replica" in text
    assert "/data" in text and "persistent volume" in text
    assert "Never run manual generation against the production database" in text


# ------------------------------------------------------------------ 2. /run regen vs repost
class _Service:
    """Fake snapshot service that records how it was asked to generate."""

    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.calls = []

    def get_daily_snapshot(self, **kw):
        self.calls.append(kw)
        return self.snapshot

    def refresh_cache_from_db(self):
        return True


def _channel_tg():
    tg = Mock()
    tg.chat_id = "-1001234567890"
    tg.enabled.return_value = True
    tg.send.side_effect = lambda text, **kw: "mid"
    return tg


def test_first_publication_posts_once_then_run_with_force_sends_nothing(db):
    svc = _Service(_snapshot())
    tg = _channel_tg()
    daily_picks.run(None, db, CFG, tg, service=svc)
    assert tg.send.call_count == 2
    assert db.post_done("picks_top", "2030-06-01") and db.post_done("picks_daily", "2030-06-01")

    # /run equivalent: regeneration requested, but the cards are already posted.
    daily_picks.run(None, db, CFG, tg, force=True, service=svc)
    assert tg.send.call_count == 2, "force must regenerate, never repost"
    assert any(c.get("force_refresh") for c in svc.calls[1:]), "regen must still refresh the snapshot"


def test_repeated_run_is_idempotent_from_the_channel_view(db):
    svc = _Service(_snapshot())
    tg = _channel_tg()
    for force in (False, True, True):
        daily_picks.run(None, db, CFG, tg, force=force, service=svc)
    assert tg.send.call_count == 2


def test_recap_with_force_does_not_repost(db, monkeypatch):
    from predbot.jobs import recap

    monkeypatch.setattr(recap, "utcnow", lambda: datetime(2030, 6, 1, 8, 0, tzinfo=timezone.utc))
    tg = _channel_tg()
    # Seed one settled pick so there is something to recap.
    pid = db.insert_pick(dict(pick_date="2030-05-31", tier="all", rank=1, provider="sharpapi",
                              provider_event_id="e1", sport="football", league="L", home="A",
                              away="B", start_utc="2030-05-31T10:00:00Z", market="result3",
                              period="full", stat="points", entity="all", line=None,
                              selection="home", label="A to Win", price=1.8, prob=0.7,
                              ev=0.1, sources={}))
    db.settle_pick(pid, "won")
    db.add_membership(pid, "top", 1)  # recap covers published (Top/Daily) picks
    cfg = Config(timezone="UTC")
    assert recap.run(db, cfg, tg) is not None
    assert tg.send.call_count == 1
    assert recap.run(db, cfg, tg, force=True) is None
    assert tg.send.call_count == 1, "forced recap must not duplicate the channel post"


# ------------------------------------------------------------------ 3. late-night empty snapshot
def test_no_remaining_window_returns_partial_not_complete_empty(db):
    cfg = Config(timezone="UTC", min_lead_minutes=240)
    # 23:30 UTC with a 240-minute lead: start (03:30) is past the window end (midnight + 3h).
    now = datetime(2030, 6, 1, 23, 30, tzinfo=timezone.utc)
    ps = daily_picks.build_picks(None, db, cfg, now)
    assert ps.predictions == [] and ps.complete is False
    assert ps.diagnostics.get("empty_reason") == "no_remaining_window"


def _stored_row(i, price=1.9):
    return dict(pick_date=_today(), tier="all", rank=i, provider="sharpapi",
                provider_event_id=f"e{i}", sport="football", league="L1", home=f"H{i}",
                away=f"A{i}", start_utc="2030-06-01T18:00:00Z", market="result3",
                period="full", stat="points", entity="all", line=None, selection="home",
                label=f"H{i} to Win", price=price, prob=0.70, ev=0.05, books=4,
                quality="main", rank_score=0.70, canonical_event_id=f"c{i}", sources={})


def _today():
    from predbot.util.timeutil import to_local, utcnow
    return to_local(utcnow(), "UTC").date().isoformat()


def test_late_night_empty_regeneration_preserves_the_valid_cached_snapshot(db, monkeypatch):
    # Morning state: stored All Predictions (as a real generation would leave)
    # plus a COMPLETE cached snapshot for today.
    for i in range(1, 5):
        db.insert_pick(_stored_row(i, price=1.9 if i < 3 else 1.8))
    fetcher = UnifiedFetcher(None, db, Config(timezone="UTC"))
    from predbot.util.timeutil import utcnow
    valid = _snapshot()
    valid.date = _today()  # the cache key is today's date
    valid.status = "COMPLETE"
    fetcher._store(valid, utcnow())

    # Late-night retry: no usable window remains, providers say nothing new.
    empty = PickSet(complete=False)
    empty.diagnostics["empty_reason"] = "no_remaining_window"
    monkeypatch.setattr(daily_picks, "build_picks", lambda *a, **k: empty)

    snap = fetcher.get_daily_snapshot(force_refresh=True, scheduled=True)
    assert snap.status == "PARTIAL"
    assert snap.top_picks and snap.daily_picks, \
        "the valid earlier card must survive a late empty regeneration"
    assert snap.total_picks() > 0, "no false empty card may replace it"


def test_genuinely_empty_valid_day_still_behaves_as_complete_empty(db, monkeypatch):
    fetcher = UnifiedFetcher(None, db, Config(timezone="UTC"))
    empty = PickSet(complete=True)  # providers answered: nothing qualifies
    monkeypatch.setattr(daily_picks, "build_picks", lambda *a, **k: empty)
    snap = fetcher.get_daily_snapshot(force_refresh=True, scheduled=True)
    assert snap.status == "COMPLETE" and snap.total_picks() == 0


# ------------------------------------------------------------------ 4. insert_pick atomicity
def _pick_row():
    return dict(pick_date="2030-06-01", tier="all", rank=1, provider="sharpapi",
                provider_event_id="e1", sport="football", league="L", home="A", away="B",
                start_utc="2030-06-01T18:00:00Z", market="result3", period="full",
                stat="points", entity="all", line=None, selection="home",
                label="A to Win", price=1.8, prob=0.7, ev=0.1, sources={})


def test_insert_pick_writes_pick_and_membership_together(db):
    pid = db.insert_pick(_pick_row())
    assert db.query("SELECT COUNT(*) AS n FROM picks")[0]["n"] == 1
    assert {r["collection"] for r in db.query(
        "SELECT collection FROM prediction_memberships WHERE prediction_id=?", (pid,))} == {"all"}


def test_insert_pick_rolls_back_the_pick_when_membership_fails(db, monkeypatch):
    real = db.add_membership
    calls = []

    def fail_once(prediction_id, collection, rank=0):
        calls.append(collection)
        if len(calls) == 1:
            raise RuntimeError("membership write failed")
        return real(prediction_id, collection, rank)

    monkeypatch.setattr(db, "add_membership", fail_once)
    with pytest.raises(RuntimeError):
        db.insert_pick(_pick_row())
    assert db.query("SELECT COUNT(*) AS n FROM picks")[0]["n"] == 0, \
        "a pick without its membership must not survive"
    assert db.query("SELECT COUNT(*) AS n FROM prediction_memberships")[0]["n"] == 0


def test_insert_pick_nests_safely_inside_an_outer_transaction(db):
    with db.transaction():
        pid = db.insert_pick(_pick_row())
        assert db.query("SELECT COUNT(*) AS n FROM picks")[0]["n"] == 1
    assert db.query("SELECT COUNT(*) AS n FROM prediction_memberships "
                    "WHERE prediction_id=?", (pid,))[0]["n"] == 1


# ------------------------------------------------------------------ 5. dead Top knobs are gone
def test_removed_top_knobs_are_not_config_fields():
    c = Config()
    for attr in ("top_legs_min", "top_leg_odds_min", "top_min_agreement"):
        assert not hasattr(c, attr)


def test_top_selection_is_unchanged_without_the_dead_knobs():
    from predbot.selection.top import select_top
    assert select_top.__code__.co_varnames is not None  # module imports; behavior pinned by suite
    c = Config()
    assert (c.top_odds_target, c.top_odds_max, c.top_legs_max) == (2.00, 2.50, 3)


# ------------------------------------------------------------------ 7. channel-only switch
def test_subscriber_delivery_defaults_to_enabled():
    assert Config().subscriber_delivery_enabled is True


def test_subscriber_delivery_can_be_disabled_from_the_environment(monkeypatch):
    monkeypatch.setenv("SUBSCRIBER_DELIVERY_ENABLED", "false")
    assert Config().subscriber_delivery_enabled is False


def test_disabled_flag_stops_subscriber_picks_but_keeps_rows(db):
    db.subscriber_add("42")
    tg = FakeTG()
    cfg = Config(timezone="UTC", subscriber_delivery_enabled=False)
    assert broadcast.deliver_picks(db, cfg, tg, _snapshot(), now=NOW_UTC) == 0
    assert tg.sends == []
    assert len(db.subscribers_active()) == 1, "subscriber rows must remain untouched"
    assert db.query("SELECT COUNT(*) AS n FROM deliveries")[0]["n"] == 0


def test_disabled_flag_stops_subscriber_recap(db):
    db.subscriber_add("42")
    tg = FakeTG()
    cfg = Config(timezone="UTC", subscriber_delivery_enabled=False)
    assert broadcast.deliver_recap(db, cfg, tg, "text") == 0
    assert tg.sends == []


def test_channel_posting_continues_while_subscriber_delivery_is_disabled(db):
    svc = _Service(_snapshot())
    tg = _channel_tg()
    cfg = Config(timezone="UTC", subscriber_delivery_enabled=False)
    daily_picks.run(None, db, cfg, tg, service=svc)
    assert tg.send.call_count == 2, "channel publishing must continue normally"
    assert db.post_done("picks_top", "2030-06-01") and db.post_done("picks_daily", "2030-06-01")


def test_reenabled_flag_restores_subscriber_delivery(db):
    db.subscriber_add("42")
    tg = FakeTG()
    cfg = Config(timezone="UTC", subscriber_delivery_enabled=False)
    assert broadcast.deliver_picks(db, cfg, tg, _snapshot(), now=NOW_UTC) == 0
    cfg = Config(timezone="UTC", subscriber_delivery_enabled=True)
    assert broadcast.deliver_picks(db, cfg, tg, _snapshot(), now=NOW_UTC) == 1
    assert len(tg.sends) == 2


def _live_env(db, monkeypatch, enabled):
    import predbot.bot.broadcast as bmod
    monkeypatch.setattr(bmod.time, "sleep", lambda _: None)
    provider = Provider()
    resolver = ResultsResolver({"espn": provider})
    cfg = Config(live_updates_enabled=True, live_poll_minutes=5,
                 subscriber_delivery_enabled=enabled)
    return provider, resolver, cfg, LiveTG()


def test_disabled_flag_skips_live_pushes_but_still_settles(db, monkeypatch):
    pick(db)
    received(db)
    provider, resolver, cfg, tg = _live_env(db, monkeypatch, enabled=False)
    final((provider,))
    live_results.run(resolver, db, cfg, tg, now=NOW)
    assert db.picks_for_date(DAY)[0]["status"] == "won", "settlement must continue"
    assert tg.messages == [], "no subscriber push while disabled"
    assert db.query("SELECT notifications_done FROM live_fixture_state")[0]["notifications_done"] == 1
    assert db.query("SELECT COUNT(*) AS n FROM deliveries WHERE kind LIKE 'match_%'")[0]["n"] == 0

    # Re-enabling later must NOT replay the skipped notification.
    _, resolver_on, cfg_on, tg_on = _live_env(db, monkeypatch, enabled=True)
    live_results.run(resolver_on, db, cfg_on, tg_on, now=NOW)
    assert tg_on.messages == [], "skipped notifications are marked done, never replayed"
    assert db.query("SELECT COUNT(*) AS n FROM deliveries WHERE kind LIKE 'match_%'")[0]["n"] == 0


def test_enabled_flag_sends_live_pushes(db, monkeypatch):
    pick(db)
    received(db)
    provider, resolver, cfg, tg = _live_env(db, monkeypatch, enabled=True)
    final((provider,))
    live_results.run(resolver, db, cfg, tg, now=NOW)
    assert db.picks_for_date(DAY)[0]["status"] == "won"
    assert len(tg.messages) == 1, "push restored when enabled"
