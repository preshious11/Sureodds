"""Diagnose WHY tennis/basketball never make Daily Picks and Top Picks is empty.

Prints, per sport x provider: validated candidate count and the p_model /
implied_prob distributions, the per-sport daily-funnel rejection reasons,
which ensemble signals actually fired per sport, and the top 10 Odds API
candidates by p_model. Read-only: no thresholds are changed here.
"""
from __future__ import annotations

import math
import sys
from collections import Counter, defaultdict
from datetime import timedelta

from predbot.config import load_config
from predbot.jobs.daily_picks import score_events
from predbot.markets.catalog import spec
from predbot.providers.chain import ProviderChain, build_providers
from predbot.storage.db import Database
from predbot.util.timeutil import day_bounds, iso, to_local, utcnow


def quartiles(vals):
    if not vals:
        return "n=0"
    s = sorted(vals)

    def q(p):
        i = min(len(s) - 1, max(0, int(p * (len(s) - 1))))
        return s[i]

    return (f"n={len(s)} min={s[0]:.3f} p25={q(0.25):.3f} med={q(0.5):.3f} "
            f"p75={q(0.75):.3f} max={s[-1]:.3f}")


def base_valid(s) -> bool:
    """The picker's pre-threshold validation (kickoff/price/market checks)."""
    if not s.event.start:
        return False
    if not spec(s.line.market).score_settled:
        return False
    return math.isfinite(s.price) and s.price >= 1.15


def main():
    cfg = load_config()
    db = Database(cfg.db_path)
    now = utcnow()
    day = to_local(now, cfg.timezone).date().isoformat()
    start = now + timedelta(minutes=cfg.min_lead_minutes)
    _, midnight = day_bounds(now, cfg.timezone)
    end = min(midnight, now + timedelta(hours=cfg.pick_window_hours))
    print(f"== window: [{iso(start)} .. {iso(end)}]  "
          f"(now+{cfg.min_lead_minutes}min .. min(local midnight {cfg.timezone}, now+{cfg.pick_window_hours}h))")

    providers = build_providers(cfg, db)
    chain = ProviderChain(providers, cfg.provider_order, cfg.chain_min_events)
    chain.db, chain.cfg = db, cfg
    events = chain.collect(start, end)

    print("\n== provider reports (fetch status / budget)")
    for name, rep in chain.last_report.items():
        prov = providers.get(name)
        used = prov.budget.used_today(name) if prov else "?"
        print(f"  {name}: status={rep.get('status')} events={rep.get('events')} "
              f"budget_used_today={used} rejections={rep.get('rejections')} "
              f"sport_status={rep.get('sport_status')}")

    sgo = providers.get("sportsgameodds")
    if sgo is not None:
        print(f"\n== SGO config: sports={sgo.sports} leagues={getattr(sgo, 'leagues', None)}")
        avail = sgo.available_leagues()
        if avail is not None:
            keep = [lg for lg in sgo.leagues if lg in avail]
            drop = [lg for lg in sgo.leagues if lg not in avail]
            print(f"  covered by this tier ({len(keep)}): {keep}")
            print(f"  NOT covered by this tier ({len(drop)}): {drop}")
            for lg in drop:
                print(f"    {lg}: sportID={sgo._league_sport.get(lg, '?')}")
        else:
            print("  /leagues catalog unavailable")

    scored = score_events(events, db, cfg)
    print(f"\n== scored candidates: {len(scored)} (from {len(events)} events)")

    # ---- top-priority chain (oddsapi), same as build_picks does for Top picks
    prio = {n: providers[n] for n in cfg.top_provider_priority if n in providers
            and providers[n].enabled()}
    if prio:
        top_chain = ProviderChain(providers, cfg.top_provider_priority, cfg.chain_min_events)
        top_chain.db, top_chain.cfg = db, cfg
        top_events = top_chain.collect(start, end)
        print(f"\n== top-priority chain {cfg.top_provider_priority}: {len(top_events)} events "
              f"report={ {k: {kk: vv for kk, vv in r.items() if kk in ('status', 'events', 'rejections')} for k, r in top_chain.last_report.items()} }")
        top_scored = score_events(top_events, db, cfg)
        print(f"   scored={len(top_scored)} by sport="
              f"{dict(Counter(s.event.sport for s in top_scored))}")
        for sport in sorted({s.event.sport for s in top_scored}):
            items = [s for s in top_scored if s.event.sport == sport and base_valid(s)]
            print(f"   {sport}: validated={len(items)}")
            if items:
                print(f"       p_model : {quartiles([s.p_model for s in items])}")
                print(f"       implied : {quartiles([1.0 / s.price for s in items])}")
        scored = scored + top_scored
    return cfg, scored


def report_distributions(cfg, scored):
    groups = defaultdict(list)
    for s in scored:
        groups[(s.event.sport, s.event.provider)].append(s)
    print("\n== per sport x provider (validated candidates; p_model / implied=1/price)")
    for key in sorted(groups):
        sport, prov = key
        items = [s for s in groups[key] if base_valid(s)]
        raw = groups[key]
        print(f"  {sport:12s} {prov:15s} scored={len(raw):5d} validated={len(items):5d}")
        if not items:
            continue
        print(f"      p_model  : {quartiles([s.p_model for s in items])}")
        print(f"      implied  : {quartiles([1.0 / s.price for s in items])}")
        print(f"      p_market : {quartiles([s.p_market for s in items])}")
        below = sum(1 for s in items if s.p_model < 1.0 / s.price)
        print(f"      p_model < implied on {below}/{len(items)} "
              f"({100.0 * below / len(items):.0f}%)  [ensemble-shrink check]")

    print("\n== ensemble signal presence per sport")
    sig = defaultdict(Counter)
    for s in scored:
        sig[s.event.sport]["_candidates"] += 1
        for k in s.sources:
            sig[s.event.sport][k] += 1
    for sport in sorted(sig):
        print(f"  {sport:12s} {dict(sig[sport])}")

    print("\n== daily-funnel rejection reasons per sport (validated candidates)")
    for sport in sorted({s.event.sport for s in scored}):
        items = [s for s in scored if s.event.sport == sport and base_valid(s)]
        rej = Counter()
        for s in items:
            if not (cfg.daily_min_odds <= s.price <= cfg.daily_max_odds):
                rej["price_window"] += 1
            if s.p_model < cfg.daily_min_prob:
                rej["p_model<%.2f" % cfg.daily_min_prob] += 1
            if {"elo"} & set(s.sources) and s.ev < cfg.daily_max_neg_ev:
                rej["ev<%.2f" % cfg.daily_max_neg_ev] += 1
            if s.agreement < 0.30:
                rej["agreement<0.30"] += 1
        passing = sum(1 for s in items
                      if cfg.daily_min_odds <= s.price <= cfg.daily_max_odds
                      and s.p_model >= cfg.daily_min_prob
                      and not ({"elo"} & set(s.sources) and s.ev < cfg.daily_max_neg_ev)
                      and s.agreement >= 0.30)
        print(f"  {sport:12s} validated={len(items):5d} passing_all={passing:5d} {dict(rej)}")


def report_top(cfg, scored):
    oa = [s for s in scored if s.event.provider == "oddsapi" and base_valid(s)]
    oa.sort(key=lambda s: -s.p_model)
    print(f"\n== top 10 Odds API candidates by p_model (pool={len(oa)}) | "
          f"TOP_MIN_PROB={cfg.top_min_prob} TOP_LEG_ODDS_MIN={cfg.top_leg_odds_min} "
          f"TOP_ODDS_MAX={cfg.top_odds_max} target={cfg.top_odds_target}")
    for s in oa[:10]:
        print(f"  p={s.p_model:.3f} pmkt={s.p_market:.3f} price={s.price:.2f} ev={s.ev:+.3f} "
              f"agr={s.agreement:.2f} src={sorted(s.sources)} | {s.event.sport} "
              f"{s.event.home} v {s.event.away} | {s.label}")
    if oa:
        band = [s for s in oa if 0.55 <= s.p_model < cfg.top_min_prob]
        print(f"  candidates in the 0.55-{cfg.top_min_prob} band just under TOP_MIN_PROB: {len(band)}")
    for sport in ("tennis", "basketball"):
        sub = [s for s in scored if s.event.sport == sport]
        if sub:
            best = max(sub, key=lambda s: s.p_model)
            print(f"  best {sport} candidate overall: p={best.p_model:.3f} price={best.price:.2f} "
                  f"provider={best.event.provider} src={sorted(best.sources)} | {best.label}")
        else:
            print(f"  {sport}: no scored candidates")


if __name__ == "__main__":
    cfg, scored = main()
    report_distributions(cfg, scored)
    report_top(cfg, scored)
    sys.exit(0)
