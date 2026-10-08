"""Private UI state only. Never write tickets, watches, memberships or result work.

All discovery uses one validated archive snapshot. A valid tap needs no database
read or search. Missing/expired taps may read that same two-table archive again.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import logging
import math
import secrets
import sqlite3

from predbot import custom_picks as cp
from predbot.util.timeutil import day_bounds, to_local

log = logging.getLogger(__name__)
#: per-process key for pseudonymous user references in logs (stable within a run, not reversible to a Telegram id)
_LOG_KEY = secrets.token_bytes(16)


def log_user(user_id) -> str:
    """A short pseudonym for a user in operational logs - never the raw Telegram id."""
    return 'u' + hmac.new(_LOG_KEY, str(user_id).encode(), hashlib.sha256).hexdigest()[:10]


CALLBACK_PREFIX = 'cp|'
TTL = timedelta(minutes=10)
MAX_SUGGESTIONS, MAX_RECOVERY_SEARCHES = 3, 16
ODDS_LADDER = (20, 15, 12, 10, 8, 6, 5, 4, 3, 2)

COPY = {
    'archive_unavailable': cp.UNAVAILABLE,
    'sport_football': '⚽ No football picks made the cut today\n\nI couldn’t find strong football picks right now.',
    'sport_basketball': "🏀 No basketball on the menu today\n\nI couldn't find any basketball picks worth serving right now.",
    'sport_tennis': '🎾 The court is quiet today\n\nNo tennis picks made the cut right now.',
    'sport_unavailable': "👀 Nothing cooking for {sport} today\n\nI don't have usable {sport} picks right now.",
    'market_unavailable': "😅 That market is a little dry today\n\nI couldn't find enough {market} picks that meet the mark.",
    'sport_market_unavailable': "👀 That combo isn't on the board today\n\nI couldn't find usable {market} picks for {sport}.",
    'insufficient_count': "😅 We're a few games short\n\nI couldn’t find enough strong picks for that request.",
    'target_unreachable': "😅 Couldn't stretch it to {target_odds} odds\n\nI won't force weaker picks just to hit the number.",
    'target_too_low': "👀 That's a tight squeeze\n\nI couldn't build a solid ticket close to {target_odds} odds from what's available.",
    'conflicting_constraints': '😂 You boxed me in there\n\nNothing available matches all those filters.',
    'all_started': '⏰ We missed the window\n\nThe matching games have already started.',
    'partial_insufficient': "👀 Slim pickings right now\n\nNothing strong enough matches your request yet.",
    'invalid_request': cp.HELP,
    'future_date': cp.TODAY_ONLY,
    'search_failed': "😅 Almost, but I won't force it\n\nI couldn't build a solid ticket around that request from today's available picks.",
    'below_quality_floor': "✂️ Nothing made the cut today\n\nI'd rather give you nothing than force weak picks.",
    'expired': '⏰ That option has expired\n\nTry these instead:',
    'expired_empty': "⏰ That option has expired\n\nThere aren't enough upcoming picks left to rebuild it right now.",
    'competition_unavailable': 'No matching league picks right now. Try a different competition.',
    'unsupported_competition': 'Which competition would you like? I don’t recognize that league.',
    'unsupported_sport': 'I can’t offer that sport right now. Which other sport would you like?',
}


@dataclass(frozen=True)
class Response:
    text: str
    reason: str
    reply_markup: dict | None = None


@dataclass(frozen=True)
class Tap:
    response: Response | None
    toast: str | None = None


@dataclass(frozen=True)
class Pool:
    prediction_date: str
    generated_at: datetime
    partial: bool
    candidates: tuple[cp.Candidate, ...]  # Started rows retained ONLY to explain an empty state.


@dataclass(frozen=True)
class RecoverySuggestion:
    token: str
    user_id: str
    chat_id: str
    label: str
    request: cp.Request
    ticket: cp.State
    created_at: datetime
    prediction_date: str
    pool_generated_at: datetime
    evaluation_times: tuple[datetime, ...]
    fixture_start_times: tuple[datetime, ...]
    expires_at: datetime
    partial: bool


def scoped(c, request, zone='UTC'):
    local = to_local(c.start, zone)
    return ((not request.sports or c.sport in request.sports) and
            (not request.competitions or c.competition in request.competitions) and
            (not request.allowed_markets or c.market in request.allowed_markets) and
            c.market not in request.excluded_markets and
            (request.market_selection is None or c.selection == request.market_selection) and
            (request.market_line is None or (c.line == request.market_line and
             c.market == 'total' and c.period in ('full', 'reg') and c.stat == 'points' and c.entity == 'all')) and
            (request.time_intent != 'tonight' or local.hour >= 18))


def eligible(candidates, request, now, zone='UTC'):
    return [c for c in candidates if c.start > now and
            (not request.time_intent or to_local(c.start, zone).date() == to_local(now, zone).date()) and
            c.probability >= cp.MIN_PROBABILITY[request.preference] and
            scoped(c, request, zone)]


def valid_ticket(ticket, request, now, zone='UTC'):
    """Independent promise check before storing a button or serving its ticket."""
    if not ticket or not 1 <= len(ticket.picks) <= cp.MAX_LEGS:
        return False
    if len({c.fixture_id for c in ticket.picks}) != len(ticket.picks):
        return False
    if len({c.fixture_identity or c.fixture_id for c in ticket.picks}) != len(ticket.picks):
        return False
    if request.pick_count and len(ticket.picks) != request.pick_count:
        return False
    if len(eligible(ticket.picks, request, now, zone)) != len(ticket.picks):
        return False
    product = math.prod(c.price for c in ticket.picks)
    return math.isfinite(product) and math.isclose(product, ticket.odds) and (
        not request.target_odds or request.target_odds * (1 - cp.TOLERANCE) <= product <=
        request.target_odds * (1 + cp.TOLERANCE))


def sport_name(sports):
    return ' / '.join(cp.SPORT_LABEL.get(s, s.replace('_', ' ').title()) for s in sorted(sports))


def market_name(markets):
    names = {'result2': 'To Win', 'result3': 'Match Result', 'dc': 'Double Chance',
             'dnb': 'Draw No Bet', 'btts': 'BTTS', 'total': 'Over/Under', 'handicap': 'Handicap'}
    return ' / '.join(names.get(m, cp.CATALOG[m].name) for m in sorted(markets))


def failure_reason(pool, request, now, matches, zone='UTC'):
    structural = [c for c in pool.candidates if scoped(c, request, zone)]
    good = [c for c in structural if c.probability >= cp.MIN_PROBABILITY[request.preference]]
    if good and not any(c.start > now for c in good):
        return 'all_started'
    if pool.partial:
        return 'partial_insufficient'
    future = [c for c in pool.candidates if c.start > now]
    sports = [c for c in future if not request.sports or c.sport in request.sports]
    if request.sports and not sports:
        return 'sport_unavailable'
    if request.competitions and not any(c.competition in request.competitions for c in sports):
        return 'competition_unavailable'
    if request.allowed_markets and not any(c.market in request.allowed_markets for c in sports):
        return 'sport_market_unavailable' if request.sports else 'market_unavailable'
    if not structural and request.excluded_markets:
        return 'conflicting_constraints'
    if not matches:
        return 'below_quality_floor'
    if request.pick_count and len({c.fixture_id for c in matches}) < request.pick_count:
        return 'insufficient_count'
    if request.target_odds:
        prices = {}
        for c in matches:
            prices[c.fixture_id] = min(prices.get(c.fixture_id, math.inf), c.price)
        minimum = math.prod(sorted(prices.values())[:request.pick_count or 1])
        if minimum > request.target_odds * (1 + cp.TOLERANCE):
            return 'target_too_low'
        maxima = {}
        for c in matches:
            maxima[c.fixture_id] = max(maxima.get(c.fixture_id, 0), c.price)
        maximum = math.prod(sorted(maxima.values(), reverse=True)[:request.pick_count or cp.MAX_LEGS])
        if maximum < request.target_odds * (1 - cp.TOLERANCE):
            return 'target_unreachable'
    return 'search_failed'


def empty_copy(reason, request, available_count=0):
    if reason == 'sport_unavailable' and len(request.sports) == 1:
        special = 'sport_' + next(iter(request.sports))
        if special in COPY:
            return COPY[special]
    return COPY[reason].format(sport=cp.esc(sport_name(request.sports).lower()),
                              market=cp.esc(market_name(request.allowed_markets)),
                              requested_count=request.pick_count, available_count=available_count,
                              target_odds=f'{request.target_odds:g}' if request.target_odds else '')


def label_for(request):
    icon = {'safe': '🛡️', 'strong': '⭐', 'best': '🔥'}[request.preference]
    prefix = request.preference.title()
    sport = sport_name(request.sports)
    if request.target_odds:
        label = f'{icon} {prefix} {request.target_odds:g} Odds' + (f' · {sport}' if sport else '')
        if request.pick_count:
            label += f' · {request.pick_count} Picks'
    elif sport:
        icon = cp.SPORT_ICON.get(next(iter(sorted(request.sports))), '🎯')
        label = f'{icon} {request.pick_count} {sport} Picks' if request.pick_count else f'{icon} {prefix} {sport} Picks'
        label += f' · {prefix}'
    else:
        label = '🌍 Best Across All Sports' if request.preference == 'best' and not request.pick_count else (
            f'{icon} {request.pick_count or cp.DEFAULT_COUNT} {prefix} Picks · All Sports')
    if request.allowed_markets:
        label += ' · ' + market_name(request.allowed_markets)
    if request.excluded_markets:
        label += ' · No ' + market_name(request.excluded_markets)
    if request.competitions:
        names = {'epl': 'Premier League', 'la_liga': 'La Liga', 'ucl': 'Champions League'}
        label += ' · ' + ' / '.join(names.get(c, c.replace('_', ' ').title()) for c in sorted(request.competitions))
    if request.market_line is not None:
        label += f' · {request.market_selection.title()} {request.market_line:g}'
    if request.time_intent == 'tonight':
        label += ' · Tonight'
    return label


def alternatives(pool, request, reason, now, zone='UTC'):
    """Prepare honest, explicitly named alternatives without weakening quality."""
    original = eligible(pool.candidates, request, now, zone)
    if reason == 'insufficient_count' and original:
        count = min(cp.MAX_LEGS, len({c.fixture_identity or c.fixture_id for c in original}))
        yield replace(request, pick_count=count, target_odds=None)
    if request.target_odds and original:
        targets = [n for n in ODDS_LADDER if n < request.target_odds]
        if reason == 'target_too_low':
            targets = [n for n in reversed(ODDS_LADDER) if n > request.target_odds]
        for target in targets:
            yield replace(request, target_odds=target)
    yield replace(request, target_odds=None, pick_count=None)
    # Changing a market or sport is an explicit alternative, never a silently
    # relaxed answer. Exclusions, time intent and quality always survive.
    if reason in ('market_unavailable', 'sport_market_unavailable'):
        relaxed = replace(request, target_odds=None, pick_count=None,
                          allowed_markets=frozenset(), market_selection=None, market_line=None)
        for market in sorted({c.market for c in eligible(pool.candidates, relaxed, now, zone)}):
            yield replace(relaxed, allowed_markets=frozenset({market}))
    base = replace(request, target_odds=None, pick_count=None, sports=frozenset(), competitions=frozenset())
    yield base
    by_sport = {}
    for c in eligible(pool.candidates, base, now, zone):
        by_sport.setdefault(c.sport, set()).add(c.fixture_identity or c.fixture_id)
    for sport in sorted(by_sport, key=lambda s: (-len(by_sport[s]), s)):
        count = min(request.pick_count or cp.DEFAULT_COUNT, cp.MAX_LEGS, len(by_sport[sport]))
        yield replace(base, pick_count=count, sports=frozenset({sport}))


@dataclass(frozen=True)
class Conversation:
    request: cp.Request
    expires_at: datetime


def refine(previous, current):
    changes = {name: getattr(current, name) for name in current.specified_fields}
    if 'excluded_markets' in changes:
        changes['excluded_markets'] |= previous.excluded_markets
    if 'sports' in changes and 'competitions' not in changes:
        from predbot.markets.competitions import BY_ID
        changes['competitions'] = frozenset(c for c in previous.competitions
            if BY_ID[c].sport in changes['sports'])
    if 'competitions' in changes and 'sports' not in changes:
        changes['sports'] = frozenset()
    if 'allowed_markets' in changes and 'market_line' not in changes:
        changes.update(market_line=None, market_selection=None)
    if current.quality_upgrade:
        changes['preference'] = max((previous.preference, current.preference), key=cp.MIN_PROBABILITY.get)
    result = replace(previous, **changes, specified_fields=current.specified_fields, refinement=True)
    if result.allowed_markets & result.excluded_markets:
        raise cp.RequestError('That market was excluded. Which market would you like instead?', 'conflicting_constraints')
    return result


class Recovery:
    """Single polling-thread UI cache; expired records are pruned on each interaction.

    Tokens remain usable across follow-up requests until their time deadline.
    There is no user quota and no persistence. Restart loses only temporary UI state.
    """
    def __init__(self, db, cfg):
        self.db, self.cfg = db, cfg
        self.ready: dict[str, RecoverySuggestion] = {}
        self.conversations: dict[tuple[str, str], Conversation] = {}
        #: search expansions the most recent respond()/select() used (the caller meters CPU per user with it)
        self.last_work = 0

    def load(self, now):
        day, zone = to_local(now, self.cfg.timezone).date().isoformat(), self.cfg.timezone
        try:
            coverage = self.db.get_custom_pick_pool(day, zone)
            rows = self.db.get_custom_pick_candidates(day, zone) if coverage else []
            generated = cp.aware_time(coverage.get('generated_at')) if coverage else None
            if not coverage or coverage.get('prediction_date') != day or coverage.get('timezone') != zone or \
                    coverage.get('status') not in ('COMPLETE', 'PARTIAL') or generated is None or generated > now or \
                    to_local(generated, zone).date().isoformat() != day or not rows:
                return None
            supported = set(cp.SPORT_LABEL) | set(self.cfg.allowed_sports)
            candidates = []
            for row in rows:
                try:
                    candidate = cp.normalize(row, day, zone, now, generated, supported, include_started=True)
                except (TypeError, ValueError, KeyError, AttributeError):
                    candidate = None  # One malformed local row must not hide the remaining valid pool.
                if candidate:
                    candidates.append(candidate)
            # Conflicting archived identities must not create a plausible wrong
            # fixture. Different market alternatives for one fixture remain.
            identities = {}
            for c in candidates:
                identities.setdefault(c.fixture_id, set()).add((c.sport, c.start, c.home, c.away, c.fixture_identity))
            candidates = [c for c in candidates if len(identities[c.fixture_id]) == 1]
            return Pool(day, generated, coverage['status'] == 'PARTIAL', tuple(candidates))
        except (sqlite3.Error, TypeError, ValueError, AttributeError) as exc:
            log.warning('Custom Picks local read failed | error_type=%s', type(exc).__name__)
            return None

    def leave(self, user_id, chat_id):
        self.conversations.pop((user_id, chat_id), None)
        self.ready = {t: s for t, s in self.ready.items() if (s.user_id, s.chat_id) != (user_id, chat_id)}

    def remember(self, user_id, chat_id, request, expires_at):
        key = (user_id, chat_id)
        self.conversations.pop(key, None)
        self.conversations[key] = Conversation(request, expires_at)
        while len(self.conversations) > 512:
            del self.conversations[next(iter(self.conversations))]

    def prune(self, now):
        for key, context in list(self.conversations.items()):
            if now >= context.expires_at:
                del self.conversations[key]
        for token, suggestion in list(self.ready.items()):
            if not self.current(suggestion, now):
                del self.ready[token]

    def current(self, suggestion, now):
        return suggestion.created_at <= now < suggestion.expires_at and \
            suggestion.prediction_date == to_local(now, self.cfg.timezone).date().isoformat() and \
            all(start > now for start in suggestion.fixture_start_times)

    def prepare(self, pool, request, reason, user_id, chat_id, now, budget=None):
        self.prune(now)
        budget = budget or cp.WorkBudget()
        proven, seen_requests, seen_fixtures = [], set(), set()
        odds_options = []
        searches = 0
        for option in alternatives(pool, request, reason, now, self.cfg.timezone):
            if option in seen_requests:
                continue
            seen_requests.add(option)
            searches += 1
            if searches > MAX_RECOVERY_SEARCHES or budget.exhausted():
                break                       # the request's work budget is spent: offer what is already proven
            ticket = cp.search(eligible(pool.candidates, option, now, self.cfg.timezone), option, budget)
            if not valid_ticket(ticket, option, now, self.cfg.timezone):
                continue
            signature = frozenset(c.fixture_id for c in ticket.picks)
            if signature in seen_fixtures:
                continue
            if option.target_odds:
                # At most two sufficiently different target experiences.
                if len(odds_options) == 2 or any(max(ticket.odds, n) / min(ticket.odds, n) < 1.35 for n in odds_options):
                    continue
                odds_options.append(ticket.odds)
            seen_fixtures.add(signature)
            token = secrets.token_urlsafe(12)
            while token in self.ready:
                token = secrets.token_urlsafe(12)
            starts = tuple(c.start for c in ticket.picks)
            suggestion = RecoverySuggestion(token, user_id, chat_id, label_for(option), option, ticket, now,
                                            pool.prediction_date, pool.generated_at,
                                            tuple(c.evaluated_at for c in ticket.picks), starts,
                                            min(now + TTL, day_bounds(now, self.cfg.timezone)[1], min(starts)), pool.partial)
            while len(self.ready) >= 1536:
                del self.ready[next(iter(self.ready))]
            self.ready[token] = suggestion
            proven.append(suggestion)
            if len(proven) == MAX_SUGGESTIONS:
                break
        log.info('Custom Picks recovery | user=%s | reason=%s | suggestions=%s | labels=%s',
                 log_user(user_id), reason, len(proven), [s.label for s in proven])
        return {'inline_keyboard': [[{'text': s.label, 'callback_data': CALLBACK_PREFIX + s.token}] for s in proven]} if proven else None

    def respond(self, text, user_id, chat_id, now):
        try:
            now = now.astimezone(timezone.utc)
            self.prune(now)
            reason, request = None, cp.Request()
            try:
                request = cp.parse_request(text, set(cp.SPORT_LABEL) | set(self.cfg.allowed_sports))
                key = (user_id, chat_id)
                previous = self.conversations.get(key)
                if request.refinement and previous:
                    request = refine(previous.request, request)
                self.remember(user_id, chat_id, request, min(now + TTL, day_bounds(now, self.cfg.timezone)[1]))
            except cp.RequestError as exc:
                return Response(str(exc), exc.reason)
            pool = self.load(now)
            budget = cp.WorkBudget()
            self.last_work = 0
            matches = eligible(pool.candidates, request, now, self.cfg.timezone) if pool else []
            ticket = cp.search(matches, request, budget) if not reason and matches else None
            if ticket and valid_ticket(ticket, request, now, self.cfg.timezone):
                result = Response(cp.format_ticket(ticket, pool.partial, self.cfg.timezone), 'success')
            else:
                reason = reason or ('archive_unavailable' if pool is None else failure_reason(pool, request, now, matches, self.cfg.timezone))
                markup = self.prepare(pool, request, reason, user_id, chat_id, now, budget) if pool else None
                copy = empty_copy(reason, request, len({c.fixture_id for c in matches}))
                result = Response(copy + ('\n\nTry these instead:' if markup else ''), reason, markup)
            self.last_work = cp.REQUEST_WORK_LIMIT - budget.remaining
            log.info('Custom Picks request | user=%s | target=%s | count=%s | sports=%s | result=%s',
                     log_user(user_id), request.target_odds, request.pick_count, ','.join(sorted(request.sports)) or 'all', result.reason)
            return result
        except Exception as exc:  # Every active private request gets a safe reply, even unexpected local failures.
            log.warning('Custom Picks request failed | user=%s | error_type=%s', log_user(user_id), type(exc).__name__)
            return Response(COPY['search_failed'], 'search_failed')

    def expired(self, request, user_id, chat_id, now):
        pool = self.load(now)
        budget = cp.WorkBudget()
        markup = self.prepare(pool, request, 'expired', user_id, chat_id, now, budget) if pool else None
        self.last_work = cp.REQUEST_WORK_LIMIT - budget.remaining
        return Response(COPY['expired'] if markup else COPY['expired_empty'], 'expired', markup)

    def select(self, callback, now):
        user_id = str((callback.get('from') or {}).get('id'))
        chat = ((callback.get('message') or {}).get('chat') or {})
        chat_id, token = str(chat.get('id')), str(callback.get('data', ''))[len(CALLBACK_PREFIX):]
        suggestion = self.ready.get(token)
        status = 'missing'
        self.last_work = 0
        try:
            now = now.astimezone(timezone.utc)
            if chat.get('type') != 'private' or not user_id.isdigit() or not chat_id.isdigit() or (
                    suggestion and (suggestion.user_id != user_id or suggestion.chat_id != chat_id)):
                status = 'ownership_rejected'
                return Tap(None, 'Open your own Custom Picks options in a private chat with the bot.')
            if suggestion and self.current(suggestion, now) and valid_ticket(suggestion.ticket, suggestion.request, now, self.cfg.timezone):
                status = 'served'
                self.remember(user_id, chat_id, suggestion.request, suggestion.expires_at)
                self.prune(now)
                return Tap(Response(cp.format_ticket(suggestion.ticket, suggestion.partial, self.cfg.timezone), 'success'))
            request = suggestion.request if suggestion else cp.Request()
            if suggestion:
                status = 'stale' if any(start <= now for start in suggestion.fixture_start_times) else 'expired'
            # Prefer already prepared options from this same user's private chat.
            replacements = [s for s in self.ready.values() if s.user_id == user_id and s.chat_id == chat_id and
                            self.current(s, now) and valid_ticket(s.ticket, s.request, now, self.cfg.timezone)]
            self.prune(now)
            if replacements:
                # An automatic replacement may not silently relax the selected request.
                compatible = next((s for s in replacements if suggestion and valid_ticket(s.ticket, request, now, self.cfg.timezone)), None)
                if compatible:
                    return Tap(Response(cp.format_ticket(compatible.ticket, compatible.partial, self.cfg.timezone), 'success'))
                markup = {'inline_keyboard': [[{'text': s.label, 'callback_data': CALLBACK_PREFIX + s.token}]
                                               for s in replacements[-MAX_SUGGESTIONS:]]}
                return Tap(Response(COPY['expired'], 'expired', markup))
            return Tap(self.expired(request, user_id, chat_id, now))
        except Exception as exc:
            log.warning('Custom Picks recovery failed | user=%s | error_type=%s', log_user(user_id), type(exc).__name__)
            return Tap(Response(COPY['search_failed'], 'search_failed'))
        finally:
            # Log the status and user; never echo an untrusted callback payload or whole ticket.
            log.info('Custom Picks recovery selected | user=%s | suggestion=%s | status=%s',
                     log_user(user_id), 'known' if suggestion else 'missing', status)
