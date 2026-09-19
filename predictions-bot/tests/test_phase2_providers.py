"""Phase 2a/2b: provider order, the required SharpAPI key, the Odds API as a full chain member."""
from __future__ import annotations

import logging
import os
from unittest.mock import Mock

import pytest

from predbot.config import Config, require_provider_keys
from predbot.jobs.daily_picks import build_picks
from predbot.providers.budget import Budget
from predbot.providers.chain import ProviderChain
from predbot.providers.oddsapi import OddsApiProvider
from tests.conftest import dt
from tests.test_oddsapi import oddssgo_event
from tests.test_picker import sc


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    monkeypatch.setattr(os, "environ", os.environ.copy())
    for n in ("PROVIDER_ORDER", "ODDSAPI_MAX_CREDITS_PER_CYCLE", "ODDSAPI_RESERVE_CREDITS",
              "ODDS_API_LEAGUE_ALLOWLIST"):
        monkeypatch.delenv(n, raising=False)


def test_defaults_put_oddsapi_first_and_size_the_credit_budget_for_a_500_quota():
    cfg = Config()
    assert cfg.provider_order == ["oddsapi", "sharpapi", "sportsgameodds"]
    assert cfg.oddsapi_max_credits_per_cycle == 9 and cfg.oddsapi_reserve_credits == 80
    assert "soccer_epl" in cfg.odds_api_league_allowlist and len(cfg.odds_api_league_allowlist) == 11


def test_an_unknown_provider_name_is_warned_about(monkeypatch, caplog):
    monkeypatch.setenv("PROVIDER_ORDER", "oddsapi,removed_provider,sharpapi")
    with caplog.at_level(logging.WARNING):
        Config()
    assert any("unknown provider" in r.message and "removed_provider" in r.message for r in caplog.records)


def test_known_provider_names_do_not_warn(caplog):
    with caplog.at_level(logging.WARNING):
        Config()
    assert not any("unknown provider" in r.message for r in caplog.records)


def test_missing_sharpapi_key_exits_with_code_2(caplog):
    with caplog.at_level(logging.ERROR), pytest.raises(SystemExit) as exc:
        require_provider_keys(Config(sharpapi_key="", odds_api_key="o", sgo_key="s"))
    assert exc.value.code == 2
    assert any("SHARPAPI_KEY is required" in r.message and "https://sharpapi.io" in r.message for r in caplog.records)


def test_only_sharpapi_is_required_the_others_just_warn(caplog):
    with caplog.at_level(logging.WARNING):
        require_provider_keys(Config(sharpapi_key="k", odds_api_key="", sgo_key=""))
    text = " ".join(r.message for r in caplog.records)
    assert "ODDS_API_KEY is not set" in text and "SPORTSGAMEODDS_KEY is not set" in text


def test_main_and_cli_both_refuse_to_start_without_sharpapi(monkeypatch, tmp_path):
    import cli
    import main
    cfg = Config(sharpapi_key="", db_path=str(tmp_path / "x.db"), log_file="")
    for mod in (cli, main):
        monkeypatch.setattr(mod, "load_config", lambda cfg=cfg: cfg)
        monkeypatch.setattr(mod, "setup_logging", lambda *a, **k: None)
    with pytest.raises(SystemExit) as exc:
        cli.main(["cli.py", "picks", "--dry-run"])
    assert exc.value.code == 2
    with pytest.raises(SystemExit) as exc:
        main.main()
    assert exc.value.code == 2


# ------------------------------------------------------- Odds API league allowlist
SPORTS = [{"key": "soccer_epl", "active": True}, {"key": "soccer_brazil_campeonato", "active": True},
          {"key": "soccer_uefa_champs_league", "active": True}, {"key": "tennis_atp_us_open", "active": True}]


def _discover(db, allowlist):
    p = OddsApiProvider("k", Budget(db), league_allowlist=allowlist)
    p._get = Mock(return_value=SPORTS)
    return p._discover_sports()


def test_allowlist_limits_discovery_to_the_listed_leagues(db):
    out = _discover(db, ["soccer_epl", "soccer_uefa_champs_league"])
    assert out == {"football": ["soccer_epl", "soccer_uefa_champs_league"]}


def test_an_empty_allowlist_means_every_in_season_key(db):
    out = _discover(db, [])
    assert set(out["football"]) == {"soccer_epl", "soccer_brazil_campeonato", "soccer_uefa_champs_league"}
    assert out["tennis"] == ["tennis_atp_us_open"]


# --------------------------------------------- Odds API is a chain member, not a second chain
def _oddsapi_provider(events):
    p = Mock()
    p.enabled.return_value = True
    p.fetch_events.return_value = events
    return p


def test_build_picks_does_not_build_a_second_chain_when_oddsapi_is_in_provider_order(db, monkeypatch):
    e1 = oddssgo_event(price=1.50, i=1)
    e2 = oddssgo_event(price=1.45, i=2)
    e2.home, e2.away = "Sporting", "Feyenoord"
    oa = _oddsapi_provider([e1, e2])
    chain = ProviderChain({"oddsapi": oa}, ["oddsapi"], 1)
    chain.db, chain.cfg = None, None
    seen = {}

    def fake_select(scored, cfg, now, top_priority_source=None, **kw):
        seen["source"] = top_priority_source
        from predbot.selection.picker import PickSet
        return PickSet()

    monkeypatch.setattr("predbot.jobs.daily_picks.score_events",
                        lambda events, *a: [sc(e, "result3", "home", 0.75, 1.5, sources=2) for e in events])
    monkeypatch.setattr("predbot.jobs.daily_picks.select", fake_select)

    build_picks(chain, db, Config(timezone="UTC", provider_order=["oddsapi"], top_provider_priority=["oddsapi"]), dt())

    assert oa.fetch_events.call_count == 1          # collected once: credits are not spent twice
    assert seen["source"] == "oddsapi"              # but it is still handed to select()
