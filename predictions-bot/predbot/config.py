"""Environment-driven configuration. Every knob has a sane default so the bot
runs with just a Telegram token and at least one API key."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import List

log = logging.getLogger(__name__)


def _env(name: str, default: str = "") -> str:
    v = os.environ.get(name)
    return default if v is None or v == "" else v


def _int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    return _env(name, "true" if default else "false").strip().lower() in ("1", "true", "yes", "on")


def _list(name: str, default: str = "") -> List[str]:
    raw = _env(name, default)
    return [x.strip() for x in raw.split(",") if x.strip()]


@dataclass
class Config:
    cache_retry_seconds: int = field(default_factory=lambda: max(60, _int("CACHE_RETRY_SECONDS", 900)))
    cache_retention_days: int = field(default_factory=lambda: max(1, _int("CACHE_RETENTION_DAYS", 7)))

    # Telegram
    telegram_token: str = field(default_factory=lambda: _env("TELEGRAM_BOT_TOKEN"))
    telegram_chat_id: str = field(default_factory=lambda: _env("TELEGRAM_CHAT_ID"))
    telegram_admin_ids: List[str] = field(default_factory=lambda: _list("TELEGRAM_ADMIN_IDS"))
    enable_commands: bool = field(default_factory=lambda: _bool("ENABLE_COMMANDS", True))

    # Providers
    api_football_key: str = field(default_factory=lambda: _env("API_FOOTBALL_KEY"))
    api_football_host: str = field(default_factory=lambda: _env("API_FOOTBALL_HOST", "v3.football.api-sports.io"))
    sgo_key: str = field(default_factory=lambda: _env("SPORTSGAMEODDS_KEY"))
    sharpapi_key: str = field(default_factory=lambda: _env("SHARPAPI_KEY"))
    odds_api_key: str = field(default_factory=lambda: _env("ODDS_API_KEY"))
    odds_api_regions: List[str] = field(default_factory=lambda: _list("ODDS_API_REGIONS", "us,eu"))
    odds_api_min_interval: float = field(default_factory=lambda: _float("ODDS_API_MIN_INTERVAL_SEC", 1.1))
    odds_api_max_leagues_per_sport: int = field(default_factory=lambda: _int("ODDS_API_MAX_LEAGUES_PER_SPORT", 8))
    odds_api_monthly_limit: int = field(default_factory=lambda: _int("ODDS_API_MONTHLY_LIMIT", 500))
    provider_order: List[str] = field(default_factory=lambda: _list("PROVIDER_ORDER", "api_football,sportsgameodds,sharpapi"))
    #: Provider tried first for the Top ticket only; falls back to
    #: ``provider_order`` when it can't supply enough qualifying picks.
    top_provider_priority: List[str] = field(default_factory=lambda: _list("TOP_PROVIDER_PRIORITY", "oddsapi"))
    chain_min_events: int = field(default_factory=lambda: _int("CHAIN_MIN_EVENTS", 40))
    sgo_sports: List[str] = field(default_factory=lambda: _list("SGO_SPORTS", ""))
    sgo_leagues: List[str] = field(default_factory=lambda: _list("SGO_LEAGUES"))
    sgo_event_limit: int = field(default_factory=lambda: _int("SGO_EVENT_LIMIT", 200))
    sgo_monthly_object_limit: int = field(default_factory=lambda: _int("SGO_MONTHLY_OBJECT_LIMIT", 1000))
    sharpapi_leagues: List[str] = field(default_factory=lambda: _list("SHARPAPI_LEAGUES"))
    # SHARPAPI_SPORTS walks the kick-off ordered global sport feed(s), e.g. soccer:
    # one or two pages reach today's fixtures without per-league requests.
    sharpapi_sports: List[str] = field(default_factory=lambda: _list("SHARPAPI_SPORTS", "auto"))
    # Provider capability discovery (no artificial sport/league gates)
    # Provider metadata and actual API responses determine what is available
    # The Odds API is intentionally restricted to Football/Tennis only
    # Match-market filter: drops the futures/outrights that flood the raw feed.
    sharpapi_markets: List[str] = field(
        default_factory=lambda: _list("SHARPAPI_MARKETS", "moneyline,spread,total"))
    sharpapi_max_pages: int = field(default_factory=lambda: _int("SHARPAPI_MAX_PAGES", 12))
    sharpapi_events_per_target: int = field(
        default_factory=lambda: _int("SHARPAPI_EVENTS_PER_TARGET", 40))
    sharpapi_min_interval: float = field(default_factory=lambda: _float("SHARPAPI_MIN_INTERVAL_SEC", 5.5))
    apif_league_ids: List[str] = field(default_factory=lambda: _list("APIF_LEAGUE_IDS"))
    apif_daily_limit: int = field(default_factory=lambda: _int("APIF_DAILY_LIMIT", 100))
    apif_predictions_per_run: int = field(default_factory=lambda: _int("APIF_PREDICTIONS_PER_RUN", 25))
    apif_odds_pages: int = field(default_factory=lambda: _int("APIF_ODDS_PAGES", 3))

    # Picks - the daily post is ONE ticket, so combined odds (not pick counts)
    # size the lists; daily_target is only a safety cap on the number of picks.
    daily_target: int = field(default_factory=lambda: _int("DAILY_PICKS_TARGET", 20))
    top_odds_target: float = field(default_factory=lambda: _float("TOP_COMBINED_ODDS", 2.00))
    daily_odds_min: float = field(default_factory=lambda: _float("DAILY_COMBINED_MIN_ODDS", 10.0))
    daily_odds_max: float = field(default_factory=lambda: _float("DAILY_COMBINED_MAX_ODDS", 20.0))
    pick_window_hours: int = field(default_factory=lambda: _int("PICK_WINDOW_HOURS", 24))
    min_lead_minutes: int = field(default_factory=lambda: _int("MIN_LEAD_MINUTES", 30))
    daily_min_odds: float = field(default_factory=lambda: _float("DAILY_MIN_ODDS", 1.30))
    daily_max_odds: float = field(default_factory=lambda: _float("DAILY_MAX_ODDS", 5.00))
    #: Top Picks is an accumulator: the search tries every 2..3-leg combination
    #: of different fixtures and keeps the one closest to top_odds_target.
    #: top_odds_max is a HARD reject on the combined ticket - distinct from
    #: top_max_odds above, which bounds each individual leg's own price.
    top_legs_min: int = field(default_factory=lambda: _int("TOP_LEGS_MIN", 2))
    top_legs_max: int = field(default_factory=lambda: _int("TOP_LEGS_MAX", 3))
    top_odds_max: float = field(default_factory=lambda: _float("TOP_ODDS_MAX", 2.50))
    daily_min_ev: float = field(default_factory=lambda: _float("DAILY_MIN_EV", 0.02))
    daily_min_prob: float = field(default_factory=lambda: _float("DAILY_MIN_PROB", 0.30))
    top_min_odds: float = field(default_factory=lambda: _float("TOP_MIN_ODDS", 1.35))
    top_max_odds: float = field(default_factory=lambda: _float("TOP_MAX_ODDS", 2.60))
    top_min_prob: float = field(default_factory=lambda: _float("TOP_MIN_PROB", 0.62))
    top_min_ev: float = field(default_factory=lambda: _float("TOP_MIN_EV", 0.02))
    #: How closely the model's own signals must agree with each other before a
    #: selection may enter the Top ticket (0.50 = the historical bar). Lowering
    #: it admits model-vs-market disagreement picks; the default is unchanged.
    top_min_agreement: float = field(default_factory=lambda: _float("TOP_MIN_AGREEMENT", 0.50))
    max_picks_per_event: int = field(default_factory=lambda: _int("MAX_PICKS_PER_EVENT", 2))
    max_player_props: int = field(default_factory=lambda: _int("MAX_PLAYER_PROPS", 2))
    #: Daily-list diversity quotas, so one market (e.g. corners totals), one
    #: sport or one league can never take over the single daily ticket. Shares
    #: are of daily_target. They are relaxed only when the qualifying pool is
    #: genuinely too small to reach DAILY_COMBINED_MIN_ODDS, and that relaxation
    #: is recorded in the selection diagnostics.
    daily_max_market_share: float = field(default_factory=lambda: _float("DAILY_MAX_MARKET_SHARE", 0.25))
    #: Cross-market cap on one stat ("corners" can appear as totals AND 1X2
    #: result3 lines; the (market, stat) cap above sees them as two markets).
    daily_max_stat_share: float = field(default_factory=lambda: _float("DAILY_MAX_STAT_SHARE", 0.40))
    daily_max_sport_share: float = field(default_factory=lambda: _float("DAILY_MAX_SPORT_SHARE", 0.50))
    daily_max_per_league: int = field(default_factory=lambda: _int("DAILY_MAX_PER_LEAGUE", 3))
    model_shrink: float = field(default_factory=lambda: _float("MODEL_SHRINK_TO_MARKET", 0.80))

    # Schedule
    timezone: str = field(default_factory=lambda: _env("TIMEZONE", "Africa/Lagos"))
    # POST_TIME is in TIMEZONE; every subscriber receives the same scheduled instant.
    post_time: str = field(default_factory=lambda: _env("POST_TIME", "09:00"))
    recap_time: str = field(default_factory=lambda: _env("RECAP_TIME", "07:45"))
    # Note: settlement now runs dynamically after all daily predictions end (latest match + sport duration + buffer)

    # Cache / pagination
    page_size: int = field(default_factory=lambda: _int("PAGE_SIZE", 5))

    # Storage / logging
    db_path: str = field(default_factory=lambda: _env("DB_PATH", "./data/predbot.db"))
    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "INFO"))

    def __post_init__(self) -> None:
        """A malformed env var already falls back to its default in _float/_int;
        this catches the other failure mode - a *valid* but nonsensical value
        (min > max, an odds target <= 1.0, a probability outside [0,1]) that
        would otherwise only surface downstream as an empty/broken selection
        pipeline with nothing pointing back at the config."""
        checks = [
            ("DAILY_COMBINED_MIN_ODDS/MAX", self.daily_odds_min <= self.daily_odds_max),
            ("DAILY_MIN_ODDS/MAX_ODDS", self.daily_min_odds <= self.daily_max_odds),
            ("TOP_MIN_ODDS/MAX_ODDS", self.top_min_odds <= self.top_max_odds),
            ("TOP_COMBINED_ODDS > 1.0", self.top_odds_target > 1.0),
            ("DAILY_MIN_ODDS > 1.0", self.daily_min_odds > 1.0),
            ("TOP_MIN_PROB in [0,1]", 0.0 <= self.top_min_prob <= 1.0),
            ("DAILY_MIN_PROB in [0,1]", 0.0 <= self.daily_min_prob <= 1.0),
            ("TOP_MIN_AGREEMENT in [0,1]", 0.0 <= self.top_min_agreement <= 1.0),
            ("MODEL_SHRINK_TO_MARKET in [0,1]", 0.0 <= self.model_shrink <= 1.0),
            ("TOP_LEGS_MIN >= 1", self.top_legs_min >= 1),
            ("TOP_LEGS_MAX >= TOP_LEGS_MIN", self.top_legs_max >= self.top_legs_min),
            ("TOP_ODDS_MAX >= TOP_COMBINED_ODDS", self.top_odds_max >= self.top_odds_target),
            ("MAX_PICKS_PER_EVENT >= 1", self.max_picks_per_event >= 1),
            ("DAILY_MAX_MARKET_SHARE in (0,1]", 0.0 < self.daily_max_market_share <= 1.0),
            ("DAILY_MAX_STAT_SHARE in (0,1]", 0.0 < self.daily_max_stat_share <= 1.0),
            ("DAILY_MAX_SPORT_SHARE in (0,1]", 0.0 < self.daily_max_sport_share <= 1.0),
            ("DAILY_MAX_PER_LEAGUE >= 1", self.daily_max_per_league >= 1),
        ]
        for name, ok in checks:
            if not ok:
                log.warning("Config: %s does not hold - selection may behave unexpectedly", name)

    def enabled_providers(self) -> List[str]:
        keys = {
            "api_football": bool(self.api_football_key),
            "sportsgameodds": bool(self.sgo_key),
            "sharpapi": bool(self.sharpapi_key),
        }
        return [p for p in self.provider_order if keys.get(p)]


def load_config() -> Config:
    # Load a local .env if present (Railway injects real env vars; this is for local runs).
    env_file = os.path.join(os.getcwd(), ".env")
    if os.path.exists(env_file):
        with open(env_file, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                v = v.split("#", 1)[0].strip().strip('"').strip("'")
                os.environ.setdefault(k.strip(), v)
    return Config()
