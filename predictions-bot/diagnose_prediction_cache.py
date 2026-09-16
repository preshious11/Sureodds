"""Read configured SQLite cache status without generation, migration or API calls."""
import json
import sqlite3
from pathlib import Path

from predbot.config import load_config
from predbot.util.timeutil import to_local, utcnow


def main():
    cfg = load_config()
    path = Path(cfg.db_path).resolve()
    print('Prediction cache backend=sqlite')
    print('SQLite database path:', path, '| exists=', path.is_file())
    if not path.is_file():
        print('No database exists here; check DB_PATH and the persistent mount.')
        return
    connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    try:
        exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='prediction_cache'").fetchone()
        if not exists:
            print('prediction_cache table: absent; startup will create it automatically.')
            return
        day = to_local(utcnow(), cfg.timezone).date().isoformat()
        key = f'daily_predictions:{cfg.timezone}:{day}'
        row = connection.execute('SELECT * FROM prediction_cache WHERE cache_key=?', (key,)).fetchone()
        if row is None:
            print('Today: CACHE MISS')
            return
        payload = json.loads(row['payload_json'])
        print(json.dumps({
            'date': row['local_date'], 'timezone': row['timezone'], 'status': row['status'],
            'version': row['cache_version'], 'generated_at': row['generated_at'],
            'updated_at': row['updated_at'], 'expires_at': row['expires_at'],
            'retry_after': row['retry_after'], 'sports': json.loads(row['sport_status_json']),
            'top_count': len(payload.get('top_picks', [])),
            'daily_additional_count': len(payload.get('daily_picks', [])),
        }, indent=2))
    finally:
        connection.close()


if __name__ == '__main__':
    main()
