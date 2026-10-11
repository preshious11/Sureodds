"""Build a sanitized staging fixture from a COPY of the production database.

Strips everything that identifies a real user while preserving engine
history (predictions, results, ratings, quotas, caches) so staging behaves
like production:

  subscribers      -> replaced with 2 synthetic rows (STAGING_1/2)
  deliveries       -> dropped (reference real chat_ids)
  telegram_updates -> dropped (runtime state; staging starts empty)
  job_state        -> leases cleared, attempts preserved
  everything else  -> byte-identical copy (history must match production)

After building, asserts: no numeric chat_id remains in subscribers or
deliveries, and row counts of all history tables equal the source.

Usage:
  copy data\\predbot.db staging_src_copy.db
  python workers/scripts/build_staging_fixture.py --src staging_src_copy.db --out staging_fixture.db
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys

NUMERIC_CHAT = re.compile(r"^-?\d+$")

HISTORY_TABLES = (
    "picks", "prediction_memberships", "prediction_cache", "provider_cache",
    "match_results", "match_odds", "match_odds_ext", "history_fetches",
    "participant_aliases", "provider_identity", "canonical_events",
    "canonical_participants", "ratings", "results_seen", "api_budget",
    "calibration", "sgo_usage", "kv", "live_fixture_state",
    "custom_pick_candidates", "custom_pick_pools", "posts",
)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    live = {os.path.abspath(p) for p in
            ("predictions-bot/data/predbot.db", "data/predbot.db")}
    if os.path.abspath(args.src) in live or os.path.abspath(args.out) in live:
        print("refusing to read/write a live database path", file=sys.stderr)
        return 2
    if os.path.abspath(args.src) == os.path.abspath(args.out):
        print("src and out must differ", file=sys.stderr)
        return 2

    with open(args.src, "rb") as fh_in, open(args.out, "wb") as fh_out:
        fh_out.write(fh_in.read())
    con = sqlite3.connect(args.out)

    before = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in HISTORY_TABLES}
    src_subs = con.execute("SELECT COUNT(*) FROM subscribers").fetchone()[0]
    src_deliv = con.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0]

    existing = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}

    con.execute("DELETE FROM subscribers")
    con.execute("INSERT INTO subscribers(chat_id,username,first_name,timezone,subscribed_at,active)"
                " VALUES('STAGING_1','staging_one','Staging','Africa/Lagos','2026-01-01T00:00:00Z',1)")
    con.execute("INSERT INTO subscribers(chat_id,username,first_name,timezone,subscribed_at,active)"
                " VALUES('STAGING_2','staging_two','Staging','','2026-01-01T00:00:00Z',1)")
    con.execute("DELETE FROM deliveries")
    if "telegram_updates" in existing:
        con.execute("DELETE FROM telegram_updates")
    if "job_state" in existing:
        con.execute("UPDATE job_state SET lease_owner=NULL, lease_until=NULL")
    con.commit()

    problems = []
    for (chat_id,) in con.execute("SELECT chat_id FROM subscribers").fetchall():
        if NUMERIC_CHAT.match(str(chat_id)):
            problems.append(f"subscribers still holds numeric chat_id {chat_id}")
    if con.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0] != 0:
        problems.append("deliveries not empty")
    for t in HISTORY_TABLES:
        got = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        if got != before[t]:
            problems.append(f"{t}: {before[t]} -> {got} (history must be identical)")
    con.close()

    print(f"sanitized: subscribers {src_subs} -> 2 synthetic, deliveries {src_deliv} -> 0, "
          f"telegram_updates emptied, leases cleared")
    if problems:
        for p in problems:
            print(f"FAIL {p}", file=sys.stderr)
        return 1
    print(f"fixture OK: {len(HISTORY_TABLES)} history tables identical, no production chat IDs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
