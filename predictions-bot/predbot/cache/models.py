"""Unified cache schema: the canonical JSON model written once per UTC day.

Data flow
---------
provider parsers -> Event / MarketLine (predbot.providers.base) -> pricing model
-> picker (PickSet) -> **this schema** -> daily cache -> Telegram pages.

Everything here JSON-round-trips cleanly: ``to_dict()`` -> ``json.dumps`` ->
``from_dict()``.  The cache payload is an immutable snapshot for the rest of the
UTC day; pagination slices it in memory.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

SCHEMA_VERSION = 1

VIEW_TOP = "top"
VIEW_DAILY = "daily"
VIEW_STATS = "stats"


@dataclass
class PickRecord:
    """One selection (top or daily tier), provider-agnostic and cache-ready."""

    pick_id: Optional[int] = None          # SQLite picks.id once persisted
    tier: str = "daily"                    # top | daily
    rank: int = 0
    sport: str = "other"
    league: str = ""
    home: str = ""
    away: str = ""
    kickoff_utc: str = ""                  # ISO-8601 Z
    label: str = ""                        # human-readable selection label
    market: str = ""
    selection: str = ""
    line: Optional[float] = None
    price: float = 0.0                     # decimal odds
    prob: float = 0.0                      # model probability (0..1)
    ev: float = 0.0                        # expected value per 1 unit staked
    p_market: Optional[float] = None       # de-vigged market probability
    bookmaker: str = ""
    n_books: int = 1
    sources: List[str] = field(default_factory=list)   # contributing signals/providers
    status: str = "pending"                # pending | won | lost | push | ...
    pick_date: str = ""                    # YYYY-MM-DD the pick was published for

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PickRecord":
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class StatsBlock:
    """Win/loss/ROI aggregate over a window (flat 1-unit stakes)."""

    won: int = 0
    lost: int = 0
    push: int = 0
    n: int = 0
    roi: float = 0.0
    profit: float = 0.0
    best_win: Optional[Dict[str, Any]] = None

    @classmethod
    def from_raw(cls, data: Optional[Dict[str, Any]]) -> "StatsBlock":
        data = data or {}
        return cls(
            won=int(data.get("won", 0)),
            lost=int(data.get("lost", 0)),
            push=int(data.get("push", 0)),
            n=int(data.get("n", 0)),
            roi=float(data.get("roi", 0.0)),
            profit=float(data.get("profit", 0.0)),
            best_win=data.get("best_win"),
        )


@dataclass
class StatsSummary:
    """System performance snapshot: win rates, ROI, streak, calibration."""

    last7: StatsBlock = field(default_factory=StatsBlock)
    last30: StatsBlock = field(default_factory=StatsBlock)
    all: StatsBlock = field(default_factory=StatsBlock)
    top_all: StatsBlock = field(default_factory=StatsBlock)
    yesterday: StatsBlock = field(default_factory=StatsBlock)
    yesterday_top: StatsBlock = field(default_factory=StatsBlock)
    streak: str = ""
    brier: Optional[float] = None

    @classmethod
    def from_raw(cls, raw: Optional[Dict[str, Any]]) -> "StatsSummary":
        """Accepts predbot.jobs.stats.compute() output as well as the asdict()
        form of this class (both share the same shape)."""
        raw = raw or {}
        return cls(
            last7=StatsBlock.from_raw(raw.get("last7")),
            last30=StatsBlock.from_raw(raw.get("last30")),
            all=StatsBlock.from_raw(raw.get("all")),
            top_all=StatsBlock.from_raw(raw.get("top_all")),
            yesterday=StatsBlock.from_raw(raw.get("yesterday")),
            yesterday_top=StatsBlock.from_raw(raw.get("yesterday_top")),
            streak=str(raw.get("streak") or ""),
            brier=(float(raw["brier"]) if raw.get("brier") is not None else None),
        )

    def to_raw(self) -> Dict[str, Any]:
        """Shape expected by predbot.bot.format.stats_message / recap_message."""
        return {
            "last7": asdict(self.last7),
            "last30": asdict(self.last30),
            "all": asdict(self.all),
            "top_all": asdict(self.top_all),
            "yesterday": asdict(self.yesterday),
            "yesterday_top": asdict(self.yesterday_top),
            "streak": self.streak,
            "brier": self.brier,
        }


@dataclass
class DailySnapshot:
    """The whole day's public data in one cache entry (write-once-per-day)."""

    date: str                              # UTC date "YYYY-MM-DD" this belongs to
    generated_at: str = ""                 # ISO-8601 Z build timestamp
    origin: str = "cache"                  # cache | db | api  (how it was produced)
    top_picks: List[PickRecord] = field(default_factory=list)
    daily_picks: List[PickRecord] = field(default_factory=list)
    stats: Optional[StatsSummary] = None
    schema_version: int = SCHEMA_VERSION

    def view_items(self, view: str) -> List[PickRecord]:
        if view == VIEW_TOP:
            return self.top_picks
        if view == VIEW_DAILY:
            # The picker builds the daily accumulator by extending the top ticket.
            return self.top_picks + self.daily_picks
        return []

    def total_picks(self) -> int:
        return len(self.top_picks) + len(self.daily_picks)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "date": self.date,
            "generated_at": self.generated_at,
            "origin": self.origin,
            "top_picks": [p.to_dict() for p in self.top_picks],
            "daily_picks": [p.to_dict() for p in self.daily_picks],
            "stats": (asdict(self.stats) if self.stats else None),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DailySnapshot":
        version = int(data.get("schema_version", 0))
        if version != SCHEMA_VERSION:
            raise ValueError(f"unsupported snapshot schema version: {version}")
        stats_raw = data.get("stats")
        return cls(
            date=str(data["date"]),
            generated_at=str(data.get("generated_at", "")),
            origin=str(data.get("origin", "cache")),
            top_picks=[PickRecord.from_dict(p) for p in data.get("top_picks", [])],
            daily_picks=[PickRecord.from_dict(p) for p in data.get("daily_picks", [])],
            stats=(StatsSummary.from_raw(stats_raw) if stats_raw else None),
        )

