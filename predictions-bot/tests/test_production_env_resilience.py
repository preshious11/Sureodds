"""The production dashboard's environment cannot be edited from here, so the code must survive what is in it:
stale copies of old defaults, and league lists written before international football was supported."""
from __future__ import annotations

import logging

import pytest

from predbot import config as cfgmod
from predbot.config import INTERNATIONAL_ODDS_KEYS, INTERNATIONAL_SHARP_LEAGUES, Config


@pytest.fixture(autouse=True)
def _fresh_warnings(monkeypatch):
    cfgmod._STALE_WARNED.clear()
    for name in ("HONOR_PINNED_VALUES", "INTERNATIONAL_FOOTBALL", "SHARPAPI_LEAGUES", "ODDS_API_LEAGUE_ALLOWLIST",
                 "DAILY_COMBINED_MAX_ODDS", "DAILY_PICKS_TARGET", "DAILY_MIN_PROB", "TOP_RELAX_PROB", "TOP_MIN_BOOKS"):
        monkeypatch.delenv(name, raising=False)


# ----------------------------------------------------------------------------------------- stale pins
@pytest.mark.parametrize("name,old,attr,new", [
    ("DAILY_COMBINED_MAX_ODDS", "20", "daily_odds_max", 12.0),
    ("DAILY_PICKS_TARGET", "20", "daily_target", 12),
    ("DAILY_MIN_PROB", "0.30", "daily_min_prob", 0.55),
    ("DAILY_MIN_PROB", "0.3", "daily_min_prob", 0.55),
])
def test_a_pinned_pre_rework_default_is_ignored_with_a_warning(monkeypatch, caplog, name, old, attr, new):
    monkeypatch.setenv(name, old)
    with caplog.at_level(logging.WARNING):
        assert getattr(Config(), attr) == new
    assert any(name in r.message and "pre-rework default" in r.message for r in caplog.records)


@pytest.mark.parametrize("name,value,attr", [
    ("TOP_RELAXED_FALLBACK", "1", "top_relaxed_fallback"), ("TOP_RELAX_PROB", "0.04", "top_relax_prob"),
    ("CROSS_TIER_SAME_FIXTURE", "1", "cross_tier_same_fixture"), ("MAX_PICKS_PER_EVENT", "3", "max_picks_per_event"),
    ("DAILY_MAX_PERIOD_SHARE", "0.5", "daily_max_period_share"),
    ("TOP_PROVIDER_PRIORITY", "oddsapi", "top_provider_priority"),
    ("TOP_LEGS_MIN", "2", "top_legs_min"), ("TOP_LEG_ODDS_MIN", "1.05", "top_leg_odds_min"),
    ("TOP_MIN_AGREEMENT", "0.50", "top_min_agreement"),
])
def test_an_env_that_still_sets_a_removed_flag_is_simply_ignored(monkeypatch, name, value, attr):
    """The dashboard may still carry flags of the deleted Top fallback / cross-tier / priority architecture."""
    monkeypatch.setenv(name, value)
    c = Config()
    assert not hasattr(c, attr)
    assert c.top_min_books >= 1 and c.daily_target == 12        # the rest of the config is unaffected


def test_a_stale_pin_is_not_reported_as_an_active_override(monkeypatch):
    monkeypatch.setenv("DAILY_MIN_PROB", "0.30")
    Config()
    default, effective = cfgmod.KNOWN_KEYS["DAILY_MIN_PROB"]
    assert default == effective          # the ignored pin is not an override


def test_a_deliberate_value_is_never_touched(monkeypatch):
    monkeypatch.setenv("DAILY_MIN_PROB", "0.60")
    monkeypatch.setenv("DAILY_COMBINED_MAX_ODDS", "15")
    monkeypatch.setenv("TOP_MIN_BOOKS", "2")                    # the documented way to loosen the Top
    c = Config()
    assert (c.daily_min_prob, c.daily_odds_max, c.top_min_books) == (0.60, 15.0, 2)


def test_an_old_value_can_be_kept_on_purpose(monkeypatch):
    monkeypatch.setenv("DAILY_COMBINED_MAX_ODDS", "20")
    monkeypatch.setenv("HONOR_PINNED_VALUES", "1")
    assert Config().daily_odds_max == 20.0


# ----------------------------------------------------------------------------------------- league lists
OLD_LIST = "england_-_premier_league,spain_-_la_liga,atp,wta,mlb"
OLD_ALLOW = "soccer_epl,soccer_spain_la_liga"


def test_pinned_league_lists_still_get_international_football(monkeypatch, caplog):
    monkeypatch.setenv("SHARPAPI_LEAGUES", OLD_LIST)
    monkeypatch.setenv("ODDS_API_LEAGUE_ALLOWLIST", OLD_ALLOW)
    with caplog.at_level(logging.WARNING):
        c = Config()
    assert c.sharpapi_leagues[:5] == OLD_LIST.split(",")                         # what was pinned stays, in order
    assert all(lg in c.sharpapi_leagues for lg in INTERNATIONAL_SHARP_LEAGUES)
    assert all(k in c.odds_api_league_allowlist for k in INTERNATIONAL_ODDS_KEYS)
    assert any("did not include" in r.message for r in caplog.records)


def test_the_top_up_adds_nothing_twice(monkeypatch):
    c = Config()                                     # the defaults already list them
    assert len(set(c.sharpapi_leagues)) == len(c.sharpapi_leagues) == 22
    assert len(set(c.odds_api_league_allowlist)) == len(c.odds_api_league_allowlist) == 12


def test_an_explicit_constructor_argument_is_taken_as_given():
    c = Config(sharpapi_leagues=["mlb"], odds_api_league_allowlist=["soccer_epl"])
    assert c.sharpapi_leagues == ["mlb"] and c.odds_api_league_allowlist == ["soccer_epl"]


def test_international_football_can_be_opted_out_of(monkeypatch):
    monkeypatch.setenv("SHARPAPI_LEAGUES", OLD_LIST)
    monkeypatch.setenv("INTERNATIONAL_FOOTBALL", "false")
    assert not set(INTERNATIONAL_SHARP_LEAGUES) & set(Config().sharpapi_leagues)


def test_an_empty_odds_allowlist_still_means_every_key(monkeypatch):
    monkeypatch.setenv("ODDS_API_LEAGUE_ALLOWLIST", "")
    assert Config().odds_api_league_allowlist != []                # empty env falls back to the default list, as before
