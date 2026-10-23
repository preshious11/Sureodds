"""Diagnose why the still-unobtainable void picks found no result (read-only HTTP to ESPN / EuroLeague /
football-data.org; nothing is written anywhere except analysis/data/unmatched_probe.json).

For every not-obtainable fixture in analysis/data/void_grades.csv it lists the provider's events within +-1 day
and +-6h of the stored kick-off, the names the provider used, and whether predbot's matcher accepts each side
with the CURRENT code (post-60ee400) and with 60ee400's alias table emptied (= pre-60ee400 behaviour, since that
table is the only thing the commit added to names.py).
"""
from __future__ import annotations

import csv
import json
import os
import sys
from datetime import timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "predictions-bot"))

import predbot.util.names as names  # noqa: E402
from predbot.markets.competitions import lookup, BY_ID  # noqa: E402
from predbot.providers.espn import ESPNProvider, _names, _by_side  # noqa: E402
from predbot.providers.football_data import FootballDataProvider, competition_for  # noqa: E402
from predbot.util.timeutil import parse_iso  # noqa: E402


def matcher_both(stored, provider_names):
    cur = any(names.same_team(stored, n) for n in provider_names)
    saved = dict(names.FOOTBALL_ALIASES)
    try:
        names.FOOTBALL_ALIASES.clear()
        names.canonical_team.__globals__["FOOTBALL_ALIASES"].clear()
        pre = any(names.same_team(stored, n) for n in provider_names)
    finally:
        names.FOOTBALL_ALIASES.update(saved)
    return cur, pre


def main():
    with open(os.path.join(HERE, "data", "void_grades.csv"), encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r["graded_outcome"] == "not_obtainable"]
    fixtures = {}
    for r in rows:
        fixtures.setdefault((r["league"], r["home"], r["away"], r["start_utc"], r["canonical_competition_id"]), []).append(r["pick_id"])
    espn = ESPNProvider()
    key = os.environ.get("FOOTBALL_DATA_KEY", "")
    fd = FootballDataProvider(key) if key else None
    out = []
    for (league, home, away, start_s, comp_id), ids in fixtures.items():
        start = parse_iso(start_s)
        comp = BY_ID.get(comp_id) or lookup("", league)
        entry = {"league": league, "home": home, "away": away, "start_utc": start_s, "pick_ids": ids,
                 "competition": comp.id if comp else None, "espn": [], "football_data": []}
        if comp is not None and comp.espn:
            seen = set()
            for d in (-1, 0, 1):
                day = start.astimezone(timezone.utc) + timedelta(days=d)
                try:
                    evs = espn._scoreboard(comp, day)
                except Exception as e:  # noqa: BLE001
                    entry["espn"].append({"error": str(e)[:200]})
                    continue
                for c in espn._candidates(comp, evs):
                    when = parse_iso(str(c.get("date") or ""))
                    pair = _by_side(c.get("competitors") or [])
                    if when is None or pair is None or abs((when - start).total_seconds()) > 6 * 3600:
                        continue
                    h, a = pair
                    hn, an = _names(h), _names(a)
                    sig = (c.get("id"), tuple(hn), tuple(an))
                    if sig in seen:
                        continue
                    seen.add(sig)
                    hc, hp = matcher_both(home, hn)
                    ac, ap = matcher_both(away, an)
                    entry["espn"].append({
                        "date": c.get("date"), "status": espn._status(c), "home_names": hn, "away_names": an,
                        "score": [h.get("score"), a.get("score")],
                        "home_match_current": hc, "away_match_current": ac,
                        "home_match_pre60ee400": hp, "away_match_pre60ee400": ap,
                        "related_home": any(names.related_names(home, n, "football") for n in hn),
                        "related_away": any(names.related_names(away, n, "football") for n in an)})
        if fd is not None and comp is not None and comp.football_data:
            try:
                ms = fd._matches_on(comp.football_data, start)
            except Exception as e:  # noqa: BLE001
                ms = []
                entry["football_data"].append({"error": str(e)[:200]})
            for m in ms:
                when = parse_iso(m.get("utcDate", ""))
                if when is None or abs((when - start).total_seconds()) > 6 * 3600:
                    continue
                ht, at = m.get("homeTeam") or {}, m.get("awayTeam") or {}
                hn = [t for t in (ht.get("name"), ht.get("shortName"), ht.get("tla")) if t]
                an = [t for t in (at.get("name"), at.get("shortName"), at.get("tla")) if t]
                hc, hp = matcher_both(home, hn)
                ac, ap = matcher_both(away, an)
                entry["football_data"].append({"date": m.get("utcDate"), "status": m.get("status"),
                                               "home_names": hn, "away_names": an,
                                               "score": (m.get("score") or {}).get("fullTime"),
                                               "home_match_current": hc, "away_match_current": ac,
                                               "home_match_pre60ee400": hp, "away_match_pre60ee400": ap})
        out.append(entry)
        print(f"\n== {league}: {home} v {away} @ {start_s} (picks {ids}) comp={entry['competition']}")
        for src in ("espn", "football_data"):
            for e in entry[src]:
                if "error" in e:
                    print(f"   {src}: ERROR {e['error']}")
                    continue
                flag = ""
                if e.get("related_home") or e.get("related_away") or e["home_match_current"] or e["away_match_current"]:
                    flag = "  <-- candidate"
                print(f"   {src}: {e['date']} {e['status']} {e['home_names'][:2]} v {e['away_names'][:2]} "
                      f"score={e['score']} cur=({e['home_match_current']},{e['away_match_current']}) "
                      f"pre=({e['home_match_pre60ee400']},{e['away_match_pre60ee400']}){flag}")
    with open(os.path.join(HERE, "data", "unmatched_probe.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1, ensure_ascii=False)


if __name__ == "__main__":
    main()
