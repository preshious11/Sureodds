"""Pull every published prediction (any prediction_memberships row) from production D1, read-only.

Output: analysis/data/picks_raw.json - one dict per pick with its memberships ("all,daily,top") and all columns
needed for B1/B2 (sources and result_json included).
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from d1 import select  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "data", "picks_raw.json")

COLS = ("p.id, p.pick_date, p.tier, p.rank, p.provider, p.provider_event_id, p.sport, p.league, p.home, p.away, "
        "p.start_utc, p.market, p.period, p.stat, p.entity, p.line, p.selection, p.label, p.price, p.prob, p.ev, "
        "p.p_market, p.prob_raw, p.p_fair, p.edge_ev, p.rank_score, p.books, p.quality, p.status, p.settled_at, "
        "p.result_json, p.created_at, p.closing_price, p.clv, p.fixture_key, p.canonical_event_id, "
        "p.canonical_competition_id, p.home_id, p.away_id, p.is_representative, p.sources")


def main():
    bounds = select("SELECT MIN(prediction_id) lo, MAX(prediction_id) hi, COUNT(DISTINCT prediction_id) n "
                    "FROM prediction_memberships")[0]
    lo, hi = int(bounds["lo"]), int(bounds["hi"])
    print(f"membership prediction ids {lo}..{hi}, distinct={bounds['n']}")
    rows, step = [], 150
    for a in range(lo, hi + 1, step):
        b = a + step - 1
        page = select(
            f"SELECT {COLS}, (SELECT group_concat(m.collection) FROM prediction_memberships m "
            f"WHERE m.prediction_id = p.id) AS collections FROM picks p "
            f"WHERE p.id BETWEEN {a} AND {b} AND EXISTS (SELECT 1 FROM prediction_memberships m2 "
            f"WHERE m2.prediction_id = p.id) ORDER BY p.id")
        rows.extend(page)
        print(f"  ids {a}-{b}: {len(page)} rows (total {len(rows)})")
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1)
    print(f"wrote {len(rows)} rows -> {OUT}")


if __name__ == "__main__":
    main()
