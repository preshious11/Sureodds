"""Trace the full pipeline: raw events -> normalized -> scored -> selected."""
import sys
sys.path.insert(0, '.')

from predbot.config import load_config
from predbot.storage.db import Database
from predbot.cache.fetcher import UnifiedFetcher
from predbot.providers.chain import ProviderChain
from predbot.model.ensemble import EventModel, Scored
from predbot.model.elo import Elo
from predbot.model.calibration import Calibrator
from predbot.selection.picker import select, PickSet
from datetime import datetime, timedelta, timezone
from collections import Counter

cfg = load_config()
print(f"Timezone: {cfg.timezone}")
print(f"Config loaded from: {cfg.db_path}")

def _load_provider_cache(self):
        """Load raw provider cache directly from DB (for diagnostics)."""
        from predbot.util.timeutil import day_bounds, utcnow, parse_iso, iso
        from predbot.cache.models import decode_events
        
        tz = self.cfg.timezone
        now = utcnow()
        day_start, midnight = day_bounds(now, tz)
        
        rows = self.db.fetch_all(
            "SELECT provider, sport, status, events, payload_json, metadata_json "
            "FROM provider_cache WHERE local_date = ? ORDER BY provider, sport",
            (self.cache_key(day_start),)
        )
        result = {}
        for row in rows:
            provider = row['provider']
            if provider not in result:
                result[provider] = {
                    'status': row['status'],
                    'sport_status': {},
                    'events': [],
                    'events_by_sport': {},
                    'totals': {'raw_fixtures': 0, 'in_window': 0, 'events_with_markets': 0, 
                               'odds_rows': 0, 'league_rejected': 0, 'parse_rejections': {}},
                }
            result[provider]['sport_status'][row['sport']] = {
                'status': row['status'],
                'events': row['events'],
            }
            try:
                events = decode_events(json.loads(row['payload_json']))
                result[provider]['events'].extend(events)
            except:
                pass
            try:
                meta = json.loads(row['metadata_json'])
                for k in ['raw_fixtures', 'in_window', 'events_with_markets', 'odds_rows', 'league_rejected']:
                    result[provider]['totals'][k] += meta.get(k, 0)
                for k, v in meta.get('parse_rejections', {}).items():
                    result[provider]['totals']['parse_rejections'][k] = result[provider]['totals']['parse_rejections'].get(k, 0) + v
            except:
                pass
        # Build events_by_sport
        for provider, data in result.items():
            result[provider]['events_by_sport'] = {e.sport: result[provider]['events_by_sport'].get(e.sport, 0) + 1 for e in data['events']}
        return result


# Step 1: Get raw provider cache
db = Database(cfg.db_path)
fetcher = UnifiedFetcher(None, db, cfg)

raw = fetcher._load_provider_cache()
print("\n=== STEP 1: RAW PROVIDER CACHE ===")
for provider, data in raw.items():
    print(f'\n--- {provider} ---')
    print(f"  Status: {data.get('status')}")
    sport_status = data.get('sport_status', {})
    print(f"  Sport status: {sport_status}")
    events = data.get('events', [])
    print(f"  Total events: {len(events)}")
    sports = Counter(e.sport for e in events)
    print(f"  By sport: {dict(sports)}")
    # Show a sample event
    if events:
        e = events[0]
        print(f"  Sample event: {e.sport} | {e.league} | {e.home} vs {e.away}")
        print(f"    Markets: {len(e.markets)}")
        for m in e.markets[:3]:
            print(f"      {m.market}:{m.selection} price={m.price} line={m.line} stat={m.stat} entity={m.entity}")

# Step 2: Build the chain and collect events
print("\n=== STEP 2: CHAIN COLLECTION ===")
from predbot.providers.base import Event
from predbot.util.timeutil import day_bounds, parse_iso

tz = cfg.timezone
now = datetime.now(timezone.utc)
day_start, midnight = day_bounds(now, tz)

chain = ProviderChain(raw, cfg.provider_order)
chain.cfg = cfg
chain.db = db

events = chain.collect(day_start, midnight)
print(f"Collected events: {len(events)}")
sports = Counter(e.sport for e in events)
print(f"By sport: {dict(sports)}")
for e in events[:3]:
    print(f"  {e.sport} | {e.league} | {e.home} vs {e.away} | markets={len(e.markets)} | sources={e.sources}")

# Step 3: Score all events
print("\n=== STEP 3: SCORING ===")
elo = Elo()
cal = Calibrator()
all_scored = []
for e in events:
    model = EventModel(e, elo, cal, cfg.model_shrink_to_market)
    scored = model.score_all()
    all_scored.extend(scored)
    if scored:
        print(f"  {e.sport} | {e.home} vs {e.away} | scored {len(scored)} lines")
        for s in scored[:2]:
            print(f"    {s.line.market}:{s.line.selection} prob={s.p_model:.3f} ev={s.ev:.3f} price={s.price:.2f} sources={list(s.sources.keys())}")

print(f"\nTotal scored lines: {len(all_scored)}")
sports_scored = Counter(s.event.sport for s in all_scored)
print(f"By sport: {dict(sports_scored)}")

# Step 4: Check eligibility
print("\n=== STEP 4: ELIGIBILITY FILTER ===")
eligible = []
rejected = []
for s in all_scored:
    reasons = []
    if s.p_model < cfg.daily_min_prob:
        reasons.append(f"prob={s.p_model:.2f}<{cfg.daily_min_prob}")
    if {"elo"} & set(s.sources) and s.ev < cfg.daily_max_neg_ev:
        reasons.append(f"ev={s.ev:.3f}<{cfg.daily_max_neg_ev}")
    if s.price < cfg.daily_min_odds:
        reasons.append(f"price={s.price:.2f}<{cfg.daily_min_odds}")
    if s.price > cfg.daily_max_odds:
        reasons.append(f"price={s.price:.2f}>{cfg.daily_max_odds}")
    if reasons:
        rejected.append((s, reasons))
    else:
        eligible.append(s)

print(f"Eligible: {len(eligible)}")
print(f"Rejected: {len(rejected)}")
sports_eligible = Counter(s.event.sport for s in eligible)
print(f"Eligible by sport: {dict(sports_eligible)}")
sports_rejected = Counter(s.event.sport for s, _ in rejected)
print(f"Rejected by sport: {dict(sports_rejected)}")

if rejected:
    print("\nRejection reasons (sample):")
    for s, reasons in rejected[:5]:
        print(f"  {s.event.sport} | {s.label} | {'; '.join(reasons)}")

# Step 5: Run picker
print("\n=== STEP 5: PICKER ===")
ps = select(all_scored, cfg)
print(f"Top picks: {len(ps.top)}")
print(f"Daily picks: {len(ps.daily)}")
print(f"Pool size: {ps.pool_size}")
print(f"Eligible: {ps.eligible}")

print("\nTop picks by sport:")
for s in ps.top:
    print(f"  {s.event.sport} | {s.label} @ {s.price:.2f} (prob={s.p_model:.2f})")

print("\nDaily picks by sport:")
for s in ps.daily:
    print(f"  {s.event.sport} | {s.label} @ {s.price:.2f} (prob={s.p_model:.2f})")

print(f"\nDiagnostics: {ps.diagnostics}")
