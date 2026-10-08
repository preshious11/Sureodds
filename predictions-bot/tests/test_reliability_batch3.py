"""Batch 3 - reliability and Telegram resilience.

Delivery runs that cannot overlap and record each receipt as the message is accepted (H5); Telegram rate limits that
never freeze the polling thread, with per-chat / per-user / group / global limits (H6); user views that never
generate (M4); SharpAPI 429 that stops the walk (M5); blocked users deactivated (M9); bounded shutdown and job
overlap protection (M10); recap computed once for its explicit date (M11); Telegram edit/length/getUpdates/migration
robustness; stored update progress; Custom Picks CPU protection (CP1).

Concurrency is synchronised with events, never with sleeps; no test reaches a network."""
from __future__ import annotations

import contextlib
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from predbot.bot import broadcast, callbacks, commands, pagination
from predbot.bot.commands import CommandLoop
from predbot.bot.telegram import MAX_LEN, PollingError, Telegram, _split, utf16_len
from predbot.cache.fetcher import UnifiedFetcher
from predbot.cache.models import DailySnapshot
from predbot.config import Config
from predbot.jobs import daily_picks, scheduler
from predbot.util import lifecycle
from tests.test_subscribers_broadcast import FakeTG

DAY = "2026-09-19"
NOW = datetime(2026, 9, 19, 8, 31, tzinfo=timezone.utc)          # 09:31 Lagos, after DELIVERY_TIME


# ============================================================================== H5 delivery
class GateTG:
    """Records every send; the first send blocks until ``release`` so a second run can be started meanwhile."""
    token = "t"

    def __init__(self, block_first=False, fail_after=None, raise_after=None):
        self.sent, self.lock = [], threading.Lock()
        self.entered, self.release = threading.Event(), threading.Event()
        self.block_first, self.fail_after, self.raise_after = block_first, fail_after, raise_after
        self.hook = None

    def send(self, text, chat_id=None, **kw):
        with self.lock:
            n = len(self.sent)
            if self.raise_after is not None and n >= self.raise_after:
                raise KeyboardInterrupt("process killed mid-run")
            first = n == 0
            self.sent.append((str(chat_id), text))
        if first and self.block_first:
            self.entered.set()
            assert self.release.wait(10)
        if self.hook:
            self.hook(str(chat_id), text)
        return "1"


@pytest.fixture
def cards(monkeypatch):
    snap = DailySnapshot(date=DAY, generated_at=f"{DAY}T08:00:00Z", top_picks=[object()], daily_picks=[])
    monkeypatch.setattr(broadcast, "picks_messages", lambda s, tz, now=None: [
        ("picks_top", "top", None), ("picks_daily", "daily", None)])
    monkeypatch.setattr(broadcast, "SENDS_PER_SECOND", 1e9)
    return snap


def subscribe(db, n):
    for i in range(n):
        db.subscriber_add(str(100 + i))
    db.conn.execute("UPDATE subscribers SET subscribed_at='2026-09-01T00:00:00Z'")
    db.conn.commit()


@pytest.mark.parametrize("n", [3, 20])                    # sequential path and the paced pool
def test_two_concurrent_picks_deliveries_send_each_message_exactly_once(db, cards, n):
    subscribe(db, n)
    tg = GateTG(block_first=True)
    first = threading.Thread(target=broadcast.deliver_picks, args=(db, Config(), tg, cards), kwargs={"now": NOW})
    first.start()
    assert tg.entered.wait(10)                            # run 1 is inside a send
    assert broadcast.deliver_picks(db, Config(), tg, cards, now=NOW) == 0     # run 2 does not start a second pass
    tg.release.set()
    first.join(10)
    assert sorted(tg.sent) == sorted((str(100 + i), t) for i in range(n) for t in ("top", "daily"))
    assert len(db.deliveries_done("picks_top", DAY)) == len(db.deliveries_done("picks_daily", DAY)) == n


def test_a_run_killed_mid_way_keeps_receipts_for_every_accepted_message_and_resumes(db, cards):
    subscribe(db, 4)
    with pytest.raises(KeyboardInterrupt):
        broadcast.deliver_picks(db, Config(), GateTG(raise_after=3), cards, now=NOW)
    done = {k: db.deliveries_done(k, DAY) for k in ("picks_top", "picks_daily")}
    assert done == {"picks_top": {"100", "101"}, "picks_daily": {"100"}}     # 3 accepted -> 3 receipts
    resumed = GateTG()
    broadcast.deliver_picks(db, Config(), resumed, cards, now=NOW)
    assert sorted(resumed.sent) == sorted([("101", "daily"), ("102", "top"), ("102", "daily"),
                                           ("103", "top"), ("103", "daily")])


def test_a_receipt_written_meanwhile_is_rechecked_before_sending(db, cards):
    subscribe(db, 3)
    tg = GateTG()
    # while chat 100 is being served, "another run" (a previous leader's late receipt) records chat 102's top card
    tg.hook = lambda chat, text: chat == "100" and text == "top" and db.mark_delivery("picks_top", DAY, "102")
    broadcast.deliver_picks(db, Config(), tg, cards, now=NOW)
    assert ("102", "top") not in tg.sent and ("102", "daily") in tg.sent


def test_a_shutdown_request_stops_delivery_between_chats_and_the_rest_is_sent_later(db, cards):
    subscribe(db, 4)
    tg = GateTG()
    tg.hook = lambda chat, text: text == "daily" and chat == "101" and lifecycle.request_shutdown()
    broadcast.deliver_picks(db, Config(), tg, cards, now=NOW)
    assert {c for c, _ in tg.sent} == {"100", "101"}
    lifecycle.reset()
    later = GateTG()
    broadcast.deliver_picks(db, Config(), later, cards, now=NOW)
    assert {c for c, _ in later.sent} == {"102", "103"}


# ============================================================================== M11 recap
def _recap_world(db, monkeypatch, now_iso):
    from tests.test_subscribers_broadcast import _seed_yesterday_fixture
    for chat, zone in (("1", "Africa/Lagos"), ("2", "Asia/Tokyo"), ("3", "America/New_York")):
        db.subscriber_add(chat, timezone=zone)
    _seed_yesterday_fixture(db, "2030-05-31", "2030-05-31T15:00:00Z")
    frozen = datetime.fromisoformat(now_iso).replace(tzinfo=timezone.utc)
    monkeypatch.setattr(broadcast, "utcnow", lambda: frozen)
    from predbot.jobs import stats
    seen = []
    real = stats.compute
    monkeypatch.setattr(stats, "compute", lambda db_, today: seen.append(today) or real(db_, today))
    return seen


def test_the_recap_is_computed_once_for_its_explicit_date_whatever_each_subscribers_zone(db, monkeypatch):
    # 16:00 UTC on 1 June = 17:00 Lagos (1 June), 01:00 Tokyo (2 June!), 12:00 New York (1 June)
    seen = _recap_world(db, monkeypatch, "2030-06-01T16:00:00")
    tg = FakeTG()
    assert broadcast.deliver_recap(db, Config(timezone="Africa/Lagos"), tg, "x") == 3
    assert len(seen) == 1                                       # once per run, not once per subscriber
    assert seen[0].date().isoformat() == "2030-06-01"           # i.e. the recap of 31 May for every reader
    assert len({s["text"] for s in tg.sends}) == 1               # Tokyo gets the same card as Lagos
    assert {d for d in ("2030-05-31",) if db.deliveries_done("recap", d) == {"1", "2", "3"}} == {"2030-05-31"}


def test_overlapping_recap_runs_do_not_double_send(db, monkeypatch):
    _recap_world(db, monkeypatch, "2030-06-01T16:00:00")
    lock = broadcast.run_lock(db, "recap")
    lock.acquire()                                              # a run is in progress
    try:
        tg = FakeTG()
        assert broadcast.deliver_recap(db, Config(timezone="Africa/Lagos"), tg, "x") == 0 and tg.sends == []
    finally:
        lock.release()
    assert broadcast.deliver_recap(db, Config(timezone="Africa/Lagos"), FakeTG(), "x") == 3


def test_the_recap_still_waits_for_the_last_fixture(db, monkeypatch):
    _recap_world(db, monkeypatch, "2030-05-31T18:00:00")          # last kick-off 15:00 + 4 h not reached
    assert broadcast.deliver_recap(db, Config(timezone="Africa/Lagos"), FakeTG(), "x") == 0


# ============================================================================== Telegram client
def response(status=200, payload=None, text="ok"):
    r = Mock(status_code=status, text=text)
    r.json.return_value = payload if payload is not None else {"ok": True, "result": {"message_id": 1}}
    return r


def client(*results, chat="-1001234567890"):
    tg = Telegram("test-token", chat)
    tg.session = Mock()
    tg.session.post.side_effect = list(results)
    return tg


@pytest.fixture
def sleeps(monkeypatch):
    slept, paused = [], []
    monkeypatch.setattr("predbot.bot.telegram.time.sleep", slept.append)
    monkeypatch.setattr("predbot.bot.telegram._PAUSE", SimpleNamespace(wait=paused.append))
    return SimpleNamespace(slept=slept, paused=paused)


def test_an_interactive_429_fails_fast_and_cools_only_that_chat(sleeps):
    tg = client(response(429, {"parameters": {"retry_after": 30}}), response())
    with tg.interactive():
        assert tg.send("hi", chat_id="42") is None
        assert tg.last_failure("42") == "rate_limited"
        assert tg.send("again", chat_id="42") is None            # cooling down: not even attempted
        assert tg.session.post.call_count == 1
        assert tg.send("other user", chat_id="43") == "1"       # nobody else waits
    assert sleeps.slept == [] and all(p < 1 for p in sleeps.paused)


def test_a_scheduled_send_waits_a_bounded_time_and_never_a_long_one(sleeps):
    tg = client(response(429, {"parameters": {"retry_after": 120}}))
    assert tg.send("card", chat_id="42") is None and sleeps.slept == []       # > SCHEDULED_MAX_WAIT: next run retries
    tg2 = client(response(429, {"parameters": {"retry_after": 3}}), response())
    assert tg2.send("card", chat_id="42") == "1" and sleeps.slept == [3]


def test_a_scheduled_send_waits_out_an_existing_short_cooldown(sleeps):
    tg = client(response())
    tg._cool("42", 5)
    waited = []
    # the pause really lets the cool-down pass
    import predbot.bot.telegram as telegram_module
    telegram_module._PAUSE = SimpleNamespace(wait=lambda s: (waited.append(s), tg._cooldown.clear()))
    assert tg.send("card", chat_id="42") == "1"
    assert any(4 < p <= 5 for p in waited) and sleeps.slept == []


def test_a_group_gets_at_most_twenty_messages_a_minute_from_taps(sleeps):
    tg = client(*[response() for _ in range(25)])
    with tg.interactive():
        results = [tg.send(f"m{i}", chat_id="-100777") for i in range(21)]
    assert results[:20] == ["1"] * 20 and results[20] is None
    assert tg.session.post.call_count == 20 and tg.rate_limited("-100777")


def test_every_send_is_paced_globally(sleeps):
    tg = client(*[response() for _ in range(5)])
    for i in range(5):
        tg.send("m", chat_id=str(i + 1))
    assert len([p for p in sleeps.paused if p > 0]) >= 3          # slots 40 ms apart


def test_edit_429_fails_fast_interactively_and_long_text_becomes_new_messages(sleeps):
    tg = client(response(429, {"parameters": {"retry_after": 20}}))
    with tg.interactive():
        assert tg.edit_message_text("42", 7, "page 2") is False
    assert sleeps.slept == [] and tg.rate_limited("42")
    long_text = "\n\n".join("😀" * 1500 for _ in range(3))        # 9,000 UTF-16 units
    tg = client(response(), response(), response())
    assert tg.edit_message_text("43", 7, long_text) is True
    assert all(c.kwargs["json"].get("message_id") is None for c in tg.session.post.call_args_list)
    assert tg.session.post.call_count == 3


def test_message_length_is_measured_in_utf16_units():
    text = "\n\n".join("😀" * 1500 for _ in range(3))
    assert len(text) < MAX_LEN * 2 and utf16_len(text) > MAX_LEN * 2
    chunks = _split(text)
    assert len(chunks) == 3 and all(utf16_len(c) <= MAX_LEN for c in chunks)
    one_block = "😀" * 2500                                      # a single block longer than a message
    assert all(utf16_len(c) <= MAX_LEN for c in _split(one_block)) and "".join(_split(one_block)) == one_block


@pytest.mark.parametrize("make,retry_in", [
    (lambda: Mock(status_code=200, json=Mock(side_effect=ValueError("<html>"))), 5),
    (lambda: Mock(status_code=429, json=Mock(return_value={"parameters": {"retry_after": 9}})), 9),
    (lambda: Mock(status_code=502, json=Mock(return_value={})), 5),
    (lambda: Mock(status_code=200, json=Mock(return_value={"ok": True, "result": "nope"})), 5),
])
def test_get_updates_failures_are_short_pauses_not_a_dead_poller(make, retry_in):
    tg = Telegram("t", "")
    tg.session = Mock()
    tg.session.get.return_value = make()
    with pytest.raises(PollingError) as e:
        tg.get_updates(None)
    assert e.value.retry_in == retry_in


def test_the_poller_waits_only_what_the_failure_asks(db):
    loop = CommandLoop(FakeTG(), db, Config(), {})
    calls = []

    def get_updates(offset, timeout=20):
        calls.append(offset)
        raise PollingError("non-JSON", retry_in=5)
    loop.tg.get_updates = get_updates
    loop._stop_event = Mock()
    loop._stop_event.is_set.side_effect = [False, False, False, False, True]
    loop._poll()
    assert all(c.args == (5,) for c in loop._stop_event.wait.call_args_list)


def test_a_group_migration_is_followed_once(sleeps):
    migrated = response(400, {"ok": False, "description": "Bad Request: group chat was upgraded to a supergroup chat",
                              "parameters": {"migrate_to_chat_id": -1009999}})
    tg = client(migrated, response(), chat="-555")
    assert tg.send("card") == "1"
    assert [c.kwargs["json"]["chat_id"] for c in tg.session.post.call_args_list] == ["-555", "-1009999"]
    assert tg.chat_id == "-1009999"


@pytest.mark.parametrize("status,description,permanent", [
    (403, "Forbidden: bot was blocked by the user", True),
    (403, "Forbidden: user is deactivated", True),
    (400, "Bad Request: chat not found", True),
    (400, "Bad Request: message text is empty", False),
    (500, "Internal Server Error", False),
    (429, "Too Many Requests", False),
])
def test_only_definitive_refusals_are_reported_as_permanent(sleeps, status, description, permanent):
    tg = client(response(status, {"ok": False, "description": description}))
    seen = []
    tg.on_permanent_failure = lambda chat, reason: seen.append(chat)
    assert tg.send("x", chat_id="42") is None
    assert (seen == ["42"]) is permanent


def test_a_transport_failure_is_never_permanent(sleeps):
    tg = client(requests.Timeout("slow"))
    seen = []
    tg.on_permanent_failure = lambda chat, reason: seen.append(chat)
    assert tg.send("x", chat_id="42") is None and seen == [] and tg.last_failure("42") == "transport"


def test_answer_callback_query_never_raises():
    tg = client(requests.ConnectionError("down"))
    assert tg.answer_callback_query("cb", text="x") is False


# ============================================================================== M9 blocked users
def test_a_blocked_subscriber_is_deactivated_and_not_retried_every_poll(db, cards, sleeps):
    subscribe(db, 2)                                              # 100 blocked the bot, 101 is fine
    tg = Telegram("t", "")
    tg.session = Mock()

    def post(url, json=None, timeout=None):
        if json["chat_id"] == "100":
            return response(403, {"ok": False, "description": "Forbidden: bot was blocked by the user"})
        return response()
    tg.session.post.side_effect = post
    tg.on_permanent_failure = broadcast.deactivate_unreachable(db)
    broadcast.deliver_picks(db, Config(), tg, cards, now=NOW)
    assert [r["chat_id"] for r in db.subscribers_active()] == ["101"]
    calls = tg.session.post.call_count
    broadcast.deliver_picks(db, Config(), tg, cards, now=NOW)     # the next poll: nobody left to try
    assert tg.session.post.call_count == calls
    db.subscriber_add("100")                                      # /start again re-subscribes
    assert "100" in [r["chat_id"] for r in db.subscribers_active()]


def test_a_temporary_failure_never_unsubscribes(db, cards, sleeps):
    subscribe(db, 1)
    tg = Telegram("t", "")
    tg.session = Mock()
    tg.session.post.side_effect = [response(502, {"ok": False, "description": "Bad Gateway"}),
                                   requests.Timeout("slow")]
    tg.on_permanent_failure = broadcast.deactivate_unreachable(db)
    broadcast.deliver_picks(db, Config(), tg, cards, now=NOW)
    broadcast.deliver_picks(db, Config(), tg, cards, now=NOW)
    assert [r["chat_id"] for r in db.subscribers_active()] == ["100"]


def test_a_refusing_group_never_touches_subscribers(db):
    db.subscriber_add("100")
    broadcast.deactivate_unreachable(db)("-1001234", "403: bot was kicked")
    assert [r["chat_id"] for r in db.subscribers_active()] == ["100"]


# ============================================================================== H6 polling thread
class LimitedTG(FakeTG):
    """A FakeTG whose sends to one chat are rate limited, like the real client after a 429."""

    def __init__(self, limited=()):
        super().__init__()
        self.limited = set(limited)
        self.interactive_calls = 0

    @contextlib.contextmanager
    def interactive(self):
        self.interactive_calls += 1
        yield self

    def send(self, text, chat_id=None, reply_markup=None, parse_mode="HTML"):
        if str(chat_id) in self.limited:
            return None
        return super().send(text, chat_id, reply_markup, parse_mode)

    def edit_message_text(self, chat_id, message_id, text, reply_markup=None, parse_mode="HTML"):
        if str(chat_id) in self.limited:
            return False
        return super().edit_message_text(chat_id, message_id, text, reply_markup, parse_mode)

    def last_failure(self, chat_id):
        return "rate_limited" if str(chat_id) in self.limited else None

    def rate_limited(self, chat_id):
        return str(chat_id) in self.limited


def test_retry_send_does_not_retry_a_rate_limited_chat():
    tg = LimitedTG(limited={"42"})
    tg.send = Mock(side_effect=tg.send)
    assert commands._retry_send(tg, "hello", "42") is False
    assert tg.send.call_count == 1


def test_a_rate_limited_view_answers_busy_instead_of_failing(db, monkeypatch):
    service = SimpleNamespace(get_daily_snapshot=lambda **k: DailySnapshot(date=DAY, generated_at=f"{DAY}T08:00:00Z"))
    tg = LimitedTG(limited={"42"})
    cb = {"id": "cb1", "data": "pg|stats|0", "message": {"message_id": 3, "chat": {"id": 42, "type": "private"}}}
    callbacks.handle_callback(cb, service=service, tg=tg, cfg=Config(view_navigation="new"), db=db)
    assert tg.answers[-1]["text"].startswith("Busy")


def updates_from(user, n, chat=None, admin=False):
    chat = chat or user
    return [{"update_id": i, "message": {"text": "/help", "chat": {"id": int(chat), "type": "private"},
                                         "from": {"id": int(user)}}} for i in range(n)]


def test_one_user_cannot_flood_the_bot(db):
    loop = CommandLoop(FakeTG(), db, Config(), {})
    loop.handle = Mock(return_value="ok")
    for upd in updates_from("7", commands.USER_LIMIT + 5):
        loop._handle_update(upd)
    assert loop.handle.call_count == commands.USER_LIMIT
    notices = [s for s in loop.tg.sends if s["text"] == commands.SLOW_DOWN]
    assert len(notices) == 1                                            # told once, not per dropped update
    for upd in updates_from("8", 3):
        loop._handle_update(upd)                                        # other users are unaffected
    assert loop.handle.call_count == commands.USER_LIMIT + 3


def test_admins_are_not_interaction_limited(db):
    loop = CommandLoop(FakeTG(), db, Config(telegram_admin_ids=["7"]), {})
    loop.handle = Mock(return_value="ok")
    for upd in updates_from("7", commands.USER_LIMIT + 5):
        loop._handle_update(upd)
    assert loop.handle.call_count == commands.USER_LIMIT + 5


def test_tapping_too_fast_gets_a_toast_not_a_message(db):
    loop = CommandLoop(FakeTG(), db, Config(), {}, service=Mock())
    loop._dispatch_callback = Mock()
    tap = lambda i: {"update_id": i, "callback_query": {"id": f"cb{i}", "data": "pg|top|0", "from": {"id": 7},
                                                         "message": {"chat": {"id": 7, "type": "private"}}}}
    for i in range(commands.USER_LIMIT + 2):
        loop._handle_update(tap(i))
    assert loop._dispatch_callback.call_count == commands.USER_LIMIT
    assert loop.tg.sends == [] and loop.tg.answers[0]["text"] == commands.TAP_SLOW_DOWN


def test_each_update_is_handled_as_interactive(db):
    tg = LimitedTG()
    loop = CommandLoop(tg, db, Config(), {})
    seen = []
    loop._handle_update = lambda upd: seen.append(upd["update_id"])
    batches = iter([[{"update_id": 5}, {"update_id": 6}]])

    def get_updates(offset, timeout=20):
        try:
            return next(batches)
        except StopIteration:
            loop.stop()
            return []
    tg.get_updates = get_updates
    loop._poll()
    assert seen == [5, 6] and tg.interactive_calls == 2


# ============================================================================== community anti-flood
COMMUNITY = "-1001234567890"


def group_tap(data, i=1, user=40):
    return {"id": f"cb{i}", "data": data, "from": {"id": user + i},
            "message": {"message_id": 99, "text": callbacks.COMMUNITY_MENU_TEXT,
                        "chat": {"id": int(COMMUNITY), "type": "supergroup"}}}


@pytest.fixture
def group_loop(db, monkeypatch):
    loop = CommandLoop(FakeTG(), db, Config(telegram_chat_id=COMMUNITY), {}, service=Mock())
    posted = []
    monkeypatch.setattr(commands, "handle_callback", lambda cb, **kw: posted.append(cb["data"]))
    loop.posted = posted
    return loop


def test_the_same_public_view_is_not_reposted_into_the_group_by_repeated_taps(group_loop):
    for i in range(5):
        group_loop._dispatch_callback(group_tap("pg|top|0", i))
    assert group_loop.posted == ["pg|top|0"]
    assert [a["text"] for a in group_loop.tg.answers] == [commands.GROUP_REPEAT] * 4
    group_loop._dispatch_callback(group_tap("pg|daily|0", 9))          # a different view still opens
    assert group_loop.posted == ["pg|top|0", "pg|daily|0"]


def test_a_group_gets_a_bounded_number_of_view_posts_per_minute(group_loop, monkeypatch):
    views = [f"pg|{v}|0" for v in ("top", "daily", "all", "stats", "yesterday")]
    monkeypatch.setattr(commands, "GROUP_REPEAT_SECONDS", 0.0)
    group_loop._group_repeat = commands.Cooldown(0.0)
    for i in range(commands.GROUP_POSTS_PER_MINUTE + 4):
        group_loop._dispatch_callback(group_tap(views[i % len(views)], i))
    assert len(group_loop.posted) == commands.GROUP_POSTS_PER_MINUTE
    assert group_loop.tg.answers[-1]["text"] == commands.GROUP_BUSY


def test_page_turns_and_private_chats_are_not_group_limited(group_loop):
    private = {"id": "p", "data": "pg|top|0", "from": {"id": 5}, "message": {"chat": {"id": 5, "type": "private"}}}
    for _ in range(3):
        group_loop._dispatch_callback(private)
        group_loop._dispatch_callback(group_tap("pg|daily|1|20260919"))
    assert group_loop.posted.count("pg|top|0") == 3 and group_loop.posted.count("pg|daily|1|20260919") == 3


# ============================================================================== M4 user views never generate
@pytest.fixture
def cold(db, monkeypatch):
    chain = Mock()
    chain.collect.side_effect = AssertionError("a user view reached a provider")
    built = Mock(side_effect=AssertionError("a user view ran build_picks"))
    monkeypatch.setattr(daily_picks, "build_picks", built)
    clock = {"now": datetime(2030, 6, 1, 12, 0, tzinfo=timezone.utc)}
    monkeypatch.setattr("predbot.cache.fetcher.utcnow", lambda: clock["now"])
    cfg = Config(timezone="UTC", post_time="09:00")
    return UnifiedFetcher(chain, db, cfg), cfg, built, clock


VIEWS = ["/top", "/daily", "/all", "/today"]
TAPS = ["pg|top|0", "pg|daily|0", "pg|all|0", "pg|all_football|0", "pg|top|1", "pg|all|2", "pg|stats|0",
        "pg|yesterday|0", "pg|daily|1|20300601"]


def test_no_user_view_or_tap_generates_on_a_cold_cache(db, cold):
    service, cfg, built, _ = cold
    tg = FakeTG()
    for cmd in VIEWS:
        callbacks.handle_command(cmd, "42", service=service, tg=tg, cfg=cfg, db=db)
    for data in TAPS:
        callbacks.handle_callback({"id": "c", "data": data, "message": {"message_id": 1, "chat": {"id": 42}}},
                                  service=service, tg=tg, cfg=cfg, db=db)
    built.assert_not_called()
    service.chain.collect.assert_not_called()
    assert service.api_batches == 0
    assert any("not been generated yet" in s["text"] for s in tg.sends)
    assert not any("Something went wrong" in s["text"] for s in tg.sends)


def test_before_post_time_a_cold_view_still_says_when_picks_are_ready(db, cold):
    service, cfg, built, clock = cold
    clock["now"] = datetime(2030, 6, 1, 7, 0, tzinfo=timezone.utc)
    tg = FakeTG()
    callbacks.handle_command("/top", "42", service=service, tg=tg, cfg=cfg, db=db)
    assert "ready at 09:00" in tg.sends[0]["text"]
    built.assert_not_called()


def test_scheduled_generation_is_still_allowed(db, cold, monkeypatch):
    service, cfg, built, _ = cold
    built.side_effect = None
    built.return_value = Mock(diagnostics={}, complete=True)
    monkeypatch.setattr(service, "_snapshot_from_pickset", Mock(side_effect=RuntimeError("stop here")))
    service.get_daily_snapshot(scheduled=True)
    built.assert_called_once()


# ============================================================================== M5 SharpAPI 429
def test_sharpapi_429_stops_the_walk_keeps_data_and_cools_down(db, monkeypatch):
    from predbot.providers.budget import Budget
    from predbot.providers.sharpapi import SharpApiProvider
    from predbot.util.http import ProviderError
    from tests.test_sharpapi_volume import START, page, row
    monkeypatch.setattr("predbot.util.http.time.sleep", Mock(side_effect=AssertionError("slept on a 429")))
    calls = []

    class Http:
        def get(self, path, params=None):
            calls.append(params["league"])
            if len(calls) == 2:
                raise ProviderError("429 from /odds: rate limited", status=429, retry_after=30)
            return page([row("e1", START + timedelta(hours=3))], more=False)

    p = SharpApiProvider("k", Budget(db), leagues=["l1", "l2", "l3", "l4", "l5"], min_interval=0)
    p.http = Http()
    events = p.fetch_events(START, START + timedelta(hours=12))
    assert calls == ["l1", "l2"]                                       # no request per remaining league
    assert events and not p.last_fetch_complete                        # league 1's data is kept
    assert [p.fetch_counts[l]["stop_reason"] for l in ("l3", "l4", "l5")] == ["rate_limited"] * 3
    assert p.fetch_events(START, START + timedelta(hours=12)) == [] and calls == ["l1", "l2"]  # cooling down
    db.set_kv(SharpApiProvider.COOLDOWN_KEY, "0")
    p._cooldown_until = 0.0
    p.fetch_events(START, START + timedelta(hours=12))
    assert len(calls) > 2                                              # a later run asks again


def test_sharpapi_never_sleeps_through_a_429_in_the_http_layer():
    from predbot.providers.sharpapi import SharpApiProvider
    p = SharpApiProvider("k", SimpleNamespace(db=None), leagues=["l1"])
    assert p.http.sleep_on_429 is False and p.http.retries == 0


# ============================================================================== M10 shutdown and overlap
def test_an_interval_job_never_overlaps_itself(monkeypatch):
    release, started = threading.Event(), []

    def body():
        started.append(1)
        release.wait(10)
    job = scheduler.IntervalJob("deliver", 5, body)
    now = datetime.now(timezone.utc)
    job.run(now)
    job.run(now + timedelta(minutes=5))                                  # previous run still going
    assert job.running()
    release.set()
    job.thread.join(5)
    assert started == [1]
    job.run(now + timedelta(minutes=10))
    job.thread.join(5)
    assert started == [1, 1]


def test_a_daily_or_deadline_job_that_is_still_running_is_retried_not_lost():
    release = threading.Event()
    job = scheduler.DailyJob("picks", "09:00", "UTC", lambda: release.wait(10))
    now = datetime(2030, 6, 1, 10, 0, tzinfo=timezone.utc)
    job.thread = threading.Thread(target=release.wait, args=(10,))
    job.thread.start()
    job.run(now)
    assert job.last_run_date is None and job.due(now)                  # not marked: runs once the other ends
    release.set()
    job.thread.join(5)
    dyn = scheduler.DynamicJob("settle", "UTC", lambda n: now, lambda: None)
    dyn.thread = threading.Thread(target=threading.Event().wait, args=(0.5,))
    dyn.thread.start()
    assert dyn.due(now)
    dyn.run(now)
    assert dyn.last_run_deadline is None and dyn.next == now           # the deadline is kept
    dyn.thread.join(5)


def test_drain_waits_a_bounded_time_and_reports_what_is_left():
    release = threading.Event()
    job = scheduler.IntervalJob("deliver", 5, lambda: release.wait(10))
    job.run(datetime.now(timezone.utc))
    assert scheduler.drain([job], 0.05) == ["deliver"]
    release.set()
    assert scheduler.drain([job], 5) == []


def test_the_scheduler_loop_returns_promptly_when_stopped(monkeypatch):
    monkeypatch.setattr(scheduler.signal, "signal", lambda *a: None)
    stop = threading.Event()
    t = threading.Thread(target=scheduler.loop, args=([],), kwargs={"poll_seconds": 30, "stop_event": stop})
    t.start()
    stop.set()
    t.join(5)
    assert not t.is_alive()


def test_the_worker_shuts_down_in_order_and_releases_the_lease(db, monkeypatch):
    import main
    from tests.test_security_hardening import _patch_main
    _patch_main(monkeypatch, db, Config(enable_commands=False))
    order = []
    monkeypatch.setattr(main, "loop", lambda jobs, **kw: order.append("loop returned"))
    monkeypatch.setattr(main, "drain", lambda jobs, timeout: order.append(
        ("drain", timeout, lifecycle.shutting_down())) or [])
    main.main()
    assert order == ["loop returned", ("drain", main.SHUTDOWN_DRAIN_SECONDS, True)]
    assert not db.query("SELECT 1 FROM leader_lease")
    assert main.SHUTDOWN_DRAIN_SECONDS + main.POLLER_JOIN_SECONDS < 10      # inside Docker's default grace


def test_a_new_worker_starts_running_again():
    lifecycle.request_shutdown()
    lifecycle.reset()
    assert not lifecycle.shutting_down()


# ============================================================================== update progress
def test_update_progress_is_stored_and_a_new_leader_resumes_after_it(db):
    tg = LimitedTG()
    loop = CommandLoop(tg, db, Config(), {})
    batches = iter([[{"update_id": 10}, {"update_id": 11}]])

    def get_updates(offset, timeout=20):
        try:
            return next(batches)
        except StopIteration:
            loop.stop()
            return []
    tg.get_updates = get_updates
    loop._handle_update = lambda upd: None
    loop._poll()
    successor = CommandLoop(LimitedTG(), db, Config(), {})
    assert successor._stored_offset() == 12                           # 10 and 11 are never replayed


def test_an_update_that_did_not_finish_is_delivered_again(db):
    tg = LimitedTG()
    loop = CommandLoop(tg, db, Config(), {})
    tg.get_updates = lambda offset, timeout=20: [{"update_id": 20}, {"update_id": 21}]

    def handle(upd):
        if upd["update_id"] == 21:
            raise KeyboardInterrupt("killed while handling 21")
    loop._handle_update = handle
    with pytest.raises(KeyboardInterrupt):
        loop._poll()
    assert CommandLoop(LimitedTG(), db, Config(), {})._stored_offset() == 21     # 21 is replayed, 20 is not


def test_the_stored_offset_is_per_bot_token(db):
    a, b = LimitedTG(), LimitedTG()
    b.token = "another-token"
    CommandLoop(a, db, Config(), {})._store_offset(99)
    assert CommandLoop(b, db, Config(), {})._stored_offset() is None
    assert "test-token" not in commands.offset_key("test-token")


# ============================================================================== CP1 Custom Picks
def test_a_search_is_charged_to_its_budget_and_capped_by_it(db):
    from predbot import custom_picks as cp
    from predbot.custom_recovery import Recovery, eligible
    from tests.test_custom_picks import NOW, ZONE, rows_for, store
    store(db, rows_for(1) + rows_for(2) + rows_for(3))
    pool = Recovery(db, Config(timezone=ZONE)).load(NOW)
    request = cp.parse_request("3 picks around 4 odds", set(cp.SPORT_LABEL))
    matches = eligible(pool.candidates, request, NOW, ZONE)
    unbounded = cp.search(matches, request)
    budget = cp.WorkBudget()
    assert cp.search(matches, request, budget) == unbounded                     # same answer with a budget
    used = cp.REQUEST_WORK_LIMIT - budget.remaining
    assert 0 < used <= cp.MAX_EXPANSIONS
    tiny = cp.WorkBudget(1)
    cp.search(matches, request, tiny)
    assert tiny.remaining <= 0                                                   # it stops at its cap
    assert cp.search(matches, request, cp.WorkBudget(0)) is None                # nothing left: no work at all


def test_recovery_stops_when_the_request_budget_is_spent(db, monkeypatch):
    from predbot import custom_picks as cp
    from predbot.custom_recovery import Recovery
    from tests.test_custom_picks import NOW, ZONE, rows_for, store
    store(db, rows_for(1) + rows_for(2) + rows_for(3))
    rec = Recovery(db, Config(timezone=ZONE))
    searched = []
    real = cp.search

    def spy(candidates, request, budget=None):
        searched.append(budget)
        result = real(candidates, request, budget)
        if budget is not None:
            budget.remaining = 0                                       # pretend each search is the worst case
        return result
    monkeypatch.setattr(cp, "search", spy)
    rec.respond("100 odds", "42", "42", NOW)
    assert len(searched) <= 2                                          # primary + at most one recovery search
    assert all(b is searched[0] for b in searched)                     # one shared budget per request
    assert rec.last_work == cp.REQUEST_WORK_LIMIT


def _custom_loop(db, monkeypatch, executor=True):
    from tests.test_custom_picks import NOW, rows_for, store
    store(db, rows_for(1) + rows_for(2) + rows_for(3))
    tg = FakeTG()
    loop = CommandLoop(tg, db, Config(timezone="UTC"), {}, service=Mock())
    loop._custom_users.add(("42", "42"))
    if executor:
        loop._custom_executor = ThreadPoolExecutor(max_workers=1)
    return loop


def msg(text, user="42", i=1):
    return {"update_id": i, "message": {"text": text, "chat": {"id": int(user), "type": "private"},
                                        "from": {"id": int(user)}}}


def test_a_custom_picks_search_never_blocks_the_polling_thread(db, monkeypatch):
    loop = _custom_loop(db, monkeypatch)
    entered, release = threading.Event(), threading.Event()

    def slow(text, user_id, chat_id):
        entered.set()
        release.wait(10)
        return SimpleNamespace(text="🔥 done", reply_markup=None)
    loop._custom_picks.respond = slow
    loop._handle_update(msg("3 picks"))                                 # returns at once
    assert entered.wait(10)
    loop._handle_update(msg("/help", user="43", i=2))                   # another user is answered meanwhile
    assert any("Commands" in s["text"] for s in loop.tg.sends)
    loop._handle_update(msg("3 more picks", i=3))                       # same user, still in flight
    assert loop.tg.sends[-1]["text"] == commands.CUSTOM_BUSY
    release.set()
    loop._custom_executor.shutdown(wait=True)
    assert any(e["text"] == "🔥 done" for e in loop.tg.edits)              # the loading message becomes the reply


def test_custom_picks_cpu_per_user_is_limited(db, monkeypatch):
    loop = _custom_loop(db, monkeypatch, executor=False)
    loop._custom_picks.respond = lambda text, user_id, chat_id: SimpleNamespace(text="🔥 ok", reply_markup=None)
    loop._custom_picks.recovery.last_work = commands.CUSTOM_WORK_LIMIT // 2      # two worst-case requests
    for i in range(3):
        loop._handle_update(msg("3 picks", i=i))
    replies = [m["text"] for m in loop.tg.edits + loop.tg.sends if m["text"] in ("🔥 ok", commands.SLOW_DOWN)]
    assert replies == ["🔥 ok", "🔥 ok", commands.SLOW_DOWN]
    loop._handle_update(msg("3 picks", user="43", i=9))                  # another user is unaffected
    loop._custom_users.add(("43", "43"))
    loop._handle_update(msg("3 picks", user="43", i=10))
    assert loop.tg.edits[-1]["text"] == "🔥 ok" and loop.tg.edits[-1]["chat_id"] == "43"


def test_the_custom_picks_queue_is_bounded(db, monkeypatch):
    loop = _custom_loop(db, monkeypatch)
    loop._custom_pending = commands.CUSTOM_QUEUE_LIMIT
    loop._handle_update(msg("3 picks"))
    assert loop.tg.sends[-1]["text"] == commands.CUSTOM_FULL
    loop._custom_executor.shutdown(wait=True)


def test_custom_picks_logs_never_contain_raw_telegram_ids(db, caplog):
    from predbot.custom_picks import CustomPicks
    from tests.test_custom_picks import NOW, rows_for, store
    store(db, rows_for(1))
    picks = CustomPicks(db, Config(timezone="UTC"))
    with caplog.at_level(logging.INFO):
        picks.respond("100 odds basketball", "987654321", "987654321", NOW)
        picks.select({"id": "c", "data": "cp|missing", "from": {"id": 987654321},
                      "message": {"chat": {"id": 987654321, "type": "private"}}}, NOW)
    assert "Custom Picks" in caplog.text and "987654321" not in caplog.text
