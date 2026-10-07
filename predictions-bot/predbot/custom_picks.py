"""Private, ephemeral tickets from archived evaluations. SQLite reads + local work only."""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import math
import re
import sqlite3

from predbot.bot.format import SPORT_ICON, SPORT_LABEL, combined_odds, outcome_label
from predbot.bot.telegram import esc
from predbot.markets.catalog import CATALOG, PERIOD_NAMES, readable_stat, selection_label
from predbot.providers.base import MarketLine
from predbot.util.timeutil import parse_iso, to_local, utcnow

log = logging.getLogger(__name__)
INTRO = ("✨ Custom Picks\n\nTell me what you want.\n\n"
         "• Safe 4 odds\n• 3 basketball picks\n• 5 odds, double chance only\n"
         "• 2 strong tennis picks\n• 6 odds from football and basketball")
UNAVAILABLE = "Custom Picks aren't available yet for today. Try again after today's predictions are generated."
TODAY_ONLY = "Custom Picks currently uses today's generated predictions only."
HELP = "Try ‘safe 4 odds’, ‘3 basketball picks’ or ‘5 odds, double chance only’."
DEFAULT_COUNT, MAX_LEGS = 3, 8
MAX_FIXTURES, ALTERNATIVES, BEAM_PER_COUNT, ODDS_BUCKETS = 60, 4, 48, 12
MAX_EXPANSIONS = 100_000
TOLERANCE = .10
MIN_PROBABILITY = {'safe': .65, 'strong': .60, 'best': .50}
SPORT_ALIASES = {'soccer': 'football', 'american football': 'american_football',
                 'nfl': 'american_football', 'nba': 'basketball', 'wnba': 'basketball',
                 'mlb': 'baseball', 'atp': 'tennis', 'wta': 'tennis', 'ufc': 'mma'}
MARKET_ALIASES = {**{key: key for key, spec in CATALOG.items() if spec.modelable},
                  **{spec.name.lower(): key for key, spec in CATALOG.items() if spec.modelable},
                  'double chance': 'dc', 'dc': 'dc', 'draw no bet': 'dnb', 'dnb': 'dnb',
                  'moneyline': 'result2', 'money line': 'result2', 'ml': 'result2',
                  'totals': 'total', 'total': 'total', 'over under': 'total',
                  'over/under': 'total', 'over-under': 'total', 'btts': 'btts',
                  'both teams to score': 'btts', 'handicap': 'handicap', 'handicaps': 'handicap',
                  'spread': 'handicap', 'spreads': 'handicap', '1x2': 'result3', 'match result': 'result3'}


class RequestError(ValueError):
    pass


@dataclass(frozen=True)
class Request:
    target_odds: float | None = None
    pick_count: int | None = None
    sports: frozenset[str] = frozenset()
    allowed_markets: frozenset[str] = frozenset()
    excluded_markets: frozenset[str] = frozenset()
    preference: str = 'best'


def phrase_pattern(phrases):
    return r'\b(?:' + '|'.join(re.escape(p) for p in sorted(phrases, key=lambda p: (-len(p), p))) + r')\b'


def parse_request(text, supported_sports=()):
    """Small explicit grammar: unsupported wording requests clarification, never inferred constraints."""
    text = ' '.join(str(text).lower().replace('’', "'").split())
    text = re.sub(r'\bover\s*/\s*under\b', 'over/under', text)
    if not text or len(text) > 512:
        raise RequestError(HELP)
    if re.search(r'\b(tomorrow|yesterday|next|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b|\b\d{4}-\d{2}-\d{2}\b', text):
        raise RequestError(TODAY_ONLY)
    aliases = {k.replace('_', ' '): k for k in set(SPORT_LABEL) | set(supported_sports)}
    aliases.update({k: k for k in set(SPORT_LABEL) | set(supported_sports)})
    aliases.update(SPORT_ALIASES)
    sport_matches = list(re.finditer(phrase_pattern(aliases), text))
    if any(re.search(r'\b(no|without|avoid)\s*$', text[:m.start()]) for m in sport_matches):
        raise RequestError("Choose the sports you want to include. " + HELP)
    sports = frozenset(aliases[m.group()] for m in sport_matches)
    if re.search(r'\ball sports\b', text):
        if sports:
            raise RequestError("Choose specific sports or all sports. " + HELP)
        sports = frozenset()
    allowed, excluded, spans = set(), set(), []
    previous_end, negative = 0, False
    for match in re.finditer(phrase_pattern(MARKET_ALIASES), text):
        prefix = text[previous_end:match.start()]
        negative = bool(re.search(r'\b(no|without|avoid)\s*$', prefix)) or (
            negative and bool(re.fullmatch(r'\s+(?:and|or)\s+', prefix)))
        (excluded if negative else allowed).add(MARKET_ALIASES[match.group()])
        spans.append(match.span())
        previous_end = match.end()
    if allowed & excluded:
        raise RequestError("A market can't be both included and excluded. " + HELP)
    number = r'([+-]?\d+(?:\.\d+)?)'
    targets = []
    for pattern in (r'(?<![\w.])' + number + r'\s*(?:combined\s+)?odds\b',
                    r'\bodds\s*(?:(?:of|around|about|near)\s+|[:=]\s*)?' + number + r'(?![\w.])'):
        for match in re.finditer(pattern, text):
            targets.append(float(match.group(1)))
            spans.append(match.span())
    qualifiers = phrase_pattern(set(aliases) | {'safe', 'strong', 'best', 'and'})
    counts = []
    for match in re.finditer(r'(?<![\w.])' + number + r'\s+(?:' + qualifiers + r'\s+)*(?:picks?|games?|selections?)\b', text):
        counts.append(float(match.group(1)))
        spans.append(match.span())
    if len(set(targets)) > 1 or len(set(counts)) > 1:
        raise RequestError("Please give one odds target and one selection count. " + HELP)
    target, count = (targets[0] if targets else None), (counts[0] if counts else None)
    if target is not None and (not math.isfinite(target) or target <= 1):
        raise RequestError("Combined decimal odds must be greater than 1.00.")
    if count is not None and (not count.is_integer() or not 1 <= count <= MAX_LEGS):
        raise RequestError(f"I can build tickets with 1–{MAX_LEGS} distinct games. Try a smaller count.")
    preference = next((p for p in ('safe', 'strong', 'best') if re.search(r'\b' + p + r'\b', text)), 'best')
    remainder = list(text)
    for start, end in spans:
        remainder[start:end] = ' ' * (end - start)
    remainder = re.sub(phrase_pattern(aliases), ' ', ''.join(remainder))
    remainder = re.sub(r"\btoday(?:'s|s)?\b", ' ', remainder)
    fillers = {'give', 'me', 'i', 'need', 'want', 'please', 'the', 'some', 'safe', 'strong', 'best',
               'picks', 'pick', 'games', 'game', 'selections', 'selection', 'around', 'about', 'near',
               'for', 'from', 'and', 'or', 'only', 'no', 'without', 'avoid', 'across', 'all', 'sports',
               'make', 'it', 'of', 'a', 'ticket'}
    remainder = re.sub(phrase_pattern(fillers), ' ', remainder)
    if re.sub(r'[\s,.;:!?/\-]', '', remainder) or not (targets or counts or sports or allowed or excluded or
                                                       re.search(r'\b(safe|strong|best|picks|games|selections)\b', text)):
        raise RequestError(HELP)
    return Request(target, int(count) if count is not None else None, sports,
                   frozenset(allowed), frozenset(excluded), preference)


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


def normalize(row, day, zone, now, pool_time, supported_sports):
    """Validate archived fields without altering the stored evaluation."""
    if not isinstance(row, dict) or row.get('prediction_date') != day or row.get('timezone') != zone:
        return None
    start, evaluated = aware_time(row.get('start_utc')), aware_time(row.get('generated_at'))
    if start is None or start <= now or evaluated is None or evaluated > pool_time or evaluated > now:
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
    line = finite(row.get('line'))
    if (row.get('line') is not None and line is None) or (market in ('total', 'handicap', 'result_total') and line is None):
        return None
    period, stat, entity = row.get('period'), row.get('stat'), row.get('entity')
    if not all(isinstance(v, str) and v.strip() for v in (period, stat, entity)):
        return None
    ml = MarketLine(market, selection, price, period=period, stat=stat, entity=entity, line=line)
    outcome = selection_label(ml, home, away, sport)
    if market == 'result3' and selection == 'draw':
        outcome = 'Match Result: ' + outcome
    if market == 'result2':
        outcome = outcome.replace(' to Win', ' Moneyline', 1)
    if market == 'dc':
        outcome = 'Double Chance: ' + {'home_draw': '1X', 'away_draw': 'X2', 'home_away': '12'}[selection]
        if stat != 'points':
            outcome += f' ({readable_stat(stat)})'
        if period not in ('full', 'reg'):
            outcome += f' ({PERIOD_NAMES.get(period, period)})'
    outcome = outcome_label(outcome, ml, home, away, sport)
    if not outcome:
        return None
    key = repr((market, period, stat, entity, line, selection))
    return Candidate(fixture.strip(), key, sport, market, selection, price, probability, start, home, away, outcome,
                     max(0, min(1, finite(row.get('agreement'), 0))),
                     max(0, min(1, finite(row.get('liquidity'), 0))),
                     max(0, min(10, finite(row.get('bookmaker_count'), 0))),
                     max(-.1, min(.1, finite(row.get('edge_ev'), -.1))))


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


def state_rank(state, request):
    distance = abs(math.log(state.odds / request.target_odds)) if request.target_odds else 0
    return (-strength(state, request), distance, len(state.picks),
            -sum(c.agreement for c in state.picks) / len(state.picks),
            -sum(c.liquidity for c in state.picks) / len(state.picks),
            -sum(c.books for c in state.picks) / len(state.picks),
            -sum(c.reference_ev for c in state.picks) / len(state.picks),
            tuple((c.fixture_id, c.key) for c in state.picks))


def search(candidates, request):
    """Bounded per-fixture beam; every branch processes a fixture at most once."""
    grouped = defaultdict(list)
    for candidate in sorted(candidates, key=Candidate.rank):
        grouped[candidate.fixture_id].append(candidate)
    groups = sorted(grouped.values(), key=lambda rows: rows[0].rank())[:MAX_FIXTURES]
    limit = request.pick_count or (MAX_LEGS if request.target_odds else DEFAULT_COUNT)
    if len(groups) < (request.pick_count or 1):
        return None
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
                    if expansions > MAX_EXPANSIONS:
                        break
                    odds = state.odds * candidate.price
                    if math.isfinite(odds) and odds <= upper:
                        updated.setdefault(count + 1, []).append(State(state.picks + (candidate,), odds))
                if expansions > MAX_EXPANSIONS:
                    break
            if expansions > MAX_EXPANSIONS:
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
        if expansions > MAX_EXPANSIONS:
            break
    counts = [request.pick_count] if request.pick_count else (
        list(range(1, limit + 1)) if request.target_odds else [min(DEFAULT_COUNT, len(groups))])
    feasible = [state for count in counts for state in beams.get(count, []) if not request.target_odds or
                request.target_odds * (1 - TOLERANCE) <= state.odds <= upper]
    return min(feasible, key=lambda s: state_rank(s, request)) if feasible else None


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
    blocks = ['🔥 Custom Picks', 'Stronger available picks']
    for pick in sorted(state.picks, key=lambda c: (c.start, c.fixture_id, c.key)):
        when = to_local(pick.start, zone).strftime('%d %b %H:%M %Z')
        blocks.append(f"{SPORT_ICON.get(pick.sport, '🎯')} {compact(pick.home, 60)} vs {compact(pick.away, 60)}\n"
                      f"{compact(pick.outcome, 140)}\nOdds: {pick.price:.2f} · Confidence: {round(pick.probability * 100)}%\n"
                      f"🕘 {esc(when)}")
    blocks.append(f'Combined Odds: {combined_odds(c.price for c in state.picks):.2f}\n'
                  f'{len(state.picks)} selection' + ('s' if len(state.picks) != 1 else ''))
    if partial:
        blocks.append("⚠️ Today's available prediction pool is partial.")
    return '\n\n'.join(blocks)


class CustomPicks:
    """No fetcher, provider or scoring dependency. Tickets are never persisted."""
    def __init__(self, db, cfg):
        self.db, self.cfg = db, cfg

    def reply(self, text, user_id, now=None):
        now = (now or utcnow()).astimezone(timezone.utc)
        day = to_local(now, self.cfg.timezone).date().isoformat()
        supported = set(SPORT_LABEL) | set(self.cfg.allowed_sports)
        try:
            request = parse_request(text, supported)
        except RequestError as exc:
            log.info('Custom Picks request | user=%s | result=clarification', user_id)
            return str(exc)
        def unavailable():
            log.info('Custom Picks request | user=%s | target=%s | count=%s | sports=%s | candidates=0 | eligible=0 | result=unavailable',
                     user_id, request.target_odds, request.pick_count, ','.join(sorted(request.sports)) or 'all')
            return UNAVAILABLE
        try:
            coverage = self.db.get_custom_pick_pool(day, self.cfg.timezone)
            rows = self.db.get_custom_pick_candidates(day, self.cfg.timezone) if coverage else []
        except (sqlite3.Error, ValueError, TypeError) as exc:
            log.warning('Custom Picks local read failed | error_type=%s', type(exc).__name__)
            return unavailable()
        pool_time = aware_time(coverage.get('generated_at')) if coverage else None
        if not coverage or coverage.get('prediction_date') != day or coverage.get('timezone') != self.cfg.timezone or \
                coverage.get('status') not in ('COMPLETE', 'PARTIAL') or pool_time is None or pool_time > now or \
                to_local(pool_time, self.cfg.timezone).date().isoformat() != day:
            return unavailable()
        if not rows:
            return unavailable()
        candidates = [c for row in rows if (c := normalize(row, day, self.cfg.timezone, now, pool_time, supported))]
        eligible = [c for c in candidates if c.probability >= MIN_PROBABILITY[request.preference] and
                    (not request.sports or c.sport in request.sports) and
                    (not request.allowed_markets or c.market in request.allowed_markets) and
                    c.market not in request.excluded_markets]
        state = search(eligible, request) if eligible else None
        log.info('Custom Picks request | user=%s | target=%s | count=%s | sports=%s | candidates=%s | eligible=%s | result=%s',
                 user_id, request.target_odds, request.pick_count, ','.join(sorted(request.sports)) or 'all',
                 len(rows), len(eligible), round(state.odds, 2) if state else 'unavailable')
        if state:
            return format_ticket(state, coverage['status'] == 'PARTIAL', self.cfg.timezone)
        if request.sports and not any(c.sport in request.sports for c in candidates):
            sports = ' / '.join(SPORT_LABEL.get(s, s.replace('_', ' ').title()).lower() for s in sorted(request.sports))
            return f"I don't have usable {sports} picks in today's available pool."
        fixtures = len({c.fixture_id for c in eligible})
        if request.pick_count and fixtures < request.pick_count:
            return f'Only {fixtures} distinct games match your request today; I need {request.pick_count}. Try fewer picks or fewer restrictions.'
        if not eligible:
            return "No remaining picks match those sports, markets and strength preferences today. Try fewer restrictions."
        target = f'a {request.target_odds:.2f} odds ticket' if request.target_odds else 'that ticket'
        return f"I couldn't build {target} from the remaining games today. Try a lower target or fewer restrictions."
