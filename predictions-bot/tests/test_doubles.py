"""Tennis doubles cannot be settled (ESPN's tennis parser grades singles only): never pick them."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace as N

from predbot.config import Config
from predbot.markets.settlement import make_results_gate, result_paths
from predbot.providers.sharpapi import SharpApiProvider
from predbot.selection.picker import select
from predbot.util.names import is_doubles
from tests.conftest import dt
from tests.test_picker import ev, sc

START = datetime(2026, 9, 19, 12, tzinfo=timezone.utc)
END = datetime(2026, 9, 21, tzinfo=timezone.utc)


def _rows(home, away, eid="t1"):
    base = {"sportsbook": "b", "event_id": eid, "sport": "tennis", "league": "wta", "home_team": home,
            "away_team": away, "market_type": "moneyline", "odds_decimal": 1.5, "line": None,
            "is_main_line": True, "is_alternate_line": False, "event_start_time": "2026-09-19T15:00:00Z",
            "is_live": False, "market_segment": None}
    return [dict(base, id=f"{eid}-h", selection=home, selection_type="home"),
            dict(base, id=f"{eid}-a", selection=away, selection_type="away", odds_decimal=2.4)]


def test_the_doubles_names_from_the_live_card_are_detected():
    assert is_doubles("Dabrowski / Stefani", "Y. Dang / X. You")
    assert not is_doubles("Lisa Pigato", "Susan Bandecchi")


def test_sharpapi_drops_doubles_and_keeps_singles(budget):
    p = SharpApiProvider("k", budget, ["wta"])
    rows = _rows("Dabrowski / Stefani", "Y. Dang / X. You", "d1") + _rows("Lisa Pigato", "Susan Bandecchi", "s1")
    events = p._rows_to_events(rows, START, END)
    assert [e.home for e in events] == ["Lisa Pigato"]
    assert p.parse_counts["DOUBLES_UNSUPPORTED"] == 1


def test_a_doubles_event_reaching_the_gate_gets_no_results_path():
    espn = object()
    doubles = N(sport="tennis", league="WTA", league_id="wta", provider="sharpapi", sources=["sharpapi"], extra={},
                home="Dabrowski / Stefani", away="Y. Dang / X. You")
    singles = N(**{**doubles.__dict__, "home": "Lisa Pigato", "away": "Susan Bandecchi"})
    assert result_paths(doubles, Config(), {"espn": espn}) == []
    assert [p.name for p in result_paths(singles, Config(), {"espn": espn})] == ["espn"]
    e = ev(1, home="Dabrowski / Stefani", away="Y. Dang / X. You", sport="tennis", league="WTA")
    e.league_id = "wta"
    e2 = ev(2, home="Lisa Pigato", away="Susan Bandecchi", sport="tennis", league="WTA")
    e2.league_id = "wta"
    gate = make_results_gate(Config(), {"espn": espn})
    ps = select([sc(e, "result2", "home", 0.7, 1.5), sc(e2, "result2", "home", 0.7, 1.5)], Config(), dt(), results_gate=gate)
    assert ps.diagnostics["selection_rejections"].get("NO_RESULTS_PATH") == 1
    assert all(s.event.home != "Dabrowski / Stefani" for s in ps.all())

