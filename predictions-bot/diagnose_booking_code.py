"""One explicit, sanitized booking-code retrieval check. Never runs from tests or startup.

Usage (PowerShell):
    $env:BOOKING_DIAG_CODE = '<one known code>'
    python diagnose_booking_code.py --source sportybet-direct --live
    python diagnose_booking_code.py --source mybetcode --bookmaker bet9ja --live

Exactly ONE GET per run, to a fixed allowlisted host. The code is read from the
environment (not argv/shell history), never printed in full and never persisted
(the cache keys on a salted hash). MyBetCode reads MYBETCODE_API_KEY from the
environment/.env; without it the result is NOT_TESTED_NO_KEY. The sidecar used
for the ledger/cache is ./data/booking_diagnostic.sqlite3, not the bot's DB.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

from predbot.ticket_audit import booking as bk
from predbot.ticket_audit.booking_sources import MyBetCodeResolver, SportyBetDirectResolver

FIELDS = ('sport', 'competition', 'home', 'away', 'kickoff', 'market', 'market_label', 'line', 'specifier',
          'outcome', 'outcome_label', 'odds', 'period', 'stat', 'entity', 'selected_participant',
          'provider_market_id')


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument('--source', choices=('sportybet-direct', 'mybetcode'), required=True)
    p.add_argument('--bookmaker', choices=tuple(bk.BOOKMAKERS), default='sportybet')
    p.add_argument('--live', action='store_true', help='required: confirms ONE real GET may be made')
    p.add_argument('--env-file', default='.env')
    a = p.parse_args(argv)
    report = {'source': a.source, 'bookmaker': a.bookmaker, 'live_requests': 0,
              'checked_at': datetime.now(timezone.utc).isoformat()}
    raw = os.environ.get('BOOKING_DIAG_CODE', '')
    if not raw:
        report['result'] = 'NOT_TESTED_NO_CODE'
        return emit(report)
    try:
        code = bk.normalize_code(raw)
    except bk.BookingCodeFailure:
        report['result'] = bk.MALFORMED_CODE
        return emit(report)
    report['code'] = bk.masked(code)
    if not a.live:
        report['result'] = 'NOT_RUN_PASS_--live'
        return emit(report)
    store = bk.BookingStore(Path('data') / 'booking_diagnostic.sqlite3')
    if a.source == 'mybetcode':
        settings = bk.BookingSettings.from_environment(a.env_file)
        if not settings.mybetcode_key:
            report['result'] = 'NOT_TESTED_NO_KEY'
            return emit(report)
        resolver = MyBetCodeResolver(store, settings.mybetcode_key, 1_000, 30_000)
    else:
        if a.bookmaker != 'sportybet':
            report['result'] = bk.UNSUPPORTED_BOOKMAKER
            return emit(report)
        resolver = SportyBetDirectResolver(store, 1_000)
    now = datetime.now(timezone.utc)
    key = store.key(a.bookmaker, code)
    try:
        report['live_requests'] = 1
        result = resolver.retrieve(a.bookmaker, code, key, now)
    except bk.BookingCodeFailure as exc:
        report.update(result=exc.status, http=resolver.last_diagnostic)
        return emit(report)
    except Exception as exc:  # report the type only, never provider text
        report.update(result='ERROR', error_type=type(exc).__name__, http=resolver.last_diagnostic)
        return emit(report)
    ticket_ok = True
    try:
        bk.to_ticket(result)
    except Exception:
        ticket_ok = False
    report.update(result=result.status, http=resolver.last_diagnostic, legs=len(result.selections),
                  declared_count=result.declared_count, total_odds=result.total_odds,
                  ticket_unresolved=list(result.unresolved), all_legs_normalized=ticket_ok and result.status == bk.OK,
                  selections=[{**{f: getattr(l, f) for f in FIELDS}, 'unresolved': list(l.unresolved)}
                              for l in result.selections],
                  field_coverage={f: sum(getattr(l, f) is not None for l in result.selections) for f in FIELDS})
    return emit(report)


def emit(report):
    print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == '__main__':
    sys.exit(main())
