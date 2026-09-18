"""Print the Odds API's LIVE quota headers and the gate decision.

/sports and /sports/{key}/events cost 0 credits, so this is safe to run even
when the quota is exhausted - it still reports x-requests-remaining/used.
"""
from __future__ import annotations

from predbot.config import load_config
from predbot.providers.budget import Budget
from predbot.providers.oddsapi import OddsApiProvider
from predbot.storage.db import Database
from predbot.util.http import ProviderError


def main():
    cfg = load_config()
    db = Database(cfg.db_path)
    p = OddsApiProvider(cfg.odds_api_key, Budget(db), regions=cfg.odds_api_regions,
                        monthly_limit=cfg.odds_api_monthly_limit,
                        max_credits_per_cycle=cfg.oddsapi_max_credits_per_cycle,
                        reserve_credits=cfg.oddsapi_reserve_credits)
    print(f"config: regions={p.regions} markets={p.markets} credits/key={p.credits_per_key} "
          f"cycle_cap={p.max_credits_per_cycle} reserve={p.reserve_credits}")
    print(f"persisted kv before: remaining={db.get_kv('oddsapi:requests_remaining')} "
          f"used={db.get_kv('oddsapi:requests_used')}")

    print("\n-- GET /sports (0 credits)"
          " ------------------------------------------------------------------")
    try:
        data = p._get("/sports")
    except ProviderError as e:
        print(f"  failed: {e}")
        data = None
    headers = getattr(p.http, "last_headers", {}) or {}
    print(f"  x-requests-remaining = {headers.get('x-requests-remaining')}")
    print(f"  x-requests-used      = {headers.get('x-requests-used')}")
    print(f"  x-requests-last      = {headers.get('x-requests-last')}")
    if data:
        keys = [r.get("key") for r in data if isinstance(r, dict)]
        print(f"  in-season keys returned: {len(keys)}")

    print("\n-- GET /sports/{key}/events (0 credits, first in-season key) ---------")
    key = None
    if data:
        key = next((r["key"] for r in data if isinstance(r, dict) and r.get("key")), None)
    if key:
        try:
            events = p._get(f"/sports/{key}/events", {"dateFormat": "iso"})
            print(f"  {key}: {len(events) if isinstance(events, list) else '?'} events")
        except ProviderError as e:
            print(f"  failed: {e}")
        h = getattr(p.http, "last_headers", {}) or {}
        print(f"  x-requests-remaining = {h.get('x-requests-remaining')}")
        print(f"  x-requests-used      = {h.get('x-requests-used')}")
        print(f"  x-requests-last      = {h.get('x-requests-last')}")

    print("\n-- gate ------------------------------------------------------------------")
    print(f"  provider.requests_remaining = {p.requests_remaining}")
    print(f"  provider.requests_used      = {p.requests_used}")
    print(f"  may spend {p.credits_per_key} credits now? {p.can_spend_credits(p.credits_per_key)}")
    print(f"  persisted kv after : remaining={db.get_kv('oddsapi:requests_remaining')} "
          f"used={db.get_kv('oddsapi:requests_used')}")


if __name__ == "__main__":
    main()