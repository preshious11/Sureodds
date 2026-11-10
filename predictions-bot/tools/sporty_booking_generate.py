#!/usr/bin/env python3
"""Validate Top Picks against SportyBet, optionally create one verified code.

Dry-run by default; live creation requires both --live and
SPORTYBET_BOOKING_ENABLED=true. Output never contains a booking code.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from predbot.config import load_config
from predbot.sporty_booking import (
    BookingError,
    BookingQuota,
    BookingRunLock,
    Selection,
    SportyBooking,
    default_quota_path,
    derive_competition_bindings,
    load_top_picks,
)
from predbot.sporty_booking_delivery import (
    VERIFIED_STATUS,
    preflight_owner_delivery,
    send_owner_booking_result,
)
from predbot.storage.db import Database
from predbot.util.timeutil import to_local


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--date', help='Top Picks date (YYYY-MM-DD, default: configured local date)')
    parser.add_argument('--db', help='Prediction database path (default: DB_PATH)')
    parser.add_argument('--live', action='store_true',
                        help='Create one code; also requires SPORTYBET_BOOKING_ENABLED=true')
    return parser.parse_args(argv)


def _emit(status, **details):
    print(json.dumps({'status': status, **details}, sort_keys=True))


def run(args, *, cfg=None, db_factory=Database, transport=None):
    cfg = cfg or load_config()
    day = args.date or to_local(datetime.now(timezone.utc), cfg.timezone).date().isoformat()
    try:
        parsed_day = date.fromisoformat(day)
        if parsed_day.isoformat() != day:
            raise ValueError
    except ValueError:
        _emit('blocked', reason='invalid_date')
        return 2
    if args.live and not cfg.sportybet_booking_enabled:
        _emit('blocked', reason='booking_feature_disabled')
        return 2

    db_path = args.db or cfg.db_path
    quota_path = cfg.sportybet_booking_quota_db or default_quota_path(db_path)
    lock_path = quota_path + '.lock'
    db = None
    try:
        with BookingRunLock(lock_path):
            db = db_factory(db_path, read_only=True)
            rows, revision = load_top_picks(db, day)
            selections = [Selection.from_candidate(row) for row in rows]
            adapter = SportyBooking({}, transport=transport)
            adapter.validate_selections(selections, require_bindings=False)
            tournaments, events = adapter.fetch_catalogue(selections)
            bindings = derive_competition_bindings(rows, tournaments)
            adapter = SportyBooking(bindings, transport=adapter.transport)
            resolved = adapter.prepare_creation(selections, events=events)
            if not args.live:
                _emit('validated', collection='top', date=day, revision=revision,
                      selections=len(resolved))
                return 0

            owner = preflight_owner_delivery(cfg)
            if not owner['ready']:
                _emit('blocked', reason=owner['failure'])
                return 1
            if db.active_ticket_revision(day) != revision:
                raise BookingError('active_revision_changed')
            quota = BookingQuota(quota_path, cfg.sportybet_booking_max_per_day,
                                 prediction_db=db_path)
            quota_day = datetime.now(timezone.utc).date().isoformat()
            if quota.used(quota_day) >= cfg.sportybet_booking_max_per_day:
                raise BookingError('quota_exhausted')
            result = adapter.create_booking_code(selections, quota=quota, events=events)
            if (not result.programmatic_verified
                    or result.verification_reason != VERIFIED_STATUS):
                raise BookingError('verification_required')
            receipt = send_owner_booking_result(
                cfg, day=day, revision=revision, selection_count=len(resolved),
                code=result.code, verification=VERIFIED_STATUS)
            if not receipt['sent']:
                _emit('created_but_owner_delivery_failed', date=day,
                      revision=revision, selections=len(resolved),
                      reason=receipt['failure'])
                return 1
            _emit('verified_and_delivered', date=day, revision=revision,
                  selections=len(resolved), verification=VERIFIED_STATUS,
                  recipient='owner_private')
            return 0
    except BookingError as exc:
        _emit('blocked', reason=exc.status)
        return 1
    except (OSError, sqlite3.Error):
        _emit('blocked', reason='database_or_lock_unavailable')
        return 1
    finally:
        if db is not None:
            db.close()


def main(argv=None):
    args = parse_args(argv)
    return run(args)


if __name__ == '__main__':
    raise SystemExit(main())
