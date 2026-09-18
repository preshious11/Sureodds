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


def event_list_payload(commence="2030-06-01T19:00:00Z", sport_key="soccer_epl"):
    # GET /sports/{key}/events (free): bare event rows, no odds.
    return [{"id": "evt-1", "sport_key": sport_key, "commence_time": commence,
             "home_team": "Arsenal", "away_team": "Chelsea"}]


def empty_events():
    return []


def make_provider(budget, responses, max_leagues_per_sport=8, reserve_credits=50):
    p = OddsApiProvider("k", budget, max_leagues_per_sport=max_leagues_per_sport,
                        reserve_credits=reserve_credits)
    p.http = Mock()
    p.http.get = Mock(side_effect=responses)
    # The Mock http never ran a real call, so no response headers exist: the
    # quota gate must then fall back to the local monthly counter (allowed).
    p.http.last_headers = {}
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
        event_list_payload(),   # soccer_epl events (free)
        empty_events(),          # tennis events (free, none)
        odds_payload(),          # football league odds (spends 3 credits)
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


def test_fetch_events_drops_events_outside_window_without_spending_credits(budget):
    p = make_provider(budget, [
        sports_payload(),
        event_list_payload(commence="2030-06-05T19:00:00Z"),  # far outside window
        empty_events(),
    ])
    events = p.fetch_events(dt(), dt(hh=23))
    assert events == []
    # Only the free endpoints were called: /sports and per-key /events.
    assert p.http.get.call_count == 3
    assert p.budget.used_month("oddsapi") == 0


def test_sport_keys_prioritized_but_not_capped_at_discovery(budget):
    payload = [{"key": "soccer_league_of_ireland"}, {"key": "soccer_epl"},
               {"key": "soccer_spain_la_liga"}, {"key": "soccer_china_superleague"}]
    p = make_provider(budget, [payload])
    discovered = p._discover_sports()
    # EPL/La Liga come first via FOOTBALL_LEAGUE_PRIORITY; the credit cap (not
    # a discovery cap) decides how many of them are actually fetched.
    assert discovered["football"] == ["soccer_epl", "soccer_spain_la_liga",
                                      "soccer_china_superleague", "soccer_league_of_ireland"]


def test_fetch_events_accepts_any_bookmaker_by_default(budget):
    payload = odds_payload()
    payload[0]["bookmakers"][0]["key"] = "winamax_de"  # not in any hardcoded allowlist
    p = make_provider(budget, [sports_payload(), event_list_payload(), empty_events(), payload])
    events = p.fetch_events(dt(), dt(hh=23))
    assert events and events[0].markets


def test_credit_cap_limits_keys_fetched_per_cycle(budget):
    """Every key has in-window events, but the per-cycle credit cap (15 at
    3 credits/key) means only 5 odds requests are ever made."""
    many = [{"key": f"soccer_x{i}", "title": f"X{i}", "group": "Soccer"}
            for i in range(8)]
    upcoming = [{"id": f"e{i}", "sport_key": f"soccer_x{i}",
                 "commence_time": "2030-06-01T19:00:00Z",
                 "home_team": f"H{i}", "away_team": f"A{i}"} for i in range(8)]
    p = OddsApiProvider("k", budget, max_credits_per_cycle=15)   # 15 // 3 = 5 keys
    p.http = Mock()
    p.http.last_headers = {}   # no real headers -> gate falls back to the local counter
    p.http.get = Mock(side_effect=[many]
                      + [[{"id": f"e{i}", "sport_key": f"soccer_x{i}",
                           "commence_time": "2030-06-01T19:00:00Z",
                           "home_team": f"H{i}", "away_team": f"A{i}"}] for i in range(8)]
                      + [odds_payload()] * 8)
    p.fetch_events(dt(), dt(hh=23))
    odds_calls = p.http.get.call_count - 9   # minus /sports and 8 free /events calls
    assert odds_calls == 5
    assert p.budget.used_month("oddsapi") == 15


def test_quota_gate_refuses_when_header_remaining_is_below_reserve(budget):
    """The header is authoritative: x-requests-remaining=10 with reserve=50
    refuses every odds request and spends nothing."""
    p = make_provider(budget, [
        sports_payload(),
        [{"id": "e1", "sport_key": "soccer_epl", "commence_time": "2030-06-01T19:00:00Z",
          "home_team": "Arsenal", "away_team": "Chelsea"}],
        [{"id": "t1", "sport_key": "tennis_atp_us_open", "commence_time": "2030-06-01T20:00:00Z",
          "home_team": "Alcaraz", "away_team": "Sinner"}],
    ])
    p.track_calls = []
    orig = OddsApiProvider._track_quota

    def fake_track(self, path=""):
        self.track_calls.append(path)
        self.requests_remaining = 10
        self.requests_used = 490

    OddsApiProvider._track_quota = fake_track
    try:
        events = p.fetch_events(dt(), dt(hh=23))
    finally:
        OddsApiProvider._track_quota = orig
    assert events == []
    assert p.http.get.call_count == 3          # /sports + 2 free /events only
    assert p.budget.used_month("oddsapi") == 0
    assert p.can_spend_credits(3) is False


def test_quota_gate_allows_spend_when_headers_have_headroom(budget):
    """x-requests-remaining=100 with reserve=50 allows the 3-credit fetch."""
    p = make_provider(budget, [
        sports_payload(),
        [{"id": "e1", "sport_key": "soccer_epl", "commence_time": "2030-06-01T19:00:00Z",
          "home_team": "Arsenal", "away_team": "Chelsea"}],
        [],
        odds_payload(),
    ], reserve_credits=50)
    orig = OddsApiProvider._track_quota
    OddsApiProvider._track_quota = lambda self, path="": setattr(self, "requests_remaining", 100)
    try:
        events = p.fetch_events(dt(), dt(hh=23))
    finally:
        OddsApiProvider._track_quota = orig
    assert len(events) == 1
    assert p.budget.used_month("oddsapi") == 3


def test_zero_event_keys_never_cost_credits(budget):
    p = make_provider(budget, [
        sports_payload(),
        empty_events(),          # soccer_epl: no in-window events -> no odds request
        event_list_payload(commence="2030-06-01T20:00:00Z", sport_key="tennis_atp_us_open"),
        odds_payload(),          # tennis odds fetch (3 credits)
    ])
    events = p.fetch_events(dt(), dt(hh=23))
    # football key had no in-window events -> no odds request for it
    assert p.http.get.call_count == 4
    assert p.budget.used_month("oddsapi") == 3


def test_fetch_result_and_find_result_return_none(budget):
    p = OddsApiProvider("k", budget)
    assert p.fetch_result("evt-1") is None
    assert p.find_result("football", "Arsenal", "Chelsea", dt()) is None


def test_enabled_requires_key(budget):
    assert OddsApiProvider("", budget).enabled() is False
    assert OddsApiProvider("k", budget).enabled() is True


# ------------------------------------------------------- Top-picks priority

def oddsapi_football_event(price=1.60, i=1):
    # Genuinely distinct club names: same_team() fuzzily matches names that
    # differ only by a trailing digit, and choose_top now refuses two legs
    # from the same real fixture even across providers.
    clubs = [("Real Madrid", "Valencia"), ("Ajax", "Feyenoord"), ("Porto", "Braga")]
    home, away = clubs[(i - 1) % len(clubs)]
    e = Event(provider="oddsapi", provider_event_id=f"oa{i}", sport="football", league="L",
              home=home, away=away, start=dt(hh=22), sources=["oddsapi"])
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
    # Real fixture builds go through ProviderChain.collect() -> merge_events(),
    # unlike the picker-only tests above. same_team() fuzzily matches names
    # that differ only by a trailing digit (e.g. "OddsHome1"/"OddsHome2"), so
    # this second event needs a genuinely distinct club name or merge_events()
    # folds both legs into one fixture before scoring ever sees two.
    oa_event2 = oddsapi_football_event(price=1.45, i=2)
    oa_event2.home, oa_event2.away = "Sporting", "Feyenoord"
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
