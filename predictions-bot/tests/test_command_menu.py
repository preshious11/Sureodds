"""The '/' menu: the commands are registered with Telegram's setMyCommands at startup - idempotently, and a failure
is logged, never fatal."""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
import requests

from predbot.bot import callbacks
from predbot.bot.commands import CommandLoop
from predbot.bot.telegram import Telegram
from predbot.config import Config


class Client:
    token = "fake-token"

    def __init__(self, result=True, raises=None):
        self.registered, self.result, self.raises = [], result, raises

    def set_my_commands(self, commands):
        self.registered.append(commands)
        if self.raises:
            raise self.raises
        return self.result


def loop(tg):
    return CommandLoop(tg, SimpleNamespace(), Config(timezone="UTC"), {})


def test_the_menu_lists_the_seven_commands_with_one_line_descriptions():
    names = [c["command"] for c in callbacks.BOT_COMMANDS]
    assert names == ["all", "top", "daily", "stats", "yesterday", "timezone", "help"]
    for c in callbacks.BOT_COMMANDS:
        assert c["description"] and "\n" not in c["description"] and len(c["description"]) <= 256
        assert set(c) == {"command", "description"} and c["command"] == c["command"].lower()


def test_startup_registers_the_commands_and_registering_again_sends_the_same_list():
    tg = Client()
    for _ in range(2):                                         # a restarted poller registers again: idempotent
        loop(tg)._register_commands()
    assert tg.registered == [callbacks.BOT_COMMANDS, callbacks.BOT_COMMANDS]


def test_a_failed_registration_is_logged_and_never_fatal(caplog):
    with caplog.at_level(logging.INFO):
        loop(Client(result=False))._register_commands()            # Telegram said no
        loop(Client(raises=RuntimeError("boom")))._register_commands()      # the client blew up
    assert any("not registered" in r.getMessage() for r in caplog.records)


def test_a_client_without_the_method_is_skipped():
    loop(SimpleNamespace(token="t"))._register_commands()          # no exception


def test_the_real_client_posts_setmycommands_and_swallows_transport_failures(monkeypatch):
    calls = []

    class Session:
        def post(self, url, json=None, timeout=None):
            calls.append((url, json))
            return SimpleNamespace(status_code=200, text="")

    tg = Telegram("token", "1")
    tg.session = Session()
    assert tg.set_my_commands(callbacks.BOT_COMMANDS) is True
    assert calls[0][0].endswith("/setMyCommands") and calls[0][1] == {"commands": callbacks.BOT_COMMANDS}

    class Down:
        def post(self, *a, **k):
            raise requests.ConnectionError("down")

    tg.session = Down()
    assert tg.set_my_commands(callbacks.BOT_COMMANDS) is False

    class Refused:
        def post(self, *a, **k):
            return SimpleNamespace(status_code=400, text="bad")

    tg.session = Refused()
    assert tg.set_my_commands(callbacks.BOT_COMMANDS) is False


def test_run_registers_before_polling(monkeypatch):
    order = []
    tg = Client()
    lp = loop(tg)
    monkeypatch.setattr(lp, "_poll", lambda: order.append("poll"))
    monkeypatch.setattr(lp, "_register_commands", lambda: order.append("register"))
    monkeypatch.setattr("predbot.bot.commands.PollingLease", lambda token: __import__("contextlib").nullcontext())
    monkeypatch.setattr("predbot.bot.commands.log_identity", lambda handler: "instance")
    lp.run()
    assert order == ["register", "poll"]
