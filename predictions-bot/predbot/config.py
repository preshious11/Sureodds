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

_SECRET_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "URL", "DSN", "IDENTITY", "CREDENTIAL", "AUTH")
#: account identifiers are not credentials, but they say who runs the bot and where it posts: never logged either
#: (TELEGRAM_CHAT_ID, TELEGRAM_ADMIN_IDS, ...). Matched as name suffixes so PROVIDER_ORDER-style names stay visible.
_IDENTIFIER_SUFFIXES = ("_ID", "_IDS", "_CHAT", "_EMAIL", "_PHONE")


def is_secret(name: str) -> bool:
    upper = name.upper()
    return any(m in upper for m in _SECRET_MARKERS) or upper.endswith(_IDENTIFIER_SUFFIXES)


def mask(name: str, value: Any) -> str:
    """Never show a secret: reports print ``***`` for anything that looks like one."""
    return "***" if is_secret(name) else str(value)


#: Values this rework moved away from (only ones that were old DEFAULTS - a deliberately chosen setting such as
#: TOP_MIN_BOOKS=2 is never touched). A dashboard that still pins one of them is almost certainly holding a stale COPY
#: of the old default (nobody chose "20 combined odds" or "a 30% Daily leg" on purpose after this change), so it is
#: ignored with a warning and the new default applies. Set HONOR_PINNED_VALUES=1 to make an old value stick anyway.
_STALE_PINS: Dict[str, Tuple[str, ...]] = {
    "DAILY_COMBINED_MAX_ODDS": ("20", "20.0", "20.00"),
    "DAILY_PICKS_TARGET": ("20",),
    "DAILY_MIN_PROB": ("0.30", "0.3"),
    "TOP_RELAX_PROB": ("0.04",),
}
_STALE_WARNED: set = set()


def _env(name: str, default: str = "") -> str:
    v = os.environ.get(name)
    value = default if v is None or v == "" else v
    if (name in _STALE_PINS and value.strip() in _STALE_PINS[name] and value != default
            and os.environ.get("HONOR_PINNED_VALUES", "").strip().lower() not in ("1", "true", "yes", "on")):
        if name not in _STALE_WARNED:
            _STALE_WARNED.add(name)
            log.warning("config: %s=%s is the pre-rework default; ignoring it and using %s "
                        "(set HONOR_PINNED_VALUES=1 to keep an old value)", name, value, default)
        value = default
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


#: verified against the providers' live catalogues (see markets/competitions.py)
INTERNATIONAL_SHARP_LEAGUES = ("uefa_-_nations_league", "concacaf_-_nations_league", "world_-_international_friendlies")
INTERNATIONAL_ODDS_KEYS = ("soccer_uefa_nations_league",)


def _with_international(listed: List[str], required: Tuple[str, ...]) -> List[str]:
    """``listed`` (read from the environment / defaults) plus any international competition it lacks. An EMPTY list
    is left alone (the Odds API reads it as "every in-season key"), and INTERNATIONAL_FOOTBALL=false opts out. Applied
    only to values that come from the environment: an explicit ``Config(sharpapi_leagues=[...])`` is taken as given."""
    if not _bool("INTERNATIONAL_FOOTBALL", True) or not listed:
        return listed
    missing = [x for x in required if x not in listed]
    if missing:
        log.warning("config: league list did not include %s - added (INTERNATIONAL_FOOTBALL=false to opt out)",
                    ", ".join(missing))
    return listed + missing


@dataclass
class Config:
    supported_competitions: List[str] = field(default_factory=lambda: _list("SUPPORTED_COMPETITIONS", ""))
    cache_retry_seconds: int = field(default_factory=lambda: max(60, _int("CACHE_RETRY_SECONDS", 900)))
    cache_retention_days: int = field(default_factory=lambda: max(1, _int("CACHE_RETENTION_DAYS", 7)))

    # Telegram
    telegram_token: str = field(default_factory=lambda: _env("TELEGRAM_BOT_TOKEN"))
    telegram_chat_id: str = field(default_factory=lambda: _env("TELEGRAM_CHAT_ID"))
    telegram_admin_ids: List[str] = field(default_factory=lambda: _list("TELEGRAM_ADMIN_IDS"))
    enable_commands: bool = field(default_factory=lambda: _bool("ENABLE_COMMANDS", True))
    #: Automatic subscriber DMs (daily picks, recap, live-result pushes). The
    #: channel mirror is unaffected. ``false`` keeps every subscriber row and
    #: all delivery history; flipping it back to ``true`` and restarting
    #: restores automatic subscriber delivery.
    subscriber_delivery_enabled: bool = field(default_factory=lambda: _bool("SUBSCRIBER_DELIVERY_ENABLED", True))

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
    #: Credits ABOVE that reserve kept for settlement's /scores calls only: generation stops this much earlier.
    #: 0 (default) keeps one shared floor, i.e. generation behaves exactly as before.
    oddsapi_settlement_reserve_credits: int = field(
        default_factory=lambda: _int("ODDSAPI_SETTLEMENT_RESERVE_CREDITS", 0))
    #: Each allowed Odds API sport (football, tennis) that has >= 1 event in the
    #: window is guaranteed this many odds keys per cycle before the remaining
    #: credits are ranked by event count, so one busy sport can't starve another.
    #: Football league keys the Odds API may spend credits on (empty = every
    #: in-season football key). The default is the nine-plus leagues Nigerian
    #: punters bet on; everything else is dropped at discovery.
    odds_api_league_allowlist: List[str] = field(default_factory=lambda: _with_international(_list(
        "ODDS_API_LEAGUE_ALLOWLIST",
        "soccer_epl,soccer_spain_la_liga,soccer_italy_serie_a,soccer_germany_bundesliga,soccer_france_ligue_one,soccer_uefa_champs_league,soccer_uefa_europa_league,soccer_uefa_nations_league,soccer_efl_champ,soccer_netherlands_eredivisie,soccer_portugal_primeira_liga,soccer_turkey_super_league"), INTERNATIONAL_ODDS_KEYS))
    oddsapi_min_keys_per_sport: int = field(default_factory=lambda: _int("ODDSAPI_MIN_KEYS_PER_SPORT", 1))
    provider_order: List[str] = field(default_factory=lambda: _list("PROVIDER_ORDER", "oddsapi,sharpapi,sportsgameodds"))
    #: Completeness policy: only these providers MUST report status 'complete'
    #: for a slate to be COMPLETE. SharpAPI is the fixture-feed backbone (the
    #: bot refuses to start without it); the Odds API and SportsGameOdds are
    #: optional by default, so a quota-gated SGO (or a failed odds feed) yields
    #: a COMPLETE slate with RECORDED degraded coverage instead of a PARTIAL
    #: slate that blocks publication. Narrower than PROVIDER_ORDER on purpose;
    #: widen it only with justification (every name must earn its veto).
    required_providers: List[str] = field(default_factory=lambda: _list("REQUIRED_PROVIDERS", "sharpapi"))
    sgo_sports: List[str] = field(default_factory=lambda: _list("SGO_SPORTS", ""))
    sgo_leagues: List[str] = field(default_factory=lambda: _list("SGO_LEAGUES", "NBA,NFL,MLB,MLS,NCAAF,NCAAB,UEFA_CHAMPIONS_LEAGUE"))
    sgo_event_limit: int = field(default_factory=lambda: _int("SGO_EVENT_LIMIT", 50))
    #: SGO odd objects outside these statIDs / periodIDs are rejected at parse time
    #: (UNSUPPORTED_STAT / UNSUPPORTED_PERIOD); player props are capped per event.
    sgo_allowed_stats: List[str] = field(default_factory=lambda: _list(
        "SGO_ALLOWED_STATS", "points,cornerKicks,yellowCards,weightedCards,bothTeamsScored"))
    sgo_allowed_periods: List[str] = field(default_factory=lambda: _list("SGO_ALLOWED_PERIODS", "game,reg,1h,2h"))
    sgo_monthly_object_limit: int = field(default_factory=lambda: _int("SGO_MONTHLY_OBJECT_LIMIT", 2500))
    #: SGO's Amateur plan allows 10 requests/minute; pace below it
    sgo_min_interval_sec: float = field(default_factory=lambda: _float("SGO_MIN_INTERVAL_SEC", 6.5))
    sharpapi_leagues: List[str] = field(default_factory=lambda: _with_international(_list(
        "SHARPAPI_LEAGUES",
        "england_-_premier_league,spain_-_la_liga,italy_-_serie_a,germany_-_bundesliga,france_-_ligue_1,england_-_championship,netherlands_-_eredivisie,portugal_-_primeira_liga,turkey_-_super_lig,uefa_-_champions_league,uefa_-_europa_league,uefa_-_europa_conference_league,uefa_-_nations_league,concacaf_-_nations_league,world_-_international_friendlies,nigeria_-_premier_league,atp,wta,nba,nfl,mlb,euroleague"), INTERNATIONAL_SHARP_LEAGUES))
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

    #: International football (Nations League, friendlies) is ALWAYS fetched, even when the environment pins its own
    #: league lists: a hand-set SHARPAPI_LEAGUES / ODDS_API_LEAGUE_ALLOWLIST written before these competitions existed
    #: would otherwise exclude them silently (which is how match days with only internationals produced no picks).
    #: INTERNATIONAL_FOOTBALL=false turns the top-up off. An EMPTY Odds API allowlist already means "every key".
    international_football: bool = field(default_factory=lambda: _bool("INTERNATIONAL_FOOTBALL", True))

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
    #: SAFETY cap on the number of picks (Top + Daily). The Daily is sized by its combined odds, never by this.
    daily_target: int = field(default_factory=lambda: _int("DAILY_PICKS_TARGET", 12))
    top_odds_target: float = field(default_factory=lambda: _float("TOP_COMBINED_ODDS", 2.00))
    #: Daily ticket: ``DAILY_COMBINED_MIN_ODDS`` is the odds the builder AIMS for (it stops adding legs once it is
    #: reached) and ``DAILY_COMBINED_MAX_ODDS`` is a hard ceiling it never exceeds. Neither is ever bought with a
    #: weaker pick: a slate that only holds 6.4 of qualifying odds posts 6.4.
    daily_odds_min: float = field(default_factory=lambda: _float("DAILY_COMBINED_MIN_ODDS", 10.0))
    daily_odds_max: float = field(default_factory=lambda: _float("DAILY_COMBINED_MAX_ODDS", 12.0))
    pick_window_hours: int = field(default_factory=lambda: _int("PICK_WINDOW_HOURS", 24))
    #: The day window ends at local midnight + this many hours, so late-evening
    #: kickoffs (e.g. 23:30 Lagos) stay in scope; dedupe against the previous
    #: day's stored picks stops a fixture from being posted twice.
    day_window_end_offset_hours: int = field(default_factory=lambda: _int("DAY_WINDOW_END_OFFSET_HOURS", 3))
    min_lead_minutes: int = field(default_factory=lambda: _int("MIN_LEAD_MINUTES", 30))
    daily_min_odds: float = field(default_factory=lambda: _float("DAILY_MIN_ODDS", 1.20))
    daily_max_odds: float = field(default_factory=lambda: _float("DAILY_MAX_ODDS", 5.00))
    #: Top Picks allows 1..3 legs: the search tries every combination
    #: of different fixtures and keeps the one with the highest product of
    #: p_model inside top_odds_target..top_odds_max. top_odds_max is a HARD
    #: reject on the combined ticket. Per-leg odds do not matter for this
    #: confidence product.
    # TOP_LEGS_MAX is canonical; honour the operator's TOP_MAX_PICKS alias.
    top_legs_max: int = field(default_factory=lambda: _int("TOP_LEGS_MAX", _int("TOP_MAX_PICKS", 3)))
    top_odds_max: float = field(default_factory=lambda: _float("TOP_ODDS_MAX", 2.50))
    #: EV is no longer an edge requirement - this is a confidence product. A
    #: candidate with an independent signal (elo) is rejected only when
    #: the signal says the market is MATERIALLY overpricing it (ev below the
    #: NEG floor, default -0.05). Market-only candidates skip the gate
    #: entirely: their p_model is de-vigged market probability, so their EV
    #: is negative by construction and carries no information.
    daily_max_neg_ev: float = field(default_factory=lambda: _float("DAILY_MAX_NEG_EV", -0.05))
    #: THE Daily probability floor (one gate, no second "preferred" pass behind it): a leg whose calibrated
    #: probability is below this is never a Daily candidate, whatever the odds it would add to the ticket.
    daily_min_prob: float = field(default_factory=lambda: _float("DAILY_MIN_PROB", 0.55))
    #: floor on the LOWER confidence bound of a Daily leg (probability minus its uncertainty, see
    #: selection/evidence.py): a leg that is only above 50% if the market is exactly right is not a strong pick
    daily_min_lcb: float = field(default_factory=lambda: _float("DAILY_MIN_LCB", 0.50))
    top_min_prob: float = field(default_factory=lambda: _float("TOP_MIN_PROB", 0.62))
    top_max_neg_ev: float = field(default_factory=lambda: _float("TOP_MAX_NEG_EV", -0.05))
    #: DISTINCT bookmakers that must quote a Top leg. Measured on the 19 Sep capture: 65% of SharpAPI football main
    #: lines and 50% of SGO's had a single book, while the Odds API EU feed and SGO consensus carry 6+, so 3 keeps
    #: the Top on well-covered lines without emptying the slate.
    top_min_books: int = field(default_factory=lambda: _int("TOP_MIN_BOOKS", 3))
    #: Daily coverage by market quality (main / secondary / restricted, see selection/evidence.py). A single-book
    #: line is never strong confirmation; thinner market classes need more independent books.
    daily_min_books: int = field(default_factory=lambda: _int("DAILY_MIN_BOOKS", 2))
    daily_secondary_min_books: int = field(default_factory=lambda: _int("DAILY_SECONDARY_MIN_BOOKS", 3))
    daily_restricted_min_books: int = field(default_factory=lambda: _int("DAILY_RESTRICTED_MIN_BOOKS", 4))
    #: books quoting the same line must agree within this many probability points (max - min implied), else the
    #: line is treated as stale / an outlier and rejected
    max_book_dispersion: float = field(default_factory=lambda: _float("MAX_BOOK_DISPERSION", 0.08))
    #: Bookmakers treated as the SHARP reference (their de-vigged price is the fair probability; everyone else's
    #: price is only the AVAILABLE price). Matched on the normalised bookmaker key a provider reports - nothing is
    #: assumed to be present: a candidate without any of these simply has no reference (and no edge).
    sharp_books: List[str] = field(default_factory=lambda: _list(
        "SHARP_BOOKMAKERS", "pinnacle,betfair_ex_eu,betfair_ex_uk,matchbook"))
    #: With a sharp reference, even the BEST soft price may be at most this far below fair value (EV against the
    #: de-vigged sharp probability) before the leg is dropped as not-value. Not applied without a reference.
    #: Judgement value from a live capture of 28 sharp-covered lines (best soft price vs fair: median -3.1%, quartiles
    #: -5.7% / -0.6%): -0.04 drops roughly the worst-priced third. -0.01 dropped 71% of them and, measured against the
    #: MEDIAN soft price, 100% - a median book always carries its margin. Refit from ``cli.py accuracy`` later.
    min_edge_vs_sharp: float = field(default_factory=lambda: _float("MIN_EDGE_VS_SHARP", -0.04))
    #: Which soft price the edge is measured against: ``best`` (the shoppable price, default) or ``median``.
    edge_price: str = field(default_factory=lambda: _env("EDGE_PRICE", "best").strip().lower())
    #: Without a sharp reference the model may not claim to beat the market by more than this (p_model - p_market):
    #: a bigger claim with nothing independent behind it is noise, not an edge.
    max_unsupported_edge: float = field(default_factory=lambda: _float("MAX_UNSUPPORTED_EDGE", 0.08))
    #: Ranking (never displayed): score = p_model - RANK_Z * se + EDGE_RANK_WEIGHT * clamp(edge vs sharp). ``se`` is
    #: the uncertainty of the probability estimate (selection/evidence.py); ranking on the lower bound is the
    #: standard guard against the winner's curse when the best of ~1,000 noisy lines is picked.
    rank_z: float = field(default_factory=lambda: _float("RANK_Z", 1.0))
    edge_rank_weight: float = field(default_factory=lambda: _float("EDGE_RANK_WEIGHT", 1.0))
    #: Prior noise (probability points) of a de-vigged market probability by market quality. Judgement values, NOT
    #: fitted: refit them from ``accuracy`` reports (hit rate vs predicted probability per quality tier) once
    #: enough picks have settled.
    se_main: float = field(default_factory=lambda: _float("SE_MAIN", 0.015))
    se_secondary: float = field(default_factory=lambda: _float("SE_SECONDARY", 0.030))
    se_restricted: float = field(default_factory=lambda: _float("SE_RESTRICTED", 0.050))
    se_single_book: float = field(default_factory=lambda: _float("SE_SINGLE_BOOK", 0.060))
    se_no_sharp: float = field(default_factory=lambda: _float("SE_NO_SHARP", 0.020))
    #: Elo only counts as an independent signal once BOTH teams have this many rated games
    elo_min_games: int = field(default_factory=lambda: _int("ELO_MIN_GAMES", 10))
    max_player_props: int = field(default_factory=lambda: _int("MAX_PLAYER_PROPS", 0))
    #: Daily-list diversity quotas, so one market (e.g. corners totals), one
    #: sport or one league can never take over the single daily ticket. Shares
    #: are of daily_target. They are never relaxed - not even when the qualifying pool then cannot reach
    #: DAILY_COMBINED_MIN_ODDS (the ticket is posted below the target instead) - and no QUALITY gate
    #: (probability floor, book coverage, edge) is ever relaxed either. See predbot/selection/daily.py.
    daily_max_market_share: float = field(default_factory=lambda: _float("DAILY_MAX_MARKET_SHARE", 0.25))
    #: Cross-market cap on one stat ("corners" can appear as totals AND 1X2
    #: result3 lines; the (market, stat) cap above sees them as two markets).
    daily_max_stat_share: float = field(default_factory=lambda: _float("DAILY_MAX_STAT_SHARE", 0.40))
    daily_max_sport_share: float = field(default_factory=lambda: _float("DAILY_MAX_SPORT_SHARE", 0.60))
    daily_max_per_league: int = field(default_factory=lambda: _int("DAILY_MAX_PER_LEAGUE", 3))
    model_shrink: float = field(default_factory=lambda: _float("MODEL_SHRINK_TO_MARKET", 0.80))
    #: How the independent (history-based) estimate relates to p_model. ``off``: not computed. ``shadow`` (default):
    #: computed and stored next to p_model (p_independent, edge, EV, status) but p_model is unchanged. ``independent``:
    #: p_model IS the independent estimate where one exists (the market-derived value stays as the labelled
    #: fallback elsewhere). Blends with market information are deliberately not offered until the backtest has
    #: chosen their weights.
    independent_model_mode: str = field(default_factory=lambda: _env("INDEPENDENT_MODEL_MODE", "shadow").strip().lower())
    #: Strength-model hyper-parameters. Provisional defaults: select them with tools/backtest_football.py on real history.
    strength_half_life_days: float = field(default_factory=lambda: _float("STRENGTH_HALF_LIFE_DAYS", 365.0))
    strength_prior: float = field(default_factory=lambda: _float("STRENGTH_PRIOR", 8.0))
    strength_min_matches: float = field(default_factory=lambda: _float("STRENGTH_MIN_MATCHES", 5.0))

    # Schedule
    live_updates_enabled: bool = field(default_factory=lambda: _bool("LIVE_UPDATES_ENABLED", True))
    live_poll_minutes: int = field(default_factory=lambda: _int("LIVE_POLL_MINUTES", 5))
    #: A kickoff alert is only worth sending shortly after kickoff; later than this it is dropped, never sent late.
    match_start_window_min: int = field(default_factory=lambda: _int("MATCH_START_WINDOW_MIN", 10))
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
        if not 1 <= self.top_legs_max <= 3:
            log.warning("Config: TOP_LEGS_MAX=%s outside the supported 1-3 range; using 3", self.top_legs_max)
            self.top_legs_max = 3
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
            ("TOP_MIN_PROB in [0,1]", 0.0 <= self.top_min_prob <= 1.0),
            ("DAILY_MIN_PROB in [0,1]", 0.0 <= self.daily_min_prob <= 1.0),
            ("DAILY_MIN_PROB >= 0.5 (a Daily leg is never worse than a coin flip)", self.daily_min_prob >= 0.5),
            ("DAILY_MIN_BOOKS <= DAILY_SECONDARY_MIN_BOOKS <= DAILY_RESTRICTED_MIN_BOOKS",
             self.daily_min_books <= self.daily_secondary_min_books <= self.daily_restricted_min_books),
            ("DAILY_MAX_NEG_EV in [-1,0.5]", -1.0 <= self.daily_max_neg_ev <= 0.5),
            ("TOP_MAX_NEG_EV in [-1,0.5]", -1.0 <= self.top_max_neg_ev <= 0.5),
            ("TOP_MIN_BOOKS >= 1", self.top_min_books >= 1),
            ("MODEL_SHRINK_TO_MARKET in [0,1]", 0.0 <= self.model_shrink <= 1.0),
            ("DAILY_MAX_MARKET_SHARE in (0,1]", 0.0 < self.daily_max_market_share <= 1.0),
            ("DAILY_MAX_STAT_SHARE in (0,1]", 0.0 < self.daily_max_stat_share <= 1.0),
            ("DAILY_MAX_SPORT_SHARE in (0,1]", 0.0 < self.daily_max_sport_share <= 1.0),
            ("DAILY_MAX_PER_LEAGUE >= 1", self.daily_max_per_league >= 1),
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
