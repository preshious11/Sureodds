"""Community delivery against real publication/lifecycle history; no network."""
from datetime import timedelta
from unittest.mock import Mock

import pytest

from predbot.bot import broadcast
from predbot.bot.commands import CommandLoop
from predbot.cache.fetcher import UnifiedFetcher
from predbot.config import Config
from predbot.jobs import daily_picks, live_results, recap
from predbot.providers.base import MatchResult
from predbot.providers.chain import ResultsResolver
from predbot.storage.db import Database
from tests.test_live_results import DAY, NOW, Provider, pick, received
from tests.test_subscribers_broadcast import FakeTG, _cb, _snapshot

COMMUNITY = "-1001234567890"


class CommunityTelegram(FakeTG):
    def __init__(self, chat_id=COMMUNITY):
        super().__init__()
        self.chat_id = chat_id
        self.fail = set()
        self.fail_daily = False

    def enabled(self):
        return bool(self.token and self.chat_id)

    def send(self, text, chat_id=None, reply_markup=None, parse_mode="HTML"):
        target = str(chat_id or self.chat_id)
        if target in self.fail or (self.fail_daily and text.startswith("📋")):
            return None
        return super().send(text, target, reply_markup, parse_mode)


@pytest.fixture
def community(db):
    cfg = Config(timezone="UTC", telegram_chat_id=COMMUNITY,
                 subscriber_delivery_enabled=False, live_updates_enabled=True)
    provider = Provider()
    return cfg, CommunityTelegram(), provider, ResultsResolver({"espn": provider})


def publish(db, cfg, tg):
    # Render the real stored collections without fetching or selecting anything.
    snapshot = UnifiedFetcher(None, db, cfg).snapshot_for(DAY)
    service = Mock()
    service.get_daily_snapshot.return_value = snapshot
    assert daily_picks.run(None, db, cfg, tg, service=service) is snapshot
    return snapshot


def poll(db, community, now=NOW):
    cfg, tg, _, resolver = community
    live_results.run(resolver, db, cfg, tg, now=now)


def test_community_picks_and_recap_continue_without_subscriber_broadcasts(db, community, monkeypatch):
    cfg, tg, _, _ = community
    top_id = pick(db)
    pick(db, tier="daily", market="total", selection="over", line=3.5, label="Over 3.5 Total Goals")
    received(db, "42", ("top", "daily"))
    before = [dict(r) for r in db.picks_for_date(DAY)]
    history = [dict(r) for r in db.query("SELECT * FROM subscribers")]
    monkeypatch.setattr(db, "subscribers_active", Mock(side_effect=AssertionError("subscriber enumeration")))
    snapshot = publish(db, cfg, tg)
    assert [s["text"].split(" · ")[0] for s in tg.sends] == ["🔥 TOP PICKS", "📋 DAILY PICKS"]
    assert all(s["chat_id"] == COMMUNITY and s["reply_markup"] is None for s in tg.sends)
    assert [dict(r) for r in db.picks_for_date(DAY)] == before
    assert broadcast.deliver_picks(db, cfg, tg, snapshot, now=NOW) == 0

    db.settle_pick(top_id, "won")
    monkeypatch.setattr(recap, "utcnow", lambda: NOW + timedelta(days=1))
    text = recap.run(db, cfg, tg)
    assert text and tg.sends[-1]["text"] == text
    assert db.post_done("recap", DAY)
    assert broadcast.deliver_recap(db, cfg, tg, text) == 0
    assert len(tg.sends) == 3 and {s["chat_id"] for s in tg.sends} == {COMMUNITY}
    assert [dict(r) for r in db.query("SELECT * FROM subscribers")] == history
    assert not db.query("SELECT 1 FROM subscribers WHERE chat_id=?", (COMMUNITY,))


def test_community_live_lifecycle_repeated_polls_and_restart(db, community, monkeypatch):
    cfg, tg, provider, _ = community
    pick(db)
    pick(db, tier="daily", market="total", selection="over", line=3.5, label="Over 3.5 Total Goals")
    pick(db, tier="all", market="btts", selection="yes", label="Both Teams to Score - Yes")
    received(db, "42", ("top", "daily"))
    publish(db, cfg, tg)
    tg.sends.clear()
    monkeypatch.setattr(db, "subscribers_active", Mock(side_effect=AssertionError("subscriber enumeration")))

    provider.result = MatchResult("in_progress", source="espn")
    poll(db, community)
    poll(db, community)
    path = db.path
    db.close()
    reopened = Database(path)
    try:
        poll(reopened, community)
        assert len(tg.sends) == 1 and "Match Started" in tg.sends[0]["text"]
        assert "Chelsea to Win" in tg.sends[0]["text"] and "Over 3.5" in tg.sends[0]["text"]
        assert "Both Teams to Score" not in tg.sends[0]["text"]
        provider.result = MatchResult("final", {"full": (2., 1.)}, source="espn")
        poll(reopened, community)
        settled = [dict(r) for r in reopened.picks_for_date(DAY)]
    finally:
        reopened.close()
    calls = provider.calls
    reopened = Database(path)
    try:
        poll(reopened, community)
        poll(reopened, community)
        assert provider.calls == calls
        assert [dict(r) for r in reopened.picks_for_date(DAY)] == settled
        assert len(tg.sends) == 2 and all(s["chat_id"] == COMMUNITY for s in tg.sends)
        assert "Match Ended" in tg.sends[-1]["text"]
        assert "— WON" in tg.sends[-1]["text"] and "— LOST" in tg.sends[-1]["text"]
        assert len(reopened.query("SELECT 1 FROM deliveries WHERE kind LIKE 'match_%'")) == 2
        assert not reopened.query("SELECT 1 FROM subscribers WHERE chat_id=?", (COMMUNITY,))
    finally:
        reopened.close()


@pytest.mark.parametrize("status", ["in_progress", "final"])
@pytest.mark.parametrize("publication", ["none", "legacy_posts", "other_destination", "subscriber_only", "all_only"])
def test_community_rejects_unpublished_or_unrelated_predictions(db, community, status, publication):
    cfg, tg, provider, _ = community
    pick(db, tier="all" if publication == "all_only" else "top")
    if publication == "legacy_posts":
        db.mark_post("picks_top", DAY, "old-message")
    elif publication == "other_destination":
        db.mark_delivery("community_picks_top", DAY, "-1009999999999")
    elif publication == "subscriber_only":
        received(db, "42")
    elif publication == "all_only":
        db.mark_delivery("community_picks_top", DAY, COMMUNITY)
    provider.result = MatchResult(status, {"full": (2., 1.)} if status == "final" else {}, source="espn")
    poll(db, community)
    assert tg.sends == []
    assert not db.query("SELECT 1 FROM deliveries WHERE kind LIKE 'match_%' AND chat_id=?", (COMMUNITY,))
    if status == "final" and publication != "all_only":
        assert db.picks_for_date(DAY)[0]["status"] == "won"


def test_partial_publication_only_notifies_for_the_successful_collection(db, community):
    cfg, tg, provider, _ = community
    pick(db)
    pick(db, tier="daily", market="total", selection="over", line=3.5, label="Over 3.5 Total Goals")
    pick(db, tier="daily", home="Liverpool", away="Everton", provider_event_id="other")
    tg.fail_daily = True
    publish(db, cfg, tg)
    assert db.post_done("picks_top", DAY) and not db.post_done("picks_daily", DAY)
    tg.sends.clear()
    provider.result = MatchResult("in_progress", source="espn")
    poll(db, community)
    assert len(tg.sends) == 1
    assert "Chelsea to Win" in tg.sends[0]["text"]
    assert "Over 3.5" not in tg.sends[0]["text"] and "Liverpool" not in tg.sends[0]["text"]


@pytest.mark.parametrize("ended", [False, True])
@pytest.mark.parametrize("raises", [False, True])
def test_failed_live_send_remains_retryable_after_restart(db, community, monkeypatch, ended, raises):
    cfg, tg, provider, _ = community
    pick(db)
    publish(db, cfg, tg)
    tg.sends.clear()
    provider.result = MatchResult("final" if ended else "in_progress",
                                  {"full": (2., 1.)} if ended else {}, source="espn")
    real_send = tg.send
    with monkeypatch.context() as patch:
        patch.setattr(tg, "send", Mock(side_effect=RuntimeError("offline")) if raises else Mock(return_value=None))
        poll(db, community)
    row = db.picks_for_date(DAY)[0]
    kind = broadcast.live_delivery_kind(row["fixture_key"], ended)
    assert not db.delivery_done(kind, DAY, COMMUNITY) and tg.sends == []
    if ended:
        assert row["status"] == "won" and not db.live_state(DAY, row["fixture_key"])["notifications_done"]
    path = db.path
    db.close()
    reopened = Database(path)
    try:
        assert tg.send == real_send
        poll(reopened, community, NOW + timedelta(days=6) if ended else NOW)
        assert len(tg.sends) == 1 and reopened.delivery_done(kind, DAY, COMMUNITY)
        if ended:
            assert provider.calls == 1
            assert reopened.picks_for_date(DAY)[0]["settled_at"] == row["settled_at"]
            assert reopened.live_state(DAY, row["fixture_key"])["notifications_done"]
    finally:
        reopened.close()


def test_subscriber_and_community_completion_are_independent(db, community, monkeypatch):
    cfg, tg, provider, _ = community
    cfg.subscriber_delivery_enabled = True
    monkeypatch.setattr(broadcast.time, "sleep", lambda _: None)
    pick(db)
    received(db, "42")
    publish(db, cfg, tg)
    tg.sends.clear()
    tg.fail.add("42")
    provider.result = MatchResult("final", {"full": (2., 1.)}, source="espn")
    poll(db, community)
    assert [s["chat_id"] for s in tg.sends] == [COMMUNITY]
    row = db.picks_for_date(DAY)[0]
    assert not db.live_state(DAY, row["fixture_key"])["notifications_done"]
    tg.fail.clear()
    poll(db, community)
    assert [s["chat_id"] for s in tg.sends] == [COMMUNITY, "42"]
    assert db.live_state(DAY, row["fixture_key"])["notifications_done"]
    assert provider.calls == 1


def test_changing_destination_does_not_repost_or_attribute_legacy_posts(db, community):
    cfg, tg, provider, _ = community
    pick(db)
    publish(db, cfg, tg)
    tg.chat_id = "-1009999999999"
    cfg.telegram_chat_id = tg.chat_id
    tg.sends.clear()
    publish(db, cfg, tg)
    provider.result = MatchResult("in_progress", source="espn")
    poll(db, community)
    assert tg.sends == []
    assert not db.delivery_done("community_picks_top", DAY, tg.chat_id)


def test_community_receipts_preserve_publication_freeze_and_retry_history(db, community):
    cfg, tg, _, _ = community
    pick(db)
    publish(db, cfg, tg)
    assert db.post_done("picks_top", DAY)
    assert not db.deliveries_exist("picks", DAY), "community receipt must not impersonate subscriber delivery"
    with pytest.raises(ValueError, match="published prediction day"):
        db.reset_prediction_day(DAY, "UTC", "2030-06-01T00:00:00Z")


def test_community_filtered_reversed_leg_preserves_score_orientation(db, community):
    cfg, tg, provider, _ = community
    pick(db)
    pick(db, tier="daily", home="Arsenal", away="Chelsea", selection="away", label="Chelsea to Win",
         provider="oddsapi", provider_event_id="reversed")
    # Legacy Top publication has no destination receipt; only Daily is newly
    # published here. Live lookup uses Top's orientation, delivery uses Daily's.
    db.mark_post("picks_top", DAY, "legacy-top")
    publish(db, cfg, tg)
    tg.sends.clear()
    provider.result = MatchResult("final", {"full": (2., 1.)}, source="espn")
    poll(db, community)
    assert len(tg.sends) == 1 and "Arsenal 1–2 Chelsea" in tg.sends[0]["text"]
    assert "— WON" in tg.sends[0]["text"] and provider.calls == 1


@pytest.mark.parametrize("command", ["/top", "/daily", "/stats", "/yesterday", "/all", "/timezone", "/help"])
def test_private_commands_reply_to_private_chat_with_subscriber_delivery_disabled(db, community, command):
    cfg, tg, _, _ = community
    service = Mock()
    service.get_daily_snapshot.return_value = _snapshot()
    loop = CommandLoop(tg, db, cfg, {}, service=service)
    loop._handle_update({"update_id": 1, "message": {"text": command,
                        "chat": {"id": 42, "type": "private"}, "from": {"id": 42}}})
    assert tg.sends and all(s["chat_id"] == "42" for s in tg.sends)
    if command in ("/top", "/daily", "/stats", "/yesterday", "/all"):
        assert tg.sends[0]["reply_markup"]["inline_keyboard"]


def test_private_start_timezone_and_navigation_keep_existing_behavior(db, community):
    cfg, tg, _, _ = community
    snapshot = _snapshot()
    service = Mock()
    service.get_daily_snapshot.return_value = snapshot
    service.snapshot_for.return_value = snapshot
    loop = CommandLoop(tg, db, cfg, {}, service=service, page_size=1)
    loop.instance = "test"
    for i, text in enumerate(("/start", "/timezone Asia/Tokyo", "/daily")):
        loop._handle_update({"update_id": i, "message": {"text": text,
                            "chat": {"id": 42, "type": "private"}, "from": {"id": 42}}})
    assert db.subscriber_timezone("42") == "Asia/Tokyo"
    assert db.subscribers_active()[0]["chat_id"] == "42"
    loop._handle_update({"callback_query": _cb("pg|daily|1|20300601")})
    assert tg.edits[-1]["chat_id"] == "42" and tg.edits[-1]["message_id"] == 55
    assert tg.edits[-1]["reply_markup"]["inline_keyboard"] and "Page 2 of 3" in tg.edits[-1]["text"]
    assert all(s["chat_id"] == "42" for s in tg.sends)


@pytest.mark.parametrize("chat_type", ["group", "supergroup", "channel", None])
@pytest.mark.parametrize("command", ["/start", "/timezone Asia/Tokyo"])
def test_community_commands_cannot_create_or_change_subscriber_records(db, community, chat_type, command):
    cfg, tg, _, _ = community
    loop = CommandLoop(tg, db, cfg, {})
    loop.instance = "test"
    chat = {"id": int(COMMUNITY)}
    if chat_type:
        chat["type"] = chat_type
    update = {"update_id": 1, "message": {"text": command, "chat": chat, "from": {"id": 42}}}
    loop._handle_update(update)
    assert not db.query("SELECT 1 FROM subscribers WHERE chat_id=?", (COMMUNITY,))
    # Existing group records are retained, but cannot be reactivated or have their timezone overwritten.
    db.subscriber_add(COMMUNITY, timezone="UTC")
    db.subscriber_remove(COMMUNITY)
    before = [dict(r) for r in db.query("SELECT * FROM subscribers")]
    loop._handle_update(update)
    assert [dict(r) for r in db.query("SELECT * FROM subscribers")] == before
