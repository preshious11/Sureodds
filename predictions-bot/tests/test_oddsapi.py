"""Odds API provider: football/tennis-only discovery, normalization, priority fallback."""
from __future__ import annotations

from unittest.mock import Mock

from predbot.config import Config
from predbot.jobs.daily_picks import build_picks
from predbot.providers.base import Event, MarketLine
from predbot.providers.chain import ProviderChain
from predbot.providers.oddsapi import OddsApiProvider, sport_key_to_canonical
from predbot.selection.picker import select
from tests.conftest import dt
from tests.test_picker import ev, sc


def sports_payload():
    return [
        {"key": "soccer_epl", "title": "EPL", "group": "Soccer"},
        {"key": "tennis_atp_us_open", "title": "ATP US Open", "group": "Tennis"},
        {"key": "basketball_nba", "title": "NBA", "group": "Basketball"},
        {"key": "americanfootball_nfl", "title": "NFL", "group": "American Football"},
    ]


def odds_payload(sport_title="EPL", commence="2030-06-01T19:00:00Z"):
    # The Odds API returns a bare JSON array from /sports/{key}/odds, and names
    # h2h/spreads outcomes by the actual team name, not "home"/"away" -
    # Arsenal is home_team here, so its outcome name is "Arsenal".
    return [{
        "id": "evt-1", "sport_key": "soccer_epl", "sport_title": sport_title,
        "commence_time": commence, "home_team": "Arsenal", "away_team": "Chelsea",
        "bookmakers": [{
            "key": "pinnacle", "title": "Pinnacle",
            "markets": [
                {"key": "h2h", "outcomes": [
                    {"name": "Arsenal", "price": 2.10}, {"name": "Chelsea", "price": 3.40},
                    {"name": "Draw", "price": 3.30}]},
                {"key": "spreads", "outcomes": [
                    {"name": "Arsenal", "price": 1.85, "point": -1.5},
                    {"name": "Chelsea", "price": 1.95, "point": 1.5}]},
                {"key": "totals", "outcomes": [
                    {"name": "Over", "price": 1.90, "point": 2.5},
                    {"name": "Under", "price": 1.95, "point": 2.5}]},
            ],
        }],
    }]


def make_provider(budget, responses, max_leagues_per_sport=8):
    p = OddsApiProvider("k", budget, max_leagues_per_sport=max_leagues_per_sport)
    p.http = Mock()
    p.http.get = Mock(side_effect=responses)
    return p


def test_sport_key_mapping_covers_football_and_tennis_only():
    assert sport_key_to_canonical("soccer_epl") == "football"
    assert sport_key_to_canonical("tennis_atp_us_open") == "tennis"
    assert sport_key_to_canonical("basketball_nba") == "other"
    assert sport_key_to_canonical("americanfootball_nfl") == "other"


def test_discover_sports_drops_non_football_tennis(budget):
    p = make_provider(budget, [sports_payload()])
    discovered = p._discover_sports()
    assert set(discovered.keys()) == {"football", "tennis"}
    assert discovered["football"] == ["soccer_epl"]
    assert discovered["tennis"] == ["tennis_atp_us_open"]


def test_fetch_events_normalizes_h2h_totals_and_spreads_into_market_lines(budget):
    p = make_provider(budget, [
        sports_payload(),
        odds_payload(),   # football league feed
        [],                # tennis league feed, empty
    ])
    events = p.fetch_events(dt(), dt(hh=23))
    assert len(events) == 1
    e = events[0]
    assert e.sport == "football"
    assert e.home == "Arsenal" and e.away == "Chelsea"
    assert e.provider == "oddsapi"
    markets = {(m.market, m.selection, m.line): m.price for m in e.markets}
    assert markets[("result3", "home", None)] == 2.10
    assert markets[("result3", "away", None)] == 3.40
    assert markets[("result3", "draw", None)] == 3.30
    assert markets[("handicap", "home", -1.5)] == 1.85
    assert markets[("handicap", "away", 1.5)] == 1.95
    assert markets[("total", "over", 2.5)] == 1.90
    assert markets[("total", "under", 2.5)] == 1.95


def test_fetch_events_drops_events_outside_window(budget):
    p = make_provider(budget, [
        sports_payload(),
        odds_payload(commence="2030-06-05T19:00:00Z"),  # far outside window
        [],
    ])
    events = p.fetch_events(dt(), dt(hh=23))
    assert events == []


def test_discover_sports_prioritizes_major_leagues_and_caps_count(budget):
    payload = [{"key": "soccer_league_of_ireland"}, {"key": "soccer_epl"},
               {"key": "soccer_spain_la_liga"}, {"key": "soccer_china_superleague"}]
    p = make_provider(budget, [payload], max_leagues_per_sport=2)
    discovered = p._discover_sports()
    # EPL/La Liga are in FOOTBALL_LEAGUE_PRIORITY, so they win the 2-league cap
    # even though the unranked leagues appeared first in the raw feed.
    assert discovered["football"] == ["soccer_epl", "soccer_spain_la_liga"]


def test_fetch_events_accepts_any_bookmaker_by_default(budget):
    payload = odds_payload()
    payload[0]["bookmakers"][0]["key"] = "winamax_de"  # not in any hardcoded allowlist
    p = make_provider(budget, [sports_payload(), payload, []])
    events = p.fetch_events(dt(), dt(hh=23))
    assert events and events[0].markets


def test_fetch_result_and_find_result_return_none(budget):
    p = OddsApiProvider("k", budget)
    assert p.fetch_result("evt-1") is None
    assert p.find_result("football", "Arsenal", "Chelsea", dt()) is None


def test_enabled_requires_key(budget):
    assert OddsApiProvider("", budget).enabled() is False
    assert OddsApiProvider("k", budget).enabled() is True


# ------------------------------------------------------- Top-picks priority

def oddsapi_football_event(price=1.60, i=1):
    e = Event(provider="oddsapi", provider_event_id=f"oa{i}", sport="football", league="L",
              home=f"OddsHome{i}", away=f"OddsAway{i}", start=dt(hh=22), sources=["oddsapi"])
    e.markets = [MarketLine("result3", "home", price)]
    return e


def test_top_priority_uses_oddsapi_pool_when_it_reaches_target():
    oa_event = oddsapi_football_event(price=1.45)
    other_event = ev(2)
    pool = [
        sc(oa_event, "result3", "home", 0.75, 1.45, sources=2),
        sc(other_event, "result3", "home", 0.70, 1.45, sources=2),
    ]
    # combined 1.45*1.45 = 2.1 >= target 2.0, priority pool alone should suffice
    # but a single oddsapi pick alone (1.45) doesn't reach 2.0, so add a second oddsapi event
    oa_event2 = oddsapi_football_event(price=1.50, i=2)
    pool.append(sc(oa_event2, "result3", "home", 0.75, 1.50, sources=2))
    result = select(pool, Config(), dt(), top_priority_source="oddsapi")
    assert result.top
    assert all("oddsapi" in s.event.sources for s in result.top)
    assert result.diagnostics["top_priority_used"] is True


def test_top_priority_falls_back_to_other_providers_when_oddsapi_insufficient():
    # Only one weak oddsapi candidate: cannot reach the 2.00 target alone.
    oa_event = oddsapi_football_event(price=1.40)
    other_event = ev(2)
    pool = [
        sc(oa_event, "result3", "home", 0.75, 1.40, sources=2),
        sc(other_event, "result3", "home", 0.75, 1.50, sources=2),
    ]
    result = select(pool, Config(), dt(), top_priority_source="oddsapi")
    assert result.top
    assert result.diagnostics["top_priority_used"] is False
    # fallback pool includes the non-oddsapi event
    assert any("oddsapi" not in s.event.sources for s in result.top)


def test_oddsapi_only_events_excluded_from_daily_ticket():
    oa_event = oddsapi_football_event(price=1.20)
    pool = [sc(oa_event, "result3", "home", 0.40, 1.20, sources=2, ev_val=0.06)]
    result = select(pool, Config(), dt(), top_priority_source="oddsapi")
    assert result.daily == []
    assert result.diagnostics["daily_pool_rejections"].get("RESERVED_FOR_TOP_PRIORITY_SOURCE") == 1


def test_build_picks_tries_oddsapi_chain_first_for_top(db, monkeypatch):
    now = dt()
    oa_event = oddsapi_football_event(price=1.50, i=1)
    oa_event2 = oddsapi_football_event(price=1.45, i=2)
    api_football_event = ev(3)

    oddsapi_provider = Mock()
    oddsapi_provider.enabled.return_value = True
    oddsapi_provider.fetch_events.return_value = [oa_event, oa_event2]

    daily_provider = Mock()
    daily_provider.enabled.return_value = True
    daily_provider.fetch_events.return_value = [api_football_event]

    chain = ProviderChain({"api_football": daily_provider, "oddsapi": oddsapi_provider},
                          ["api_football"], 1)
    chain.db, chain.cfg = None, None

    def fake_score(events, db_, cfg_):
        out = []
        for e in events:
            if e.provider == "oddsapi":
                out.append(sc(e, "result3", "home", 0.75, e.markets[0].price, sources=2))
            else:
                out.append(sc(e, "result3", "home", 0.60, 1.50, sources=2))
        return out

    monkeypatch.setattr("predbot.jobs.daily_picks.score_events", fake_score)
    cfg = Config(timezone="UTC", top_provider_priority=["oddsapi"])
    result = build_picks(chain, db, cfg, now)
    assert result.top
    assert all(s.event.provider == "oddsapi" for s in result.top)
    oddsapi_provider.fetch_events.assert_called_once()
    daily_provider.fetch_events.assert_called_once()
