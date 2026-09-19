"""H19: SportsGameOdds pages are parsed as they arrive and the raw payload is released, not held until the end."""
from __future__ import annotations

import copy

from predbot.providers.budget import Budget
from predbot.providers.sportsgameodds import SportsGameOddsProvider
from predbot.util.timeutil import parse_iso
from tests.conftest import load


def test_each_page_is_parsed_before_the_next_is_requested(db):
    base = load("sgo_event.json")
    base = base["data"][0] if "data" in base else base
    order = []
    p = SportsGameOddsProvider("k", Budget(db), [], ["MLB", "MLS"])
    real_parse = p._parse_event

    def parse(ev):
        order.append(("parse", ev["leagueID"]))
        return real_parse(ev)

    p._parse_event = parse

    class Http:
        def get(self, path, params=None):
            order.append(("get", params["leagueID"]))
            ev = copy.deepcopy(base)
            ev["leagueID"], ev["eventID"] = params["leagueID"], "E-" + params["leagueID"]
            ev["status"]["startsAt"] = "2026-09-19T20:00:00Z"
            return {"success": True, "data": [ev]}

    p.http = Http()
    out = p.fetch_events(parse_iso("2026-09-19T00:00:00Z"), parse_iso("2026-09-20T12:00:00Z"))
    assert len(out) == 2
    assert order == [("get", "MLB"), ("parse", "MLB"), ("get", "MLS"), ("parse", "MLS")]
    assert p.fetch_counts["MLB"]["raw_events"] == 1 and p.fetch_counts["MLS"]["raw_events"] == 1
