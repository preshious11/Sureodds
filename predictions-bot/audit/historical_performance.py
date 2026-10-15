"""Read-only audit: frozen recorded forecasts, equal unit stakes, no providers.

No Database wrapper (and hence no migrations) is used. Outcomes are joined only
AFTER deterministic selections. Runtime snapshots are separate cohorts.
"""
import hashlib
import itertools
import json
import math
from pathlib import Path
import random
import sqlite3
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import date, timedelta
from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'predictions-bot'))
from predbot.config import Config
from predbot.providers.base import Event, MarketLine, MatchResult
from predbot.model.ensemble import Scored
from predbot.markets.settlement import grade
from predbot.selection.evidence import assess
from predbot.selection.daily import _fits, select_daily
from predbot.selection.top import promotion_reason, select_top, ranked
from predbot.selection.search import ticket_key
from predbot.util.timeutil import parse_iso

AS_OF = date(2026, 10, 13)
PATHS = ['data/predbot.db', 'predictions-bot/data/predbot.db',
         'predictions-bot/data/predbot.backup-before-clear.db']
DECIDED = {'won', 'lost', 'half_won', 'half_lost'}


def connect(path):
    db = sqlite3.connect((ROOT/path).resolve().as_uri()+'?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    return db


def digest(path):
    return hashlib.sha256((ROOT/path).read_bytes()).hexdigest()


def coord(row):
    return (row['sport'], row['home'], row['away'], row['start_utc'], row['market'],
            row['period'], row['stat'], row['entity'], row['line'], row['selection'])


def fixture(row):
    return (row['sport'], row['home'], row['away'], row['start_utc'])


def profit(row, status=None):
    st = status or row['status']
    return {'won': row['price']-1, 'lost': -1, 'half_won': (row['price']-1)/2,
            'half_lost': -.5, 'push': 0, 'void': 0}[st]


def wilson(wins, n):
    if not n:
        return None
    z = 1.959963984540054
    p = wins/n; den = 1+z*z/n
    center = (p+z*z/(2*n))/den
    half = z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/den
    return [center-half, center+half]


def roi_interval(rows):
    clusters = defaultdict(list)
    for row in rows:
        clusters[fixture(row)].append(profit(row))
    if len(clusters) < 2:
        return None
    clusters = list(clusters.values()); rng = random.Random(415)
    samples = []
    for _ in range(5000):
        selected = [clusters[rng.randrange(len(clusters))] for _ in clusters]
        samples.append(sum(map(sum, selected))/sum(map(len, selected)))
    samples.sort()
    return [samples[125], samples[4874]]


def metrics(rows):
    resolved = [r for r in rows if r['status'] in DECIDED or r['status']=='push']
    binary = [r for r in rows if r['status'] in ('won', 'lost')]
    wins = sum(r['status']=='won' for r in binary)
    return {'records': len(rows), 'statuses': dict(Counter(r['status'] for r in rows)),
            'decisive_full_results': len(binary), 'wins': wins,
            'full_win_rate': wins/len(binary) if binary else None,
            'nominal_wilson_95': wilson(wins, len(binary)),
            'settled_stakes_excluding_unresolved_voids': len(resolved),
            'unique_settled_fixtures': len({fixture(r) for r in resolved}),
            'average_odds_all_records': sum(r['price'] for r in rows)/len(rows) if rows else None,
            'average_odds_resolved': sum(r['price'] for r in resolved)/len(resolved) if resolved else None,
            'unit_stake_profit': sum(profit(r) for r in resolved),
            'resolved_only_roi': sum(profit(r) for r in resolved)/len(resolved) if resolved else None,
            'roi_fixture_cluster_bootstrap_95': roi_interval(resolved),
            'mean_binary_probability': sum(r['prob'] for r in binary)/len(binary) if binary else None,
            'binary_brier': sum((r['prob']-(r['status']=='won'))**2 for r in binary)/len(binary) if binary else None}


def group_metrics(rows, function):
    grouped = defaultdict(list)
    for row in rows:
        grouped[str(function(row))].append(row)
    return {key: metrics(values) for key, values in sorted(grouped.items())}


def confidence_band(row):
    p = row['prob']
    return '<50%' if p < .5 else '50–59%' if p < .6 else '60–69%' if p < .7 else '70–79%' if p < .8 else '80–89%' if p < .9 else '90–100%'


def odds_band(row):
    p = row['price']
    return '<1.20' if p < 1.2 else '1.20–1.49' if p < 1.5 else '1.50–1.99' if p < 2 else '2.00–2.99' if p < 3 else '3.00+'


def known_result(row):
    payload = json.loads(row.get('result_json') or '{}')
    if payload.get('status') != 'final':
        return None
    return MatchResult(status='final', scores={k: tuple(v) for k,v in payload.get('scores', {}).items()},
                       stats={(s,p):tuple(v) for s,p,v in payload.get('stats',[])},
                       odd_scores=payload.get('odd_scores', {}), source=payload.get('source', ''))


def line(row):
    return MarketLine(row['market'], row['selection'], row['price'], period=row['period'],
                      stat=row['stat'], entity=row['entity'], line=row['line'],
                      provider_odd_id=row.get('provider_odd_id') or '')


def binary_market(row):
    if row['market'] in ('result3', 'dc', 'btts'):
        return True
    if row['market']=='result2' and row['period']=='full' and row['sport'] in ('tennis','basketball','baseball','mma'):
        return True
    return row['market'] in ('total','handicap') and row['line'] is not None and abs(row['line']%1-.5)<1e-9


def publication(db, day, kind, tables):
    stamps = [r[0] for r in db.execute('SELECT created_at FROM posts WHERE post_date=? AND kind IN (?,?)', (day, 'picks_'+kind, 'picks'))]
    stamps += [r[0] for r in db.execute('SELECT created_at FROM deliveries WHERE post_date=? AND kind IN (?,?,?)',
                                      (day, 'community_picks_'+kind, 'picks', 'picks_'+kind))]
    return min(stamps) if stamps else None


def result_for(row, bank):
    values = bank.get(fixture(row), [])
    grades = set()
    for reference, result in values:
        status = grade(line(row), result, row['sport'])
        if status in DECIDED or status=='push':
            grades.add(status)
    return next(iter(grades)) if len(grades)==1 else None


def evaluated_ticket(rows, bank):
    if not rows:
        return {'legs': 0, 'complete': False, 'reason': 'no_qualifying_combination'}
    outcomes = [result_for(r, bank) for r in rows]
    complete = all(outcomes)
    return {'legs': len(rows), 'combined_odds': math.prod(r['price'] for r in rows),
            'mean_probability': sum(r['prob'] for r in rows)/len(rows),
            'confidence_product': math.prod(r.get('rank_score') or r['prob'] for r in rows),
            'selections': [{'home': r['home'], 'away': r['away'], 'market': r['market'],
                            'selection': r['selection'], 'line': r['line'], 'price': r['price'],
                            'prob': r['prob'], 'start_utc': r['start_utc']} for r in rows],
            'outcomes': outcomes, 'complete': complete,
            'strict_ticket_hit': all(st=='won' for st in outcomes) if complete else None,
            'unit_stake_roi': math.prod(1+profit(r,st) for r,st in zip(rows,outcomes))-1 if complete else None}


def old_selectors():
    old_top = ModuleType('audit_original_top')
    old_daily = ModuleType('audit_original_daily')
    for mod, name in [(old_top,'top'), (old_daily,'daily')]:
        code = subprocess.check_output(['git','show','dcf3a17:predictions-bot/predbot/selection/'+name+'.py'], cwd=ROOT, text=True, encoding='utf8')
        if name=='daily':
            code = code.replace('from predbot.selection.top import promotion_reason, ranked', '')
            mod.promotion_reason, mod.ranked = old_top.promotion_reason, old_top.ranked
        exec(compile(code, '<frozen-original-'+name+'>', 'exec'), mod.__dict__)
    return old_top, old_daily


def main():
    report = {'as_of_application_date': str(AS_OF), 'sources': {}, 'method': {
        'stake': 'Equal hypothetical 1-unit stakes, recorded decimal prices; no estimated EV counted as profit',
        'void_policy': 'Unresolved administrative voids excluded, not assumed bookmaker refunds',
        'windows': 'Pick-date calendar windows: Oct 7–13 (7 days), Sep 14–Oct 13 (30 days)',
        'intervals': 'Nominal Wilson binomial intervals; ROI bootstrap clusters by fixture. Very small/missing-not-at-random samples prevent strong inference',
        'replays': 'Selectors/common binary subset only: no reconstruction of missing push distributions or future ratings',
        'selection_rule': 'Choose by recorded evidence rank and pre-match prices; join results after choosing. Never filter candidates by availability or outcome of settlement'}}
    before = {path: digest(path) for path in PATHS}
    sources = {}
    bank = defaultdict(list)
    for path in PATHS:
        db = connect(path)
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        rows = [dict(r) for r in db.execute('SELECT * FROM picks')]
        membership = defaultdict(set)
        if 'prediction_memberships' in tables:
            for row in db.execute('SELECT prediction_id,collection FROM prediction_memberships'):
                membership[row[0]].add(row[1])
        else:
            for row in rows:
                membership[row['id']] = {'all', row['tier']}
        for row in rows:
            row['_memberships'] = sorted(membership[row['id']])
            row['_publication'] = {kind: publication(db, row['pick_date'], kind, tables)
                                   for kind in ('top','daily') if kind in membership[row['id']]}
            row['_published'] = any(row['_publication'].values())
            row['_pre_start_record'] = bool(row.get('created_at') and row['created_at'] < row['start_utc'])
            result = known_result(row)
            if result:
                bank[fixture(row)].append((row, result))
        sources[path] = rows
        published = [r for r in rows if r['_pre_start_record'] and any(stamp and stamp<r['start_utc'] for stamp in r['_publication'].values())]
        cohort = {'rows': len(rows), 'published_before_start': len(published),
                  'unpublished_or_publication_unverified': len(rows)-len(published),
                  'status_counts': dict(Counter(r['status'] for r in rows)),
                  'null_canonical_identity': sum(not r.get('canonical_event_id') for r in rows),
                  'missing_precalibration_probability': sum(r.get('prob_raw') is None for r in rows),
                  'candidate_archive': {}, 'windows': {}, 'tickets': []}
        if 'custom_pick_candidates' in tables:
            for r in db.execute('SELECT prediction_date,COUNT(*) n,MIN(first_seen_at) first,MAX(generated_at) latest FROM custom_pick_candidates GROUP BY prediction_date'):
                cohort['candidate_archive'][r[0]] = dict(r)
        for window,days in [('7_days',7),('30_days',30),('all',None)]:
            start = AS_OF-timedelta(days=days-1) if days else date.min
            filtered = [r for r in rows if start <= date.fromisoformat(r['pick_date']) <= AS_OF]
            pub = [r for r in filtered if r in published]
            cohort['windows'][window] = {'all_recorded_forecasts': metrics(filtered), 'published': metrics(pub),
                'by_market_all': group_metrics(filtered,lambda r:r['market']),
                'by_market_published': group_metrics(pub,lambda r:r['market']),
                'by_confidence_all': group_metrics(filtered,confidence_band),
                'by_confidence_published': group_metrics(pub,confidence_band),
                'by_market_and_confidence_all': group_metrics(filtered,lambda r:r['market']+'/'+confidence_band(r)),
                'by_odds_all': group_metrics(filtered,odds_band),
                'by_league_all': group_metrics(filtered,lambda r:r['sport']+'/'+r['league']),
                'by_league_published': group_metrics(pub,lambda r:r['sport']+'/'+r['league'])}
        for day in sorted({r['pick_date'] for r in rows}):
            for kind in ('top','daily'):
                legs = [r for r in rows if r['pick_date']==day and kind in r['_memberships']]
                stamp = publication(db,day,kind,tables)
                if not legs and not stamp:
                    continue
                outcomes = [r['status'] if r['status'] in DECIDED or r['status']=='push' else None for r in legs]
                complete = bool(legs) and all(outcomes)
                cohort['tickets'].append({'date':day, 'collection':kind, 'publication':stamp, 'legs':len(legs),
                    'combined_odds': math.prod(r['price'] for r in legs) if legs else None,
                    'complete_realized_settlement':complete, 'published':bool(stamp),
                    'strict_hit': all(st=='won' for st in outcomes) if complete else None,
                    'unit_stake_roi': math.prod(1+profit(r,st) for r,st in zip(legs,outcomes))-1 if complete else None,
                    'recorded_outcomes': outcomes, 'all_legs_published_before_start': bool(stamp and legs and all(stamp<r['start_utc'] for r in legs)),
                    'unique_fixture_count':len({fixture(r) for r in legs})})
        report['sources'][path] = cohort
        db.close()
    # Exact identity/time only: no fuzzy retrospective fixture substitution.
    recovery = []
    for path, rows in sources.items():
        for row in rows:
            if row['status'] not in ('pending','void'):
                continue
            recovered = result_for(row,bank)
            if recovered:
                recovery.append({'source':path,'id':row['id'],'home':row['home'],'away':row['away'],
                                 'stored_status':row['status'],'read_only_regrade':recovered,
                                 'evidence':'Exact sport/home/away/kickoff match to an existing serialized final result'})
    report['local_recovery_candidates'] = recovery
    report['cross_source_same_selection_overlap'] = len(set(map(coord,sources[PATHS[0]])) & set(map(coord,sources[PATHS[1]])))
    old_top, old_daily = old_selectors()
    cfg = Config()  # Code defaults, explicitly reported; no .env or retrospective fitting.
    report['replay_settings'] = {key:getattr(cfg,key) for key in ['daily_min_prob','top_min_prob','daily_min_lcb','daily_min_books','daily_secondary_min_books','top_min_books','daily_min_odds','daily_target','daily_max_market_share','daily_max_per_league']}
    report['strategy_grid'] = []
    report['old_revised_selector_comparison'] = []
    for path, original in sources.items():
        for day in sorted({r['pick_date'] for r in original if r.get('rank_score') is not None}):
            full = [r for r in original if r['pick_date']==day and r.get('rank_score') is not None]
            now = max(r['created_at'] for r in full)
            # Known forecast fields, not settled status, become the replay inputs.
            safe = [dict(r,status='pending') for r in full if r['_pre_start_record'] and binary_market(r) and now<r['start_utc']]
            for kind,old,new in [('top',old_top.select_top,select_top),('daily',old_daily.select_daily,select_daily)]:
                a,ad = old(safe,cfg,now); b,bd = new(safe,cfg,now)
                report['old_revised_selector_comparison'].append({'source':path,'date':day,'decision_time':now,
                    'collection':kind,'input_binary_rows':len(safe),'original':evaluated_ticket(a,bank),
                    'revised':evaluated_ticket(b,bank),'original_diagnostics':ad,'revised_diagnostics':bd})
            eligible = [r for r in safe if not promotion_reason(r,cfg,now)
                        and cfg.daily_min_odds <= r['price'] <= cfg.daily_max_odds]
            for n in (2,3,4):
                for lower,upper in [(1.8,2.5),(2.5,3.5),(3.5,5.)]:
                    best = None; checked = 0
                    for combo in itertools.combinations(eligible,n):
                        if not _fits(combo,cfg,cfg.daily_target):
                            continue
                        odds = math.prod(r['price'] for r in combo)
                        if not lower<=odds<=upper:
                            continue
                        checked += 1
                        key = ticket_key(combo,lower)
                        if best is None or key < best[0]:
                            best = key,combo
                    report['strategy_grid'].append({'source':path,'date':day,'decision_time':now,'legs_required':n,
                        'odds_band':[lower,upper], 'eligible_binary_rows':len(eligible),'feasible_combinations':checked,
                        'selected':evaluated_ticket(list(best[1]) if best else [],bank)})
    # Archive replay uses only captured binary probabilities. Rounded lambda
    # notes cannot exactly reconstruct legacy push/partial distributions.
    report['archived_candidate_selector_comparisons'] = []
    for path in PATHS[:2]:
        db = connect(path)
        records = [dict(r) for r in db.execute('SELECT * FROM custom_pick_candidates')]
        db.close()
        by_day = defaultdict(list)
        for record in records:
            by_day[record['prediction_date']].append(record)
        for day, items in by_day.items():
            now = max(r['generated_at'] for r in items)
            events, scored = {}, []
            excluded = Counter()
            for item in items:
                record = json.loads(item['payload_json'])
                if item['generated_at'] >= record['start_utc'] or now >= record['start_utc']:
                    excluded['not_available_before_fixture_at_snapshot_time'] += 1
                    continue
                if not binary_market(record):
                    excluded['push_or_partial_model_not_exactly_reconstructable'] += 1
                    continue
                eid = record['canonical_event_id']
                if eid not in events:
                    kwargs = {k:record[k] for k in Event.__dataclass_fields__ if k in record and k not in ('start','markets','sources')}
                    events[eid] = Event(**kwargs, start=parse_iso(record['start_utc']), sources=record.get('event_sources',[]))
                event = events[eid]
                kwargs = {k:record[k] for k in MarketLine.__dataclass_fields__ if k in record and k!='label'}
                ml = MarketLine(**kwargs, label=record.get('market_label',''))
                event.markets.append(ml)
                fields = {k:record[k] for k in Scored.__dataclass_fields__
                          if k in record and k not in ('event','line','quality','rank_score','se','p_lcb')}
                scored.append((Scored(event=event,line=ml,**fields),item['candidate_id']))
            tables, input_rows = {}, []
            for s,cid in scored:
                e = assess(s,cfg,tables)
                record = {'id':cid,'canonical_event_id':s.event.canonical_event_id,'fixture_key':s.event.canonical_event_id,
                    'sport':s.event.sport,'home':s.event.home,'away':s.event.away,'league':s.event.league,
                    'canonical_competition_id':s.event.canonical_competition_id,
                    'start_utc':s.event.start.strftime('%Y-%m-%dT%H:%M:%SZ'),'status':'pending','market':s.line.market,
                    'period':s.line.period,'stat':s.line.stat,'entity':s.line.entity,'line':s.line.line,
                    'selection':s.line.selection,'provider_odd_id':s.line.provider_odd_id,'price':s.price,
                    'prob':s.p_model,'ev':s.ev,'p_fair':s.p_fair,'edge_ev':s.edge_ev,'books':e.books,
                    'quality':e.quality,'rank_score':e.rank,
                    'sources':json.dumps({'_notes':{**s.notes,'edge':s.edge,'independent':s.independent,'evidence':e.as_notes()}})}
                input_rows.append(record)
            for kind,old,new in [('top',old_top.select_top,select_top),('daily',old_daily.select_daily,select_daily)]:
                a,_ = old(input_rows,cfg,now); b,_ = new(input_rows,cfg,now)
                report['archived_candidate_selector_comparisons'].append({'source':path,'date':day,'decision_time':now,
                    'scope':'Selector-only replay on identical captured binary candidates; not a full old/revised model backtest',
                    'collection':kind,'retained_candidates':len(input_rows),'excluded':dict(excluded),
                    'original':evaluated_ticket(a,bank),'revised':evaluated_ticket(b,bank)})
    report['database_hashes_before'] = before
    report['database_hashes_after'] = {path:digest(path) for path in PATHS}
    assert report['database_hashes_before']==report['database_hashes_after']
    target = ROOT/'predictions-bot/historical_performance_evidence.json'
    target.write_text(json.dumps(report,indent=2,ensure_ascii=True),encoding='utf8')
    print(target)
    for path,cohort in report['sources'].items():
        print(path,'rows',cohort['rows'],'published',cohort['published_before_start'],cohort['status_counts'])
        print('7-day published',cohort['windows']['7_days']['published'])
    print('Recoverable',recovery)
    for row in report['strategy_grid']:
        if row['selected']['legs']:
            print('Strategy',row['source'],row['date'],row['legs_required'],row['odds_band'],
                  row['selected']['complete'],row['selected']['unit_stake_roi'])


if __name__=='__main__':
    sys.stdout.reconfigure(errors='backslashreplace')
    main()
