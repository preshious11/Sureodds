"""Write-once-per-day cache layer.

    predbot.cache.models    unified JSON schema (DailySnapshot / PickRecord / StatsSummary)
    predbot.cache.manager   CacheManager: UTC keys, TTL store, per-day mutex
    predbot.cache.fetcher   UnifiedFetcher: cache-first reads, one API batch per day
"""
from predbot.cache.manager import CacheManager
from predbot.cache.models import DailySnapshot, PickRecord, StatsBlock, StatsSummary
from predbot.cache.fetcher import UnifiedFetcher

__all__ = [
    "CacheManager",
    "DailySnapshot",
    "PickRecord",
    "StatsBlock",
    "StatsSummary",
    "UnifiedFetcher",
]
