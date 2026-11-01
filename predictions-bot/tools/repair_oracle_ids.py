"""Recover ignored generation rows from retained, verified relay batches.

Default is a rollback-only rehearsal. --apply commits one transaction. Does not
change any existing prediction result, price, identity, or publication receipt.
"""
import argparse
import json
from pathlib import Path
import sqlite3


IDENTITY = ('pick_date','fixture_key','market','period','stat','entity','line','selection')


def restore_rejections(db, incoming):
    schema=db.execute("SELECT sql FROM sqlite_master WHERE name='selection_rejections'").fetchone()
    if schema is None: return 0
    inserted=0
    for batch in sorted(Path(incoming).glob('generation-*')):
        scratch=sqlite3.connect(':memory:');scratch.row_factory=sqlite3.Row
        scratch.execute(schema[0])
        for file in sorted(batch.glob('p0_*_selection_rejections_*.sql')):
            scratch.executescript(file.read_text(encoding='utf-8'))
        for row in scratch.execute('SELECT * FROM selection_rejections'):
            cols=[key for key in row.keys() if key!='id']
            cursor=db.execute('INSERT OR IGNORE INTO selection_rejections ('+','.join(cols)+') VALUES ('+
                              ','.join('?' for _ in cols)+')',tuple(row[key] for key in cols))
            inserted+=cursor.rowcount
        scratch.close()
    return inserted


def repair(db, incoming):
    db.row_factory=sqlite3.Row
    schema=db.execute("SELECT sql FROM sqlite_master WHERE name='picks'").fetchone()[0]
    counts={'inserted':0,'cache_ids_remapped':0,'memberships_added':0,'memberships_removed':0}
    # Latest evidence for a date wins; existing production outcomes always win.
    references={}
    for cache in db.execute('SELECT local_date,payload_json FROM prediction_cache'):
        payload=json.loads(cache['payload_json'])
        for field in ('all_predictions','top_picks','daily_picks'):
            for pick in payload.get(field,[]):
                references[(cache['local_date'],pick.get('pick_id'))]=pick
    recovered={}
    for batch in sorted(Path(incoming).glob('generation-*')):
        scratch=sqlite3.connect(':memory:');scratch.row_factory=sqlite3.Row
        scratch.execute(schema)
        for file in sorted(batch.glob('p0_*_picks_*.sql')):
            scratch.executescript(file.read_text(encoding='utf-8'))
        for row in scratch.execute('SELECT * FROM picks'):
            key=(row['pick_date'],row['id']);reference=references.get(key)
            if reference and all(row[k]==reference.get(k) for k in ('home','away','market','selection','line','price','period','stat','entity')):
                recovered[key]=dict(row)
        scratch.close()
    mappings={}
    for (day,old),row in recovered.items():
        where=' AND '.join(k+' IS ?' for k in IDENTITY)
        matches=db.execute('SELECT id FROM picks WHERE '+where,tuple(row[k] for k in IDENTITY)).fetchall()
        if len(matches)>1: raise RuntimeError('ambiguous prediction identity; manual review required')
        if matches: new=matches[0][0]
        else:
            conflict=db.execute('SELECT id,home,away,market,selection,line FROM picks WHERE pick_date=? AND canonical_event_id=? AND market=? AND is_representative=1',(day,row['canonical_event_id'],row['market'])).fetchall()
            if conflict and row['is_representative']:
                raise RuntimeError('representative conflict '+str((day,old,row['home'],row['away'],row['market'],row['selection'],row['line'],[dict(r) for r in conflict])))
            columns=[k for k in row if k!='id']
            new=db.execute('INSERT INTO picks ('+','.join(columns)+') VALUES ('+','.join('?' for _ in columns)+')',tuple(row[k] for k in columns)).lastrowid
            counts['inserted']+=1
        mappings[(day,old)]=new
    for cache in db.execute('SELECT cache_key,local_date,payload_json FROM prediction_cache').fetchall():
        day=cache['local_date'];payload=json.loads(cache['payload_json']);desired={};changed=False
        for field,collection in [('all_predictions','all'),('top_picks','top'),('daily_picks','daily')]:
            for rank,pick in enumerate(payload.get(field,[]),1):
                old=pick.get('pick_id');new=mappings.get((day,old))
                if new is None:
                    existing=db.execute('SELECT pick_date FROM picks WHERE id=?',(old,)).fetchone()
                    if not existing or existing[0]!=day: raise RuntimeError('missing authoritative batch evidence for '+day)
                    new=old
                stored=db.execute('SELECT home,away,market,selection,price FROM picks WHERE id=?',(new,)).fetchone()
                if any(stored[k]!=pick.get(k) for k in ('home','away','market','selection','price')):
                    raise RuntimeError('cache and batch disagree for '+day)
                desired[(new,collection)]=rank
                if old!=new:
                    pick['pick_id']=new;changed=True;counts['cache_ids_remapped']+=1
        if changed: db.execute('UPDATE prediction_cache SET payload_json=? WHERE cache_key=?',(json.dumps(payload),cache['cache_key']))
        # Remove only provably incorrect collection links on dates with retained evidence.
        if any(key[0]==day for key in recovered):
            actual=db.execute('SELECT m.* FROM prediction_memberships m JOIN picks p ON p.id=m.prediction_id WHERE p.pick_date=?',(day,)).fetchall()
            for m in actual:
                key=(m['prediction_id'],m['collection'])
                if key not in desired and m['collection'] in ('top','daily'):
                    db.execute('DELETE FROM prediction_memberships WHERE prediction_id=? AND collection=?',key);counts['memberships_removed']+=1
        for key,rank in desired.items():
            if not db.execute('SELECT 1 FROM prediction_memberships WHERE prediction_id=? AND collection=?',key).fetchone():
                db.execute('INSERT INTO prediction_memberships(prediction_id,collection,rank) VALUES(?,?,?)',(*key,rank));counts['memberships_added']+=1
    counts['diagnostics_restored']=restore_rejections(db,incoming)
    return counts


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('database');p.add_argument('incoming');p.add_argument('--apply',action='store_true');a=p.parse_args()
    c=sqlite3.connect(a.database);c.execute('BEGIN IMMEDIATE')
    try:
        result=repair(c,a.incoming)
        if a.apply: c.commit()
        else: c.rollback()
        print(json.dumps({'mode':'applied' if a.apply else 'rolled_back_rehearsal',**result}))
    except BaseException:
        c.rollback();raise
    finally: c.close()
