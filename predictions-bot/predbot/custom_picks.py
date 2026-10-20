"""Private, ephemeral tickets from archived evaluations. SQLite reads + local work only."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
import math
import re

from predbot.bot.format import SPORT_ICON, SPORT_LABEL, combined_odds, outcome_label
from predbot.bot.telegram import esc
from predbot.markets.catalog import CATALOG, PERIOD_NAMES, readable_stat, selection_label
from predbot.providers.base import MarketLine
from predbot.util.timeutil import parse_iso, to_local, utcnow

log = logging.getLogger(__name__)
#: Every sport the parser recognizes names/aliases for - this NEVER narrows,
#: so "mma"/"ufc" stay fully parseable even while disabled in production
#: (see enabled_sports() below). SPORT_LABEL's own keys (minus "other").
RECOGNIZED_SPORTS = tuple(s for s in SPORT_LABEL if s != 'other')


def enabled_sports(allowed_sports):
    """RECOGNIZED_SPORTS actually enabled in a deployment: the real generation
    config (Config.allowed_sports) intersected with what the parser knows by
    name - never a second, separately-maintained sport list. A sport can be
    recognized (RECOGNIZED_SPORTS) without being enabled (this), which is
    exactly MMA's current state: parseable, not yet advertised or servable."""
    allowed = set(allowed_sports)
    return [s for s in RECOGNIZED_SPORTS if s in allowed]


def enabled_sports_label(allowed_sports):
    return ', '.join(SPORT_LABEL[s] for s in enabled_sports(allowed_sports))


def intro_text(allowed_sports):
    """The welcome message, built fresh from whatever's actually enabled in
    THIS deployment - never a separately hardcoded sport list, so it can
    never advertise a sport the real pipeline isn't producing."""
    return ("✨ Custom Picks\n\nTell me what you want.\n\n"
            f"Supported sports: {enabled_sports_label(allowed_sports)}\n\n"
            "Example: \"3 basketball picks at 5 odds\"")


#: Mirrors the real production ALLOWED_SPORTS config (predbot/config.py's own
#: default, read from the SAME env var) - the one place this module hardcodes
#: an enabled-sports list, kept in lockstep by reading Config's own default
#: rather than repeating the literal sport names here. Used only where no
#: live Config/cfg is available (module-level INTRO, and parse_request's
#: default) - every real request path passes the request's own
#: cfg.allowed_sports instead, so a config change takes effect immediately.
def _default_allowed_sports():
    from predbot.config import Config
    return Config().allowed_sports


INTRO = intro_text(_default_allowed_sports())
UNAVAILABLE = "👨‍🍳 Still cooking\n\nToday's picks aren't ready yet. Check back in a bit."
TODAY_ONLY = "😅 I'm living in today for now\n\nCustom Picks currently works with today's games only."
HELP = '😅 I didn\'t quite catch that\n\nWhat odds or how many games would you like?'
DEFAULT_COUNT, MAX_LEGS = 3, 8
MAX_FIXTURES, ALTERNATIVES, BEAM_PER_COUNT, ODDS_BUCKETS = 60, 4, 48, 12
MAX_EXPANSIONS = 100_000
#: Strict minimum individual price for EVERY Custom Picks selection, at any
#: target odds, in any sport, including fallback and recovery tickets. Never
#: relaxed to complete a ticket; enforced in search(), valid_ticket() and the
#: delivery boundary alike.
MIN_PRICE = 1.25
#: Compact tickets for low odds targets (e.g. a ~2.00 request): prefer 1-2
#: legs with an absolute maximum of 3. Higher targets keep MAX_LEGS
#: (configurable below) so the 3-leg cap is never applied universally
#: without evaluating feasibility.
LOW_TARGET_CEILING = 2.5
LOW_TARGET_MAX_LEGS = 3
#: Shortlist size for ticket variety: the top-N distinct feasible tickets are
#: ranked, then rotation picks one per user. search() still returns rank 0,
#: so combination selection stays fully deterministic.
SHORTLIST_LIMIT = 5
#: all searches one request may run together (the primary search plus every recovery search): a failing request
#: used to be able to run 17 full searches; the primary search keeps its full MAX_EXPANSIONS, recovery shares the rest
REQUEST_WORK_LIMIT = 400_000


class WorkBudget:
    """Expansions left for one request; every ``search`` given it draws from the same pool and stops when empty."""

    def __init__(self, limit=REQUEST_WORK_LIMIT):
        self.remaining = limit

    def exhausted(self):
        return self.remaining <= 0
TOLERANCE = .10
MIN_PROBABILITY = {'safe': .65, 'strong': .60, 'best': .50}
SPORT_ALIASES = {'soccer': 'football', 'association football': 'football',
                 'american football': 'american_football', 'nfl': 'american_football',
                 'gridiron': 'american_football', 'pigskin': 'american_football',
                 'nba': 'basketball', 'wnba': 'basketball', 'hoops': 'basketball', 'basket': 'basketball',
                 'mlb': 'baseball', 'atp': 'tennis', 'wta': 'tennis', 'ufc': 'mma',
                 'mixed martial arts': 'mma'}
#: Sports recognized by NAME but not currently offered, mapped to their
#: canonical display name for the "We do not cover X yet" response. Curated
#: like SPORT_ALIASES/LEAGUES - an unlisted word is never guessed at.
UNSUPPORTED_SPORTS = dict(sorted({
    'ice hockey': 'Hockey', 'field hockey': 'Hockey', 'hockey': 'Hockey',
    'rugby league': 'Rugby', 'rugby union': 'Rugby', 'rugby': 'Rugby',
    'cricket': 'Cricket', 'volleyball': 'Volleyball', 'handball': 'Handball', 'golf': 'Golf',
    'boxing': 'Boxing', 'darts': 'Darts', 'snooker': 'Snooker', 'table tennis': 'Table Tennis',
    'badminton': 'Badminton', 'cycling': 'Cycling', 'formula 1': 'Formula 1', 'formula one': 'Formula 1',
    'f1': 'Formula 1', 'esports': 'Esports', 'e-sports': 'Esports', 'swimming': 'Swimming',
    'athletics': 'Athletics', 'motorsport': 'Motorsport',
}.items(), key=lambda kv: -len(kv[0])))  # longest phrase first: "ice hockey" before bare "hockey"
MARKET_ALIASES = {**{key: key for key, spec in CATALOG.items() if spec.modelable},
                  **{spec.name.lower(): key for key, spec in CATALOG.items() if spec.modelable},
                  'double chance': 'dc', 'dc': 'dc', 'draw no bet': 'dnb', 'dnb': 'dnb',
                  'moneyline': 'result2', 'money line': 'result2', 'ml': 'result2',
                  'totals': 'total', 'total': 'total', 'over under': 'total',
                  'over/under': 'total', 'over-under': 'total', 'btts': 'btts',
                  'both teams to score': 'btts', 'handicap': 'handicap', 'handicaps': 'handicap',
                  'spread': 'handicap', 'spreads': 'handicap', '1x2': 'result3', 'match result': 'result3'}


class RequestError(ValueError):
    def __init__(self, message, reason='invalid_request'):
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class Request:
    target_odds: float | None = None
    pick_count: int | None = None
    sports: frozenset[str] = frozenset()
    allowed_markets: frozenset[str] = frozenset()
    excluded_markets: frozenset[str] = frozenset()
    preference: str = 'best'
    competitions: frozenset[str] = frozenset()
    market_selection: str | None = None
    market_line: float | None = None
    time_intent: str | None = None
    specified_fields: frozenset[str] = field(default=frozenset(), compare=False)
    refinement: bool = field(default=False, compare=False)
    quality_upgrade: bool = field(default=False, compare=False)
    #: Lower-cased team names mentioned by name, matched against the loaded
    #: pool (there's no static team catalog - teams change daily). Populated
    #: AFTER parse_request(), once the pool is loaded; see
    #: match_teams_in_pool(). Appended last (not inserted among the fields
    #: above) so custom_intent.py's positional Request(...) construction
    #: never shifts. Not in specified_fields, so refine() preserves it
    #: across turns like any other field the current message doesn't
    #: re-specify.
    teams: frozenset[str] = frozenset()
    #: A bare decimal that reads like a goal/point/set line ("1.5 options")
    #: but named no side - set fresh on EVERY parse (never carried forward
    #: by refine(), which always takes the CURRENT message's own value; see
    #: its 'ambiguous_line' field name), so Recovery.respond() can ask "Over
    #: or Under?" unless the conversation already resolved this exact number
    #: via market_line (carried forward normally). Never silently defaulted
    #: to Over. Appended last for the same positional-construction reason as
    #: teams, above.
    ambiguous_line: float | None = None


def phrase_pattern(phrases):
    return r'\b(?:' + '|'.join(re.escape(p) for p in sorted(phrases, key=lambda p: (-len(p), p))) + r')\b'


def parse_request(text, supported_sports=(), allowed_sports=None):
    from predbot.custom_intent import parse
    return parse(text, supported_sports, allowed_sports)


def match_teams_in_pool(text, candidates):
    """Team names mentioned in ``text``, matched against today's actual pool - there's
    no static team catalog (teams change daily), so a name is only ever recognized
    because it's genuinely playing today, never guessed from free text alone."""
    lowered = text.lower()
    seen, matched = set(), set()
    for c in candidates:
        for name in (c.home, c.away):
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            if re.search(r'\b' + re.escape(key) + r'\b', lowered):
                matched.add(key)
    return frozenset(matched)


def aware_time(value):
    if not isinstance(value, str) or not re.search(r'(?:Z|[+-]\d{2}:\d{2})$', value):
        return None
    return parse_iso(value)


def finite(value, default=None):
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError, OverflowError):
        return default


@dataclass(frozen=True)
class Candidate:
    fixture_id: str
    key: str
    sport: str
    market: str
    selection: str
    price: float
    probability: float
    start: datetime
    home: str
    away: str
    outcome: str
    agreement: float
    liquidity: float
    books: float
    reference_ev: float
    evaluated_at: datetime | None = None
    competition: str | None = None
    competition_label: str | None = None
    line: float | None = None
    period: str = 'full'
    stat: str = 'points'
    entity: str = 'all'
    fixture_identity: tuple = ()

    def rank(self):
        return (-self.probability, -self.agreement, -self.liquidity, -self.books,
                -self.reference_ev, self.price, self.start, self.fixture_id, self.key)


def valid_selection(market, selection):
    choices = {'result3': {'home', 'draw', 'away'}, 'result2': {'home', 'away'}, 'dnb': {'home', 'away'},
               'dc': {'home_draw', 'away_draw', 'home_away'}, 'btts': {'yes', 'no'},
               'total': {'over', 'under'}, 'handicap': {'home', 'away'}, 'odd_even': {'odd', 'even'},
               'clean_sheet': {'yes', 'no'}, 'win_to_nil': {'yes', 'no'}, 'team_to_score': {'yes', 'no'}}
    patterns = {'ht_ft': r'(home|draw|away)/(home|draw|away)', 'correct_score': r'\d+-\d+',
                'result_total': r'(home|draw|away)_(over|under)', 'result_btts': r'(home|draw|away)_(yes|no)',
                'winning_margin': r'(home|away)_\d+(?:[_+-]\d*)?'}
    return selection in choices.get(market, set()) or bool(re.fullmatch(patterns.get(market, r'(?!)'), selection))


def normalize(row, day, zone, now, pool_time, supported_sports, *, include_started=False):
    """Validate archived fields without altering the stored evaluation."""
    if not isinstance(row, dict) or row.get('prediction_date') != day or row.get('timezone') != zone:
        return None
    extra = row.get('extra') if isinstance(row.get('extra'), dict) else {}
    statuses = [str(source.get(key, '')).lower() for source in (row, extra)
                for key in ('status', 'event_status', 'fixture_status')]
    if any(s in {'cancelled', 'canceled', 'postponed', 'abandoned', 'final', 'finished',
                 'completed', 'live', 'in_progress', 'suspended'} for s in statuses) or any(
            source.get(key) is True for source in (row, extra)
            for key in ('cancelled', 'canceled', 'postponed', 'started', 'finished', 'is_live')):
        return None
    start, evaluated = aware_time(row.get('start_utc')), aware_time(row.get('generated_at'))
    if start is None or (start <= now and not include_started) or evaluated is None or evaluated > pool_time or evaluated > now:
        return None
    if to_local(evaluated, zone).date().isoformat() != day:
        return None
    price, probability = finite(row.get('price')), finite(row.get('p_model'))
    if price is None or price <= 1 or probability is None or not 0 < probability < 1:
        return None
    fixture = row.get('canonical_event_id') or row.get('fixture_key')
    sport, market, selection = row.get('sport'), row.get('market'), row.get('selection')
    if not isinstance(fixture, str) or not fixture.strip() or not isinstance(sport, str) or sport not in supported_sports:
        return None
    if not isinstance(market, str) or market not in CATALOG or not CATALOG[market].modelable or not isinstance(selection, str) or not valid_selection(market, selection):
        return None
    home, away = row.get('home'), row.get('away')
    if not isinstance(home, str) or not home.strip() or not isinstance(away, str) or not away.strip():
        return None
    if home.strip().lower() == away.strip().lower():
        return None  # A team cannot play itself: incorrectly mapped fixture.
    home_id, away_id = row.get('home_id'), row.get('away_id')
    if (isinstance(home_id, str) and home_id.strip() and isinstance(away_id, str) and away_id.strip()
            and home_id.strip() == away_id.strip()):
        return None  # Same participant on both sides: incorrectly mapped fixture.
    line = finite(row.get('line'))
    if (row.get('line') is not None and line is None) or (market in ('total', 'handicap', 'result_total') and line is None):
        return None
    period, stat, entity = row.get('period'), row.get('stat'), row.get('entity')
    if not all(isinstance(v, str) and v.strip() for v in (period, stat, entity)):
        return None
    ml = MarketLine(market, selection, price, period=period, stat=stat, entity=entity, line=line)
    outcome = selection_label(ml, home, away, sport)
    if market == 'total' and entity == 'all':
        outcome = outcome.replace(' Total ', ' ', 1)
    if market == 'result3' and selection == 'draw':
        outcome = 'Match Result: ' + outcome
    if market == 'dc':
        outcome = 'Double Chance: ' + {'home_draw': '1X', 'away_draw': 'X2', 'home_away': '12'}[selection]
        if stat != 'points':
            outcome += f' ({readable_stat(stat)})'
        if period not in ('full', 'reg'):
            outcome += f' ({PERIOD_NAMES.get(period, period)})'
    if market == 'btts':
        outcome = outcome.replace('BTTS ', 'BTTS: ', 1)
    if market == 'handicap':
        outcome = 'Handicap: ' + outcome
    outcome = outcome_label(outcome, ml, home, away, sport)
    if not outcome:
        return None
    key = repr((market, period, stat, entity, line, selection))
    from predbot.custom_intent import competition_id
    competition = competition_id(row.get('canonical_competition_id'), row.get('league_id'), row.get('league'))
    label = row.get('league')
    label = label.strip() if isinstance(label, str) and label.strip() else None
    if label and ('_' in label or ':' in label or label.lower() == row.get('league_id')):
        label = None  # A provider identifier is not a competition display name.
    identity = (sport, competition, start, *sorted((row['home_id'], row['away_id']))) if all(
        isinstance(row.get(k), str) and row[k].strip() for k in ('home_id', 'away_id')) else ()
    return Candidate(fixture.strip(), key, sport, market, selection, price, probability, start, home, away, outcome,
                     max(0, min(1, finite(row.get('agreement'), 0))),
                     max(0, min(1, finite(row.get('liquidity'), 0))),
                     max(0, min(10, finite(row.get('bookmaker_count'), 0))),
                     max(-.1, min(.1, finite(row.get('edge_ev'), -.1))), evaluated,
                     competition, label, line, period, stat, entity, identity)


@dataclass(frozen=True)
class State:
    picks: tuple[Candidate, ...] = ()
    odds: float = 1.0


def strength(state, request):
    count = len(state.picks)
    mean = sum(c.probability for c in state.picks) / count
    # Comparable model strength can favor varied market families. Explicit inclusion disables this preference.
    repetition = count - len({c.market for c in state.picks})
    return mean - (0 if request.allowed_markets else .02 * repetition / count)


def is_compact_target(request):
    """Low odds targets (e.g. ~2.00) that must stay compact: 1-2 legs preferred, 3 max."""
    return request.target_odds is not None and request.pick_count is None and request.target_odds <= LOW_TARGET_CEILING


def min_price_for(request):
    """Strict minimum individual price: 1.25 for every Custom Picks ticket.

    Applies at all target odds, in all sports, with or without an explicit
    pick count. Never relaxed to fill a ticket -- when nothing qualifies the
    request honestly fails instead.
    """
    return MIN_PRICE


def max_legs_for(request):
    """Effective leg cap: explicit counts win, low targets stay compact, higher targets stay configurable."""
    if request.pick_count:
        return request.pick_count
    if request.target_odds is not None and request.target_odds <= LOW_TARGET_CEILING:
        return LOW_TARGET_MAX_LEGS
    if request.target_odds:
        return MAX_LEGS
    return DEFAULT_COUNT


def state_rank(state, request):
    distance = abs(math.log(state.odds / request.target_odds)) if request.target_odds else 0
    # For odds requests, equally confident tickets prefer fewer fixtures before
    # market variety or target distance. Never trade confidence for fewer legs.
    quality = (sum(c.probability for c in state.picks) / len(state.picks)
               if request.target_odds else strength(state, request))
    return (-round(quality, 12), len(state.picks) if request.target_odds else distance,
            distance if request.target_odds else len(state.picks),
            -sum(c.agreement for c in state.picks) / len(state.picks),
            -sum(c.liquidity for c in state.picks) / len(state.picks),
            -sum(c.books for c in state.picks) / len(state.picks),
            -sum(c.reference_ev for c in state.picks) / len(state.picks),
            tuple((c.fixture_id, c.key) for c in state.picks))


def _run_beam(candidates, request, cap):
    """Shared beam expansion; returns (beams, upper, limit, groups, expansions).

    Every branch processes a fixture at most once and one leg per fixture is
    ever taken. Selections below the strict minimum price never participate.
    """
    priced = [c for c in candidates if c.price >= min_price_for(request)]
    grouped = defaultdict(list)
    for candidate in sorted(priced, key=Candidate.rank):
        grouped[candidate.fixture_identity or candidate.fixture_id].append(candidate)
    groups = sorted(grouped.values(), key=lambda rows: rows[0].rank())[:MAX_FIXTURES]
    limit = max_legs_for(request)
    upper = request.target_odds * (1 + TOLERANCE) if request.target_odds else math.inf
    beams = {0: [State()]}
    expansions = 0
    for group in groups:
        # Keep strong representatives of different available families, then fill with strong alternatives.
        options = [group[0]]
        families = {group[0].market}
        for candidate in group[1:]:
            if candidate.market not in families and len(options) < ALTERNATIVES:
                options.append(candidate)
                families.add(candidate.market)
        options += [c for c in group if c not in options][:ALTERNATIVES - len(options)]
        updated = {count: list(states) for count, states in beams.items()}
        for count, states in beams.items():
            if count >= limit:
                continue
            for state in states:
                for candidate in options:
                    expansions += 1
                    if expansions > cap:
                        break
                    odds = state.odds * candidate.price
                    if math.isfinite(odds) and odds <= upper:
                        updated.setdefault(count + 1, []).append(State(state.picks + (candidate,), odds))
                if expansions > cap:
                    break
            if expansions > cap:
                break
        beams = {0: updated[0]}
        for count, states in updated.items():
            if count == 0:
                continue
            if request.target_odds:
                buckets = defaultdict(list)
                for state in states:
                    bucket = min(ODDS_BUCKETS - 1, int(math.log(state.odds) / math.log(upper) * ODDS_BUCKETS))
                    buckets[bucket].append(state)
                retained = [state for bucket in sorted(buckets) for state in
                            sorted(buckets[bucket], key=lambda s: state_rank(s, request))[:BEAM_PER_COUNT // ODDS_BUCKETS]]
            else:
                retained = sorted(states, key=lambda s: state_rank(s, request))[:BEAM_PER_COUNT]
            beams[count] = retained
        if expansions > cap:
            break
    return beams, upper, limit, groups, expansions


def _feasible_states(beams, upper, limit, groups, request):
    counts = [request.pick_count] if request.pick_count else (
        list(range(1, limit + 1)) if request.target_odds else [min(DEFAULT_COUNT, len(groups))])
    return [state for count in counts for state in beams.get(count, []) if not request.target_odds or
            request.target_odds * (1 - TOLERANCE) <= state.odds <= upper]


def search(candidates, request, budget=None):
    """Best ticket for a request, or None. ``budget`` (a WorkBudget) caps the
    expansions further and is charged with what this search used; with nothing
    left the search returns None. Fully deterministic for the same pool order
    up to fixture sorting: the top-ranked feasible ticket always wins, so
    ticket variety is layered on top via shortlist(), never inside search()."""
    cap = MAX_EXPANSIONS if budget is None else min(MAX_EXPANSIONS, budget.remaining)
    if cap <= 0:
        return None
    beams, upper, limit, groups, expansions = _run_beam(candidates, request, cap)
    if budget is not None:
        budget.remaining -= min(expansions, cap)
    if len(groups) < (request.pick_count or 1):
        return None
    # One- and two-leg states are retained per count throughout the beam and
    # ranked here alongside three-leg states: estimated probability and market
    # quality (agreement/liquidity/books/EV) come first via state_rank, then leg
    # count, then target proximity -- so fewer legs win ties but confidence is
    # never traded for fewer legs.
    feasible = _feasible_states(beams, upper, limit, groups, request)
    return min(feasible, key=lambda s: state_rank(s, request)) if feasible else None


def shortlist(candidates, request, budget=None, limit=SHORTLIST_LIMIT):
    """Top-N distinct feasible tickets, best first. Used for per-user rotation
    so identical requests need not all serve the same ticket. Every entry
    satisfies the same constraints search() enforces (price floor, one leg per
    fixture, target tolerance, exact count when requested)."""
    cap = MAX_EXPANSIONS if budget is None else min(MAX_EXPANSIONS, budget.remaining)
    if cap <= 0 or limit <= 0:
        return []
    beams, upper, cap_limit, groups, expansions = _run_beam(candidates, request, cap)
    if budget is not None:
        budget.remaining -= min(expansions, cap)
    if len(groups) < (request.pick_count or 1):
        return []
    feasible = sorted(_feasible_states(beams, upper, cap_limit, groups, request),
                      key=lambda s: state_rank(s, request))
    ranked, seen = [], set()
    for state in feasible:
        signature = ticket_signature(state)
        if signature in seen:
            continue
        seen.add(signature)
        ranked.append(state)
        if len(ranked) >= limit:
            break
    return ranked


def ticket_signature(ticket):
    """Stable identity of a ticket's fixture set and combined odds."""
    parts = sorted(str(c.fixture_identity) if c.fixture_identity else c.fixture_id for c in ticket.picks)
    return ','.join(parts) + f'@{ticket.odds:.2f}'


def request_key(request):
    """Stable identity of a request's constraints for variety rotation."""
    return '|'.join((
        f'{request.target_odds:g}' if request.target_odds else '',
        str(request.pick_count or ''),
        ','.join(sorted(request.sports)),
        ','.join(sorted(request.allowed_markets)),
        ','.join(sorted(request.excluded_markets)),
        request.preference,
        ','.join(sorted(request.competitions)),
        request.market_selection or '',
        f'{request.market_line:g}' if request.market_line is not None else '',
        request.time_intent or '',
    ))


def compact(value, limit):
    """Bound escaped text without splitting HTML entities."""
    result = ''
    for character in str(value).replace('\n', ' '):
        encoded = esc(character)
        if len(result) + len(encoded) > limit:
            return result + '…'
        result += encoded
    return result


def format_ticket(state, partial, zone):
    blocks = [f'🔥 {combined_odds(c.price for c in state.picks):.2f} ODDS']
    for pick in sorted(state.picks, key=lambda c: (c.start, c.fixture_id, c.key)):
        league = compact(pick.competition_label, 80) + '\n' if pick.competition_label else ''
        blocks.append(f"{SPORT_ICON.get(pick.sport, '🎯')} {compact(pick.home, 60)} vs {compact(pick.away, 60)}\n"
                      f"{league}{compact(pick.outcome, 140)}\n"
                      f"Odds: {pick.price:.2f} · Confidence: {round(pick.probability * 100)}%")
    blocks.append('Bet only what you can afford to lose.')
    return '\n\n'.join(blocks)


class CustomPicks:
    """No fetcher, provider or scoring dependency. Tickets are never persisted."""
    def __init__(self, db, cfg):
        self.db, self.cfg = db, cfg
        from predbot.custom_recovery import Recovery
        self.recovery = Recovery(db, cfg)

    def reply(self, text, user_id, now=None):
        """Text-only compatibility API; Telegram uses respond() to include ready buttons."""
        return self.respond(text, user_id, user_id, now).text

    def respond(self, text, user_id, chat_id, now=None):
        return self.recovery.respond(text, str(user_id), str(chat_id), now or utcnow())

    def select(self, callback, now=None):
        return self.recovery.select(callback, now or utcnow())

    def leave(self, user_id, chat_id):
        self.recovery.leave(str(user_id), str(chat_id))
