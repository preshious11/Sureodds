"""Merge-regression guards: onboarding never sends predictions, commands and
buttons share ONE view path, and the canonical card formatter is the only one.

These tests exist so the BetMiner/SQLite merge regressions (auto-sending picks
on /start, subscribe buttons, parallel command/button renderers, resurrected
card formatters) cannot silently return after another merge.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from predbot.bot import callbacks, pagination
from predbot.bot.callbacks import handle_callback, handle_command, handle_start, menu_markup
from predbot.bot.commands import CommandLoop
from predbot.cache.fetcher import UnifiedFetcher
from predbot.config import Config
from tests.test_outcome_format import _seed, _summary_snapshot
from tests.test_subscribers_broadcast import FakeTG, _cb

ROOT = Path(__file__).resolve().parent.parent
PRED = ROOT / "predbot"


def _forbidden_service():
    """A service that explodes if anything onboarding-related touches it."""
    return SimpleNamespace(
        get_daily_snapshot=lambda **_kw: (_ for _ in ()).throw(
            AssertionError("onboarding fetched the prediction snapshot")))


# ============================================================ onboarding guards
def test_start_sends_menu_only_and_never_opens_a_view(db, monkeypatch):
    monkeypatch.setattr(callbacks, "open_view",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("open_view called from onboarding")))
    monkeypatch.setattr(pagination, "render_view",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("render_view called from onboarding")))
    tg = FakeTG()

    handle_start(db, tg, "42", {"first_name": "Zed"})

    assert len(tg.sends) == 1                        # exactly one message
    text = tg.sends[0]["text"]
    assert text.startswith("👋 Hi, Zed")             # the welcome screen
    assert "Combined" not in text and "Odds:" not in text and "Confidence" not in text
    assert tg.sends[0]["reply_markup"] == menu_markup()
    assert db.subscribers_active()                   # subscription state was updated


def test_repeated_start_is_always_menu_only(db):
    tg = FakeTG()
    for _ in range(3):
        handle_start(db, tg, "42", {"first_name": "Zed"})
    assert len(tg.sends) == 3
    for send in tg.sends:
        assert send["text"].startswith("👋 Hi, Zed")
        assert "Confidence" not in send["text"] and "Odds:" not in send["text"]


def test_menu_callback_only_restores_the_welcome(db):
    tg = FakeTG()
    handle_start(db, tg, "42", {"first_name": "Ada"})
    before_edits = len(tg.edits)

    cb = _cb("menu")
    cb["from"] = {"first_name": "Ada"}
    handle_callback(cb, service=_forbidden_service(), tg=tg,
                    cfg=Config(timezone="UTC"), db=db)

    assert len(tg.edits) == before_edits + 1
    assert tg.edits[-1]["text"].startswith("👋 Hi, Ada")
    assert "Confidence" not in tg.edits[-1]["text"]


def test_removed_subscribe_toggle_cannot_send_predictions(db):
    tg = FakeTG()
    handle_start(db, tg, "42", {"first_name": "Ada"})
    before = (len(tg.sends), len(tg.edits))

    handle_callback(_cb("sub|on"), service=_forbidden_service(), tg=tg,
                    cfg=Config(timezone="UTC"), db=db)
    handle_callback(_cb("sub|off"), service=_forbidden_service(), tg=tg,
                    cfg=Config(timezone="UTC"), db=db)

    assert (len(tg.sends), len(tg.edits)) == before   # unknown payloads: nothing happens
    assert db.subscribers_active()                    # and they cannot undo /start
    buttons = [b for row in menu_markup()["inline_keyboard"] for b in row]
    assert [b["text"] for b in buttons] == ["📚 All Predictions", "🔥 Top Picks", "📋 Daily Picks", "📊 Stats",
                                                 "🗓 Yesterday"]
    assert not any(b["callback_data"].startswith("sub|") for b in buttons)


# ============================================ command == button parity, zero API
def _seeded_service(db):
    _seed(db.path, _summary_snapshot())              # canonical snapshot in SQLite
    return UnifiedFetcher(None, db, Config(timezone="Africa/Lagos"))
                                                     # chain=None: any provider call explodes


@pytest.mark.parametrize("view", ["daily", "top", "stats"])
def test_command_and_button_render_identically_from_sqlite(db, view):
    """PAIR 1/2/3: /{view} and the pg|{view}|0 button use ONE shared handler,
    the same seeded SQLite snapshot, and produce byte-identical content."""
    service = _seeded_service(db)
    tg = FakeTG()
    cfg = Config(timezone="Africa/Lagos")

    handle_command("/" + view, "42", service=service, tg=tg, cfg=cfg, db=db)   # SENDS
    handle_callback(_cb(f"pg|{view}|0"), service=service, tg=tg, cfg=cfg, db=db)  # EDITS

    assert tg.sends[-1]["text"] == tg.edits[-1]["text"]
    assert tg.sends[-1]["reply_markup"] == tg.edits[-1]["reply_markup"]
    if view != "stats":
        assert "Combined odds" in tg.sends[-1]["text"]


def test_botname_commands_route_like_plain_commands(db):
    """'/top@SureOddsBot extra' is normalized by split_command, then routed by
    the SAME command_view the plain commands use - no separate content path."""
    from predbot.bot.commands import split_command
    for raw, expected in [("/top@SureOddsBot extra", ("/top", ["extra"])),
                          ("/DAILY@SureOddsBot", ("/daily", [])),
                          ("/start@SureOddsBot", ("/start", []))]:
        assert split_command(raw) == expected
    for raw, view in [("/top@SureOddsBot", "/top"), ("/daily@SureOddsBot", "/daily"),
                      ("/stats@SureOddsBot", "/stats"), ("/start@SureOddsBot", None)]:
        cmd = split_command(raw)[0]
        assert callbacks.command_view(cmd) == (callbacks.COMMANDS[view] if view else None)


def test_views_and_pagination_cause_zero_provider_calls(db):
    service = _seeded_service(db)                    # with a valid snapshot, cache-first
    tg = FakeTG()
    cfg = Config(timezone="Africa/Lagos")
    for view in ("daily", "top"):
        handle_command("/" + view, "42", service=service, tg=tg, cfg=cfg, db=db)
        handle_callback(_cb(f"pg|{view}|0"), service=service, tg=tg, cfg=cfg, db=db)
        handle_callback(_cb(f"pg|{view}|1"), service=service, tg=tg, cfg=cfg, db=db)
    assert len(tg.sends) == 2 and len(tg.edits) == 4   # every render succeeded cache-only


# ==================================== merge-regression guard: single code paths
def test_one_definition_of_every_routing_and_rendering_function():
    """The merge resurrected parallel renderers; assert there is only one of each."""
    for name in ("def handle_start(", "def open_view(", "def welcome_message(",
                 "def menu_markup(", "def handle_command(",
                 "def pick_line_from_record(", "def summary_card(",
                 "def outcome_label(", "def format_outcome(",
                 "def picks_text(", "def combined_odds("):
        files = [p for p in PRED.rglob("*.py")
                 if any(ln.strip().startswith(name)
                        for ln in p.read_text(encoding="utf-8").splitlines())]
        assert len(files) == 1, f"{name!r} defined in {len(files)} files: {files}"


def test_pagination_has_no_parallel_card_formatter(db):
    """The merged branch had a second card builder inside pagination; keep one."""
    from predbot.bot import format as fmt
    from predbot.cache.models import PickRecord

    pick = PickRecord(pick_id=1, tier="top", rank=1, sport="baseball", league="MLB",
                      home="Los Angeles Dodgers", away="San Francisco Giants",
                      kickoff_utc="2030-06-01T18:00:00Z", label="Over 8.5",
                      market="prop", selection="over", line=8.5, stat="team_hits",
                      entity="Los Angeles Dodgers", price=1.88, prob=0.7, ev=0.05)

    assert pagination.render_pick(pick, "UTC") == \
        "\n".join(fmt.pick_lines(fmt.pick_line_from_record(pick, "UTC")))
    assert pagination._pick_line(pick, "UTC") == fmt.pick_line_from_record(pick, "UTC")

    # Emoji in the *nav* labels is fine; card-only copy must not be re-implemented here.
    src = Path(pagination.__file__).read_text(encoding="utf-8")
    for frag in ("Confidence level:", "🎯", "💰", "🏆\ufe0f"):
        assert frag not in src, f"pagination.py re-implements card copy: {frag!r}"


def test_no_onboarding_send_helpers_or_subscribe_callbacks_in_source():
    """Any of these strings in active source means the old behaviour came back."""
    forbidden = ["sub|on", "sub|off", "send_daily_predictions", "send_predictions",
                 "send_daily_picks"]
    for p in PRED.rglob("*.py"):
        src = p.read_text(encoding="utf-8")
        for frag in forbidden:
            assert frag not in src, f"{p} contains {frag!r}"


def test_start_path_never_calls_prediction_renderers(db, monkeypatch):
    """Whatever a future merge renames: if /start, the menu callback or a legacy
    subscribe payload ever tries to render predictions, this fails loudly."""
    for module, fn in ((callbacks, "open_view"), (pagination, "render_view"),
                       (pagination, "render_page"), (pagination, "render_stats")):
        monkeypatch.setattr(module, fn,
                            lambda *a, **k: (_ for _ in ()).throw(
                                AssertionError(f"{fn} called from onboarding")))
    tg = FakeTG()
    loop = CommandLoop(tg, db, Config(), {}, service=None, page_size=5)

    assert loop.handle("/start", "42", "42", []) is None   # no /start reply branch at all
    handle_start(db, tg, "42", {"first_name": "Ada"})
    loop._dispatch_callback(_cb("menu"))             # menu callback
    loop._dispatch_callback(_cb("sub|on"))           # legacy subscribe payload
    loop._dispatch_callback(_cb("pg|daily|0"))       # view payload WITHOUT service: refused

    assert len(tg.sends) == 1                        # handle_start menu only
    assert tg.sends[0]["text"].startswith("👋 Hi")
    assert "Confidence" not in tg.sends[0]["text"]
    assert not any("Confidence" in e.get("text", "") for e in tg.edits)

