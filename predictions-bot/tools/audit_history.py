"""Audit the stored football history before any modelling: duplicates, impossible scores, identity, coverage, gaps.

    python tools/audit_history.py [--db data/predbot.db] [--json]

Read-only. Never calls a provider.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timedelta

ROUND_ROBIN = {"epl": 380, "la_liga": 380, "serie_a": 380, "bundesliga": 306, "ligue_1": 306, "eredivisie": 306,
               "primeira": 306, "championship": 552}          # games in a complete regular season (Championship + play-offs: 5)
GAP_DAYS = 21


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    for w in (" fc", " afc", " cf", " ac", " sc", " fk", " sv", " fc.", "fc ", "1. ", " 04", " 1899", " 1909", " 1846"):
        s = s.replace(w, " ")
    return " ".join(s.split())


def audit(db_path: str) -> dict:
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    rows = [dict(r) for r in c.execute("SELECT * FROM match_results WHERE sport='football' ORDER BY kickoff_utc")]
    names = {r["id"]: r["name"] for r in c.execute("SELECT id, name FROM canonical_participants")}
    out: dict = {"matches": len(rows), "first": rows[0]["kickoff_utc"] if rows else None, "last": rows[-1]["kickoff_utc"] if rows else None}

    per = Counter((r["competition_id"], r["season"]) for r in rows)
    out["per_competition_season"] = {f"{k[0]}:{k[1]}": v for k, v in sorted(per.items())}
    out["per_competition"] = dict(Counter(r["competition_id"] for r in rows))

    seen, dup = defaultdict(list), []
    for r in rows:
        seen[(r["competition_id"], r["home_id"], r["away_id"], r["kickoff_utc"][:10])].append(r["id"])
    dup += [k for k, v in seen.items() if len(v) > 1]
    pair_day = defaultdict(list)
    for r in rows:
        pair_day[(frozenset((r["home_id"], r["away_id"])), r["kickoff_utc"][:10])].append(r["id"])
    out["duplicate_fixtures"] = len(dup)
    out["same_pair_same_day"] = sum(1 for v in pair_day.values() if len(v) > 1)

    bad = [r["id"] for r in rows if r["home_goals"] < 0 or r["away_goals"] < 0 or r["home_goals"] > 15 or r["away_goals"] > 15
           or (r["home_ht"] is not None and (r["home_ht"] > r["home_goals"] or r["away_ht"] > r["away_goals"]))
           or r["home_id"] == r["away_id"]]
    out["impossible_scores"] = len(bad)
    out["largest_scores"] = sorted(((r["home_goals"], r["away_goals"], r["competition_id"], r["kickoff_utc"][:10]) for r in rows),
                                   key=lambda t: -(t[0] + t[1]))[:3]
    out["goals_per_match"] = round(sum(r["home_goals"] + r["away_goals"] for r in rows) / max(len(rows), 1), 3)
    out["home_win_draw_away"] = [round(sum(1 for r in rows if f(r)) / max(len(rows), 1), 3)
                                 for f in (lambda r: r["home_goals"] > r["away_goals"], lambda r: r["home_goals"] == r["away_goals"],
                                           lambda r: r["home_goals"] < r["away_goals"])]

    used = {r["home_id"] for r in rows} | {r["away_id"] for r in rows}
    out["participants_used"] = len(used)
    out["unresolved_participants"] = sorted(i for i in used if i not in names)
    groups = defaultdict(set)
    for i in used:
        groups[_norm(names.get(i, ""))].add(i)
    out["same_normalised_name_multiple_ids"] = {k: len(v) for k, v in groups.items() if len(v) > 1}
    # the same alias must never point at two clubs; a club with several provider ids is checked via the identity table
    pids = defaultdict(set)
    for r in c.execute("SELECT canonical_id, provider_id FROM provider_identity WHERE kind='participant' AND provider='football_data'"):
        pids[r["canonical_id"]].add(r["provider_id"])
    out["canonical_with_several_provider_ids"] = {names.get(k, k): sorted(v) for k, v in pids.items() if len(v) > 1}
    byprov = defaultdict(set)
    for k, v in pids.items():
        for p in v:
            byprov[p].add(k)
    out["provider_id_mapped_to_several_canonical"] = {p: sorted(v) for p, v in byprov.items() if len(v) > 1}

    comp_teams = defaultdict(lambda: defaultdict(set))
    for r in rows:
        for side in ("home_id", "away_id"):
            comp_teams[r["competition_id"]][r["season"]].add(r[side])
    out["teams_per_competition_season"] = {f"{k}:{s}": len(v) for k, d in comp_teams.items() for s, v in sorted(d.items())}
    multi = defaultdict(set)
    for comp, d in comp_teams.items():
        for s, ids in d.items():
            for i in ids:
                multi[(i, s)].add(comp)
    out["team_in_two_competitions_same_season"] = sorted({f"{names.get(i)}:{s}:{'/'.join(sorted(v))}" for (i, s), v in multi.items() if len(v) > 1})

    gaps, expected = [], {}
    by_cs = defaultdict(list)
    for r in rows:
        by_cs[(r["competition_id"], r["season"])].append(datetime.fromisoformat(r["kickoff_utc"].replace("Z", "+00:00")))
    for (comp, season), ts in sorted(by_cs.items()):
        ts.sort()
        for a, b in zip(ts, ts[1:]):
            if b - a > timedelta(days=GAP_DAYS):
                gaps.append(f"{comp}:{season} {a.date()}..{b.date()} ({(b - a).days}d)")
        want = ROUND_ROBIN.get(comp)
        if want and len(ts) < want and (comp, season) in per and season != "2026":
            expected[f"{comp}:{season}"] = {"have": len(ts), "complete_season": want, "missing": want - len(ts)}
    out["gaps_over_21_days"] = gaps
    out["incomplete_seasons"] = expected
    out["small_samples"] = {k: v for k, v in out["per_competition"].items() if v < 300}
    return out


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/predbot.db")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    report = audit(a.db)
    if a.json:
        print(json.dumps(report, indent=1, default=str))
    else:
        for k, v in report.items():
            print(f"{k}: {v}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main(sys.argv[1:])
