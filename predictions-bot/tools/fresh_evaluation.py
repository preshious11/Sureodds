"""Fresh provider evaluation in a disposable database; never publish or apply it."""
import argparse
import json
import math
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from predbot.config import Config, require_provider_keys
from predbot.jobs.daily_picks import build_picks
from predbot.log import setup_logging
from predbot.providers.chain import ProviderChain, build_providers
from predbot.selection.picker import PredictionPool
from predbot.storage.db import Database
from predbot.util.timeutil import iso, to_local, utcnow


def notice_lines(summary):
    """Actions truncates individual annotation messages at 4096 characters."""
    raw = json.dumps(summary, default=str)
    chunks = [raw[i:i + 1000] for i in range(0, len(raw), 1000)]
    for i, chunk in enumerate(chunks, 1):
        escaped = chunk.replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')
        yield f'::notice title=Fresh evaluation part {i}/{len(chunks)}::{escaped}'


def evaluate(db, cfg, now, chain, builder=build_picks):
    """Keep the selection pool separate from historical immutable forecasts."""
    day = to_local(now, cfg.timezone).date().isoformat()
    pickset = builder(chain, db, cfg, now)
    pool = PredictionPool.scratch(day)
    try:
        pool.store(pickset)
        selection = pool.package(cfg, iso(now))
        tickets = {}
        fields = ('fixture_key', 'home', 'away', 'start_utc', 'market', 'period',
                  'selection', 'line', 'price', 'prob', 'provider', 'provider_event_id')
        for kind in ('top', 'daily'):
            rows = pool.rows(kind)
            tickets[kind] = {
                'selections': [{k: r.get(k) for k in fields} for r in rows],
                'combined_odds': math.prod(r['price'] for r in rows) if rows else None,
            }
        return {'day': day, 'started_at': iso(now), 'completed_at': iso(utcnow()),
                'complete': pickset.complete, 'generation': pickset.diagnostics,
                'selection': selection, 'tickets': tickets,
                'production_applied': False, 'telegram_sent': False}
    finally:
        pool.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-sql', required=True)
    parser.add_argument('--workdir', required=True)
    args = parser.parse_args()
    folder = Path(args.workdir)
    folder.mkdir(parents=True, exist_ok=False)
    path = folder / 'evaluation.db'
    con = sqlite3.connect(path)
    try:
        con.executescript(Path(args.source_sql).read_text(encoding='utf-8'))
        # This is ONLY the disposable runner copy, never the source database.
        # Removing provider cache prevents even COMPLETE same-day quote reuse.
        con.execute('DELETE FROM provider_cache')
        con.commit()
    finally:
        con.close()
    cfg = Config()
    assert (cfg.top_odds_target, cfg.top_odds_max, cfg.daily_odds_min, cfg.daily_odds_max) == (2, 2.5, 5, 7)
    require_provider_keys(cfg)
    cfg.telegram_token = cfg.telegram_chat_id = ''
    setup_logging('WARNING', str(folder / 'evaluation.log'),
                  secrets=[cfg.odds_api_key, cfg.sharpapi_key, cfg.sgo_key, cfg.football_data_key])
    db = Database(str(path))
    try:
        chain = ProviderChain(build_providers(cfg, db), cfg.provider_order)
        chain.db, chain.cfg = db, cfg
        report = evaluate(db, cfg, utcnow(), chain)
        (folder / 'report.json').write_text(json.dumps(report, indent=2, default=str), encoding='utf-8')
        summary = {k: report[k] for k in ('day', 'started_at', 'completed_at', 'complete', 'tickets', 'production_applied', 'telegram_sent')}
        summary['funnel'] = {k: report['generation'].get(k) for k in (
            'fixtures_discovered', 'supported_fixtures', 'normalized', 'events_by_sport',
            'provider_coverage', 'providers', 'api_usage')}
        summary['selection'] = report['selection']
        # A private-repository Actions annotation is readable through its API.
        for line in notice_lines(summary):
            print(line)
    finally:
        db.close()


if __name__ == '__main__':
    main()
