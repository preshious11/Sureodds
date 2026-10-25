"""Pro value picks - SHADOW runner. Nothing here publishes; results go to the shadow database only.

  run      read the raw OddsAPI files saved by the shared generation fetch, take one SportyBet 1X2 read in the same
           run, apply the Pro value gates, store the shadow results.
  closing  capture closing 1X2 prices for shadow picks that are shortly before kickoff (the only new OddsAPI calls;
           capped by PRO_VALUE_CREDIT_CAP and the 80-credit reserve). Needs --live.

Both commands do nothing unless PRO_VALUE_SHADOW_ENABLED=true. Secrets are never printed.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from predbot.config import Config  # noqa: E402
from predbot.pro_value import botd_archive, closing, shadow  # noqa: E402
from predbot.pro_value.credits import CreditGuard  # noqa: E402
from predbot.pro_value.oddsapi_raw import OddsApiClient  # noqa: E402
from predbot.pro_value.sporty1x2 import fetch_sporty_1x2  # noqa: E402
from predbot.pro_value.store import ShadowStore  # noqa: E402


def _cmd_run(cfg, args) -> int:
    if not cfg.pro_value_shadow_enabled:
        print("pro value shadow is disabled (PRO_VALUE_SHADOW_ENABLED=false); nothing done")
        return 0
    from predbot.pro_value.sporty_catalogue import CatalogueTransport
    now = int(time.time())
    raw_dir = args.raw_dir or cfg.pro_value_raw_dir
    if not raw_dir:
        print("no raw OddsAPI directory given (--raw-dir or PRO_VALUE_RAW_DIR)")
        return 2
    botd = json.load(open(args.botd_json, encoding="utf-8")) if args.botd_json else None
    archive_rows, archive_info = None, {}
    if args.archive_db:
        # Read-only: the generation run's candidate archive, opened with mode=ro.
        day = args.day or botd_archive.latest_pick_date(args.archive_db)
        if day:
            archive_rows = botd_archive.load_archive_rows(args.archive_db, day)
        archive_info = {"pick_date": day, "candidates": len(archive_rows or [])}
    sporty = fetch_sporty_1x2(CatalogueTransport(limit=2, total_timeout=30), os.path.join(raw_dir, "sportybet"), now)
    store = ShadowStore(cfg.pro_value_db_path)
    try:
        result = shadow.run_shadow(cfg, raw_dir=raw_dir, sporty_events=sporty, store=store, now_ts=now, botd=botd,
                                   archive_rows=archive_rows)
    finally:
        store.close()
    result["archive"] = archive_info
    print(json.dumps(result, indent=1, default=str))
    return 0


def _cmd_closing(cfg, args) -> int:
    if not cfg.pro_value_shadow_enabled:
        print("pro value shadow is disabled; nothing done")
        return 0
    if not args.live:
        print("closing capture makes OddsAPI calls: re-run with --live to proceed")
        return 2
    if not cfg.odds_api_key:
        print("ODDS_API_KEY is not configured")
        return 2
    import requests
    now = int(time.time())
    # Free balance read: /sports does not count against the quota (OddsAPI docs).
    probe = requests.get("https://api.the-odds-api.com/v4/sports", params={"apiKey": cfg.odds_api_key}, timeout=30)
    day = time.strftime("%Y-%m-%d", time.gmtime(now))
    store = ShadowStore(cfg.pro_value_db_path)
    try:
        # The daily cap is shared by every scheduled run that day, so today's spend is read from the shadow DB.
        spent_before = store.spent_on(day)
        guard = CreditGuard(reserve=cfg.oddsapi_reserve_credits, cap=cfg.pro_value_credit_cap, spent_today=spent_before)
        guard.observe(probe.headers.get("x-requests-remaining"))
        client = OddsApiClient(cfg.odds_api_key, os.path.join(cfg.pro_value_raw_dir or "data", "closing_raw"), guard)
        totals = closing.run_closing(client, store, now, closing.CAPTURE_HI_MIN, guard, lo_min=closing.CAPTURE_LO_MIN)
        store.add_spent(day, guard.spent_today - spent_before)
    finally:
        store.close()
    print(json.dumps({"day_utc": day, "closing": totals, "credits_remaining": guard.remaining,
                      "credits_spent_today": guard.spent_today, "reserve": guard.reserve, "cap": guard.cap},
                     indent=1))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--raw-dir", default="")
    r.add_argument("--botd-json", default="", help="Bet of the Day candidates in rank order: [{match_key, outcome}]")
    r.add_argument("--archive-db", default="", help="generation DB holding ticket_candidates (opened read-only)")
    r.add_argument("--day", default="", help="pick date in the archive (default: latest)")
    c = sub.add_parser("closing")
    c.add_argument("--live", action="store_true")
    args = p.parse_args(argv)
    cfg = Config()
    return _cmd_run(cfg, args) if args.cmd == "run" else _cmd_closing(cfg, args)


if __name__ == "__main__":
    raise SystemExit(main())
