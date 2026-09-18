"""Free-tier budget accounting so the bot never silently burns a quota.

api_football   : requests per day
sportsgameodds : *objects* (events) per month, spread over the month
sharpapi       : requests per minute (handled by RateLimiter) - tracked daily for info only
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from predbot.storage.db import Database

log = logging.getLogger(__name__)


class Budget:
    def __init__(self, db: Database):
        self.db = db

    @staticmethod
    def _day() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    @staticmethod
    def _month() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m")

    def used_today(self, provider: str) -> int:
        return self.db.budget_used(provider, self._day())

    def used_month(self, provider: str) -> int:
        return self.db.budget_used(provider, self._month())

    def spend(self, provider: str, n: int = 1) -> None:
        # Both buckets commit together so a crash between the two calls can't
        # leave the daily count updated but the monthly count stale (which
        # would let the monthly quota silently drift over its real usage).
        with self.db.transaction():
            self.db.budget_add(provider, self._day(), n)
            self.db.budget_add(provider, self._month(), n)

    def can_spend(self, provider: str, n: int, daily_limit: int = 0, monthly_limit: int = 0) -> bool:
        if daily_limit and self.used_today(provider) + n > daily_limit:
            log.warning("%s daily budget exhausted (%d/%d)", provider, self.used_today(provider), daily_limit)
            return False
        if monthly_limit and self.used_month(provider) + n > monthly_limit:
            log.warning("%s monthly budget exhausted (%d/%d)", provider, self.used_month(provider), monthly_limit)
            return False
        return True

    def monthly_daily_allowance(self, provider: str, monthly_limit: int, reserve_frac: float = 0.25) -> int:
        """How many objects we may spend today to stay within a monthly quota,
        keeping `reserve_frac` of the month's remaining quota for settlement."""
        now = datetime.now(timezone.utc)
        import calendar
        days_in_month = calendar.monthrange(now.year, now.month)[1]
        days_left = days_in_month - now.day + 1
        remaining = max(0, monthly_limit - self.used_month(provider))
        return int(remaining * (1 - reserve_frac) / max(1, days_left))

    # ---- exhaustion marks (persisted per UTC day, so restarts don't re-burn quota)
    def mark_exhausted(self, provider: str, reason: str = "") -> None:
        log.warning("%s marked exhausted for %s%s", provider, self._day(),
                    f": {reason}" if reason else "")
        self.db.budget_add(f"{provider}:exhausted", self._day(), 1)

    def is_exhausted(self, provider: str) -> bool:
        return self.db.budget_used(f"{provider}:exhausted", self._day()) > 0
