"""Environment-driven configuration. Every knob has a sane default so the bot
runs with just a Telegram token and at least one API key."""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)


#: Every knob the code reads: name -> (code default, effective value). Filled as
#: Config() is built, so the startup report of "which keys override defaults"
#: comes from the code itself instead of a hand-kept list that can go stale.
KNOWN_KEYS: Dict[str, Tuple[Any, Any]] = {}

#: Providers that can appear in PROVIDER_ORDER (football_data is results-only).
KNOWN_PROVIDERS = frozenset({"oddsapi", "sharpapi", "sportsgameodds"})

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


def _ranking(value: str) -> str:
    v = str(value or "").strip().lower()
    if v in ("family_z", "raw"):
        return v
    log.warning("DAILY_RANKING=%r is not family_z or raw - using family_z", value)
    return "family_z"


def _cross_tier(value: str) -> str:
    v = str(value or "").strip().lower()
    if v in ("different_outcome", "forbid"):
        return v
    log.warning("CROSS_TIER_SAME_FIXTURE=%r is not different_outcome or forbid - using different_outcome", value)
    return "different_outcome"


def _view_navigation(value: str) -> str:
    v = str(value or "").strip().lower()
    if v in ("new", "edit_pages", "edit"):
        return v
    log.warning("VIEW_NAVIGATION=%r is not new, edit_pages or edit - using edit_pages", value)
    return "edit_pages"


def _bool(name: str, default: bool) -> bool:
    value = _env(name, "true" if default else "false").strip().lower() in ("1", "true", "yes", "on")
    KNOWN_KEYS[name] = (default, value)
    return value


def _list(name: str, default: str = "") -> List[str]:
    raw = _env(name, default)
    value = [x.strip() for x in raw.split(",") if x.strip()]
    KNOWN_KEYS[name] = ([x.strip() for x in default.split(",") if x.strip()], value)
    return value


def _hhmm(value: str) -> Tuple[int, int]:
    """"HH:MM" -> (hour, minute); anything malformed sorts as (0, 0)."""
    try:
        h, m = (int(x) for x in str(value).split(":"))
        return h, m
    except (ValueError, TypeError):
        return 0, 0


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
    #: football-data.org token: free football RESULTS for settlement (never a fixture/odds source).
    football_data_key: str = field(default_factory=lambda: _env("FOOTBALL_DATA_KEY"))
    #: free, keyless results sources (settlement only); on by default
    espn_results: bool = field(default_factory=lambda: _bool("ESPN_RESULTS", True))
    euroleague_results: bool = field(default_factory=lambda: _bool("EUROLEAGUE_RESULTS", True))
    odds_api_regions: List[str] = field(default_factory=lambda: _list("ODDS_API_REGIONS", "eu"))
    odds_api_min_interval: float = field(default_factory=lambda: _float("ODDS_API_MIN_INTERVAL_SEC", 1.1))
    odds_api_max_leagues_per_sport: int = field(default_factory=lambda: _int("ODDS_API_MAX_LEAGUES_PER_SPORT", 8))
    odds_api_monthly_limit: int = field(default_factory=lambda: _int("ODDS_API_MONTHLY_LIMIT", 500))
    #: Hard cap on Odds API credits (regions x markets per odds request) spent
    #: in one generation cycle: 9 = three leagues at 3 credits each. One cycle a
    #: day is ~270/month; settlement /scores calls are budgeted on top (~450/500).
    oddsapi_max_credits_per_cycle: int = field(default_factory=lambda: _int("ODDSAPI_MAX_CREDITS_PER_CYCLE", 9))
    #: Never spend below this many of the REMAINING credits reported by the
    #: Odds API's x-requests-remaining response header (the authoritative
    #: quota signal), so a handful of picks never eats the whole month.
    oddsapi_reserve_credits: int = field(default_factory=lambda: _int("ODDSAPI_RESERVE_CREDITS", 80))
    #: Each allowed Odds API sport (football, tennis) that has >= 1 event in the
    #: window is guaranteed this many odds keys per cycle before the remaining
    #: credits are ranked by event count, so one busy sport can't starve another.
    #: Football league keys the Odds API may spend credits on (empty = every
    #: in-season football key). The default is the nine-plus leagues Nigerian
    #: punters bet on; everything else is dropped at discovery.
    odds_api_league_allowlist: List[str] = field(default_factory=lambda: _list(
        "ODDS_API_LEAGUE_ALLOWLIST",
        "soccer_epl,soccer_spain_la_liga,soccer_italy_serie_a,soccer_germany_bundesliga,soccer_france_ligue_one,soccer_uefa_champs_league,soccer_uefa_europa_league,soccer_efl_champ,soccer_netherlands_eredivisie,soccer_portugal_primeira_liga,soccer_turkey_super_league"))
    oddsapi_min_keys_per_sport: int = field(default_factory=lambda: _int("ODDSAPI_MIN_KEYS_PER_SPORT", 1))
    provider_order: List[str] = field(default_factory=lambda: _list("PROVIDER_ORDER", "oddsapi,sharpapi,sportsgameodds"))
    #: Provider tried first for the Top ticket only; falls back to
    #: ``provider_order`` when it can't supply enough qualifying picks.
    top_provider_priority: List[str] = field(default_factory=lambda: _list("TOP_PROVIDER_PRIORITY", "oddsapi"))
    sgo_sports: List[str] = field(default_factory=lambda: _list("SGO_SPORTS", ""))
    sgo_leagues: List[str] = field(default_factory=lambda: _list("SGO_LEAGUES", "NBA,NFL,MLB,MLS,NCAAF,NCAAB,UEFA_CHAMPIONS_LEAGUE"))
    sgo_event_limit: int = field(default_factory=lambda: _int("SGO_EVENT_LIMIT", 50))
    #: SGO odd objects outside these statIDs / periodIDs are rejected at parse time
    #: (UNSUPPORTED_STAT / UNSUPPORTED_PERIOD); player props are capped per event.
    sgo_allowed_stats: List[str] = field(default_factory=lambda: _list(
        "SGO_ALLOWED_STATS", "points,cornerKicks,yellowCards,weightedCards,bothTeamsScored"))
    sgo_allowed_periods: List[str] = field(default_factory=lambda: _list("SGO_ALLOWED_PERIODS", "game,reg,1h,2h"))
    sgo_monthly_object_limit: int = field(default_factory=lambda: _int("SGO_MONTHLY_OBJECT_LIMIT", 1000))
    sharpapi_leagues: List[str] = field(default_factory=lambda: _list(
        "SHARPAPI_LEAGUES",
        "england_-_premier_league,spain_-_la_liga,italy_-_serie_a,germany_-_bundesliga,france_-_ligue_1,england_-_championship,netherlands_-_eredivisie,portugal_-_primeira_liga,turkey_-_super_lig,uefa_-_champions_league,uefa_-_europa_league,uefa_-_europa_conference_league,nigeria_-_premier_league,atp,wta,nba,nfl,mlb,euroleague"))
    # SHARPAPI_SPORTS walks the kick-off ordered global sport feed(s), e.g. soccer:
    # one or two pages reach today's fixtures without per-league requests.
    sharpapi_sports: List[str] = field(default_factory=lambda: _list("SHARPAPI_SPORTS"))
    # Provider capability discovery (no artificial sport/league gates)
    # Provider metadata and actual API responses determine what is available
    # The Odds API is intentionally restricted to Football/Tennis only
    # Match-market filter: drops the futures/outrights that flood the raw feed.
    sharpapi_markets: List[str] = field(
        default_factory=lambda: _list("SHARPAPI_MARKETS", "moneyline,spread,total"))
    sharpapi_max_pages: int = field(default_factory=lambda: _int("SHARPAPI_MAX_PAGES", 3))     # hard per-feed ceiling
    #: is_main_line=true on every request (alternate lines are ~70% of the rows a match costs)
    sharpapi_main_lines_only: bool = field(default_factory=lambda: _bool("SHARPAPI_MAIN_LINES_ONLY", True))
    #: exact market names per sport; the aliases in SHARPAPI_MARKETS also drag in shots/fouls/corners/player rows
    sharpapi_markets_football: List[str] = field(default_factory=lambda: _list(
        "SHARPAPI_MARKETS_FOOTBALL",
        "moneyline,point_spread,total_goals,double_chance,draw_no_bet,both_teams_to_score,team_total_goals"))
    sharpapi_markets_baseball: List[str] = field(default_factory=lambda: _list(
        "SHARPAPI_MARKETS_BASEBALL", "moneyline,run_line,total_runs"))
    #: SharpAPI publishes no monthly quota (free tier = 12 requests/minute, ~17,280/day); this is OUR
    #: self-imposed budget, spread over the days left in the month, capped per generation
    sharpapi_monthly_request_limit: int = field(default_factory=lambda: _int("SHARPAPI_MONTHLY_REQUEST_LIMIT", 3000))
    sharpapi_max_requests_per_generation: int = field(
        default_factory=lambda: _int("SHARPAPI_MAX_REQUESTS_PER_GENERATION", 60))
    #: leagues fetched first (and never starved) when the request budget is tight
    sharpapi_league_priority: List[str] = field(default_factory=lambda: _list(
        "SHARPAPI_LEAGUE_PRIORITY",
        "england_-_premier_league,spain_-_la_liga,italy_-_serie_a,germany_-_bundesliga,france_-_ligue_1,"
        "uefa_-_champions_league"))
    sharpapi_events_per_target: int = field(
        default_factory=lambda: _int("SHARPAPI_EVENTS_PER_TARGET", 40))
    sharpapi_min_interval: float = field(default_factory=lambda: _float("SHARPAPI_MIN_INTERVAL_SEC", 5.5))

    #: Sports the bot will pick from; anything else a provider returns is
    #: dropped before merging and counted as ``sport_rejected`` in the provider report.
    allowed_sports: List[str] = field(default_factory=lambda: _list(
        "ALLOWED_SPORTS", "football,basketball,american_football,baseball,tennis"))

    #: Market ids the picker never selects (pure coin-flips or unsettleable for our punters).
    disabled_markets: List[str] = field(default_factory=lambda: _list(
        "DISABLED_MARKETS", "odd_even,prop,winning_margin,ht_ft"))

    #: When the card is delivered, in TIMEZONE - ONE shared instant for every
    #: subscriber (their own zones only change how times are displayed).
    delivery_time: str = field(default_factory=lambda: _env("DELIVERY_TIME", "09:30"))

    #: How a line is priced from the books that quote it: ``median`` (default: what a
    #: typical book offers) or ``best`` (the highest price, shown with its bookmaker).
    #: Region note: ODDS_API_REGIONS=uk adds UK-facing books such as Bet365 (same credit cost, one region).
    price_mode: str = field(default_factory=lambda: _env("PRICE_MODE", "median").strip().lower())

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
    #: when the strict Top search finds no ticket: first widen the odds band by TOP_RELAX_BAND on each side, then
    #: lower the per-leg probability floor by TOP_RELAX_PROB inside that band. Never fewer than 2 or more than 3 legs.
    top_relaxed_fallback: bool = field(default_factory=lambda: _bool("TOP_RELAXED_FALLBACK", True))
    top_relax_band: float = field(default_factory=lambda: _float("TOP_RELAX_BAND", 0.25))
    top_relax_prob: float = field(default_factory=lambda: _float("TOP_RELAX_PROB", 0.04))
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
    top_min_books: int = field(default_factory=lambda: _int("TOP_MIN_BOOKS", 2))
    #: One selection per real fixture across Top + Daily: two legs on one match are
    #: correlated, so the slate never carries both.
    max_picks_per_event: int = field(default_factory=lambda: _int("MAX_PICKS_PER_EVENT", 1))
    max_player_props: int = field(default_factory=lambda: _int("MAX_PLAYER_PROPS", 0))
    #: Daily-list diversity quotas, so one market (e.g. corners totals), one
    #: sport or one league can never take over the single daily ticket. Shares
    #: are of daily_target. They are relaxed only when the qualifying pool is
    #: genuinely too small to reach DAILY_COMBINED_MIN_ODDS, and that relaxation
    #: is recorded in the selection diagnostics.
    daily_max_market_share: float = field(default_factory=lambda: _float("DAILY_MAX_MARKET_SHARE", 0.25))
    #: Cross-market cap on one stat ("corners" can appear as totals AND 1X2
    #: result3 lines; the (market, stat) cap above sees them as two markets).
    daily_max_stat_share: float = field(default_factory=lambda: _float("DAILY_MAX_STAT_SHARE", 0.40))
    #: order the Daily builder is offered candidates in: ``family_z`` ranks each market family against its
    #: own peers (a straight win competes with wins, not with +1.5 lines); ``raw`` = ``_daily_score`` alone
    daily_ranking: str = field(default_factory=lambda: _ranking(_env("DAILY_RANKING", "family_z")))
    #: what the Daily may do on a match that is already in the Top ticket: ``different_outcome`` = allowed
    #: only with a tip that is neither the same nor nested in a Top leg; ``forbid`` = the match is off-limits
    cross_tier_same_fixture: str = field(default_factory=lambda: _cross_tier(_env("CROSS_TIER_SAME_FIXTURE", "different_outcome")))
    daily_max_sport_share: float = field(default_factory=lambda: _float("DAILY_MAX_SPORT_SHARE", 0.60))
    daily_max_per_league: int = field(default_factory=lambda: _int("DAILY_MAX_PER_LEAGUE", 3))
    daily_max_period_share: float = field(default_factory=lambda: _float("DAILY_MAX_PERIOD_SHARE", 0.15))
    model_shrink: float = field(default_factory=lambda: _float("MODEL_SHRINK_TO_MARKET", 0.80))

    # Schedule
    live_updates_enabled: bool = field(default_factory=lambda: _bool("LIVE_UPDATES_ENABLED", True))
    live_poll_minutes: int = field(default_factory=lambda: _int("LIVE_POLL_MINUTES", 5))
    timezone: str = field(default_factory=lambda: _env("TIMEZONE", "Africa/Lagos"))
    # POST_TIME is in TIMEZONE; every subscriber receives the same scheduled instant.
    post_time: str = field(default_factory=lambda: _env("POST_TIME", "09:00"))
    recap_time: str = field(default_factory=lambda: _env("RECAP_TIME", "07:45"))
    # Note: settlement now runs dynamically after all daily predictions end (latest match + sport duration + buffer)

    # Cache / pagination
    page_size: int = field(default_factory=lambda: _int("PAGE_SIZE", 5))
    #: what a button press does: ``edit_pages`` (default) = view switches send a NEW message, page turns edit in place;
    #: ``new`` = every press sends a new message; ``edit`` = every press edits the pressed message
    view_navigation: str = field(default_factory=lambda: _view_navigation(_env("VIEW_NAVIGATION", "edit_pages")))

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
        if self.live_poll_minutes < 1:
            raise ValueError("LIVE_POLL_MINUTES must be >= 1")
        if self.price_mode not in ("median", "best"):
            log.warning("Config: PRICE_MODE=%r is not median|best - using median", self.price_mode)
            self.price_mode = "median"
        unknown = [p for p in self.provider_order if p not in KNOWN_PROVIDERS]
        if unknown:
            log.warning("Config: PROVIDER_ORDER names unknown provider(s) %s (known: %s) - they are ignored",
                        ", ".join(unknown), ", ".join(sorted(KNOWN_PROVIDERS)))
        if _hhmm(self.delivery_time) <= _hhmm(self.post_time):
            log.warning("Config: DELIVERY_TIME %s is not after POST_TIME %s - the card would be due before it is generated",
                        self.delivery_time, self.post_time)
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
            ("TOP_MIN_BOOKS >= 1", self.top_min_books >= 1),
            ("MODEL_SHRINK_TO_MARKET in [0,1]", 0.0 <= self.model_shrink <= 1.0),
            ("TOP_LEGS_MIN >= 1", self.top_legs_min >= 1),
            ("TOP_LEGS_MAX >= TOP_LEGS_MIN", self.top_legs_max >= self.top_legs_min),
            ("MAX_PICKS_PER_EVENT >= 1", self.max_picks_per_event >= 1),
            ("DAILY_MAX_MARKET_SHARE in (0,1]", 0.0 < self.daily_max_market_share <= 1.0),
            ("DAILY_MAX_STAT_SHARE in (0,1]", 0.0 < self.daily_max_stat_share <= 1.0),
            ("DAILY_MAX_SPORT_SHARE in (0,1]", 0.0 < self.daily_max_sport_share <= 1.0),
            ("DAILY_MAX_PER_LEAGUE >= 1", self.daily_max_per_league >= 1),
            ("DAILY_MAX_PERIOD_SHARE in (0,1]", 0.0 < self.daily_max_period_share <= 1.0),
        ]
        for name, ok in checks:
            if not ok:
                log.warning("Config: %s does not hold - selection may behave unexpectedly", name)


def require_provider_keys(cfg: "Config") -> None:
    """SharpAPI is the one required provider: without it there is no fixture
    feed for the leagues we cover, so refuse to start (exit code 2). The Odds
    API and SportsGameOdds are optional and only warn."""
    if not cfg.sharpapi_key:
        log.error("SHARPAPI_KEY is required — get one at https://sharpapi.io")
        raise SystemExit(2)
    if not cfg.odds_api_key:
        log.warning("ODDS_API_KEY is not set - the Top ticket and European football lose The Odds API")
    if not cfg.sgo_key:
        log.warning("SPORTSGAMEODDS_KEY is not set - NBA/NFL/MLB/MLS/NCAA/UCL results and odds lose SportsGameOdds")


def default_env_path() -> Path:
    """The ONE .env the bot reads: next to main.py (the directory that contains
    the predbot package), never the current working directory. A CWD-relative
    lookup let a stale .env in a parent folder silently win depending on where
    the bot happened to be started from."""
    return Path(__file__).resolve().parent.parent / ".env"


def _env_value(raw: str) -> str:
    """The value part of a ``KEY=value`` line.

    ``#`` starts a comment only when it is preceded by whitespace and the value is
    not quoted, so a token or URL containing ``#`` survives; a quoted value ends at
    its closing quote (whatever follows, e.g. `` # note``, is ignored)."""
    v = raw.strip()
    if v[:1] in ("'", '"'):
        end = v.find(v[0], 1)
        if end != -1:
            return v[1:end]
        return v[1:]
    m = re.search(r"\s#", raw)
    if m:
        v = raw[:m.start()].strip()
    return v


def _parse_env_file(path: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = _env_value(v)
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
