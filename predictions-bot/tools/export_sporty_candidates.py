"""Read-only canonical archive export. No config, provider, Telegram or D1 calls.

Empty output is a blocked search result, never a usable booking slip. Tournament
bindings must subsequently come from an observed SportyBet catalogue.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from predbot.sporty_booking import Selection, SportyBooking, BookingError, decimal
from predbot.markets.contracts import market_contract_reason
from predbot.util.timeutil import parse_iso

FIELDS=('canonical_event_id','canonical_competition_id','sport','home','away',
        'start_utc','market','period','stat','entity','selection','line','price')
TABLES=('custom_pick_candidates','ticket_candidates','picks')


def inspect(paths,now=None):
    now=now or datetime.now(timezone.utc)
    sources=[]; selected=[]; seen=set(); breakdown=Counter()
    for path in paths:
        path=Path(path).resolve(strict=True)
        db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
        db.row_factory=sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        source={'source':str(path),'tables':{},'caches':{}}
        try:
            tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in TABLES:
                if table not in tables:
                    source['tables'][table]={'present':False}; continue
                columns={r[1] for r in db.execute('PRAGMA table_info('+table+')')}
                stats={'present':True,'rows':0,'future_valid':0,'rejections':{}}
                reasons=Counter()
                for record in db.execute('SELECT * FROM '+table):
                    stats['rows']+=1
                    try:
                        row=json.loads(record['payload_json']) if 'payload_json' in columns else dict(record)
                        s=Selection.from_candidate(row)
                        if not all(isinstance(getattr(s,k),str) and getattr(s,k).strip()
                                   for k in ('canonical_event_id','competition','home','away')):
                            raise BookingError('missing_fixture_identity')
                        reason=market_contract_reason(s.sport,s.market,s.selection,s.period,s.stat,s.entity,s.line)
                        if reason: raise BookingError('invalid_contract')
                        decimal(s.price)
                        start=parse_iso(s.start_utc)
                        if start is None or start<=now: raise BookingError('expired_or_invalid_kickoff')
                        stats['future_valid']+=1
                        key=tuple(row.get(k) for k in FIELDS if k!='price')
                        if key not in seen:
                            seen.add(key); breakdown[(s.sport,s.market)]+=1
                            # Current live acquisition is football only. Retain
                            # original values without aliasing or line rewrites.
                            if s.sport=='football' and SportyBooking.market_ids(s):
                                selected.append({k:row.get(k) for k in FIELDS})
                    except BookingError as exc:
                        reasons[exc.status]+=1
                    except (TypeError,ValueError,KeyError):
                        reasons['malformed_record']+=1
                stats['rejections']=dict(reasons)
                source['tables'][table]=stats
            for table in ('provider_cache','prediction_cache'):
                if table not in tables: continue
                source['caches'][table]=[dict(r) for r in db.execute(
                    'SELECT local_date,expires_at FROM '+table+' ORDER BY local_date')]
        finally:
            db.close()
        sources.append(source)
    # One exact selection per fixture, at most five; never implies promotion.
    output=[]; fixtures=set()
    for row in selected:
        if row['canonical_event_id'] not in fixtures and len(output)<5:
            output.append(row); fixtures.add(row['canonical_event_id'])
    return {'checked_at':now.isoformat(),'read_only':True,'sources':sources,
            'future_valid_unique_by_sport_market':[
                {'sport':s,'market':m,'count':n} for (s,m),n in sorted(breakdown.items())],
            'status':'pending_sporty_resolution' if output else 'blocked_no_upcoming_bookable_candidates',
            'selections':output,'competition_bindings':{},
            'note':'Unchanged archived contracts and odds. Observed competition bindings and exact live resolution are still required. No generation or booking performed.'}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--db',action='append',required=True)
    p.add_argument('--output',required=True)
    args=p.parse_args()
    output=Path(args.output)
    if output.suffix!='.json' or output.exists(): p.error('output must be a new JSON path')
    result=inspect(args.db)
    with output.open('x',encoding='utf-8') as f:
        json.dump(result,f,indent=2,allow_nan=False);f.write('\n')
    print(json.dumps({'status':result['status'],'exported':len(result['selections']),
                      'future_by_sport_market':result['future_valid_unique_by_sport_market']}))


if __name__=='__main__': main()
