"""GitHub Actions entrypoint: live match monitoring + settlement against a
D1-synced scratch SQLite copy (SureOdds Cloudflare migration, 2026-10-15).

Reuses live_results.run() UNCHANGED (status detection, settlement, Elo,
calibration) via its `sink` parameter: match-start/match-end messages are
recorded into match_events (0009_match_events.sql) instead of being sent
directly. This script never calls the Telegram API. The Cloudflare
Worker's own Cron sweep (sweepMatchEvents, workers/src/cron.ts) is the
only thing that ever delivers a match event, to the group and the owner
independently.

Never relies on a persistent local SQLite database: `db_path` is always a
fresh scratch copy pulled from D1 for this run (the workflow's "Pull
production base" step, the same wrangler d1 export pattern already used by
stage5-acceptance-production.yml), and every write this script makes is
synced back to D1 by the workflow's own d1_sync.py call afterward — nothing
here is the system of record.
"""
from __future__ import annotations

import logging
import sys

from predbot.config import Config
from predbot.jobs import live_results
from predbot.providers.chain import ResultsResolver, build_providers
from predbot.storage.db import Database
from predbot.util.timeutil import utcnow

log = logging.getLogger(__name__)


def ensure_match_events_table(db: Database) -> None:
    """Defensive: the scratch copy may predate 0009_match_events.sql being
    applied to production D1 (migrations are a separate, approval-gated
    step from this script). Matches that migration's schema exactly, so a
    later real migration run is a harmless no-op (CREATE TABLE IF NOT
    EXISTS) rather than a conflict."""
    db.execute(
        """CREATE TABLE IF NOT EXISTS match_events (
            pick_date TEXT NOT NULL,
            fixture_key TEXT NOT NULL,
            event_type TEXT NOT NULL CHECK(event_type IN ('start', 'end')),
            text TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (pick_date, fixture_key, event_type)
        )"""
    )


def make_d1_event_sink(db: Database):
    """`sink(pick_date, fixture_key, event_type, text)` — INSERT OR IGNORE
    on the natural key is the duplicate-processing guard: a re-run that
    finds the same fixture at the same status again is a silent no-op."""
    def sink(pick_date: str, fixture_key: str, event_type: str, text: str) -> None:
        db.execute(
            "INSERT OR IGNORE INTO match_events (pick_date, fixture_key, event_type, text, created_at) "
            "VALUES (?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ','now'))",
            (pick_date, fixture_key, event_type, text),
        )
    return sink


def run(db_path: str) -> dict:
    """Entry point for the GitHub Actions workflow step. Returns a small
    summary dict for the job log — never logs secrets or full payloads."""
    cfg = Config()
    if not cfg.live_updates_enabled:
        return {"skipped": "LIVE_UPDATES_ENABLED is false"}
    db = Database(db_path)
    try:
        ensure_match_events_table(db)
        before = db.query("SELECT COUNT(*) AS n FROM match_events")[0]["n"]
        providers = build_providers(cfg, db)
        resolver = ResultsResolver(providers)
        sink = make_d1_event_sink(db)
        # tg=None is safe here: sink mode never touches the `tg` parameter
        # (every deliver_live/deliver_community_live/deliver_owner_live
        # call is bypassed in favor of deliver_match_event — see
        # predbot/jobs/live_results.py's `if sink is not None` branches).
        live_results.run(resolver, db, cfg, tg=None, now=utcnow(), sink=sink)
        after = db.query("SELECT COUNT(*) AS n FROM match_events")[0]["n"]
        return {"events_recorded": after - before}
    finally:
        db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) != 2:
        print("usage: python -m predbot.jobs.live_monitor_d1 <scratch_db_path>", file=sys.stderr)
        raise SystemExit(2)
    print(run(sys.argv[1]))
