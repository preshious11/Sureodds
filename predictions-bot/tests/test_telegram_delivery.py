"""Transport receipts require every chunk; rate-limit retries stay bounded."""
from unittest.mock import Mock

import pytest
import requests

from predbot.bot.telegram import Telegram
from predbot.cache.models import DailySnapshot
from predbot.config import Config
from predbot.jobs import daily_picks
from tests.test_cards import _long_picks

LONG_TEXT = "a" * 3990 + "\n\n" + "b" * 3990


def response(status=200, payload=None):
    result = Mock(status_code=status, text="test response")
    result.json.return_value = payload if payload is not None else {"result": {"message_id": 1}}
    return result


def client(*results):
    tg = Telegram("test-token", "-1001234567890")
    tg.session = Mock()
    tg.session.post.side_effect = results
    return tg


@pytest.mark.parametrize("failure", [response(400), requests.ConnectionError("offline"), response(payload={})])
def test_partially_failed_chunks_return_failure(failure):
    tg = client(response(), failure)
    assert tg.send(LONG_TEXT) is None
    assert tg.session.post.call_count == 2


def test_all_chunks_succeed_and_private_destination_overrides_default():
    tg = client(response(), response(payload={"result": {"message_id": 2}}))
    assert tg.send(LONG_TEXT, chat_id="42") == "2"
    assert [c.kwargs["json"]["chat_id"] for c in tg.session.post.call_args_list] == ["42", "42"]


def test_rate_limit_waits_then_retries_only_rejected_chunk(monkeypatch):
    sleep = Mock()
    monkeypatch.setattr("predbot.bot.telegram.time.sleep", sleep)
    tg = client(response(), response(429, {"parameters": {"retry_after": 7}}),
                response(payload={"result": {"message_id": 2}}))
    assert tg.send(LONG_TEXT) == "2"
    sleep.assert_called_once_with(7)
    texts = [c.kwargs["json"]["text"] for c in tg.session.post.call_args_list]
    assert texts == ["a" * 3990, "b" * 3990, "b" * 3990]


def test_repeated_rate_limit_returns_failure_after_one_retry(monkeypatch):
    sleep = Mock()
    monkeypatch.setattr("predbot.bot.telegram.time.sleep", sleep)
    limited = response(429, {"parameters": {"retry_after": 2}})
    tg = client(response(), limited, limited)
    assert tg.send(LONG_TEXT) is None
    assert tg.session.post.call_count == 3
    sleep.assert_called_once_with(2)


@pytest.mark.parametrize("delay", [None, 0, -1, "2", True])
def test_invalid_retry_after_does_not_sleep_or_loop(monkeypatch, delay):
    sleep = Mock()
    monkeypatch.setattr("predbot.bot.telegram.time.sleep", sleep)
    tg = client(response(429, {"parameters": {"retry_after": delay}}))
    assert tg.send("message") is None
    sleep.assert_not_called()
    assert tg.session.post.call_count == 1


def test_malformed_json_does_not_report_delivery_success():
    malformed = response()
    malformed.json.side_effect = ValueError("bad JSON")
    tg = client(malformed)
    assert tg.send("message") is None


def test_partial_card_failure_does_not_record_a_community_publication(db):
    snapshot = DailySnapshot(date="2030-06-01", top_picks=_long_picks(24))
    service = Mock()
    service.get_daily_snapshot.return_value = snapshot
    tg = client(response(), response(400))
    daily_picks.run(None, db, Config(timezone="UTC"), tg, service=service)
    assert tg.session.post.call_count == 2
    assert not db.post_done("picks_top", snapshot.date)
    assert not db.post_done("picks_daily", snapshot.date)
    assert not db.query("SELECT 1 FROM deliveries WHERE kind LIKE 'community_picks_%'")
