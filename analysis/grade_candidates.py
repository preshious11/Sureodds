"""Grade EVERY Pro candidate in the stored pools (custom_pick_candidates), IN MEMORY ONLY.

Purpose: a far larger outcome sample per sport than the published Top/Daily picks, so we can test whether the
model's probability (p_model) and the de-vigged market probability (p_market) predict outcomes, sport by sport.

Same grading path as grade_voids.py: predbot.providers.chain.ResultsResolver + predbot.markets.settlement.grade,
FREE sources only (ESPN, EuroLeague, football-data.org). No odds-provider calls, no database writes.

Input : JSON files as returned by `wrangler d1 execute ... --json` (results[].payload_json).
Output: analysis/data/candidate_grades.csv (git-ignored).
Run   : set -a; source predictions-bot/.env; set +a
        predictions-bot/.venv/Scripts/python analysis/grade_candidates.py <pool.json> [<pool.json> ...]
"""
from __future__ import annotations

import csv
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "predictions-bot"))

from predbot.markets.settlement import grade, PENDING  # noqa: E402
from predbot.providers.base import MarketLine  # noqa: E402
from predbot.providers.chain import ResultsResolver  # noqa: E402
from predbot.providers.espn import ESPNProvider  # noqa: E402
from predbot.providers.euroleague import EuroLeagueProvider  # noqa: E402
from predbot.providers.football_data import FootballDataProvider  # noqa: E402
from predbot.util.timeutil import parse_iso  # noqa: E402

OUT = os.path.join(HERE, "data", "candidate_grades.csv")
FREE = ("espn", "euroleague", "football_data")


def load(paths):
    rows = []
    for p in paths:
        with open(p, encoding="utf-8") as f:
            for r in json.load(f)[0]["results"]:
                payload = json.loads(r["payload_json"])
                payload["_pool_date"] = r.get("prediction_date") or payload.get("prediction_date")
                payload["_candidate_id"] = r["candidate_id"]
                rows.append(payload)
    return rows


def sources_of(p):
    s = p.get("sources")
    if isinstance(s, str):
        try:
            s = json.loads(s)
        except ValueError:
            s = {}
    return s if isinstance(s, dict) else {}


def main(paths):
    key = os.environ.get("FOOTBALL_DATA_KEY", "")
    print(f"FOOTBALL_DATA_KEY present: {'yes (***)' if key else 'NO'}")
    providers = {"espn": ESPNProvider(), "euroleague": EuroLeagueProvider()}
    if key:
        providers["football_data"] = FootballDataProvider(key)
    resolver = ResultsResolver(providers)
    rows = load(paths)
    print(f"candidates loaded: {len(rows)}")

    cache = {}
    out = []
    for p in rows:
        src = sources_of(p)
        alt_ids = src.get("_alt_ids") or {}
        hints = {"sport_key": src.get("_sport_key") or "", "league_id": src.get("_league_id") or "",
                 "league": p.get("league") or ""}
        provider = p.get("provider") or ""
        ev_id = p.get("provider_event_id") or ""
        start = parse_iso(p.get("start_utc"))
        ck = (provider, ev_id, p.get("sport"), p.get("home"), p.get("away"))
        if ck not in cache:
            names = [n for n in resolver.paths_for(provider, p.get("sport"), alt_ids, hints) if n in FREE]
            results = {}
            for name in names:
                try:
                    results[name] = resolver._ask(name, provider, ev_id, alt_ids, p.get("sport"),
                                                  p.get("home"), p.get("away"), start, hints)
                except Exception:  # noqa: BLE001 - one failed lookup must not stop the sweep
                    results[name] = None
            cache[ck] = (names, results)
        names, results = cache[ck]
        ml = MarketLine(market=p["market"], selection=p["selection"], price=float(p["price"]),
                        period=p.get("period") or "full", entity=p.get("entity") or "all",
                        stat=p.get("stat") or "points",
                        line=float(p["line"]) if p.get("line") is not None else None)
        outcome, source = "not_obtainable", "none"
        for name in names:
            res = results.get(name)
            if res is None or res.status == "unknown":
                continue
            try:
                g = grade(ml, res, p.get("sport"))
            except Exception:  # noqa: BLE001
                continue
            if g is not None and g != PENDING:
                outcome, source = g, name
                break
        out.append({
            "candidate_id": p["_candidate_id"], "pool_date": p["_pool_date"], "sport": p.get("sport"),
            "league": p.get("league"), "home": p.get("home"), "away": p.get("away"),
            "start_utc": p.get("start_utc"), "market": p["market"], "selection": p["selection"],
            "line": p.get("line"), "period": p.get("period"), "stat": p.get("stat"),
            "price": p["price"], "p_model": p.get("p_model"), "p_market": p.get("p_market"),
            "p_independent": p.get("p_independent"), "independent_status": p.get("independent_status"),
            "books": p.get("bookmaker_count"), "outcome": outcome, "graded_by": source,
        })
    with open(OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(out[0].keys()))
        w.writeheader()
        w.writerows(out)
    from collections import Counter
    print("outcomes:", dict(Counter(r["outcome"] for r in out)))
    print("wrote", OUT)


if __name__ == "__main__":
    main(sys.argv[1:])
