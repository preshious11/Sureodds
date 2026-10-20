"""Deterministic conversational concepts; no ticket construction or external calls."""
from difflib import get_close_matches
import math
import re
import unicodedata

from predbot.markets.competitions import BY_ID, lookup

LEAGUES = {key.replace('_', ' '): key for key in BY_ID}
LEAGUES.update({c.sharp.replace('_', ' ').replace('-', ' '): c.id for c in BY_ID.values() if c.sharp})
LEAGUES.update({'premier league': 'epl', 'english premier league': 'epl', 'england premier league': 'epl',
    'english premiership': 'epl', 'epl': 'epl', 'la liga': 'la_liga', 'laliga': 'la_liga',
    'serie a': 'serie_a', 'ligue one': 'ligue_1', 'ligue 1': 'ligue_1',
    'champions league': 'ucl', 'europa league': 'uel', 'conference league': 'uecl',
    'nigerian premier league': 'nigeria_npfl'})
# Legacy NBA/NFL/ATP wording selects the sport. Explicit league wording selects
# those competitions, so existing sport aliases keep their established meaning.
for name in ('nba', 'nfl', 'mlb', 'atp', 'wta'):
    LEAGUES.pop(name, None)
    LEAGUES[name + ' league'] = name

NUMBERS = dict(zip(('one two three four five six seven eight nine ten eleven twelve thirteen fourteen '
                   'fifteen sixteen seventeen eighteen nineteen twenty').split(), map(str, range(1, 21))))
QUALITY = {'safe': 'safe', 'safest': 'safe', 'safer': 'safe', 'sure': 'safe',
           'strong': 'strong', 'strongest': 'strong', 'stronger': 'strong', 'best': 'best'}
SPELLINGS = {'oddz': 'odds', 'ods': 'odds', 'odds': 'odds', 'fotball': 'football',
             'odss': 'odds', 'odd': 'odds',
             'footbal': 'football', 'baskeball': 'basketball', 'bball': 'basketball',
             'strng': 'strong', 'saef': 'safe', 'safte': 'safe', 'prem': 'premier'}


def normalize_text(value):
    text = unicodedata.normalize('NFKC', str(value)).lower().replace('’', "'")
    text = re.sub(r'\bover\s*/\s*under\b', 'over/under', text)
    text = re.sub(r'\bdouble[- ]+chance\b', 'double chance', text)
    vocabulary = {'football', 'basketball', 'tennis', 'baseball', 'odds', 'picks', 'selections',
                  'matches', 'strong', 'strongest', 'safest'}
    def token(match):
        word = match.group()
        if word in NUMBERS: return NUMBERS[word]
        if word in SPELLINGS: return SPELLINGS[word]
        # Conservative single-token correction, never a whole-sentence whitelist
        # or fuzzy competition/fixture identification.
        close = get_close_matches(word, vocabulary, n=2, cutoff=.86) if len(word) >= 5 else []
        return close[0] if len(close) == 1 else word
    text = re.sub(r'\b[a-z]+\b', token, text)
    text = re.sub(r'(\d)\s*[,;:]\s*(odds|picks?|games?|matches?)\b', r'\1 \2', text)
    return ' '.join(text.split())


def competition_id(*values):
    for raw in values:
        if not isinstance(raw, str): continue
        canonical = raw.lower().strip()
        if canonical in BY_ID: return canonical
        found = lookup(raw)
        if found: return found.id
        words = re.sub(r'[^a-z0-9]+', ' ', canonical).strip()
        if words in LEAGUES: return LEAGUES[words]
        # League shorthand in stored rows is unambiguous, unlike user sport aliases.
        if words in ('nba', 'nfl', 'mlb', 'atp', 'wta'): return words
    return None


def parse(text, supported_sports=(), allowed_sports=None):
    from predbot import custom_picks as cp
    # The deployment's actually-ENABLED sports, independent of what the
    # parser merely RECOGNIZES by name (supported_sports only ever widens
    # recognition - see test_custom_picks.py's 'hockey' case). Defaults to
    # Config's own ALLOWED_SPORTS when no live cfg is passed, so a bare
    # parse_request(text) call (most unit tests) still behaves like
    # production. Real callers pass their own cfg.allowed_sports.
    enabled = set(cp.enabled_sports(allowed_sports if allowed_sports is not None else cp._default_allowed_sports()))
    if not str(text).strip() or len(str(text)) > 512:
        raise cp.RequestError(cp.HELP)
    text = normalize_text(text)
    if re.search(r'\b(tomorrow|yesterday|next|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b|\b\d{4}-\d{2}-\d{2}\b', text):
        raise cp.RequestError(cp.TODAY_ONLY, 'future_date')
    fields, spans = set(), []
    time_intent = 'tonight' if re.search(r'\b(tonight|this evening)\b', text) else (
        'today' if re.search(r"\btoday(?:'s|s)?\b", text) else None)
    if time_intent: fields.add('time_intent')
    leagues = list(re.finditer(cp.phrase_pattern(LEAGUES), text.replace('-', ' ')))
    competitions = frozenset(LEAGUES[m.group()] for m in leagues)
    spans.extend(m.span() for m in leagues)
    if competitions: fields.add('competitions')
    aliases = {k.replace('_', ' '): k for k in set(cp.SPORT_LABEL) | set(supported_sports)}
    aliases.update({k: k for k in set(cp.SPORT_LABEL) | set(supported_sports)})
    aliases.update(cp.SPORT_ALIASES)
    sport_matches = list(re.finditer(cp.phrase_pattern(aliases), text))
    if any(re.search(r'\b(no|without|avoid|exclude)\s*$', text[:m.start()]) for m in sport_matches):
        raise cp.RequestError('Which sports would you like to include?')
    sports = frozenset(aliases[m.group()] for m in sport_matches)
    if sports: fields.add('sports')
    disabled = sorted(s for s in sports if s in cp.RECOGNIZED_SPORTS and s not in enabled)
    if disabled:
        raise cp.RequestError(
            f'We do not cover {cp.SPORT_LABEL[disabled[0]]} yet. Please choose one of our supported sports.\n\n'
            f'Supported sports: {cp.enabled_sports_label(enabled)}',
            'unsupported_sport')
    if re.search(r'\ball sports\b', text):
        if sports: raise cp.RequestError(cp.HELP, 'conflicting_constraints')
        sports = frozenset()
        fields.add('sports')
    if competitions:
        league_sports = {BY_ID[c].sport for c in competitions}
        if sports and not league_sports <= sports:
            raise cp.RequestError('Those leagues and sports don’t match. Which should I use?', 'conflicting_constraints')
    for word, label in cp.UNSUPPORTED_SPORTS.items():
        if word in aliases:
            continue  # never shadow a genuinely supported alias
        if re.search(cp.phrase_pattern({word}), text):
            raise cp.RequestError(
                f'We do not cover {label} yet. Please choose one of our supported sports.\n\n'
                f'Supported sports: {cp.enabled_sports_label(enabled)}',
                'unsupported_sport')
    number = r'([+-]?\d+(?:\.\d+)?)'
    targets, counts = [], []
    for pattern in (r'(?<![\w.])' + number + r'\s*(?:(?:combined|total)\s+)?odds\b',
                    r'\bodds\s*(?:(?:of|around|about|near|like)\s+|[:=]\s*)?' + number + r'(?![\w.])'):
        for m in re.finditer(pattern, text):
            if any(a <= m.start(1) < b for a, b in (x.span() for x in leagues)):
                continue  # A competition's number is never an odds target.
            targets.append(float(m.group(1))); spans.append(m.span())
    qualifiers = cp.phrase_pattern(set(aliases) | set(QUALITY) | {'and'})
    for m in re.finditer(r'(?<![\w.])' + number + r'\s+(?:' + qualifiers + r'\s+)*(?:picks?|games?|matches?|selections?)\b', text):
        if any(a <= m.start(1) < b for a, b in (x.span() for x in leagues)):
            continue
        counts.append(float(m.group(1))); spans.append(m.span())
    if not counts:
        for m in re.finditer(cp.phrase_pattern(QUALITY) + r'\s+' + number + r'(?![\w.])', text):
            if not any(a <= m.start(1) < b for a, b in spans):
                counts.append(float(m.group(1))); spans.append(m.span())
    if len(set(targets)) > 1 or len(set(counts)) > 1:
        raise cp.RequestError('Which odds target and number of picks should I use?', 'conflicting_constraints')
    target, count = (targets[0] if targets else None), (counts[0] if counts else None)
    if target is not None:
        if not math.isfinite(target) or target <= 1:
            raise cp.RequestError('What combined odds would you like? Choose a number above 1.')
        fields.add('target_odds')
    if count is not None:
        if not count.is_integer() or not 1 <= count <= cp.MAX_LEGS:
            raise cp.RequestError(f'I can offer 1–{cp.MAX_LEGS} different games. How many would you like?')
        fields.add('pick_count')
    masked = list(text)
    for a, b in spans: masked[a:b] = ' ' * (b-a)
    market_text = ''.join(masked)
    lines = list(re.finditer(r'\b(over|under)\s*' + number + r'(?![\w.])', market_text))
    if len({(m.group(1), m.group(2)) for m in lines}) > 1:
        raise cp.RequestError('Which over/under selection would you like?', 'conflicting_constraints')
    side = line = None
    allowed, excluded = set(), set()
    if lines:
        m = lines[0]
        if re.search(r'\b(no|without|avoid)\s*$', text[:m.start()]):
            raise cp.RequestError('Which markets would you like instead?')
        side, line = m.group(1), float(m.group(2))
        if not math.isfinite(line) or line < 0: raise cp.RequestError(cp.HELP)
        allowed.add('total')
        fields.update(('allowed_markets', 'market_selection', 'market_line'))
        spans.extend(m.span() for m in lines)
    previous_end, negative = 0, False
    for m in re.finditer(cp.phrase_pattern(cp.MARKET_ALIASES), market_text):
        prefix = market_text[previous_end:m.start()]
        negative = bool(re.search(r"\b(?:(?:no|without|avoid|exclude)\s*(?:(?:any|more|all)\s+)?|"
                                  r"(?:don't|do not)\s+(?:want|include|use)\s+)$", prefix)) or (
            negative and bool(re.fullmatch(r'[\s,]+(?:(?:and|or)\s+)?', prefix)))
        (excluded if negative else allowed).add(cp.MARKET_ALIASES[m.group()])
        spans.append(m.span()); previous_end = m.end()
    if allowed & excluded: raise cp.RequestError(cp.HELP, 'conflicting_constraints')
    if allowed: fields.add('allowed_markets')
    if excluded: fields.add('excluded_markets')
    preferences = {QUALITY[m.group()] for m in re.finditer(cp.phrase_pattern(QUALITY), text)}
    preference = next((p for p in ('safe', 'strong', 'best') if p in preferences), 'best')
    if preferences: fields.add('preference')
    remaining = list(text)
    for a, b in spans: remaining[a:b] = ' ' * (b-a)
    remaining = re.sub(cp.phrase_pattern(aliases), ' ', ''.join(remaining))
    if re.search(r'\b(league|cup|liga)\b', remaining):
        raise cp.RequestError('I don’t recognize that league. Which competition would you like?', 'unsupported_competition')
    if re.search(r'(?<![\w.])\d+(?:\.\d+)?(?![\w.])', remaining):
        raise cp.RequestError('Is that number your odds target or the number of picks?')
    general = bool(re.search(r'\b(picks?|games?|matches?|selections?)\b', text))
    if not fields - {'time_intent'} and not general:
        raise cp.RequestError(cp.HELP)
    if not target and not count and not allowed and not excluded and re.search(r'\b(scores?|results?|news|weather|payment)\b', text):
        raise cp.RequestError(cp.HELP)
    refinement = bool(re.search(r'\b(make|change|switch|instead|same|safer|stronger)\b', text)) or (
        not target and not count and bool(fields) and not re.search(r'\b(give|find|research|check|need|want)\b', text))
    return cp.Request(target, int(count) if count is not None else None, sports,
        frozenset(allowed), frozenset(excluded), preference, competitions, side, line, time_intent,
        frozenset(fields), refinement, bool(re.search(r'\b(safer|stronger)\b', text)))
