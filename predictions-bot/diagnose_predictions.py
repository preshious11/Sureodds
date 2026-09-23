"""Read-only provider diagnostic: no production DB/cache writes or Telegram sends.

Uses API GETs (which consume provider quota). Prediction-feature calls are
disabled to bound diagnostic cost; resulting model counts reflect that limit.
"""
import json
import logging
from collections import Counter
from datetime import timedelta
from pathlib import Path
import sqlite3

from predbot.config import load_config
from predbot.storage.db import Database
from predbot.providers.chain import build_providers, merge_events
from predbot.jobs.daily_picks import score_events
from predbot.selection.picker import select
from predbot.util.timeutil import utcnow, to_local, day_bounds


def main():
    logging.disable(logging.CRITICAL)
    cfg = load_config()
    db = Database(':memory:')
    if Path(cfg.db_path).exists():
        source = sqlite3.connect(Path(cfg.db_path).resolve().as_uri() + '?mode=ro', uri=True)
        source.backup(db.conn)
        source.close()
    now = utcnow()
    start = now + timedelta(minutes=cfg.min_lead_minutes)
    _, end = day_bounds(now, cfg.timezone)
    report = {'date': to_local(now, cfg.timezone).date().isoformat(),
              'window': [start.isoformat(), end.isoformat()],
              'providers': {}, 'sports': {}}
    pool = []
    for name, provider in build_providers(cfg, db).items():
        if name not in cfg.provider_order:
            continue
        provider.http.retries = 0
        provider.http.timeout = 15
        if name == 'sharpapi':
            provider.max_pages = 1
        calls = []
        original = provider.http.get
        def get(path, params=None, original=original, calls=calls):
            try:
                result = original(path, params)
                rows = result.get('response', result.get('data', [])) if isinstance(result, dict) else []
                entry = {'endpoint': path, 'rows': len(rows) if isinstance(rows, list) else None}
                if isinstance(rows, list):
                    entry['sports'] = dict(Counter(str(r.get('sportID', r.get('sport', 'unknown'))) for r in rows if isinstance(r, dict)))
                    entry['leagues'] = dict(Counter(str(r.get('leagueID', r.get('league', 'unknown'))) for r in rows if isinstance(r, dict)))
                if params:
                    entry['requested_league'] = params.get('leagueID', params.get('league'))
                if isinstance(result, dict) and result.get('errors'):
                    errors = str(result['errors']).lower()
                    entry['error_category'] = ('quota' if 'limit' in errors or 'requests' in errors
                                               else 'plan' if 'plan' in errors or 'access' in errors else 'provider_error')
                calls.append(entry)
                return result
            except Exception as exc:
                calls.append({'endpoint': path, 'error_type': type(exc).__name__})
                raise
        provider.http.get = get
        try:
            events = provider.fetch_events(start, end)
            report['providers'][name] = {'calls': calls, 'events': dict(Counter(e.sport for e in events)),
                                         'markets': dict(Counter(e.sport for e in events for m in e.markets))}
            pool = merge_events(pool, events)
        except Exception as exc:
            report['providers'][name] = {'calls': calls, 'error_type': type(exc).__name__}
        print(json.dumps({name: report['providers'][name]}), flush=True)
    scored = score_events(pool, db, cfg)
    picks = select(scored, cfg, now)
    for sport in ('football', 'baseball'):
        candidates = [s for s in scored if s.event.sport == sport]
        report['sports'][sport] = {'normalized_events': sum(e.sport == sport for e in pool),
            'candidates': len(candidates), 'gte50': sum(s.p_model >= .5 for s in candidates),
            'lt50': sum(s.p_model < .5 for s in candidates),
            'daily_ticket': sum(s.event.sport == sport for s in picks.all()),
            'top': sum(s.event.sport == sport for s in picks.top)}
        gates = [('odds', lambda s: s.price >= cfg.top_leg_odds_min),
                 ('confidence', lambda s: s.p_model >= cfg.top_min_prob),
                 ('disagreement_floor', lambda s: not ({"elo"} & set(s.sources))
                  or s.ev >= cfg.top_max_neg_ev),
                 ('agreement', lambda s: s.agreement >= .5),
                 ('two_sources', lambda s: len(s.sources) >= 2),
                 ('no_props', lambda s: s.line.market != 'prop')]
        counts = {}
        for label, predicate in gates:
            candidates = [s for s in candidates if predicate(s)]
            counts[label] = len(candidates)
        report['sports'][sport]['top_gates'] = counts
    report['top'] = [{'sport': s.event.sport, 'confidence': s.p_model, 'odds': s.price} for s in picks.top]
    report['selection_diagnostics'] = getattr(picks, 'diagnostics', {})
    Path('prediction_diagnostic.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report['sports']), flush=True)


if __name__ == '__main__':
    main()
