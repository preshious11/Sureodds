"""Phase 2c-2f: sport gate, hockey/handball removal, SharpAPI explicit leagues,
SportsGameOdds filtering and the disabled-market list."""
from __future__ import annotations

import logging
import os
from unittest.mock import Mock

import pytest

from predbot.config import Config
from predbot.providers.budget import Budget
from predbot.providers.chain import ProviderChain
from tests.conftest import dt
from tests.test_picker import ev, sc


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    monkeypatch.setattr(os, "environ", os.environ.copy())
    for n in ("ALLOWED_SPORTS", "DISABLED_MARKETS", "SHARPAPI_LEAGUES", "SHARPAPI_SPORTS", "SHARPAPI_MAX_PAGES",
              "MAX_PLAYER_PROPS"):
        monkeypatch.delenv(n, raising=False)


# =========================================================== 2c: ALLOWED_SPORTS gate
def _mixed_events():
    from predbot.providers.base import Event
    mk = lambda i, sport: Event(provider="p", provider_event_id=str(i), sport=sport, league="England - Premier League" if sport == "football" else f"L{i}",
                                home=f"Alpha{i}x", away=f"Beta{i}y", start=dt(hh=20), sources=["p"])
    return [mk(1, "football"), mk(2, "hockey"), mk(3, "handball")]


def test_sport_gate_runs_before_merging_and_is_counted_in_the_provider_report():
    prov = Mock()
    prov.enabled.return_value = True
    prov.fetch_events.return_value = _mixed_events()
    chain = ProviderChain({"p": prov}, ["p"], 1)
    chain.cfg = Config()

    pool = chain.collect(dt(hh=12), dt(hh=23))

    assert [e.sport for e in pool] == ["football"]
    assert chain.last_report["p"]["sport_rejected"] == 2
    assert chain.last_report["p"]["events"] == {"football": 1}


def test_sport_gate_also_applies_on_the_persisted_cache_path(db, monkeypatch):
    from predbot.cache import providers as cache_providers
    monkeypatch.setattr(cache_providers, "utcnow", lambda: dt(hh=12))
    class Stub:
        sports = ["football"]
        fetch_events = Mock(return_value=_mixed_events())

        def enabled(self):
            return True

    chain = ProviderChain({"p": Stub()}, ["p"], 1)
    chain.db, chain.cfg = db, Config(timezone="UTC")

    pool = chain.collect(dt(hh=12), dt(hh=23))

    assert [e.sport for e in pool] == ["football"]
    assert chain.last_report["p"]["sport_rejected"] == 2


def test_hockey_and_handball_are_gone_from_every_sport_table():
    from predbot.bot import format as fmt
    from predbot.markets import catalog
    from predbot.model import elo, ensemble, poisson
    from predbot.providers import sharpapi, sportsgameodds
    from predbot.util import duration
    assert "HOCKEY" not in sportsgameodds.SPORT_MAP and "HANDBALL" not in sportsgameodds.SPORT_MAP
    assert "hockey" not in sharpapi.SPORT_MAP.values() and "nhl" not in sharpapi.LEAGUE_SPORTS
    for table in (elo.K_FACTOR, elo.HOME_ADV, elo.BASE_DRAW, poisson.MAX_GOALS, poisson.DEFAULT_RHO,
                  ensemble.SCORE_MODEL_WEIGHT, duration.DURATION_H, fmt.SPORT_ICON, fmt.SPORT_LABEL,
                  catalog.SPORT_POINTS_WORD):
        assert "hockey" not in table and "handball" not in table
    assert not ({"hockey", "handball"} & ensemble.GRID_SPORTS)
    for period in ("1p", "2p", "3p"):
        assert period not in catalog.PERIOD_NAMES and period not in poisson.PERIOD_SCALE
        assert period not in sportsgameodds.PERIOD_MAP and period not in sharpapi.SEGMENT_PERIOD.values()


# ========================================================= 2d: SharpAPI explicit leagues
def test_every_default_sharpapi_league_maps_to_a_sport():
    from predbot.providers.sharpapi import sport_for_league
    expected = {"atp": "tennis", "wta": "tennis", "nba": "basketball", "nfl": "american_football",
                "mlb": "baseball", "euroleague": "basketball"}
    for slug in Config().sharpapi_leagues:
        assert sport_for_league(slug) == expected.get(slug, "football"), slug


def test_soccer_hint_words_do_not_capture_other_sports():
    from predbot.providers.sharpapi import sport_for_league
    assert sport_for_league("table_tennis_-_tt_league") == "other"
    assert sport_for_league("league_of_legends") == "esports"
    assert sport_for_league("scotland_-_premiership") == "football"


def test_sharpapi_never_auto_expands_sports_and_warns_about_sport_feeds(db, caplog):
    from predbot.providers.sharpapi import SharpApiProvider
    p = SharpApiProvider("k", Budget(db), leagues=["nba"], sports=["auto"])
    p.sport_catalog = Mock(side_effect=AssertionError("auto-discovery must not run"))
    with caplog.at_level(logging.WARNING):
        assert p._targets() == [{"league": "nba"}]
    assert any("auto" in r.message for r in caplog.records)

    caplog.clear()
    p2 = SharpApiProvider("k", Budget(db), leagues=["nba"], sports=["soccer"])
    with caplog.at_level(logging.WARNING):
        assert p2._targets() == [{"league": "nba"}, {"sport": "soccer"}]
    assert any("sport-level" in r.message for r in caplog.records)


def test_sharpapi_defaults_are_explicit_leagues_with_a_six_page_cap():
    cfg = Config()
    assert cfg.sharpapi_sports == [] and cfg.sharpapi_max_pages == 6 and len(cfg.sharpapi_leagues) == 22


# ================================================================ 2e: SportsGameOdds
class _Http:
    def __init__(self, leagues):
        self.calls, self.leagues = [], leagues

    def get(self, path, params=None):
        self.calls.append((path, params))
        if path == "/leagues":
            return {"data": [{"leagueID": lg, "sportID": s} for lg, s in self.leagues.items()]}
        return {"success": True, "data": []}


def test_sgo_with_no_sport_filter_fetches_every_configured_league(db):
    """Regression: an empty SGO_SPORTS used to filter every league out."""
    from predbot.providers.sportsgameodds import SportsGameOddsProvider
    p = SportsGameOddsProvider("k", Budget(db), sports=[], leagues=["NBA", "MLB", "MLS"], event_limit=50)
    p.http = _Http({"NBA": "BASKETBALL", "MLB": "BASEBALL", "MLS": "SOCCER"})

    p.fetch_events(dt(hh=12), dt(hh=23))

    fetched = [c[1]["leagueID"] for c in p.http.calls if c[0] == "/events"]
    assert fetched == ["NBA", "MLB", "MLS"]


def test_sgo_sport_filter_still_applies_when_configured(db):
    from predbot.providers.sportsgameodds import SportsGameOddsProvider
    p = SportsGameOddsProvider("k", Budget(db), sports=["BASKETBALL"], leagues=["NBA", "MLB"], event_limit=50)
    p.http = _Http({"NBA": "BASKETBALL", "MLB": "BASEBALL"})

    p.fetch_events(dt(hh=12), dt(hh=23))

    assert [c[1]["leagueID"] for c in p.http.calls if c[0] == "/events"] == ["NBA"]


def _odd(stat="points", period="game", bet="ml", side="home", entity="home", odds="-120"):
    return {"statID": stat, "periodID": period, "betTypeID": bet, "sideID": side, "statEntityID": entity,
            "bookOdds": odds, "bookOddsAvailable": True}


def test_sgo_rejects_unsupported_stats_and_periods_with_counts(db):
    from predbot.providers.sportsgameodds import SportsGameOddsProvider
    p = SportsGameOddsProvider("k", Budget(db), sports=[], leagues=[])
    good = p._parse_odd("a", _odd(), "basketball", {})
    inning = p._parse_odd("b", _odd(stat="batting_hits", period="1i"), "baseball", {})
    strikeouts = p._parse_odd("c", _odd(stat="pitching_strikeouts", period="game"), "baseball", {})
    quarter = p._parse_odd("d", _odd(period="3q"), "basketball", {})

    assert good is not None and inning is None and strikeouts is None and quarter is None
    assert p.parse_counts["UNSUPPORTED_STAT"] == 2      # the inning odd fails on stat first
    assert p.parse_counts["UNSUPPORTED_PERIOD"] == 1


def test_sgo_allowed_periods_are_configurable(db):
    from predbot.providers.sportsgameodds import SportsGameOddsProvider
    p = SportsGameOddsProvider("k", Budget(db), sports=[], leagues=[], allowed_periods=["game", "3q"])
    assert p._parse_odd("d", _odd(period="3q"), "basketball", {}) is not None


def test_player_props_are_dropped_by_default_and_capped_when_allowed(db):
    from predbot.providers.sportsgameodds import SportsGameOddsProvider

    def props_event(n):
        odds = {f"o{i}": _odd(stat="points", bet="ou", side="over", entity=f"PLAYER_{i}") for i in range(n)}
        for o in odds.values():
            o["bookOverUnder"] = "20.5"
        return {"eventID": "E1", "sportID": "BASKETBALL", "leagueID": "NBA",
                "teams": {"home": {"names": {"long": "H"}}, "away": {"names": {"long": "A"}}},
                "status": {"startsAt": "2030-06-01T20:00:00Z"}, "odds": {"m": _odd(), **odds},
                "players": {}}

    default = SportsGameOddsProvider("k", Budget(db), sports=[], leagues=[])
    e = default._parse_event(props_event(3))
    assert e is not None and not any(m.is_player_prop for m in e.markets)

    capped = SportsGameOddsProvider("k", Budget(db), sports=[], leagues=[], max_player_props=2)
    e = capped._parse_event(props_event(3))
    assert sum(m.is_player_prop for m in e.markets) <= 2


# ============================================================= 2f: DISABLED_MARKETS
def test_disabled_markets_stay_predictions_and_are_only_blocked_from_promotion():
    from dataclasses import replace
    from tests.pipeline_helpers import select
    pool = [sc(ev(1), "btts", "yes", 0.75, 1.80), sc(ev(2), "dnb", "home", 0.75, 1.80),
            sc(ev(3), "total", "over", 0.75, 1.80, line=2.5), sc(ev(4), "result3", "home", 0.75, 1.80)]

    ps = select(pool, replace(Config(), disabled_markets=["btts", "dnb", "total"]), dt())

    # DISABLED_MARKETS is a product-selection choice (these markets ARE model-priced and settleable), so the
    # prediction engine keeps them; only the Top/Daily selectors refuse to promote them.
    assert "disabled_market" not in ps.diagnostics["line_rejections"]
    assert {s.line.market for s in ps.all()} == {"btts", "dnb", "total", "result3"}
    assert ps.diagnostics["top"]["rejections"]["disabled_market"] == 3
    assert ps.diagnostics["daily"]["rejections"]["disabled_market"] == 3
    assert all(s.line.market == "result3" for s in ps.top + ps.daily)


def test_disabled_markets_can_be_emptied_from_the_config():
    from tests.pipeline_helpers import select
    ps = select([sc(ev(1), "btts", "yes", 0.75, 1.80)], Config(disabled_markets=[]), dt())
    assert "disabled_market" not in ps.diagnostics["top"]["rejections"]
    assert "disabled_market" not in ps.diagnostics["daily"]["rejections"]
    assert [s.line.market for s in ps.all()] == ["btts"]
