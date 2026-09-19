"""Environment-driven configuration. Every knob has a sane default so the bot
runs with just a Telegram token and at least one API key."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)


#: Every knob the code reads: name -> (code default, effective value). Filled as
#: Config() is built, so the startup report of "which keys override defaults"
#: comes from the code itself instead of a hand-kept list that can go stale.
KNOWN_KEYS: Dict[str, Tuple[Any, Any]] = {}

_SECRET_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "URL", "DSN")


def is_secret(name: str) -> bool:
    return any(m in name.upper() for m in _SECRET_MARKERS)


def mask(name: str, value: Any) -> str:
    """Never show a secret: reports print ``***`` for anything that looks like one."""
    return "***" if is_secret(name) else str(value)


def _env(name: str, default: str = "") -> str:
    v = os.environ.get(name)
    value = default if v is None or v == "" else v
    KNOWN_KEYS[name] = (default, value)
    return value


def _int(name: str, default: int) -> int:
    try:
        value = int(_env(name, str(default)))
    except ValueError:
        value = default
    KNOWN_KEYS[name] = (default, value)
    return value


def _float(name: str, default: float) -> float:
    try:
        value = float(_env(name, str(default)))
    except ValueError:
        value = default
    KNOWN_KEYS[name] = (default, value)
    return value


def _bool(name: str, default: bool) -> bool:
    value = _env(name, "true" if default else "false").strip().lower() in ("1", "true", "yes", "on")
    KNOWN_KEYS[name] = (default, value)
    return value


def _list(name: str, default: str = "") -> List[str]:
    raw = _env(name, default)
    value = [x.strip() for x in raw.split(",") if x.strip()]
    KNOWN_KEYS[name] = ([x.strip() for x in default.split(",") if x.strip()], value)
    return value


def _default_log_file() -> str:
    raw = _env("LOG_FILE", "")
    if raw.lower() in ("none", "off", "false", "0"):
        return ""
    if raw:
        return raw
    return os.path.join(os.path.dirname(_env("DB_PATH", "./data/predbot.db")) or ".", "predbot.log")


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
    sgo_key: str = field(default_factory=lambda: _env("SPORTSGAMEODDS_KEY"))
    sharpapi_key: str = field(default_factory=lambda: _env("SHARPAPI_KEY"))
    odds_api_key: str = field(default_factory=lambda: _env("ODDS_API_KEY"))
    odds_api_regions: List[str] = field(default_factory=lambda: _list("ODDS_API_REGIONS", "eu"))
    odds_api_min_interval: float = field(default_factory=lambda: _float("ODDS_API_MIN_INTERVAL_SEC", 1.1))
    odds_api_max_leagues_per_sport: int = field(default_factory=lambda: _int("ODDS_API_MAX_LEAGUES_PER_SPORT", 8))
    odds_api_monthly_limit: int = field(default_factory=lambda: _int("ODDS_API_MONTHLY_LIMIT", 500))
    #: Hard cap on Odds API credits (regions x markets per odds request) spent
    #: in one generation cycle. At ~15/day that is ~450/month on a 500 quota.
    oddsapi_max_credits_per_cycle: int = field(default_factory=lambda: _int("ODDSAPI_MAX_CREDITS_PER_CYCLE", 15))
    #: Never spend below this many of the REMAINING credits reported by the
    #: Odds API's x-requests-remaining response header (the authoritative
    #: quota signal), so a handful of picks never eats the whole month.
    oddsapi_reserve_credits: int = field(default_factory=lambda: _int("ODDSAPI_RESERVE_CREDITS", 50))
    #: Each allowed Odds API sport (football, tennis) that has >= 1 event in the
    #: window is guaranteed this many odds keys per cycle before the remaining
    #: credits are ranked by event count, so one busy sport can't starve another.
    oddsapi_min_keys_per_sport: int = field(default_factory=lambda: _int("ODDSAPI_MIN_KEYS_PER_SPORT", 1))
    provider_order: List[str] = field(default_factory=lambda: _list("PROVIDER_ORDER", "sportsgameodds,sharpapi"))
    #: Provider tried first for the Top ticket only; falls back to
    #: ``provider_order`` when it can't supply enough qualifying picks.
    top_provider_priority: List[str] = field(default_factory=lambda: _list("TOP_PROVIDER_PRIORITY", "oddsapi"))
    chain_min_events: int = field(default_factory=lambda: _int("CHAIN_MIN_EVENTS", 40))
    sgo_sports: List[str] = field(default_factory=lambda: _list("SGO_SPORTS", ""))
    sgo_leagues: List[str] = field(default_factory=lambda: _list("SGO_LEAGUES", "UEFA_CHAMPIONS_LEAGUE,MLS,NBA,NCAAB,NHL"))
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

    # Picks - the daily post is ONE ticket, so combined odds (not pick counts)
    # size the lists; daily_target is only a safety cap on the number of picks.
    daily_target: int = field(default_factory=lambda: _int("DAILY_PICKS_TARGET", 20))
    top_odds_target: float = field(default_factory=lambda: _float("TOP_COMBINED_ODDS", 2.00))
    daily_odds_min: float = field(default_factory=lambda: _float("DAILY_COMBINED_MIN_ODDS", 10.0))
    daily_odds_max: float = field(default_factory=lambda: _float("DAILY_COMBINED_MAX_ODDS", 20.0))
    pick_window_hours: int = field(default_factory=lambda: _int("PICK_WINDOW_HOURS", 24))
    #: The day window ends at local midnight + this many hours, so late-evening
    #: kickoffs (e.g. 23:30 Lagos) stay in scope; dedupe against the previous
    #: day's stored picks stops a fixture from being posted twice.
    day_window_end_offset_hours: int = field(default_factory=lambda: _int("DAY_WINDOW_END_OFFSET_HOURS", 3))
    min_lead_minutes: int = field(default_factory=lambda: _int("MIN_LEAD_MINUTES", 30))
    daily_min_odds: float = field(default_factory=lambda: _float("DAILY_MIN_ODDS", 1.20))
    daily_max_odds: float = field(default_factory=lambda: _float("DAILY_MAX_ODDS", 5.00))
    #: Top Picks is an accumulator: the search tries every 2..3-leg combination
    #: of different fixtures and keeps the one with the highest product of
    #: p_model inside top_odds_target..top_odds_max. top_odds_max is a HARD
    #: reject on the combined ticket. Per-leg odds do not matter for this
    #: confidence product - top_leg_odds_min is only a data-sanity floor.
    top_legs_min: int = field(default_factory=lambda: _int("TOP_LEGS_MIN", 2))
    top_legs_max: int = field(default_factory=lambda: _int("TOP_LEGS_MAX", 3))
    top_odds_max: float = field(default_factory=lambda: _float("TOP_ODDS_MAX", 2.50))
    #: EV is no longer an edge requirement - this is a confidence product. A
    #: candidate with an independent signal (elo) is rejected only when
    #: the signal says the market is MATERIALLY overpricing it (ev below the
    #: NEG floor, default -0.05). Market-only candidates skip the gate
    #: entirely: their p_model is de-vigged market probability, so their EV
    #: is negative by construction and carries no information.
    daily_max_neg_ev: float = field(default_factory=lambda: _float("DAILY_MAX_NEG_EV", -0.05))
    daily_min_prob: float = field(default_factory=lambda: _float("DAILY_MIN_PROB", 0.30))
    top_leg_odds_min: float = field(default_factory=lambda: _float("TOP_LEG_ODDS_MIN", 1.05))
    top_min_prob: float = field(default_factory=lambda: _float("TOP_MIN_PROB", 0.62))
    top_max_neg_ev: float = field(default_factory=lambda: _float("TOP_MAX_NEG_EV", -0.05))
    #: How closely the model's own signals must agree with each other before a
    #: selection may enter the Top ticket (0.50 = the historical bar). Lowering
    #: it admits model-vs-market disagreement picks; the default is unchanged.
    top_min_agreement: float = field(default_factory=lambda: _float("TOP_MIN_AGREEMENT", 0.50))
    #: One selection per real fixture across Top + Daily: two legs on one match are
    #: correlated, so the slate never carries both.
    max_picks_per_event: int = field(default_factory=lambda: _int("MAX_PICKS_PER_EVENT", 1))
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
    daily_max_sport_share: float = field(default_factory=lambda: _float("DAILY_MAX_SPORT_SHARE", 0.60))
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
    #: Rotating log file (2 MB x 5). Default: predbot.log next to the database.
    #: LOG_FILE=none disables it.
    log_file: str = field(default_factory=lambda: _default_log_file())

    def __post_init__(self) -> None:
        """A malformed env var already falls back to its default in _float/_int;
        this catches the other failure mode - a *valid* but nonsensical value
        (min > max, an odds target <= 1.0, a probability outside [0,1]) that
        would otherwise only surface downstream as an empty/broken selection
        pipeline with nothing pointing back at the config."""
        checks = [
            ("DAILY_COMBINED_MIN_ODDS/MAX", self.daily_odds_min <= self.daily_odds_max),
            ("DAILY_MIN_ODDS/MAX_ODDS", self.daily_min_odds <= self.daily_max_odds),
            ("TOP_COMBINED_ODDS > 1.0", self.top_odds_target > 1.0),
            ("TOP_ODDS_MAX >= TOP_COMBINED_ODDS", self.top_odds_max >= self.top_odds_target),
            ("DAILY_MIN_ODDS > 1.0", self.daily_min_odds > 1.0),
            ("TOP_LEG_ODDS_MIN >= 1.0 (sanity floor)", self.top_leg_odds_min >= 1.0),
            ("TOP_MIN_PROB in [0,1]", 0.0 <= self.top_min_prob <= 1.0),
            ("DAILY_MIN_PROB in [0,1]", 0.0 <= self.daily_min_prob <= 1.0),
            ("DAILY_MAX_NEG_EV in [-1,0.5]", -1.0 <= self.daily_max_neg_ev <= 0.5),
            ("TOP_MAX_NEG_EV in [-1,0.5]", -1.0 <= self.top_max_neg_ev <= 0.5),
            ("TOP_MIN_AGREEMENT in [0,1]", 0.0 <= self.top_min_agreement <= 1.0),
            ("MODEL_SHRINK_TO_MARKET in [0,1]", 0.0 <= self.model_shrink <= 1.0),
            ("TOP_LEGS_MIN >= 1", self.top_legs_min >= 1),
            ("TOP_LEGS_MAX >= TOP_LEGS_MIN", self.top_legs_max >= self.top_legs_min),
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
            "sportsgameodds": bool(self.sgo_key),
            "sharpapi": bool(self.sharpapi_key),
        }
        return [p for p in self.provider_order if keys.get(p)]


def default_env_path() -> Path:
    """The ONE .env the bot reads: next to main.py (the directory that contains
    the predbot package), never the current working directory. A CWD-relative
    lookup let a stale .env in a parent folder silently win depending on where
    the bot happened to be started from."""
    return Path(__file__).resolve().parent.parent / ".env"


def _parse_env_file(path: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.split("#", 1)[0].strip().strip('"').strip("'")
    return out


def load_config(env_file: Optional[str] = None) -> Config:
    """Build the Config from real environment variables, falling back to the
    project's .env for anything not already set (real env vars win - that is what
    a hosting dashboard sets), then to the code defaults.

    The returned config carries ``env_file`` (which file was read, "" if none)
    and ``env_report`` (see :func:`describe_env`)."""
    path = Path(env_file) if env_file else default_env_path()
    from_file: Dict[str, str] = {}
    applied: List[str] = []
    if path.is_file():
        from_file = _parse_env_file(path)
        for k, v in from_file.items():
            if k not in os.environ:
                os.environ[k] = v
                applied.append(k)
    cfg = Config()
    cfg.env_file = str(path) if path.is_file() else ""
    cfg.env_report = describe_env(str(path), path.is_file(), from_file, applied)
    return cfg


def describe_env(path: str, found: bool, from_file: Dict[str, str], applied: List[str]) -> Dict[str, Any]:
    """What the environment did to this run: which file was loaded, which keys
    override a code default (secrets masked), and which .env keys this version
    never reads (silently ignored otherwise)."""
    overrides = []
    for name, (default, value) in sorted(KNOWN_KEYS.items()):
        if os.environ.get(name) in (None, "") or value == default:
            continue
        source = ".env" if name in applied else "process env"
        overrides.append({"key": name, "value": mask(name, value), "source": source})
    unread = sorted(k for k in from_file if k not in KNOWN_KEYS)
    return {"path": path, "found": found, "keys_in_file": len(from_file),
            "overrides": overrides, "unread_keys": unread}


def startup_lines(cfg: Config) -> List[str]:
    """Human-readable startup log lines for cfg.env_report (secrets already masked)."""
    rep = getattr(cfg, "env_report", None)
    if not rep:
        return []
    lines = []
    if rep["found"]:
        lines.append(f"config: loaded .env from {rep['path']} ({rep['keys_in_file']} keys)")
    else:
        lines.append(f"config: no .env at {rep['path']} - using process environment and code defaults")
    if rep["overrides"]:
        lines.append(f"config: {len(rep['overrides'])} key(s) override the code default: " +
                     ", ".join(f"{o['key']}={o['value']} [{o['source']}]" for o in rep["overrides"]))
    else:
        lines.append("config: no key overrides a code default")
    if rep["unread_keys"]:
        lines.append("config: WARNING .env keys this version never reads (ignored): " + ", ".join(rep["unread_keys"]))
    return lines
