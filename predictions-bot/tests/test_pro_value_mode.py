"""PRO_VALUE_MODE: shadow is the current behaviour; publish and back to shadow are exact inverses."""
import pytest

from predbot.config import Config
from predbot.pro_value import mode

PICKS = [{"match_key": "arsenal|chelsea|1793000400", "outcome": "away"},
         {"match_key": "lyon|nice|1793007600", "outcome": "home"}]


def test_default_mode_is_shadow(monkeypatch):
    monkeypatch.delenv("PRO_VALUE_MODE", raising=False)
    assert Config().pro_value_mode == "shadow"


def test_shadow_publishes_nothing_and_excludes_nothing():
    plan = mode.publish_plan("shadow", PICKS)
    assert plan == {"mode": "shadow", "publish": [], "botd_exclude": set(), "no_pick_message": False}


def test_publish_publishes_the_picks_and_excludes_their_events():
    plan = mode.publish_plan("publish", PICKS)
    assert plan["publish"] == PICKS
    assert plan["botd_exclude"] == {"arsenal|chelsea|1793000400", "lyon|nice|1793007600"}
    assert plan["no_pick_message"] is False


def test_switching_back_to_shadow_restores_exactly_the_shadow_plan():
    before = mode.publish_plan("shadow", PICKS)
    during = mode.publish_plan("publish", PICKS)
    after = mode.publish_plan("shadow", PICKS)
    assert during != before
    assert after == before


def test_no_pick_message_only_in_publish_mode_with_an_empty_selection():
    assert mode.publish_plan("publish", [])["no_pick_message"] is True
    assert mode.publish_plan("shadow", [])["no_pick_message"] is False


@pytest.mark.parametrize("value", ["", None, " Shadow ", "SHADOW"])
def test_shadow_spellings_are_accepted(value):
    assert mode.parse_mode(value) == "shadow"


@pytest.mark.parametrize("value", ["on", "live", "publish!", "true"])
def test_unknown_values_are_refused(value):
    with pytest.raises(ValueError):
        mode.parse_mode(value)


def test_config_refuses_an_unknown_mode(monkeypatch):
    monkeypatch.setenv("PRO_VALUE_MODE", "live")
    with pytest.raises(ValueError):
        Config()


def test_config_accepts_publish(monkeypatch):
    monkeypatch.setenv("PRO_VALUE_MODE", "publish")
    assert Config().pro_value_mode == "publish"
