"""Standalone experiment; never imported by bot startup or delivery.

Offline inventory:
  python tools/sporty_booking.py inventory --db data/predbot.db --output data/booking-inventory.json
Live (explicit; no .env, D1, scheduler or prediction-provider access):
  python tools/sporty_booking.py create --manifest data/booking-manifest.json --live --output data/booking-result.json

Manifest: {"selections":[<unchanged canonical archived rows>],
 "competition_bindings":{"<canonical competition id>":"<observed Sporty tournament id>"}}
Bindings must come from an actual SportyBet catalogue; never invent IDs.
Output may contain a real returned share code, but never cookies/user/session data.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from predbot.sporty_booking import Selection, SportyBooking, BookingError, Resolved
from predbot.markets.contracts import market_contract_reason, ambiguous_contract
from predbot.util.timeutil import parse_iso


def inventory(path, now=None):
    """Read-only source; no Database constructor/migrations and no raw extras."""
    now = now or datetime.now(timezone.utc)
    path = Path(path).resolve(strict=True)
    connection = sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
    connection.execute('PRAGMA query_only=ON')
    summary, samples = {}, {}
    try:
        for candidate_id,raw in connection.execute('SELECT candidate_id,payload_json FROM custom_pick_candidates ORDER BY candidate_id'):
            row = json.loads(raw)
            sport = row.get('sport','unknown')
            count = summary.setdefault(sport,dict(rows=0,valid_contract=0,future=0))
            count['rows'] += 1
            try:
                s = Selection.from_candidate(row)
            except BookingError:
                continue
            reason = market_contract_reason(s.sport,s.market,s.selection,s.period,s.stat,s.entity,s.line)
            if reason or ambiguous_contract(row.get('market_name')):
                continue
            count['valid_contract'] += 1
            start = parse_iso(s.start_utc)
            future = start is not None and start > now
            count['future'] += int(future)
            # Ordinary exact score markets only; diversify samples by sport.
            if s.period=='full' and s.entity=='all' and s.stat=='points' and s.market in ('result3','result2','total','handicap'):
                sample = {'candidate_id':candidate_id,'selection':asdict(s),
                          'availability':'requires_live_resolution' if future else 'expired_fixture_not_generator_failure',
                          'sporty_identifiers_verified':False}
                samples.setdefault(sport,sample)
    finally:
        connection.close()
    return dict(source=str(path),read_only=True,checked_at=now.isoformat(),summary=summary,
                samples=list(samples.values())[:5],booking_codes=[],
                note='Contract validity is not live availability, eligibility or verified bookmaker mapping.')


def run(args):
    output = Path(args.output).resolve()
    if output.suffix != '.json' or output.exists():
        raise BookingError('output_requires_new_json_path')
    if args.command=='inventory':
        result=inventory(args.db)
    else:
        if not args.live:
            raise BookingError('explicit_live_flag_required')
        manifest=json.loads(Path(args.manifest).read_text(encoding='utf-8'))
        adapter=SportyBooking(manifest['competition_bindings'])
        if args.command=='create':
            rows=[Selection.from_candidate(r) for r in manifest['selections']]
            result=asdict(adapter.create_booking_code(rows,accept_odds_changes=args.accept_odds_changes))
            result['competition_bindings']=adapter.bindings
        else:
            # Only a real previous result manifest can supply expected IDs.
            expected=[Resolved(Selection(**r['original']),r['event_id'],r['tournament_id'],
                               r['market_id'],r['specifier'],r['outcome_id'],r['current_odds'])
                      for r in manifest['selections']]
            code=manifest['code']
            result=dict(code=code,status='owner_confirmation_required',programmatic_verified=True,
                        owner_verified=False,selections=[asdict(r) for r in adapter.verify_booking_code(code,expected)])
    output.write_text(json.dumps(result,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    print(json.dumps({'status':result.get('status','inventory_complete'),'report':str(output)}))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('command',choices=('inventory','create','verify'))
    p.add_argument('--db')
    p.add_argument('--manifest')
    p.add_argument('--output',required=True)
    p.add_argument('--live',action='store_true')
    p.add_argument('--accept-odds-changes',action='store_true')
    args=p.parse_args()
    if args.command=='inventory' and not args.db:
        p.error('inventory requires --db')
    if args.command!='inventory' and not args.manifest:
        p.error('create/verify require --manifest')
    try:
        run(args)
    except BookingError as exc:
        print(json.dumps({'status':exc.status,'owner_verified':False}));return 1
    except (ValueError,TypeError,KeyError,OSError,sqlite3.Error):
        print(json.dumps({'status':'invalid_local_manifest_or_source','owner_verified':False}));return 1
    return 0


if __name__=='__main__':
    raise SystemExit(main())
