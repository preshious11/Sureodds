"""B1.3 evidence: is the Oct-15 UEL void (Uniao Torreense v Sunderland, pick 228) the 60ee400 short-name/full-name
identity bug? Read-only: one ESPN scoreboard call plus pure name-matching checks with predbot's own functions.
Also checks the EuroLeague feed for the women's games that stayed unobtainable.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "predictions-bot"))

import predbot.util.names as names  # noqa: E402
from predbot.markets.competitions import BY_ID, lookup  # noqa: E402
from predbot.providers.espn import ESPNProvider  # noqa: E402
from predbot.providers.euroleague import EuroLeagueProvider  # noqa: E402
from predbot.util.timeutil import parse_iso  # noqa: E402

STORED_HOME, STORED_AWAY = "Uniao Torreense", "Sunderland"
START = parse_iso("2026-10-15T16:45:00Z")


def show(label, a, b):
    print(f"  {label:58s} same_team(sport='')={names.same_team(a, b)!s:5s} "
          f"same_team(sport='football')={names.same_team(a, b, sport='football')!s:5s} "
          f"similarity={names.similarity(a, b):.3f}  canonical: {names.canonical_team(a, 'football')!r} / "
          f"{names.canonical_team(b, 'football')!r}")


def main():
    print("FOOTBALL_ALIASES in the current (post-60ee400) code:", names.FOOTBALL_ALIASES)
    print("league lookup for the stored league 'UEFA Europa League':", getattr(lookup('', 'UEFA Europa League'), 'id', None),
          "| football-data code for uel:", repr(BY_ID['uel'].football_data), "(empty = not on this plan)")
    espn = ESPNProvider()
    comp = BY_ID["uel"]
    res = espn.find_result("football", STORED_HOME, STORED_AWAY, START, league="UEFA Europa League")
    print(f"\nESPN find_result with the stored names (current code): {res}")
    for c in espn._candidates(comp, espn._scoreboard(comp, START)):
        sides = {s.get('homeAway'): s for s in c.get('competitors') or []}
        team_h = (sides.get('home') or {}).get('team') or {}
        team_a = (sides.get('away') or {}).get('team') or {}
        if 'Sunderland' in str(team_a) or 'Sunderland' in str(team_h):
            print("ESPN event:", c.get('date'), espn._status(c))
            print("  home team fields:", {k: team_h.get(k) for k in ('displayName', 'shortDisplayName', 'abbreviation', 'name', 'location')})
            print("  away team fields:", {k: team_a.get(k) for k in ('displayName', 'shortDisplayName', 'abbreviation', 'name', 'location')})
            print("  score:", (sides.get('home') or {}).get('score'), '-', (sides.get('away') or {}).get('score'))
            r2 = espn.find_result("football", "Torreense", STORED_AWAY, START, league="UEFA Europa League")
            print(f"  find_result if the home side were stored as ESPN's 'Torreense': status={getattr(r2, 'status', None)} "
                  f"scores={getattr(r2, 'scores', None)}")
    print("\nName-matching checks (predbot.util.names, current code):")
    show("stored 'Uniao Torreense' vs ESPN 'Torreense'", STORED_HOME, "Torreense")
    show("stored 'Sunderland' vs ESPN 'Sunderland'", STORED_AWAY, "Sunderland")
    print("\nThe 60ee400 case itself, for comparison (pick 779, also in the void set):")
    show("stored 'Tottenham' vs ESPN 'Tottenham Hotspur'", "Tottenham", "Tottenham Hotspur")
    show("stored 'Coventry' vs football-data 'Coventry City FC'", "Coventry", "Coventry City FC")
    show("stored 'Coventry' vs football-data 'Coventry City'", "Coventry", "Coventry City")
    saved = dict(names.FOOTBALL_ALIASES)
    names.FOOTBALL_ALIASES["uniao torreense"] = "torreense"   # hypothetical, in memory only
    try:
        print("\nHypothetical (in memory only): alias 'uniao torreense' -> 'torreense' added to FOOTBALL_ALIASES:")
        show("stored 'Uniao Torreense' vs ESPN 'Torreense'", STORED_HOME, "Torreense")
    finally:
        names.FOOTBALL_ALIASES.clear()
        names.FOOTBALL_ALIASES.update(saved)

    print("\nEuroLeague feed check for the women's games (stored league 'Euroleague', kick-offs 2026-10-14):")
    el = EuroLeagueProvider()
    comp = lookup("", "Euroleague")
    games = el._games(comp.euroleague, parse_iso("2026-10-14T16:00:00Z"))
    day = [g for g in games if str(g.get("utcDate", "")).startswith("2026-10-14")]
    print(f"  competition code {comp.euroleague!r}: {len(games)} games in season feed, {len(day)} on 2026-10-14:")
    for g in day:
        print("   ", g.get("utcDate"), (g.get("local") or {}).get("club", {}).get("name"), "v",
              (g.get("road") or {}).get("club", {}).get("name"))


if __name__ == "__main__":
    main()
