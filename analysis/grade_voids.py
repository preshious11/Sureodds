"""B1 - grade the production picks voided as "settlement deadline expired", IN MEMORY ONLY.

Adapted from the earlier scratchpad dry_run_grade.py. Same lookup/grade code as production settlement:
predbot.providers.chain.ResultsResolver (paths_for / _ask) + predbot.markets.settlement.grade. Only the FREE
result sources are used - ESPN, EuroLeague, football-data.org - never an odds provider. Nothing is written to any
database; output is analysis/data/void_grades.csv (+ a per-fixture source-probe dump for diagnostics).

Grading source = the first free source, in the resolver's own order, that returns a non-"unknown" result (exactly
what live settlement would do). For cross-checking, every other free source on the path is asked too and its
grade recorded (``alt_*`` columns) - this never changes the graded outcome.

Requires FOOTBALL_DATA_KEY in the environment (load predictions-bot/.env first). The key is never printed.
Run:  set -a; source predictions-bot/.env; set +a
      predictions-bot/.venv/Scripts/python analysis/grade_voids.py
"""
from __future__ import annotations

import csv
import json
import os
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "predictions-bot"))

from predbot.markets.settlement import grade, PENDING  # noqa: E402
from predbot.providers.base import MarketLine  # noqa: E402
from predbot.providers.chain import ResultsResolver  # noqa: E402
from predbot.providers.espn import ESPNProvider  # noqa: E402
from predbot.providers.euroleague import EuroLeagueProvider  # noqa: E402
from predbot.providers.football_data import FootballDataProvider  # noqa: E402
from predbot.util.timeutil import parse_iso  # noqa: E402

RAW = os.path.join(HERE, "data", "picks_raw.json")
OUT = os.path.join(HERE, "data", "void_grades.csv")
PROBE = os.path.join(HERE, "data", "void_source_probe.json")
FREE = ("espn", "euroleague", "football_data")


def main():
    key = os.environ.get("FOOTBALL_DATA_KEY", "")
    print(f"FOOTBALL_DATA_KEY present: {'yes (***)' if key else 'NO'}")
    providers = {"espn": ESPNProvider(), "euroleague": EuroLeagueProvider()}
    if key:
        providers["football_data"] = FootballDataProvider(key)
    resolver = ResultsResolver(providers)

    with open(RAW, encoding="utf-8") as f:
        rows = json.load(f)
    voids = [r for r in rows if r["status"] == "void"
             and (json.loads(r["result_json"] or "{}").get("reason") == "settlement deadline expired")]
    print(f"void 'settlement deadline expired' rows: {len(voids)}")

    probe_cache = {}   # fixture -> {"paths": [...], "results": {name: MatchResult|None}, "errors": {...}}
    out_rows, errors = [], []
    for r in voids:
        try:
            src = json.loads(r["sources"]) if r["sources"] else {}
        except ValueError:
            src = {}
        alt_ids = src.get("_alt_ids") or {}
        hints = {"sport_key": src.get("_sport_key") or "", "league_id": src.get("_league_id") or "",
                 "league": r["league"] or ""}
        start = parse_iso(r["start_utc"])
        ck = (r["provider"], r["provider_event_id"])
        if ck not in probe_cache:
            names = [n for n in resolver.paths_for(r["provider"], r["sport"], alt_ids, hints) if n in FREE]
            results, errs = {}, {}
            for name in names:
                try:
                    results[name] = resolver._ask(name, r["provider"], r["provider_event_id"], alt_ids, r["sport"],
                                                  r["home"], r["away"], start, hints)
                except Exception as e:  # noqa: BLE001 - diagnostic sweep: one bad lookup must not stop the rest
                    results[name] = None
                    errs[name] = str(e)[:200]
                    errors.append((r["id"], name, str(e)[:200]))
            probe_cache[ck] = {"paths": names, "results": results, "errors": errs}
        probe = probe_cache[ck]
        ml = MarketLine(market=r["market"], selection=r["selection"], price=float(r["price"]),
                        period=r["period"], stat=r["stat"], entity=r["entity"],
                        line=float(r["line"]) if r["line"] is not None else None)

        def grade_with(res):
            if res is None or res.status == "unknown":
                return None
            try:
                g = grade(ml, res, r["sport"])
            except Exception as e:  # noqa: BLE001
                errors.append((r["id"], "grade", str(e)[:200]))
                return "grade_error"
            return g

        outcome, source = "not_obtainable", "none"
        alt = {}
        for name in probe["paths"]:
            g = grade_with(probe["results"].get(name))
            alt[name] = g
            if outcome == "not_obtainable" and g is not None and g != PENDING:
                outcome, source = g, name
        why = ""
        if outcome == "not_obtainable":
            if not probe["paths"]:
                why = "no free source on the resolver path"
            else:
                bits = []
                for name in probe["paths"]:
                    res = probe["results"].get(name)
                    if name in probe["errors"]:
                        bits.append(f"{name}: error")
                    elif res is None:
                        bits.append(f"{name}: no match found")
                    else:
                        bits.append(f"{name}: status={res.status}" + (" (grade pending)" if res.status == "final" else ""))
                why = "; ".join(bits)
        cols = (r.get("collections") or "").split(",")
        out_rows.append({
            "pick_id": r["id"], "pick_date": r["pick_date"], "sport": r["sport"], "league": r["league"],
            "canonical_competition_id": r["canonical_competition_id"], "home": r["home"], "away": r["away"],
            "start_utc": r["start_utc"], "provider": r["provider"], "market": r["market"], "period": r["period"],
            "stat": r["stat"], "selection": r["selection"], "line": r["line"], "price": r["price"],
            "prob": r["prob"], "p_market": r["p_market"], "p_fair": r["p_fair"],
            "market_strict_win": ((src.get("_notes") or {}).get("settlement") or {}).get("market_strict_win"),
            "ev": r["ev"], "quality": r["quality"],
            "in_top": int("top" in cols), "in_daily": int("daily" in cols), "in_all": int("all" in cols),
            "graded_outcome": outcome, "grading_source": source,
            "alt_espn": alt.get("espn"), "alt_euroleague": alt.get("euroleague"),
            "alt_football_data": alt.get("football_data"),
            "free_paths": "|".join(probe["paths"]), "not_obtainable_reason": why,
        })

    with open(OUT, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(out_rows[0].keys()))
        w.writeheader()
        w.writerows(out_rows)
    with open(PROBE, "w", encoding="utf-8") as f:
        json.dump({f"{k[0]}:{k[1]}": {"paths": v["paths"], "errors": v["errors"],
                                      "results": {n: (None if res is None else {"status": res.status, "source": res.source,
                                                                               "scores": {p: list(s) for p, s in res.scores.items()}})
                                                  for n, res in v["results"].items()}}
                   for k, v in probe_cache.items()}, f, indent=1)

    print(f"\nwrote {len(out_rows)} rows -> {OUT}")
    print("\noutcomes:", dict(Counter(x["graded_outcome"] for x in out_rows)))
    by = Counter((x["grading_source"], x["graded_outcome"]) for x in out_rows)
    for s in ("espn", "euroleague", "football_data", "none"):
        print(f"  {s}: " + ", ".join(f"{o}={n}" for (ss, o), n in sorted(by.items()) if ss == s))
    # agreement where two sources both graded the same pick
    both = [x for x in out_rows if x["alt_espn"] not in (None, PENDING) and x["alt_football_data"] not in (None, PENDING)]
    agree = sum(x["alt_espn"] == x["alt_football_data"] for x in both)
    print(f"\npicks graded by BOTH espn and football_data: {len(both)}, agreeing: {agree}")
    print("\nnot obtainable reasons:")
    for k, n in Counter((x["league"], x["not_obtainable_reason"]) for x in out_rows
                        if x["graded_outcome"] == "not_obtainable").most_common():
        print(f"  {n:3d}  {k[0]} :: {k[1]}")
    if errors:
        print(f"\n{len(errors)} lookup/grade errors (first 5): {errors[:5]}")


if __name__ == "__main__":
    main()
