"""Phase 0 of the cross-tier work: reproduce Top/Daily duplicates and say WHY today's linking fails.

Read-only. Two sources, in this order:
  1. every SQLite file given on the command line, opened ``mode=ro`` - pairs of one ``tier='top'`` and one
     ``tier='daily'`` row on the same match, detected generously (same sport and date, kickoff within
     3 h, any pair of team names >= 0.6 similar on either side);
  2. CASES - the same fixture as two providers name it (built from the owner's examples, the recorded
     SharpAPI / SportsGameOdds fixtures and the last dry run), because no database on this machine
     holds a true duplicate.

Usage: python analysis/cross_tier_repro.py [db ...]
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from predbot.selection.picker import same_fixture          # noqa: E402
from predbot.util.names import same_team, similarity        # noqa: E402
from predbot.util.timeutil import parse_iso                 # noqa: E402


def why_not(a, b) -> str:
    """Why today's same_fixture(a, b) is False (or 'linked')."""
    if same_fixture(a, b):
        return "linked"
    if a.sport != b.sport:
        return f"sport label mismatch ({a.sport!r} vs {b.sport!r})"
    gap = abs((a.start - b.start).total_seconds()) / 60
    if gap > 180:
        return f"kickoff differs by {gap:.0f} min"
    hh, aa = similarity(a.home, b.home), similarity(a.away, b.away)
    ha, ah = similarity(a.home, b.away), similarity(a.away, b.home)
    if not (same_team(a.home, b.home) and same_team(a.away, b.away)):
        if same_team(a.home, b.away) and same_team(a.away, b.home):
            return f"home/away SWAPPED (home~away {ha:.2f}, away~home {ah:.2f})"
        bits = []
        if not same_team(a.home, b.home):
            bits.append(f"home {a.home!r} vs {b.home!r} = {hh:.2f} < 0.82")
        if not same_team(a.away, b.away):
            bits.append(f"away {a.away!r} vs {b.away!r} = {aa:.2f} < 0.82")
        if ha >= .5 or ah >= .5:
            bits.append(f"(swapped scores {ha:.2f}/{ah:.2f})")
        return "; ".join(bits)
    return "?"


def _t(hh, mm=0):
    return datetime(2026, 9, 19, hh, mm, tzinfo=timezone.utc)


# (label, sport, provider A event, provider B event)  - each event: (provider, league, home, away, start)
CASES = [
    ("MLB, abbreviation vs full name", "baseball",
     ("sharpapi", "MLB", "LA Dodgers", "San Diego Padres", _t(2)),
     ("sportsgameodds", "MLB", "Los Angeles Dodgers", "San Diego Padres", _t(2, 10))),
    ("MLB, abbreviation prefix + swapped sides", "baseball",
     ("sharpapi", "MLB", "TEX Rangers", "BOS Red Sox", _t(18, 10)),
     ("sportsgameodds", "MLB", "Boston Red Sox", "Texas Rangers", _t(18, 10))),
    ("MLB, nickname only vs city + nickname", "baseball",
     ("sharpapi", "MLB", "Athletics", "Seattle Mariners", _t(1, 40)),
     ("sportsgameodds", "MLB", "Oakland Athletics", "Seattle Mariners", _t(1, 40))),
    ("NBA, abbreviation prefix", "basketball",
     ("sharpapi", "NBA", "LA Lakers", "GS Warriors", _t(0, 30)),
     ("sportsgameodds", "NBA", "Los Angeles Lakers", "Golden State Warriors", _t(0, 30))),
    ("Football, swapped sides (Le Mans / Lorient)", "football",
     ("sharpapi", "FRANCE - LIGUE 2", "Le Mans", "Lorient", _t(18, 45)),
     ("oddsapi", "Ligue 2 - France", "Lorient", "Le Mans", _t(18, 45))),
    ("Football, same orientation, alias spelling", "football",
     ("sharpapi", "SPAIN - LA LIGA", "Espanyol", "Elche", _t(19)),
     ("oddsapi", "La Liga - Spain", "RCD Espanyol", "Elche CF", _t(19))),
]


def pick_row_pairs(paths):
    for p in paths:
        c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        c.row_factory = sqlite3.Row
        try:
            rows = [dict(r) for r in c.execute("SELECT * FROM picks ORDER BY pick_date, tier DESC, rank")]
        except sqlite3.Error as e:
            print(f"{p}: {e}")
            continue
        print(f"\n== {p}: {len(rows)} picks, {len({r['pick_date'] for r in rows})} date(s)")
        ev = lambda r: NS(sport=r["sport"], home=r["home"], away=r["away"], start=parse_iso(r["start_utc"]))
        same_cols = ("market", "period", "stat", "entity", "line", "selection")
        hits = 0
        for t in (r for r in rows if r["tier"] == "top"):
            for d in (r for r in rows if r["tier"] == "daily" and r["pick_date"] == t["pick_date"] and r["sport"] == t["sport"]):
                if abs((parse_iso(t["start_utc"]) - parse_iso(d["start_utc"])).total_seconds()) > 3 * 3600:
                    continue
                best = max(similarity(t["home"], d["home"]), similarity(t["home"], d["away"]),
                           similarity(t["away"], d["home"]), similarity(t["away"], d["away"]))
                if best < 0.6:
                    continue
                hits += 1
                same = all(t[k] == d[k] for k in same_cols)
                print(f"  pair ({'IDENTICAL selection' if same else 'different selection'}), same_fixture={same_fixture(ev(t), ev(d))}")
                for tag, r in (("TOP  ", t), ("DAILY", d)):
                    print(f"    {tag} {r['provider']} {r['provider_event_id']} {r['league']} | {r['home']} v {r['away']} "
                          f"| {r['start_utc']} | {r['selection']} | {r['label']}")
        print(f"  {hits} candidate pair(s)")


def main(paths):
    pick_row_pairs(paths)
    print("\n== constructed cases (two providers, one match)")
    for label, sport, a, b in CASES:
        ea = NS(sport=sport, provider=a[0], league=a[1], home=a[2], away=a[3], start=a[4])
        eb = NS(sport=sport, provider=b[0], league=b[1], home=b[2], away=b[3], start=b[4])
        print(f"- {label}\n    {a[0]:<15} {a[2]} v {a[3]} {a[4]:%H:%M}\n    {b[0]:<15} {b[2]} v {b[3]} {b[4]:%H:%M}"
              f"\n    same_fixture today: {same_fixture(ea, eb)}   why: {why_not(ea, eb)}")


if __name__ == "__main__":
    main(sys.argv[1:])
