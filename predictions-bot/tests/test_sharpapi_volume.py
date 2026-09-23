"""Volume: server-side main-line filtering, exact market names, per-generation request budgeting,
priority order and the stop conditions - measured against payloads recorded from the live API."""
from __future__ import annotations

from datetime import timedelta

import pytest

from predbot.config import Config
from predbot.providers.chain import build_providers
from predbot.providers.sharpapi import SharpApiProvider
from tests.conftest import dt, load


class _Http:
    """Serves ``pages`` in order; every call is recorded (params include the cursor)."""

    def __init__(self, pages):
        self.pages, self.calls = list(pages), []

    def get(self, path, params=None):
        self.calls.append(dict(params or {}))
        page = self.pages[min(len(self.calls) - 1, len(self.pages) - 1)]
        return page


def row(eid, start, away="Away", market="moneyline", sel="home"):
    return {"event_id": eid, "sport": "soccer", "league": "x", "home_team": "Home", "away_team": away,
            "market_type": market, "selection_type": sel, "odds_decimal": 2.0, "is_active": True,
            "event_start_time": start.strftime("%Y-%m-%dT%H:%MZ"), "sportsbook": "draftkings"}


def page(rows, more=True):
    return {"data": rows, "pagination": {"limit": 200, "count": len(rows), "has_more": more,
                                          "next_cursor": "c" if more else None}}


START, END = dt(hh=8), dt(hh=23)


def _provider(pages, leagues=("l1",), budget=None, **kw):
    p = SharpApiProvider("k", budget or _Nothing(), leagues=list(leagues), min_interval=0, **kw)
    p.http = _Http(pages)
    return p


class _Nothing:
    """A Budget stand-in for tests that do not exercise Budget."""
    db = None

    def spend(self, *a, **k):
        pass

    def monthly_daily_allowance(self, *a, **k):
        return 10 ** 6


# ------------------------------------------------------------------ what is requested
def test_football_asks_for_exact_market_names_and_main_lines_only():
    p = _provider([page([], False)], leagues=["england_-_premier_league"], main_lines_only=True,
                  sport_markets={"football": ["moneyline", "point_spread", "total_goals"]})
    p.fetch_events(START, END)
    params = p.http.calls[0]
    assert params["market"] == "moneyline,point_spread,total_goals" and params["is_main_line"] == "true"
    assert params["limit"] == 200 and params["is_live"] == "false"


def test_a_sport_without_an_exact_list_uses_the_aliases_and_is_still_main_line_only():
    p = _provider([page([], False)], leagues=["nba"], main_lines_only=True,
                  sport_markets={"football": ["moneyline"]}, markets=["moneyline", "spread", "total"])
    p.fetch_events(START, END)
    assert p.http.calls[0]["market"] == "moneyline,spread,total" and p.http.calls[0]["is_main_line"] == "true"


def test_the_old_request_shape_is_one_switch_away():
    p = _provider([page([], False)], leagues=["england_-_premier_league"], main_lines_only=False)
    p.fetch_events(START, END)
    assert "is_main_line" not in p.http.calls[0] and p.http.calls[0]["market"] == "moneyline,spread,total"


def test_the_defaults_are_wired_from_config():
    cfg = Config(sharpapi_key="k", odds_api_key="", sgo_key="")
    p = build_providers(cfg, _Nothing())["sharpapi"]
    assert p.main_lines_only and p.sport_markets["football"] == [
        "moneyline", "point_spread", "total_goals", "double_chance", "draw_no_bet", "both_teams_to_score",
        "team_total_goals"]
    assert p.sport_markets["baseball"] == ["moneyline", "run_line", "total_runs"]
    assert p.monthly_limit == 3000 and p.max_requests_per_generation == 60
    assert p.league_priority[0] == "england_-_premier_league" and "uefa_-_champions_league" in p.league_priority


# ------------------------------------------------------------------ priority
def test_priority_leagues_are_fetched_first_in_priority_order_and_the_rest_keep_their_order():
    p = _provider([page([], False)], leagues=["a", "spain_-_la_liga", "b", "england_-_premier_league"],
                  league_priority=["england_-_premier_league", "spain_-_la_liga", "not_configured"])
    assert [t["league"] for t in p._targets()] == ["england_-_premier_league", "spain_-_la_liga", "a", "b"]


# ------------------------------------------------------------------ request budget
def test_no_feed_is_starved_and_the_total_never_exceeds_the_budget():
    hungry = page([row("e1", START + timedelta(hours=1))], True)
    p = _provider([hungry], leagues=["big", "l2", "l3", "l4"], max_pages=10, max_requests_per_generation=6,
                  league_priority=["big"])
    p.fetch_events(START, END)
    per_feed = {c["requests"] for c in p.fetch_counts.values()}
    assert p.fetch_counts["big"]["requests"] == 3          # 6 - 3 feeds still owed a page
    assert all(p.fetch_counts[k]["requests"] >= 1 for k in ("l2", "l3", "l4"))
    assert len(p.http.calls) <= 6 and p.requests_spent == len(p.http.calls) and p.requests_budget == 6
    assert per_feed


def test_a_feed_with_no_budget_left_makes_no_request_and_says_why():
    hungry = page([row("e1", START + timedelta(hours=1))], True)
    p = _provider([hungry], leagues=["a", "b", "c"], max_pages=10, max_requests_per_generation=2)
    p.fetch_events(START, END)
    assert len(p.http.calls) == 2                                           # never a third request
    assert p.fetch_counts["c"]["requests"] == 0 and p.fetch_counts["c"]["stop_reason"] == "budget"
    assert p.fetch_counts["c"]["capped"] is True and p.last_fetch_complete is False


def test_a_zero_budget_fetches_nothing():
    p = _provider([page([], False)], leagues=["a"], max_requests_per_generation=0)
    assert p.fetch_events(START, END) == [] and p.http.calls == []


def test_the_budget_follows_the_monthly_limit_and_what_is_already_spent(db):
    from predbot.providers.budget import Budget
    b = Budget(db)
    p = _provider([page([], False)], budget=b, monthly_limit=1000, max_requests_per_generation=500)
    full = p.request_budget()
    for _ in range(900):
        b.spend("sharpapi", 1)
    assert p.request_budget() < full and p.request_budget() <= (1000 - 900)
    assert _provider([page([], False)], budget=b).request_budget() is None          # unbudgeted by default


# ------------------------------------------------------------------ stop conditions
def test_paging_stops_when_a_page_reaches_past_the_window_end_without_another_request():
    """The feed is ordered by kick-off: a page whose LAST match is past the end leaves nothing for later pages."""
    rows = [row("e1", START + timedelta(hours=1)), row("e2", END + timedelta(days=3))]
    p = _provider([page(rows, True), page([row("e3", END + timedelta(days=4))], False)], max_pages=5)
    p.fetch_events(START, END)
    assert len(p.http.calls) == 1 and p.fetch_counts["l1"]["stop_reason"] == "window_end"
    assert p.fetch_counts["l1"]["capped"] is False and p.last_fetch_complete is True


def test_paging_continues_while_the_page_is_still_inside_the_window():
    inside = [row("e1", START + timedelta(hours=1)), row("e2", START + timedelta(hours=2))]
    p = _provider([page(inside, True), page([row("e3", START + timedelta(hours=3))], False)], max_pages=5)
    p.fetch_events(START, END)
    assert len(p.http.calls) == 2 and p.fetch_counts["l1"]["stop_reason"] == "no_more_data"
    assert p.http.calls[1]["cursor"] == "c"


def test_events_per_target_stops_the_feed():
    rows = [row(f"e{i}", START + timedelta(hours=1)) for i in range(4)]
    p = _provider([page(rows, True)], events_per_target=3, max_pages=5)
    p.fetch_events(START, END)
    assert len(p.http.calls) == 1 and p.fetch_counts["l1"]["stop_reason"] == "events_per_target"


def test_the_page_ceiling_marks_the_feed_capped_and_partial():
    p = _provider([page([row("e1", START + timedelta(hours=1))], True)], leagues=["england_-_premier_league"], max_pages=2)
    p.fetch_events(START, END)
    c = p.fetch_counts["england_-_premier_league"]
    assert len(p.http.calls) == 2 and c["stop_reason"] == "max_pages" and c["capped"] is True
    assert p.sport_status["football"]["status"] == "partial"


def test_a_league_with_nothing_in_the_window_costs_one_request():
    p = _provider([page([row("e1", END + timedelta(days=9))], True)], max_pages=5)
    p.fetch_events(START, END)
    assert len(p.http.calls) == 1 and p.fetch_counts["l1"]["matches_in_window"] == 0


# ------------------------------------------------------------------ instrumentation
def test_fetch_counts_carry_the_volume_numbers():
    rows = [row("e1", START + timedelta(hours=1), market=m) for m in ("moneyline", "total_goals", "point_spread")]
    rows += [row("e2", START + timedelta(hours=2)), row("e3", END + timedelta(hours=5))]
    p = _provider([page(rows, False)])
    p.fetch_events(START, END)
    c = p.fetch_counts["l1"]
    assert c["requests"] == 1 and c["rows"] == 5 and c["matches_seen"] == 3 and c["matches_in_window"] == 2
    assert c["rows_per_match"] == pytest.approx(5 / 3, abs=0.05) and c["stop_reason"] == "no_more_data"
    assert c["capped"] is False


def test_lastrun_shows_the_feeds_and_the_request_budget():
    from predbot.bot.lastrun import _feed_lines
    providers = {"sharpapi": {"requests": {"spent": 22, "budget": 60},
                              "fetch": {"england_-_premier_league": {"requests": 3, "rows": 600, "matches_seen": 21,
                                                                     "matches_in_window": 9, "rows_per_match": 28.6,
                                                                     "capped": True, "stop_reason": "max_pages"}}}}
    text = "\n".join(_feed_lines(providers))
    assert "22" in text and "60" in text and "england_-_premier_league" in text
    assert "9/21" in text and "28.6" in text and "max_pages" in text and "capped" in text
    assert _feed_lines({}) == []


# ------------------------------------------------------------------ recorded payloads
def test_recorded_football_rows_with_exact_names_cost_under_40_rows_per_match():
    """recorded 2026-09-19, england_-_premier_league, market=<exact names>, is_main_line=true: 200 rows, 7 matches"""
    payload = load("sharp_pl_wide_main.json")
    per_event = {}
    for r in payload["data"]:
        per_event.setdefault(r["event_id"], []).append(r)
    assert len(per_event) >= 5 and 200 / len(per_event) < 40
    assert all(r["is_main_line"] and r.get("market_segment") is None for r in payload["data"])


# ------------------------------------------------------------------ Phase 3: the wider football market list
def _recorded_wide():
    return load("sharp_pl_wide_main.json")["data"]


def _line(market_type, selection_type):
    p = SharpApiProvider("k", _Nothing(), leagues=[])
    r = next(r for r in _recorded_wide() if r["market_type"] == market_type and r["selection_type"] == selection_type)
    return p._row_to_line(r, "football")


@pytest.mark.parametrize("market_type,selection_type,market,selection", [
    ("double_chance", "home_draw", "dc", "home_draw"), ("double_chance", "away_draw", "dc", "away_draw"),
    ("double_chance", "home_away", "dc", "home_away"),
    ("draw_no_bet", "home", "dnb", "home"), ("draw_no_bet", "away", "dnb", "away"),
    ("both_teams_to_score", "yes", "btts", "yes"), ("both_teams_to_score", "no", "btts", "no"),
    ("moneyline", "draw", "result3", "draw"), ("point_spread", "home", "handicap", "home"),
    ("total_goals", "over", "total", "over")])
def test_every_market_type_in_the_football_list_parses_from_a_recorded_row(market_type, selection_type, market, selection):
    ml = _line(market_type, selection_type)
    assert ml is not None and ml.market == market and ml.selection == selection and ml.stat == "points"


@pytest.mark.parametrize("selection_type,side,ou,line", [("home_over", "home", "over", 1.5), ("away_over", "away", "over", 0.5),
                                                         ("home_under", "home", "under", 1.5), ("away_under", "away", "under", 0.5)])
def test_team_total_goals_becomes_a_team_total_not_a_dropped_row(selection_type, side, ou, line):
    """team_total_goals used to be dropped: the generic 'total' branch wanted over/under, not home_over."""
    ml = _line("team_total_goals", selection_type)
    assert (ml.market, ml.entity, ml.selection, ml.line, ml.stat) == ("total", side, ou, line, "points")


def test_shots_fouls_and_corner_team_totals_are_still_not_mistaken_for_goals():
    p = SharpApiProvider("k", _Nothing(), leagues=[])
    for mtype in ("team_total_shots", "team_total_fouls", "team_total_exact"):
        r = dict(_recorded_wide()[0], market_type=mtype, selection_type="home_over", line=3.5)
        assert p._row_to_line(r, "football") is None


def test_the_recorded_page_becomes_events_with_all_six_market_families():
    p = SharpApiProvider("k", _Nothing(), leagues=[])
    events = p._rows_to_events(_recorded_wide(), dt(2026, 9, 19, 0), dt(2026, 10, 12, 0))
    assert len(events) >= 5
    fams = {m.market for e in events for m in e.markets}
    assert {"result3", "handicap", "total", "dc", "dnb", "btts"} <= fams


def test_the_wider_list_stays_cheap_per_match():
    """recorded: 200 rows for 7 matches = 28.6 rows/match, far below the 80-row bar for widening"""
    per_event = {}
    for r in _recorded_wide():
        per_event.setdefault(r["event_id"], []).append(r)
    assert 200 / len(per_event) <= 80
