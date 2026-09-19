"""Where .env is read from, what the startup report says, and the code defaults that
production (which has no .env) depends on."""
from __future__ import annotations

import os
import pathlib

import pytest

from predbot import config as config_mod
from predbot.config import Config, default_env_path, load_config, mask, startup_lines
from predbot.providers.budget import Budget
from predbot.providers.oddsapi import OddsApiProvider

ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    """load_config() writes to os.environ; keep every test's changes (and the real
    developer .env) out of the rest of the suite."""
    monkeypatch.setattr(os, "environ", os.environ.copy())


def _clear(monkeypatch, *names):
    for n in names:
        monkeypatch.delenv(n, raising=False)


# ---------------------------------------------------------------- code defaults
def test_odds_api_regions_default_to_eu_so_a_key_costs_three_credits(monkeypatch):
    _clear(monkeypatch, "ODDS_API_REGIONS")
    regions = Config().odds_api_regions

    assert regions == ["eu"]
    provider = OddsApiProvider("k", Budget(Mock_db()), regions=regions)
    assert provider.credits_per_key == 3
    assert 15 // provider.credits_per_key == 5          # a 15-credit cycle reaches five leagues, not two


def test_the_old_two_region_default_would_have_halved_the_leagues(monkeypatch):
    provider = OddsApiProvider("k", Budget(Mock_db()), regions=["us", "eu"])
    assert provider.credits_per_key == 6 and 15 // provider.credits_per_key == 2


def test_sgo_leagues_default_to_the_seven_covered_keys(monkeypatch):
    _clear(monkeypatch, "SGO_LEAGUES")

    assert Config().sgo_leagues == ["NBA", "NFL", "MLB", "MLS", "NCAAF", "NCAAB", "UEFA_CHAMPIONS_LEAGUE"]


LIST_KEYS = {"ODDS_API_REGIONS": "odds_api_regions", "SGO_LEAGUES": "sgo_leagues",
             "PROVIDER_ORDER": "provider_order", "ALLOWED_SPORTS": "allowed_sports",
             "DISABLED_MARKETS": "disabled_markets", "SHARPAPI_LEAGUES": "sharpapi_leagues",
             "ODDS_API_LEAGUE_ALLOWLIST": "odds_api_league_allowlist", "SGO_ALLOWED_STATS": "sgo_allowed_stats",
             "SGO_ALLOWED_PERIODS": "sgo_allowed_periods"}
SCALAR_KEYS = {"SGO_EVENT_LIMIT": "sgo_event_limit", "SHARPAPI_MAX_PAGES": "sharpapi_max_pages",
               "ODDSAPI_MAX_CREDITS_PER_CYCLE": "oddsapi_max_credits_per_cycle",
               "ODDSAPI_RESERVE_CREDITS": "oddsapi_reserve_credits", "MAX_PLAYER_PROPS": "max_player_props"}


def test_the_example_env_matches_those_defaults(monkeypatch):
    example = {}
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            example[k.strip()] = v.split("#", 1)[0].strip()
    _clear(monkeypatch, *LIST_KEYS, *SCALAR_KEYS)
    cfg = Config()

    for env_name, attr in LIST_KEYS.items():
        assert example[env_name].split(",") == getattr(cfg, attr), env_name
    for env_name, attr in SCALAR_KEYS.items():
        assert example[env_name] == str(getattr(cfg, attr)), env_name
    assert example["SHARPAPI_SPORTS"] == "" and example["SGO_SPORTS"] == ""


def Mock_db():
    from unittest.mock import Mock
    return Mock()


# ------------------------------------------------------------ where .env is read
def test_the_env_path_is_next_to_main_py_and_ignores_the_working_directory(tmp_path, monkeypatch):
    before = default_env_path()
    monkeypatch.chdir(tmp_path)

    assert default_env_path() == before == ROOT / ".env"
    assert (ROOT / "main.py").is_file()


def test_a_stale_env_in_the_working_directory_is_never_read(tmp_path, monkeypatch):
    stale_cwd = tmp_path / "elsewhere"
    stale_cwd.mkdir()
    (stale_cwd / ".env").write_text("MAX_PICKS_PER_EVENT=9\n", encoding="utf-8")
    project = tmp_path / "project.env"
    project.write_text("MAX_PICKS_PER_EVENT=3\n", encoding="utf-8")
    _clear(monkeypatch, "MAX_PICKS_PER_EVENT")
    monkeypatch.chdir(stale_cwd)

    cfg = load_config(env_file=str(project))

    assert cfg.max_picks_per_event == 3 and cfg.env_file == str(project)


def test_load_config_without_an_argument_reads_the_project_env_not_the_cwd(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("MAX_PICKS_PER_EVENT=9\n", encoding="utf-8")
    seen = {}
    monkeypatch.setattr(config_mod, "default_env_path", lambda: seen.setdefault("p", tmp_path / "proj.env"))
    (tmp_path / "proj.env").write_text("MAX_PICKS_PER_EVENT=4\n", encoding="utf-8")
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.chdir(other)
    _clear(monkeypatch, "MAX_PICKS_PER_EVENT")

    assert load_config().max_picks_per_event == 4


def test_a_real_environment_variable_beats_the_env_file(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_text("MAX_PICKS_PER_EVENT=3\n", encoding="utf-8")
    monkeypatch.setenv("MAX_PICKS_PER_EVENT", "5")            # what a hosting dashboard sets

    cfg = load_config(env_file=str(f))

    assert cfg.max_picks_per_event == 5
    assert any(o["key"] == "MAX_PICKS_PER_EVENT" and o["source"] == "process env"
               for o in cfg.env_report["overrides"])


# ------------------------------------------------------------- the startup report
def test_startup_report_names_the_file_the_overrides_and_the_ignored_keys(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_text("MAX_PICKS_PER_EVENT=3\n"
                 "ODDS_API_REGIONS=eu\n"                       # equals the default -> not an override
                 "TELEGRAM_BOT_TOKEN=abc123-very-secret\n"
                 "ODDS_API_KEY=odds-secret-key\n"
                 "LEGACY_SERVICE_URL=https://user:pw@host\n"     # a key this version never reads
                 "OLD_UNUSED_SETTING=football\n", encoding="utf-8")
    _clear(monkeypatch, "MAX_PICKS_PER_EVENT", "ODDS_API_REGIONS", "TELEGRAM_BOT_TOKEN", "ODDS_API_KEY")

    text = "\n".join(startup_lines(load_config(env_file=str(f))))

    assert f"loaded .env from {f}" in text and "(6 keys)" in text
    assert "MAX_PICKS_PER_EVENT=3 [.env]" in text
    assert "TELEGRAM_BOT_TOKEN=*** [.env]" in text and "ODDS_API_KEY=*** [.env]" in text
    assert "ODDS_API_REGIONS" not in text.split("override the code default:")[1].split("\n")[0]
    assert "WARNING .env keys this version never reads (ignored): LEGACY_SERVICE_URL, OLD_UNUSED_SETTING" in text


def test_secrets_are_masked_everywhere_in_the_report(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_text("TELEGRAM_BOT_TOKEN=abc123-very-secret\nSHARPAPI_KEY=k3\n"
                 "SPORTSGAMEODDS_KEY=k4\nODDS_API_KEY=k5\n", encoding="utf-8")
    _clear(monkeypatch, "TELEGRAM_BOT_TOKEN", "SHARPAPI_KEY", "SPORTSGAMEODDS_KEY", "ODDS_API_KEY")

    cfg = load_config(env_file=str(f))
    everything = "\n".join(startup_lines(cfg)) + repr(cfg.env_report)

    for secret in ("abc123-very-secret", "k3", "k4", "k5"):
        assert secret not in everything.replace("ODDS_API_KEY", "").replace("SHARPAPI_KEY", "")
    assert mask("TELEGRAM_BOT_TOKEN", "x") == "***" and mask("SOME_SERVICE_URL", "x") == "***"
    assert mask("MAX_PICKS_PER_EVENT", 1) == "1"


def test_a_missing_env_file_is_reported_not_fatal(tmp_path):
    cfg = load_config(env_file=str(tmp_path / "nope.env"))

    assert cfg.env_file == ""
    assert "no .env at" in "\n".join(startup_lines(cfg))


def test_no_override_is_reported_when_everything_is_default(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_text("MAX_PICKS_PER_EVENT=1\n", encoding="utf-8")          # equal to the default
    _clear(monkeypatch, "MAX_PICKS_PER_EVENT")

    assert "no key overrides a code default" in "\n".join(startup_lines(load_config(env_file=str(f))))
