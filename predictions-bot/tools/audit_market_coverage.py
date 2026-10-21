"""Offline market inventory. Source SQLite files are opened mode=ro only.

Run from predictions-bot:
  .venv/Scripts/python tools/audit_market_coverage.py --db ../data/predbot.db \
      --db data/predbot.db --output ../market-coverage-evidence.json

No collection, team-insight enrichment, settlement lookup, D1 sync or Telegram.
Replay uses an empty scratch model history; it is not historical backtesting.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
import csv
import json
from pathlib import Path
import socket
import sqlite3
import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from predbot.cache.providers import decode_events
from predbot.config import load_config
from predbot.jobs.daily_picks import prediction_row
from predbot.markets.catalog import CATALOG
from predbot.markets.settlement import make_results_gate
from predbot.model.coverage import scope_counts
from predbot.model.engine import generate, ModelSettings
from predbot.providers.budget import Budget
from predbot.providers.chain import build_providers, filter_allowed
from predbot.providers.sharpapi import SharpApiProvider, SPORT_MAP, SEGMENT_PERIOD
from predbot.providers.sportsgameodds import SportsGameOddsProvider
from predbot.selection.evidence import assess
from predbot.selection.picker import prediction_identity
from predbot.selection.top import promotion_reason
from predbot.storage.db import Database
from predbot.storage.identity import resolve_events_detailed
from predbot.util.timeutil import iso, parse_iso


@contextmanager
def offline():
    def blocked(*args, **kwargs):
        raise RuntimeError('Market coverage audit forbids network access')
    with patch('requests.sessions.Session.request', blocked), \
            patch.object(socket.socket, 'connect', blocked), \
            patch.object(socket, 'create_connection', blocked):
        yield


def safe_config(cfg):
    names = ('allowed_sports', 'provider_order', 'disabled_markets',
             'sharpapi_main_lines_only', 'sharpapi_markets',
             'sharpapi_markets_football', 'sharpapi_markets_baseball',
             'sgo_allowed_stats', 'sgo_allowed_periods', 'sgo_leagues',
             'sharpapi_leagues', 'odds_api_league_allowlist', 'odds_api_regions',
             'espn_results', 'euroleague_results', 'max_player_props')
    return {k: getattr(cfg, k) for k in names}


def fixture_inventory(directory, cfg):
    db = Database(':memory:')
    out = []
    try:
        sharp = SharpApiProvider('', Budget(db), [])
        for name in ('sharp_odds.json', 'sharp_pl_wide_main.json', 'sharp_mlb_main.json'):
            rows = json.loads((directory / name).read_text(encoding='utf-8'))['data']
            accepted, rejected, review = Counter(), Counter(), Counter()
            contracts = []
            for row in rows:
                sport = SPORT_MAP.get(row.get('sport', ''), 'other')
                m = sharp._row_to_line(row, sport)
                (accepted if m else rejected)[row['market_type']] += 1
                if m:
                    contracts.append(SimpleNamespace(event=SimpleNamespace(sport=sport), line=m))
                    segment = row.get('market_segment')
                    if segment and segment not in SEGMENT_PERIOD:
                        review['unrecognized_segment:' + str(segment)] += 1
                    if row['market_type'] == 'total_home_runs':
                        review['home_run_stat_mapped_as_points_requires_contract_review'] += 1
            out.append(dict(fixture=name, raw_rows=len(rows), parsed_rows=sum(accepted.values()),
                            parsed=dict(accepted), unparsed=dict(rejected), semantic_review=dict(review),
                            scopes=scope_counts(contracts),
                            caveat='Row parser replay; not current request entitlement or window eligibility'))
        sgo = SportsGameOddsProvider('', Budget(db), [], cfg.sgo_leagues,
                                    allowed_stats=cfg.sgo_allowed_stats,
                                    allowed_periods=cfg.sgo_allowed_periods)
        for name in ('sgo_event.json', 'sgo_event_final.json'):
            ev = json.loads((directory / name).read_text(encoding='utf-8'))
            sgo.parse_counts.clear()
            parsed = sgo._parse_event(ev)
            out.append(dict(fixture=name, raw_rows=len(ev.get('odds', {})),
                            parsed_rows=len(parsed.markets) if parsed else 0,
                            rejections=dict(sgo.parse_counts),
                            scopes=scope_counts([SimpleNamespace(event=parsed, line=m)
                                                 for m in parsed.markets]) if parsed else [],
                            caveat='Saved fixture, not proof of current account availability'))
    finally:
        db.close()
    return out


def audit_database(path, cfg):
    path = Path(path).resolve(strict=True)
    result = {'source': str(path), 'mode': 'read-only', 'replays': [], 'archives': {}}
    source = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    source.execute('PRAGMA query_only=ON')
    try:
        tables = {r[0] for r in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in ('custom_pick_candidates', 'ticket_candidates', 'picks'):
            if table not in tables:
                result['archives'][table] = {'available': False}
                continue
            if table == 'picks':
                cursor = source.execute('SELECT sport,market,period,stat,entity,line,selection FROM picks')
                values = [SimpleNamespace(event=SimpleNamespace(sport=r[0]), line=SimpleNamespace(
                    market=r[1], period=r[2], stat=r[3], entity=r[4], line=r[5], selection=r[6])) for r in cursor]
            else:
                values = []
                for (raw,) in source.execute('SELECT payload_json FROM ' + table):
                    r = json.loads(raw)
                    values.append(SimpleNamespace(event=SimpleNamespace(sport=r['sport']), line=SimpleNamespace(
                        **{k: r.get(k) for k in ('market', 'period', 'stat', 'entity', 'line', 'selection')})))
            result['archives'][table] = {'available': True, 'rows': len(values), 'scopes': scope_counts(values)}
        if 'provider_cache' not in tables:
            return result
        for day, payload, metadata in source.execute(
                'SELECT local_date,payload_json,metadata_json FROM provider_cache ORDER BY local_date,cache_key'):
            raw_events = decode_events(json.loads(payload))
            meta = json.loads(metadata)
            replay = {'day': day, 'events_in_saved_cache': len(raw_events),
                      'provider': sorted({e.provider for e in raw_events}),
                      'cached_outcome': meta.get('outcome'), 'saved_parse_rejections': meta.get('rejections'),
                      'model_history': 'empty scratch Elo/calibration; current code and local config',
                      'normalized': scope_counts([SimpleNamespace(event=e, line=m)
                                                   for e in raw_events for m in e.markets])}
            result['replays'].append(replay)
            if not raw_events:
                continue
            db = Database(':memory:')
            try:
                providers = build_providers(cfg, db)  # constructors and capability declarations only
                allowed = filter_allowed(raw_events, cfg)
                events, failures = resolve_events_detailed(allowed, db, cfg.supported_competitions)
                generation = generate(events, db, ModelSettings(cfg.model_shrink, tuple(cfg.sharp_books),
                    cfg.elo_min_games, cfg.edge_price), make_results_gate(cfg, providers))
                replay.update(identity_rejections=dict(Counter(reason for _, reason in failures)),
                              resolved_events=len(events), diagnostics=generation.diagnostics)
                # Use each snapshot's evaluation time, never today's clock on old fixtures.
                at = parse_iso(meta.get('updated_at', ''))
                if at is None:
                    replay['eligibility'] = {'unavailable': 'missing saved evaluation timestamp'}
                    continue
                rows = []
                for s in generation.ticket_predictions:
                    ev = assess(s, cfg, {})
                    s.quality, s.se, s.p_lcb, s.rank_score = ev.quality, ev.se, ev.p_lcb, ev.rank
                    s.notes['evidence'] = ev.as_notes()
                    s.notes['independent'] = s.independent
                    row = prediction_row(s, day)
                    row['status'] = 'pending'
                    # Production row reader serializes sources before selecting.
                    row['sources'] = json.dumps(row['sources'])
                    rows.append(row)
                top = Counter(promotion_reason(r, cfg, iso(at), top=True) or 'eligible' for r in rows)
                daily = Counter(promotion_reason(r, cfg, iso(at)) or 'eligible' for r in rows)
                identities = {}
                for row in rows:
                    identities.setdefault(prediction_identity(row), set()).add(row['entity'])
                replay['eligibility'] = {'top': dict(top), 'daily': dict(daily),
                    'entity_identity_collisions': sum(len(v) > 1 for v in identities.values()),
                    'note': 'Individual gates before ticket search/diversity/Top exclusions; not published counts'}
            finally:
                db.close()
    finally:
        source.close()
    return result


def write_matrix(report, path):
    """All enabled sports x catalog families, including zero observations.

    An absent observation is unknown availability, never proof of no support.
    Detailed periods, entities, sides and exact lines live in the JSON scopes.
    """
    with path.open('w', newline='', encoding='utf-8') as stream:
        fields = ('source', 'day', 'sport', 'market', 'normalized', 'scored', 'validated', 'representatives')
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for database in report['databases']:
            for replay in database['replays']:
                if not replay['events_in_saved_cache']:
                    continue
                stages = {'normalized': replay['normalized'],
                          **replay['diagnostics']['market_coverage']}
                for sport in report['configuration']['allowed_sports']:
                    for market in CATALOG:
                        row = dict(source=database['source'], day=replay['day'], sport=sport, market=market)
                        row.update({stage: sum(r['outcomes'] for r in rows
                                               if r['sport'] == sport and r['market'] == market)
                                    for stage, rows in stages.items()})
                        writer.writerow(row)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', action='append', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--env', type=str, default=None)
    args = parser.parse_args()
    targets = (args.output.resolve(), args.output.with_suffix('.csv').resolve())
    sources = {p.resolve() for p in args.db}
    if args.output.suffix.lower() != '.json' or any(p in sources for p in targets):
        parser.error('Output must be a separate JSON/CSV artifact, never a source database')
    with offline():
        cfg = load_config(args.env)
        report = {'configuration': safe_config(cfg),
                  'limitations': ['No live requests or entitlement verification',
                                  'Normalized caches cannot measure raw-response parse losses',
                                  'Overlapping snapshots must not be summed',
                                  'Empty scratch history; not historic prediction reproduction'],
                  'fixtures': fixture_inventory(Path(__file__).resolve().parents[1] / 'tests/fixtures', cfg),
                  'databases': [audit_database(p, cfg) for p in args.db]}
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    write_matrix(report, args.output.with_suffix('.csv'))
    print(f'Wrote offline evidence: {args.output} and {args.output.with_suffix(".csv")}')


if __name__ == '__main__':
    main()
