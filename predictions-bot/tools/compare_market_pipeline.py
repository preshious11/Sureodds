"""Chronological, offline comparison of recorded slates against the v2 pipeline.

Reads source SQLite files with mode=ro. All generation/storage is in memory.
Never fetches odds or results. Complete matched tickets are required for ROI.
"""
import json
import math
from pathlib import Path
import sqlite3
import sys
from collections import Counter
from datetime import timedelta
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from predbot.cache.providers import decode_events
from predbot.config import load_config
from predbot.jobs.daily_picks import score_events
from predbot.markets.settlement import grade, make_results_gate
from predbot.model.engine import representative_predictions
from predbot.model.independent import IndependentModels
from predbot.providers.base import consolidate_lines, MatchResult
from predbot.providers.chain import build_providers, merge_events
from predbot.selection.evidence import assess
from predbot.selection.picker import PickSet, PredictionPool
from predbot.storage.identity import resolve_events_detailed
from predbot.util.timeutil import day_bounds, iso, parse_iso


def readonly(path):
    db = sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    return db


def summary(rows):
    return {'legs': len(rows), 'markets': dict(Counter(r['market'] for r in rows)),
            'odds': math.prod(r['price'] for r in rows) if rows else None,
            'mean_reported_probability': sum(r['prob'] for r in rows)/len(rows) if rows else None,
            'estimated_return_independent_fixtures': math.prod(1+r['ev'] for r in rows)-1 if rows else None,
            'selections': [{k: r[k] for k in ('home', 'away', 'market', 'selection', 'line', 'prob', 'price', 'ev')}
                           for r in rows]}


def main():
    import requests
    cfg = load_config(str(ROOT/'.env'))
    evidence = json.loads((ROOT/'market_root_cause_evidence.json').read_text(encoding='utf8'))
    # Match the investigation's settings; no retuning on these recorded slates.
    for key, value in evidence['config'].items():
        setattr(cfg, key, value)
    report = {'method': 'Chronological offline replay; frozen investigation settings, empty historical model inputs',
              'limitations': ['Two recorded slates, not a representative season.',
                              'Legacy probabilities and EV have conditional/push-adjusted semantics; they are not comparable to strict-win v2 settlement metrics.',
                              'Estimated returns assume independence across fixtures; they are not observed ROI.',
                              'No tuning on replay results; no provider requests.'], 'captures': []}
    history = readonly(ROOT/'data/predbot.db')
    with patch.object(requests.Session, 'request', side_effect=RuntimeError('offline comparison')):
        for capture in sorted(evidence['captures'], key=lambda c: c['now']):
            now = parse_iso(capture['now'])
            start = now+timedelta(minutes=cfg.min_lead_minutes)
            end = day_bounds(now, cfg.timezone)[1]+timedelta(hours=cfg.day_window_end_offset_hours)
            source = readonly(ROOT/capture['source'])
            events = []
            for cached in source.execute('SELECT payload_json FROM provider_cache'):
                incoming = [e for e in decode_events(json.loads(cached[0]))
                            if start <= e.start < end and e.sport in cfg.allowed_sports]
                for event in incoming:
                    event.markets = consolidate_lines(event.markets)
                events = merge_events(events, incoming, cfg.price_mode)
            source.close()
            pool = PredictionPool.scratch(now.date().isoformat())
            events, failed = resolve_events_detailed(events, pool.db, cfg.supported_competitions)
            model = IndependentModels.default(pool.db, cfg, now)
            scored = score_events(events, pool.db, cfg, model)
            generation = representative_predictions(scored, make_results_gate(cfg, build_providers(cfg, pool.db)))
            tables = {}
            for candidate in generation.ticket_predictions:
                e = assess(candidate, cfg, tables)
                candidate.quality, candidate.se, candidate.p_lcb, candidate.rank_score = e.quality, e.se, e.p_lcb, e.rank
                candidate.notes['evidence'] = e.as_notes()
                candidate.notes['independent'] = candidate.independent
            ps = PickSet(predictions=generation.predictions, ticket_predictions=generation.ticket_predictions)
            pool.store(ps)
            diag = pool.package(cfg, iso(now))
            entry = {'recorded_at': capture['now'], 'source': capture['source'], 'new_diagnostics': diag,
                     'scored_candidates': len(scored), 'validated_candidates': len(generation.ticket_predictions),
                     'identity_failures': len(failed), 'tickets': {}}
            for kind in ('top', 'daily'):
                old = [r for r in capture['candidates'] if r['selected_'+kind]]
                new = pool.rows(kind)
                legacy_summary = summary(old)
                legacy_summary['legacy_reported_return_not_settlement_corrected'] = legacy_summary.pop('estimated_return_independent_fixtures')
                entry['tickets'][kind] = {'legacy': legacy_summary, 'v2': summary(new)}
                # Require an exact canonical fixture identity and close kickoff;
                # fuzzy names or an unrelated past match cannot stand in for a result.
                settled = []
                for row in new:
                    matches = history.execute('SELECT home_goals,away_goals,kickoff_utc FROM match_results '
                                              'WHERE competition_id=? AND home_id=? AND away_id=?',
                                              (row['canonical_competition_id'], row['home_id'], row['away_id'])).fetchall()
                    matches = [r for r in matches if abs((parse_iso(r['kickoff_utc'])-parse_iso(row['start_utc'])).total_seconds()) <= 300]
                    if len(matches) != 1:
                        continue
                    r = matches[0]
                    from predbot.providers.base import MarketLine
                    line = MarketLine(row['market'], row['selection'], row['price'], period=row['period'],
                                      stat=row['stat'], entity=row['entity'], line=row['line'])
                    status = grade(line, MatchResult(status='final', scores={'full': (r['home_goals'], r['away_goals'])}), row['sport'])
                    settled.append((row, status))
                entry['tickets'][kind]['v2']['matched_results'] = len(settled)
                if new and len(settled) == len(new):
                    multipliers = {'won': lambda o: o, 'lost': lambda o: 0, 'push': lambda o: 1,
                                   'void': lambda o: 1, 'half_won': lambda o: (o+1)/2, 'half_lost': lambda o: .5}
                    entry['tickets'][kind]['v2']['observed_unit_stake_roi'] = math.prod(multipliers[s](r['price']) for r, s in settled)-1
                    entry['tickets'][kind]['v2']['strict_ticket_hit'] = all(s == 'won' for _, s in settled)
                else:
                    entry['tickets'][kind]['v2']['observed_unit_stake_roi'] = None
                    entry['tickets'][kind]['v2']['strict_ticket_hit'] = None
            report['captures'].append(entry)
            pool.close()
    history.close()
    output = ROOT/'market_pipeline_comparison.json'
    output.write_text(json.dumps(report, indent=2), encoding='utf8')
    print(output)
    for capture in report['captures']:
        print(capture['recorded_at'])
        for kind, ticket in capture['tickets'].items():
            print(kind, ticket['legacy']['markets'], '->', ticket['v2']['markets'],
                  'odds', ticket['v2']['odds'], 'matched results', ticket['v2']['matched_results'])


if __name__ == '__main__':
    main()
