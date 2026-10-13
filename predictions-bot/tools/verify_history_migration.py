"""Validate 0005 on disposable pre-migration copies of supplied SQLite archives.

Source databases are opened mode=ro. No providers, D1, or historical writes.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3


def digest_rows(con, table, skip=()):
    columns = [r[1] for r in con.execute(f'PRAGMA table_info("{table}")') if r[1] not in skip]
    rows = sorted(repr(tuple(row)) for row in con.execute(
        'SELECT ' + ','.join('"'+c+'"' for c in columns) + f' FROM "{table}"'))
    return {'rows':len(rows), 'sha256':hashlib.sha256('\n'.join(rows).encode()).hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('sources', nargs='+')
    parser.add_argument('--out', required=True)
    args = parser.parse_args()
    out = Path(args.out).resolve()
    if out.exists():
        parser.error('output directory must be new')
    out.mkdir(parents=True)
    migration = Path(__file__).resolve().parents[2] / 'workers/migrations/0005_ticket_candidates.sql'
    evidence = []
    for index, source in enumerate(args.sources):
        path = Path(source).resolve()
        before_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        copy = out / f'archive-{index}.db'
        with sqlite3.connect(path.as_uri()+'?mode=ro', uri=True) as source_con:
            with sqlite3.connect(copy) as con:
                source_con.backup(con)
                original_columns = {r[1] for r in con.execute('PRAGMA table_info(picks)')}
                dependencies = [name for name in ('canonical_event_id','canonical_competition_id','home_id','away_id')
                                if name not in original_columns]
                for name in dependencies:
                    # 0001's D1 schema already includes these columns. Legacy
                    # local archives need this prerequisite only in the copy.
                    con.execute(f'ALTER TABLE picks ADD COLUMN {name} TEXT')
                # Existing local Phase 1 schema is converted only in this copy
                # to the exact pre-0005 shape; no source records are rewritten.
                con.execute('DROP INDEX IF EXISTS uq_prediction_event_family_day')
                if 'is_representative' in {r[1] for r in con.execute('PRAGMA table_info(picks)')}:
                    con.execute('ALTER TABLE picks DROP COLUMN is_representative')
                tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
                before = {t:digest_rows(con,t) for t in tables}
                con.commit()
                con.executescript('BEGIN IMMEDIATE;\n'+migration.read_text()+'\nCOMMIT;')
                after = {t:digest_rows(con,t, ('is_representative',) if t=='picks' else ()) for t in tables}
                assert before == after, 'migration altered historical records'
                integrity = con.execute('PRAGMA integrity_check').fetchone()[0]
                foreign_keys = con.execute('PRAGMA foreign_key_check').fetchall()
                assert integrity == 'ok' and not foreign_keys
                evidence.append({'source':str(path), 'source_sha256':before_hash,
                                 'legacy_prerequisite_columns_added_in_copy':dependencies,
                                 'source_unchanged':before_hash==hashlib.sha256(path.read_bytes()).hexdigest(),
                                 'historical_tables_preserved':before, 'integrity':integrity,
                                 'foreign_key_violations':foreign_keys,
                                 'existing_rows_default_role':con.execute('SELECT COUNT(*) FROM picks WHERE is_representative=1').fetchone()[0]})
    (out / 'evidence.json').write_text(json.dumps(evidence,indent=2),encoding='utf-8')
    print(json.dumps([{'source':e['source'], 'picks':e['historical_tables_preserved']['picks']['rows'],
                       'preserved':e['source_unchanged'], 'integrity':e['integrity']} for e in evidence]))


if __name__ == '__main__':
    main()
