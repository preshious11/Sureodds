"""Weekly Pro value shadow report (read-only). Prints markdown against the go-live criteria.

  python tools/pro_value_report.py --db data/pro_value_shadow.db [--remaining 212] [--reset 2026-11-01]
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from predbot.config import Config  # noqa: E402
from predbot.pro_value.report import render_markdown, weekly_report  # noqa: E402


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default="")
    p.add_argument("--remaining", type=int, default=None, help="current OddsAPI balance, if known")
    p.add_argument("--reset", default="2026-11-01",
                   help="the credit reset the go-live two-week clock starts from (YYYY-MM-DD, UTC)")
    args = p.parse_args(argv)
    cfg = Config()
    now = int(time.time())
    reset = dt.datetime.strptime(args.reset, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)
    report = weekly_report(args.db or cfg.pro_value_db_path, now, reset_ts=int(reset.timestamp()),
                           remaining=args.remaining, max_price_age_min=cfg.pro_value_max_price_age_min)
    print(render_markdown(report), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
