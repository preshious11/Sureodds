"""Live verification: render /top, /daily and the summary from a TODAY SQLite
snapshot through the REAL runtime paths - zero provider calls.

Run:  python verify_live_output.py
"""
from __future__ import annotations

import math
import os
import sys
import tempfile

from predbot.bot import pagination
from predbot.bot.broadcast import picks_text
from predbot.bot.callbacks import handle_command
from predbot.cache.fetcher import UnifiedFetcher
from predbot.cache.models import DailySnapshot, PickRecord
from predbot.config import Config
from predbot.storage.db import Database
from predbot.util.timeutil import to_local, utcnow
from tests.test_subscribers_broadcast import FakeTG


def rec(pick_id, tier, sport, home, away, label, market, selection, line, stat,
        entity, price, kickoff):
    return PickRecord(pick_id=pick_id, tier=tier, rank=pick_id, sport=sport,
                      league="MLB" if sport == "baseball" else "Premier League",
                      home=home, away=away, kickoff_utc=kickoff, label=label,
                      market=market, selection=selection, line=line, stat=stat,
                      entity=entity, price=price, prob=0.71, ev=0.06,
                      pick_date="2026-09-16")


# --- today's cached snapshot shape (the exact screenshot cases, canonical fields)
SNAPSHOT = DailySnapshot(
    date="", generated_at="", origin="sqlite", status="COMPLETE",
    top_picks=[
        # screenshot case 1: was "Los Angeles Dodgers Over 8.5" + "Batting_Hits"
        rec(1, "top", "baseball", "Los Angeles Dodgers", "Colorado Rockies",
            "Los Angeles Dodgers Over 8.5", "prop", "over", 8.5, "Batting_Hits",
            "home", 2.67, "2026-09-16T23:10:00Z"),
        # screenshot case 2: was "Sam Antonacci Total Bases Over/Under"
        rec(2, "top", "baseball", "St. Louis Cardinals", "Milwaukee Brewers",
            "Sam Antonacci Total Bases Over/Under", "prop", "over", 1.5, "totalBases",
            "Sam Antonacci", 1.87, "2026-09-16T22:45:00Z"),
    ],
    daily_picks=[
        rec(3, "daily", "football", "Chelsea", "Brentford", "", "total", "over",
            2.5, "points", "all", 1.86, "2026-09-16T18:30:00Z"),
        rec(4, "daily", "football", "Chelsea", "Brentford", "", "btts", "yes",
            None, "points", "all", 1.85, "2026-09-16T18:30:00Z"),
    ],
)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    cfg = Config(timezone="Africa/Lagos")
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "predbot.db")

        # 1) write today's snapshot the production way (SQLite, atomic, versioned)
        db = Database(path)
        seed = UnifiedFetcher(None, db, cfg)         # chain=None: any API call explodes
        day = to_local(utcnow(), cfg.timezone).date().isoformat()
        SNAPSHOT.date = day
        seed._store(SNAPSHOT, utcnow())
        db.close()

        # 2) "restart": fresh process, same SQLite - the documented restart path
        db = Database(path)
        service = UnifiedFetcher(None, db, cfg)      # zero provider calls, cache hit
        snap = service.get_daily_snapshot()
        assert snap.total_picks() == 4

        # 3) render EXACTLY what the live bot renders
        tg = FakeTG()
        db.subscriber_add("42", timezone="Africa/Lagos")
        handle_command("/top", "42", service=service, tg=tg, cfg=cfg, db=db)
        handle_command("/daily", "42", service=service, tg=tg, cfg=cfg, db=db)
        summary = picks_text(snap, cfg.timezone)

        print("=" * 60)
        print(tg.sends[0]["text"])
        print("=" * 60)
        print(tg.sends[1]["text"])
        print("=" * 60)
        print(summary)
        print("=" * 60)

        # 4) odds verification: displayed picks vs combined, per section
        top = math.prod(p.price for p in snap.top_picks)
        daily = math.prod(p.price for p in snap.daily_picks)
        print("individual odds:", [p.price for p in snap.view_items("daily")])
        print(f"top product    : {top:.4f}   daily product: {daily:.4f}")
        assert f"Combined Odds: {top:.2f}" in summary
        assert f"Combined Odds: {daily:.2f}" in summary
        assert "Total Picks: 2" in summary and "Total Picks: 2" in summary
        for bad in ("Batting_Hits", "Over/Under", "Total_Bases", "result3", "_"):
            assert bad not in summary and bad not in tg.sends[0]["text"] \
                and bad not in tg.sends[1]["text"], bad
        db.close()
        print("VERIFICATION OK - zero provider calls, no raw identifiers, "
              "combined odds match displayed picks")


if __name__ == "__main__":
    main()
