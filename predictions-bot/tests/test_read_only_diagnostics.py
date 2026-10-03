"""A test tool must never change what /top and /daily serve.

Three layers: the fetcher's persist switch, the real dry_run_today.main() in both
modes, and a static scan of every root script so a new diagnostic cannot start
writing snapshots or picks without this test noticing."""
from __future__ import annotations

import ast
import pathlib
from datetime import timedelta
from unittest.mock import Mock

import pytest

from predbot.cache.fetcher import UnifiedFetcher
from predbot.config import Config
from predbot.jobs import daily_picks
from predbot.selection.picker import PickSet
from tests.conftest import dt
from tests.real_events import pickset
from tests.test_picker import ev, sc

NOW = dt(hh=12)
DAY = "2030-06-01"
ROOT = pathlib.Path(__file__).resolve().parent.parent


def _pickset():
    """A real, valid slate: two Top legs (1.45 x 1.45 = 2.10) and a Daily pick."""
    a, b, c = ev(1), ev(2), ev(3)
    return pickset([sc(a, "result3", "home", 0.72, 1.45), sc(b, "result3", "home", 0.72, 1.45),
                    sc(c, "result3", "home", 0.70, 1.50)])


@pytest.fixture
def served(db, monkeypatch):
    monkeypatch.setattr("predbot.cache.fetcher.utcnow", lambda: NOW)
    monkeypatch.setattr(daily_picks, "utcnow", lambda: NOW)
    monkeypatch.setattr(daily_picks, "build_picks", lambda *a, **k: _pickset())
    cfg = Config(timezone="UTC")
    fetcher = UnifiedFetcher(Mock(), db, cfg)
    return fetcher, db, cfg


def _state(db):
    return (db.get_prediction_cache(f"daily_predictions:UTC:{DAY}"), db.has_picks_for_date(DAY))


def test_persist_false_generates_the_slate_but_writes_nothing(served):
    fetcher, db, _ = served

    snap = fetcher.get_daily_snapshot(persist=False)

    assert snap.total_picks() == 3                       # the slate really was generated...
    assert _state(db) == (None, False)                   # ...and nothing was stored


def test_persist_true_is_still_the_production_default(served):
    fetcher, db, _ = served

    fetcher.get_daily_snapshot()

    row, has_picks = _state(db)
    assert row is not None and has_picks


def test_dry_run_of_run_persists_nothing_and_never_sends(served):
    fetcher, db, cfg = served
    tg = Mock()
    tg.enabled.return_value = True

    daily_picks.run(fetcher.chain, db, cfg, tg, dry_run=True, service=fetcher)

    assert _state(db) == (None, False)
    tg.send.assert_not_called()


def test_a_read_only_run_does_not_touch_an_existing_served_snapshot(served):
    fetcher, db, _ = served
    fetcher.get_daily_snapshot()                           # the real, served snapshot
    before = db.get_prediction_cache(f"daily_predictions:UTC:{DAY}")

    fetcher.get_daily_snapshot(force_refresh=True, persist=False)

    assert db.get_prediction_cache(f"daily_predictions:UTC:{DAY}") == before


# ----------------------------------------------------- the real script, both modes
@pytest.fixture
def dry_run_script(db, monkeypatch, served):
    import dry_run_today
    _, _, cfg = served
    monkeypatch.setattr(dry_run_today, "load_config", lambda: cfg)
    monkeypatch.setattr(dry_run_today, "Database", lambda path: db)
    monkeypatch.setattr("predbot.providers.chain.build_providers", lambda c, d: {})
    return dry_run_today


def test_dry_run_today_is_read_only_by_default(dry_run_script, served, capsys):
    _, db, _ = served

    assert dry_run_script.main([]) == 0

    assert "READ-ONLY" in capsys.readouterr().out
    assert _state(db) == (None, False)


def test_dry_run_today_persists_only_when_asked(dry_run_script, served, capsys):
    _, db, _ = served

    assert dry_run_script.main(["--persist"]) == 0

    assert "PERSIST" in capsys.readouterr().out
    row, has_picks = _state(db)
    assert row is not None and has_picks


# ------------------------------------------------------- static guard, every script
WRITERS = {"store_picks", "insert_pick", "upsert_prediction_cache", "_store", "defer_prediction_retry",
           "mark_delivery", "mark_post"}
# main.py is the production entry; cli.py's picks --dry-run goes through run(dry_run=...) which is
# covered above; verify_live_output.py builds its own DB inside a TemporaryDirectory.
EXEMPT = {"main.py", "cli.py", "verify_live_output.py"}


def _calls(path):
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Call):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            yield name, {k.arg for k in node.keywords}


@pytest.mark.parametrize("script", sorted(p.name for p in ROOT.glob("*.py") if p.name not in EXEMPT))
def test_root_scripts_never_write_snapshots_or_picks_by_default(script):
    for name, kwargs in _calls(ROOT / script):
        assert name not in WRITERS, f"{script} calls {name}() - diagnostics must not write snapshot/picks state"
        if name == "get_daily_snapshot":
            assert kwargs & {"persist", "allow_api_call"}, \
                f"{script}: get_daily_snapshot() must say persist=... explicitly (or allow_api_call=False)"


def test_the_exempt_scripts_really_are_safe():
    assert "TemporaryDirectory" in (ROOT / "verify_live_output.py").read_text(encoding="utf-8")
    cli = (ROOT / "cli.py").read_text(encoding="utf-8")
    assert 'dry_run="--dry-run" in flags' in cli
