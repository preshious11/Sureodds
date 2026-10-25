"""OddsAPI credit guard: reserve floor from x-requests-remaining, and the daily cap on closing-price calls."""
from __future__ import annotations

from typing import Optional


class CreditGuard:
    """Tracks the last x-requests-remaining header and today's closing-capture spend.

    A call is allowed only when the balance is known, stays at or above ``reserve`` after the call, and the
    closing-capture spend stays within ``cap`` for the day. Refusals never raise: callers check and skip."""

    def __init__(self, reserve: int, cap: int, remaining: Optional[int] = None, spent_today: int = 0):
        self.reserve = int(reserve)
        self.cap = int(cap)
        self.remaining = remaining
        self.spent_today = int(spent_today)

    def observe(self, remaining_header: Optional[str]) -> None:
        if remaining_header is not None and str(remaining_header).strip().isdigit():
            self.remaining = int(str(remaining_header).strip())

    def can_spend(self, credits: int) -> bool:
        if self.remaining is None:
            return False
        if self.remaining - credits < self.reserve:
            return False
        return self.spent_today + credits <= self.cap

    def spend(self, credits: int) -> None:
        if not self.can_spend(credits):
            raise RuntimeError("credit guard refused the spend")
        self.spent_today += credits
        self.remaining = (self.remaining or 0) - credits
