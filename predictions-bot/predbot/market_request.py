"""Explicit Custom Picks coordinates, independent of ticket construction.

Only describes a request. Availability always comes from the validated archive;
no period scaling, nearby-line substitution, or probability creation here.
Keep the Worker port in market_request.ts covered by the same parity cases.
"""
import re


def coordinates(text):
    scope, spans = {}, []
    ordinal = r'(first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|\d+(?:st|nd|rd|th)?)'
    words = dict(zip(('first','second','third','fourth','fifth','sixth','seventh','eighth','ninth','tenth'),range(1,11)))
    def put(key, value):
        if key in scope and scope[key] != value:
            raise ValueError('Conflicting market coordinates. Choose one exact market.')
        scope[key] = value
    for m in re.finditer(r'\b' + ordinal + r'[ -]+(half|quarter|set|innings?)\b', text):
        if any(a <= m.start() < b for a, b in (x.span() for x in re.finditer(r'\bfirst[ -]+5[ -]+innings\b', text))):
            continue
        n = words.get(m[1]) or int(re.match(r'\d+', m[1])[0])
        unit = m[2]
        suffix, limit = ('h', 2) if unit == 'half' else ('q', 4) if unit == 'quarter' else ('s', 5) if unit == 'set' else ('i', 9)
        if n > limit:
            raise ValueError('That period is unsupported. Choose a supported market period.')
        put('period', f'{n}{suffix}')
        spans.append(m.span())
    for m in re.finditer(r'\b([1-9])([hqs])\b|\b([hq])([1-9])\b', text):
        n, unit = (int(m[1]), m[2]) if m[1] else (int(m[4]), m[3])
        if n > {'h':2,'q':4,'s':5}[unit]:
            raise ValueError('That period is unsupported. Choose a supported market period.')
        put('period', f'{n}{unit}'); spans.append(m.span())
    for m in re.finditer(r'\b(?:first[ -]+(?:five|5)[ -]+innings|1i5|full[ -]+(?:match|game)|match[ -]+(?:goals|games|sets)|regulation)\b', text):
        period = '1i5' if 'inning' in m[0] or m[0] == '1i5' else 'reg' if m[0] == 'regulation' else 'full'
        put('period', period)
        # Keep the statistic in "match games" for the stat scanner below.
        spans.append(m.span() if not m[0].startswith('match ') else (m.start(), m.start()+6))
    if re.search(r'\bhome[ -]+runs?\b', text):
        raise ValueError('Home-run totals are unavailable until their model and settlement contract are verified.')
    if re.search(r'\b(shots|fouls|rebounds|assists|strikeouts|touchdowns|punts|player props?|odd[ /_-]+even|winning margins?)\b', text):
        raise ValueError('That market statistic is not available in the validated inventory.')
    for m in re.finditer(r'\b(?:btts|both teams to score)\s+(yes|no)\b', text):
        put('selection', m[1]); spans.append((m.end()-len(m[1]),m.end()))
    for m in re.finditer(r'\b(home|away)[ -]+team\b|\b(home|away)(?=\s+(?:over|under|total)\b)|\b(match)[ -]+total\b', text):
        put('entity', m[1] or m[2] or 'all')
        spans.append(m.span())
    # A bare home/away followed by a signed line is an unambiguous handicap.
    handicap = re.search(r'\b(home|away)(?:[ -]+team)?(?:\s+(?:handicap|spread))?\s*([+-]\d+(?:\.\d+)?)\b', text)
    if handicap:
        if scope.get('entity') not in (None, handicap[1]):
            raise ValueError('Conflicting handicap sides. Choose home or away.')
        scope.pop('entity', None)
        put('selection', handicap[1]); put('line', float(handicap[2])); put('entity', 'all')
        put('market', 'handicap'); spans.append(handicap.span())
    elif re.search(r'\b(handicaps?|spreads?)\b', text):
        signed = re.search(r'(?<![\w.])([+-]\d+(?:\.\d+)?)\b', text)
        if signed:
            raise ValueError('Which side should receive that handicap: home or away?')
    # Do not interpret a pick count ("3 games") as the statistic games.
    masked = list(text)
    for a, b in spans:
        masked[a:b] = ' ' * (b-a)
    visible = ''.join(masked)
    stat_words = {'goals': 'points', 'points': 'points', 'runs': 'points',
                  'games': 'games', 'sets': 'sets', 'corners': 'corners', 'cards': 'cards'}
    for m in re.finditer(r'\b(goals|points|runs|games|sets|corners|cards)\b', visible):
        if m[0] in ('games', 'sets') and not (
                re.search(r'\b(?:match|total|totals)\s*$', text[:m.start()]) or
                re.search(r'\b(?:over|under)\s*\d+(?:\.\d+)?\s*$', text[:m.start()]) or
                re.match(r'\s+(?:over|under)\b', text[m.end():])):
            continue
        put('stat', stat_words[m[0]]); spans.append(m.span())
    masked = list(text)
    for a, b in spans:
        masked[a:b] = ' ' * (b-a)
    clean = ' '.join(''.join(masked).split())
    if scope.get('market') == 'handicap' and not re.search(r'\b(handicaps?|spreads?)\b', clean):
        clean += ' handicap'
    if scope and not re.search(r'\b(total|totals|over|under|handicaps?|spreads?|btts|winner|moneyline|win|double chance|draw no bet|dnb|dc|1x2|match result|correct score|result_total|clean sheet|team to score|win to nil)\b', clean):
        clean += ' total'
    return clean, scope
