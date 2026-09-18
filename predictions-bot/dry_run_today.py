"""Full dry run for today: Daily Picks (per-sport / per-stat counts) and Top
Picks, each leg with provider, league, market, p_model, odds and ev.

Usage: python dry_run_today.py [--force]   (--force re-fetches live providers)
"""
from __future__ import annotations

import sys
from collections import Counter

from predbot.config import load_config
from predbot.cache.fetcher import UnifiedFetcher
from predbot.providers.chain import build_providers
from predbot.storage.db import Database


def fmt(ps, cfg):
    lines = []
    for tier, items in (("TOP", ps.top), ("DAILY", ps.daily)):
        lines.append(f"\n== {tier} ({len(items)} legs) "
                     f"ticket={__import__('math').prod(s.price for s in items):.2f}")
        for i, s in enumerate(items, 1):
            e, m = s.event, s.line
            ln = f" {m.line:g}" if m.line is not None else ""
            lines.append(f"  {i:2d}. [{e.provider:11s}] {e.sport:10s} {e.league[:24]:24s} "
                         f"{s.label:42s} p={s.p_model:.3f} odds={m.price:.2f} ev={s.ev:+.3f} "
                         f"src={sorted(s.sources)}")
    lines.append("\n== daily counts by sport:   " + str(dict(Counter(s.event.sport for s in ps.daily))))
    lines.append("== daily counts by market/stat: " + str(dict(Counter(f'{s.line.market}/{s.line.stat}' for s in ps.daily))))
    lines.append("== top counts by sport:        " + str(dict(Counter(s.event.sport for s in ps.top))))
    lines.append("== diagnostics: daily_pool_rejections=" + str(ps.diagnostics.get("daily_pool_rejections")))
    lines.append("   top_pool_rejections=" + str(ps.diagnostics.get("top_pool_rejections")))
    lines.append("   top_priority_used=" + str(ps.diagnostics.get("top_priority_used")))
    return "\n".join(lines)


def main():
    force = "--force" in sys.argv
    cfg = load_config()
    db = Database(cfg.db_path)
    from predbot.providers.chain import build_providers, ProviderChain
    providers = build_providers(cfg, db)
    chain = ProviderChain(providers, cfg.provider_order, cfg.chain_min_events)
    service = UnifiedFetcher(chain, db, cfg)
    ps = service.get_daily_snapshot(force_refresh=force)
    # re-run the picker over the same pool to show per-leg detail (snapshot rows
    # don't carry sources dicts)
    from predbot.jobs.daily_picks import build_picks
    pickset = build_picks(chain, db, cfg)
    print(fmt(pickset, cfg))
    print(f"\n== stored snapshot: top={len(ps.top_picks)} daily={len(ps.daily_picks)} "
          f"status={ps.status}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
