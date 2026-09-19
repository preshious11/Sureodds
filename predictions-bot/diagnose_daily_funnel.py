"""Post-change Daily funnel: how many candidates survive each rule, in order,
plus an ablation matrix (which cap/rule is actually binding) and the highest-
p_model candidates that were NOT selected, with the rule that stopped each.

Read-only. Usage: python diagnose_daily_funnel.py [--force]
"""
from __future__ import annotations

import math
import sys
from collections import Counter, defaultdict
from dataclasses import replace
from datetime import timedelta
from itertools import combinations

from predbot.config import load_config
from predbot.jobs.daily_picks import score_events
from predbot.markets.catalog import spec
from predbot.providers.chain import ProviderChain, build_providers
from predbot.selection.picker import INDEPENDENT_SIGNALS, select
from predbot.storage.db import Database
from predbot.util.timeutil import day_bounds, iso, to_local, utcnow


def base_valid(s, lead) -> str:
    """The picker's pre-threshold validation, returning '' when valid."""
    if not s.event.start:
        return "INVALID_KICKOFF"
    if s.event.start < lead:
        return "WRONG_DATE"
    if not spec(s.line.market).score_settled and not (s.line.market == "prop" and s.line.provider_odd_id):
        return "UNSUPPORTED_MARKET"
    if not math.isfinite(s.price) or s.price < 1.15:
        return "INVALID_PRICE"
    return ""


def collect(cfg, db, force):
    providers = build_providers(cfg, db)
    chain = ProviderChain(providers, cfg.provider_order)
    chain.db, chain.cfg = db, cfg
    if force:
        chain.force_retry = True
    now = utcnow()
    start = now + timedelta(minutes=cfg.min_lead_minutes)
    _, midnight = day_bounds(now, cfg.timezone)
    end = min(midnight + timedelta(hours=cfg.day_window_end_offset_hours),
              now + timedelta(hours=cfg.pick_window_hours))
    day = to_local(now, cfg.timezone).date().isoformat()
    events = chain.collect(start, end)
    top_priority_source = None
    prio = [n for n in cfg.top_provider_priority if n in providers and providers[n].enabled()]
    if prio:
        top_chain = ProviderChain(providers, cfg.top_provider_priority)
        top_chain.db, top_chain.cfg = db, cfg
        prio_events = top_chain.collect(start, end)
        if prio_events:
            from predbot.providers.chain import merge_events
            events = merge_events(events, prio_events)
            top_priority_source = top_chain.order[0] if top_chain.order else None
    matched = {(r["sport"], str(r["home"]), str(r["away"])) for r in db.previous_pick_fixtures(day)}
    before = len(events)
    events = [e for e in events if (e.sport, e.home, e.away) not in matched]
    print(f"== window [{iso(start)} .. {iso(end)}] | events={before} "
          f"prior_day_dedupe_dropped={before - len(events)} | top_priority_source={top_priority_source}")
    print(f"== events by sport: {dict(Counter(e.sport for e in events))}")
    return chain, events, now, top_priority_source


def stage_table(cfg, scored, now):
    lead = now + timedelta(minutes=cfg.min_lead_minutes)
    print("\n== cumulative survivors per rule (picker's order, per sport)")
    for sport in sorted({s.event.sport for s in scored}):
        items = [s for s in scored if s.event.sport == sport]
        rows = [("scored", len(items))]
        items = [s for s in items if base_valid(s, lead) == ""]
        rows.append(("validated (settleable, price>=1.15)", len(items)))
        items = [s for s in items if cfg.daily_min_odds <= s.price <= cfg.daily_max_odds]
        rows.append((f"price window {cfg.daily_min_odds}-{cfg.daily_max_odds}", len(items)))
        items = [s for s in items if s.p_model >= cfg.daily_min_prob]
        rows.append((f"p_model>={cfg.daily_min_prob}", len(items)))
        items = [s for s in items if not (INDEPENDENT_SIGNALS & set(s.sources)) or s.ev >= cfg.daily_max_neg_ev]
        rows.append((f"disagreement floor (ev>={cfg.daily_max_neg_ev} if elo)", len(items)))
        items = [s for s in items if s.agreement >= 0.30]
        rows.append(("agreement>=0.30", len(items)))
        preferred = [s for s in items if s.p_model >= 0.5]
        rows.append(("preferred p_model>=0.5 filter (hidden rule)", len(preferred) if preferred else len(items)))
        print(f"  --- {sport} ---")
        for name, n in rows:
            print(f"    {name:56s} {n:6d}")
    return None


def unselected(cfg, scored, ps, now, trace):
    """Highest-p_model candidates NOT in the final slate, with the exact rule
    the picker recorded as blocking each one."""
    lead = now + timedelta(minutes=cfg.min_lead_minutes)
    chosen = {id(s) for s in ps.all()}
    ranked = sorted((s for s in scored if id(s) not in chosen), key=lambda s: -s.p_model)
    preferred_exists = any(base_valid(s, lead) == "" and s.p_model >= 0.5 for s in scored)
    print(f"\n== 20 highest p_model candidates NOT selected (of {len(ranked)} unselected; slate={len(ps.all())})")
    for s in ranked[:20]:
        print(f"  p={s.p_model:.3f} {s.event.sport:10s} {s.event.provider:12s} "
              f"{s.event.league[:20]:20s} {s.label[:36]:36s} odds={s.price:.2f} ev={s.ev:+.3f} "
              f"-> {trace.get(id(s), 'no rule recorded (never reached)')}")
    top_ids = {id(s) for s in ps.top}
    ranked_elig = [s for s in ranked if base_valid(s, lead) == ""
                   and cfg.daily_min_odds <= s.price <= cfg.daily_max_odds
                   and s.p_model >= cfg.daily_min_prob
                   and not (INDEPENDENT_SIGNALS & set(s.sources) and s.ev < cfg.daily_max_neg_ev)
                   and s.agreement >= 0.30 and (s.p_model >= 0.5 or not preferred_exists)]
    print(f"\n== 20 highest p_model ELIGIBLE candidates NOT selected "
          f"(passed every gate; {len(ranked_elig)} such)")
    for s in ranked_elig[:20]:
        print(f"  p={s.p_model:.3f} {s.event.sport:10s} {s.event.provider:12s} "
              f"{s.event.league[:20]:20s} {s.label[:36]:36s} odds={s.price:.2f} ev={s.ev:+.3f} "
              f"stat={s.line.stat:8s} -> {trace.get(id(s), 'no rule recorded')}")
    print("\n== basketball: top 10 by p_model with blocking rule")
    bball = sorted((s for s in scored if s.event.sport == "basketball"), key=lambda s: -s.p_model)
    for s in bball[:10]:
        print(f"  p={s.p_model:.3f} {s.event.provider:12s} {s.event.league[:18]:18s} "
              f"{s.label[:34]:34s} odds={s.price:.2f} ev={s.ev:+.3f} "
              f"sources={sorted(s.sources)} -> {trace.get(id(s), 'no rule recorded')}")
    print("\n== best candidate per sport vs the ticket")
    for sport in sorted({s.event.sport for s in scored}):
        cands = [s for s in ranked_elig if s.event.sport == sport]
        if not cands:
            cands = sorted((s for s in scored if s.event.sport == sport), key=lambda s: -s.p_model)[:1]
            if cands:
                s = cands[0]
                print(f"  {sport:10s} best p={s.p_model:.3f} @ {s.price:.2f} "
                      f"(ev={s.ev:+.3f}, sources={sorted(s.sources)}) - not eligible")
            continue
        s = cands[0]
        n_corners = "yes" if any(c.line.stat == "corners" for c in cands) else "no"
        print(f"  {sport:10s} best eligible p={s.p_model:.3f} @ {s.price:.2f} "
              f"(corners-stat candidates: {n_corners})")


def top_view(cfg, scored, ps, trace, exclude_sources=()):
    """Why Top is empty (or what it picked): the whole pool's highest-p legs,
    the Top bar, and the best 2/3-leg products inside the 2.00-2.50 band.

    ``exclude_sources`` simulates a provider being out of quota: candidates
    whose event.sources are EXACTLY that provider are dropped from the pool
    first (an Odds API-only event can never be a fallback leg)."""
    if exclude_sources:
        n0 = len(scored)
        scored = [s for s in scored if list(s.event.sources) != list(exclude_sources)]
        print(f"\n== provider simulation: dropped events seen ONLY by "
              f"{','.join(exclude_sources)} ({n0 - len(scored)} of {n0} candidates)")
    print("\n== TOP picks view")
    print(f"   bar: p_model>={cfg.top_min_prob}, len(sources)>=2, "
          f"agreement>={cfg.top_min_agreement}, price>={cfg.top_leg_odds_min}, no props")
    print(f"   ticket band: {cfg.top_odds_target}..{cfg.top_odds_max}, legs "
          f"{cfg.top_legs_min}-{cfg.top_legs_max}, rank = product of p_model then closeness to "
          f"{cfg.top_odds_target}")
    print(f"   chosen: {len(ps.top)} legs "
          f"(ticket={math.prod(s.price for s in ps.top) if ps.top else 0:.2f}, "
          f"top_priority_used={ps.diagnostics.get('top_priority_used')})")
    for s in ps.top:
        print(f"     {s.event.provider:12s} {s.event.league[:18]:18s} {s.label[:34]:34s} "
              f"p={s.p_model:.3f} odds={s.price:.2f} ev={s.ev:+.3f}")
    if ps.diagnostics.get("top_pool_rejections"):
        print(f"   top_pool_rejections: {ps.diagnostics['top_pool_rejections']}")
    print(f"   top_pool size: {ps.diagnostics.get('top_pool_size', 'n/a')}")

    whole = sorted(scored, key=lambda s: -s.p_model)
    print("\n   -- 10 highest p_model legs in the WHOLE pool (bar not applied) --")
    for s in whole[:10]:
        print(f"     p={s.p_model:.3f} {s.event.sport:10s} {s.event.provider:12s} "
              f"{s.event.league[:16]:16s} {s.label[:30]:30s} odds={s.price:.2f} ev={s.ev:+.3f} "
              f"agr={s.agreement:.2f} src={len(s.sources)} -> {trace.get(id(s), '-')}")

    bar = [s for s in scored if s.p_model >= cfg.top_min_prob
           and len(s.sources) >= 2 and s.agreement >= cfg.top_min_agreement
           and s.price >= cfg.top_leg_odds_min and s.line.market != "prop"]
    # one leg per fixture
    uniq, seen_fx = [], set()
    for s in sorted(bar, key=lambda s: -s.p_model):
        fx = (s.event.sport, s.event.home, s.event.away)
        if fx in seen_fx:
            continue
        seen_fx.add(fx)
        uniq.append(s)
    print(f"\n   -- candidates clearing the TOP bar (excl. ticket band): {len(uniq)} --")
    fx = {(s.event.sport, s.event.home, s.event.away) for s in uniq}
    psport = Counter(s.event.sport for s in uniq)
    print(f"     across {len(fx)} distinct fixtures; by sport {dict(psport)}")
    keep = [s for s in uniq if s.agreement >= cfg.top_min_agreement]
    print(f"     surviving TOP_MIN_AGREEMENT ({cfg.top_min_agreement}): {len(keep)} "
          f"across {len({(s.event.sport, s.event.home, s.event.away) for s in keep})} fixtures")
    for s in uniq[:10]:
        print(f"     p={s.p_model:.3f} {s.event.sport:10s} {s.label[:32]:32s} odds={s.price:.2f} "
              f"ev={s.ev:+.3f} agr={s.agreement:.2f}")
    # Bounded search only for the report: the picker uses its own (already
    # capped) pool; combinations over an unbounded pool would explode. Legs
    # below ~1.20 cannot contribute to a 2.00-2.50 product in 2-3 legs, so
    # they are pruned here (they stay in the bar-clearing list above).
    search = [s for s in uniq if s.price >= 1.20][:60]
    print(f"   combo search pool: {len(search)} legs priced >= 1.20 "
          f"(bar-pool price range {min((s.price for s in uniq), default=0):.2f}.."
          f"{max((s.price for s in uniq), default=0):.2f})")
    n_fixtures = len({(s.event.sport, s.event.home, s.event.away) for s in uniq})
    from predbot.selection.picker import same_fixture as _picker_same_fixture
    best = []
    for size in (2, 3):
        for combo in combinations(search, size):
            if any(_picker_same_fixture(a.event, b.event) for a, b in combinations(combo, 2)):
                continue
            price = math.prod(s.price for s in combo)
            if cfg.top_odds_target <= price <= cfg.top_odds_max:
                best.append((-math.prod(s.p_model for s in combo), size, price, combo))
    best.sort()
    print(f"   realistic fallback-ticket search over {len(search)} legs priced >= 1.20: "
          f"{len(best)} in-band combos found over {n_fixtures} distinct fixtures")
    if best:
        print(f"   -- best in-band combos in the {len(search)}-leg search pool: {len(best)} --")
        for _, size, price, combo in best[:5]:
            legs = " | ".join(f"{s.event.provider[:9]} {s.label[:24]} p={s.p_model:.3f} "
                              f"odds={s.price:.2f} src={sorted(s.sources)}" for s in combo)
            print(f"     {size} legs ticket={price:.2f} p-product={math.prod(s.p_model for s in combo):.3f}: {legs}")
    else:
        print("   -- NO combination inside the 2.00-2.50 band from the bar-clearing pool --")
        if uniq:
            lo = min(s.price for s in uniq)
            print(f"      cheapest/most expensive bar-clearing leg: {lo:.2f} / "
                  f"{max(s.price for s in uniq):.2f}; "
                  f"a 2-leg product range from this pool is {lo * lo:.2f} .. "
                  f"{max(s.price for s in uniq) ** 2:.2f}")


def ablation(cfg, scored, now):
    """Which rule is actually binding: relax ONE rule at a time and see if the
    slate grows or the ticket reaches the 10.0 floor strictly."""
    print("\n== ablation matrix (one rule relaxed at a time)")
    variants = {
        "baseline": {},
        "market share off (1.0)": {"daily_max_market_share": 1.0},
        "stat share off (1.0)": {"daily_max_stat_share": 1.0},
        "sport share off (1.0)": {"daily_max_sport_share": 1.0},
        "per-league cap off (99)": {"daily_max_per_league": 99},
        "price window = 1.01-10.0": {"daily_min_odds": 1.01, "daily_max_odds": 10.0},
        "ceiling 30 (DAILY_COMBINED_MAX_ODDS)": {"daily_odds_max": 30.0},
        "ceiling 40": {"daily_odds_max": 40.0},
        "ceiling 30 + market share 0.33": {"daily_odds_max": 30.0, "daily_max_market_share": 0.33},
        "min odds 1.20": {"daily_min_odds": 1.20},
        "sport share 0.60": {"daily_max_sport_share": 0.60},
        "min odds 1.20 + sport share 0.60": {"daily_min_odds": 1.20, "daily_max_sport_share": 0.60},
        "min odds 1.20 + market share 0.33 + sport share 0.60":
            {"daily_min_odds": 1.20, "daily_max_market_share": 0.33, "daily_max_sport_share": 0.60},
        "disagreement floor off (-1.0)": {"daily_max_neg_ev": -1.0},
        "per-event cap off (99)": {"max_picks_per_event": 99},
        "target cap off (99 picks)": {"daily_target": 99},
    }
    for name, over in variants.items():
        c = replace(cfg, **over) if over else cfg
        ps = select(scored, c, now)
        ticket = math.prod(s.price for s in ps.all())
        comp = " ".join(f"{s.event.sport[:4]}/{s.line.stat[:4]}:{s.p_model:.2f}@{s.price:.2f}"
                        for s in ps.daily)
        print(f"  {name:50s} daily={len(ps.daily):3d} top={len(ps.top):2d} "
              f"ticket={ticket:6.2f} relaxed={bool(ps.diagnostics.get('daily_caps_relaxed'))} "
              f"{ps.diagnostics.get('daily_caps_relaxed', {}).get('exceeded', '') if ps.diagnostics.get('daily_caps_relaxed') else ''}")
        print(f"       {comp}")
        if name == "baseline":
            print(f"     daily by sport={ps.diagnostics.get('daily_ticket_by_sport')} "
                  f"by market/stat={ps.diagnostics.get('daily_by_market')}")
            print(f"     strict ticket={ps.diagnostics.get('daily_caps_relaxed', {}).get('strict_ticket')} "
                  f"strict_picks={ps.diagnostics.get('daily_caps_relaxed', {}).get('strict_picks')} "
                  f"pool={ps.diagnostics.get('daily_caps_relaxed', {}).get('daily_pool')}")


def main():
    force = "--force" in sys.argv
    cfg = load_config()
    db = Database(cfg.db_path)
    chain, events, now, top_priority_source = collect(cfg, db, force)
    scored = score_events(events, db, cfg)
    print(f"== scored={len(scored)}")
    stage_table(cfg, scored, now)
    ps = select(scored, cfg, now, top_priority_source=top_priority_source)
    trace: dict = {}
    ps = select(scored, cfg, now, top_priority_source=top_priority_source, trace=trace)
    unselected(cfg, scored, ps, now, trace)
    top_view(cfg, scored, ps, trace)
    # The October scenario: Odds API out of quota. Cached oddsapi events are
    # gone, so its legs can't enter; re-check what the FALLBACK pool alone
    # can build before pushing.
    top_view(cfg, scored, ps, trace, exclude_sources=("oddsapi",))
    ablation(cfg, scored, now)


if __name__ == "__main__":
    sys.exit(main())