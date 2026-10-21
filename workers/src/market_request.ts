/** Explicit request coordinates only. Availability is decided by the archive.
 * Python counterpart: predbot/market_request.py; tested with common cases. */
export interface MarketScope {
  period?: string;
  stat?: string;
  entity?: string;
  selection?: string;
  line?: number;
}

export function coordinates(text: string): { text: string; scope: MarketScope; market?: string } {
  const scope: MarketScope = {};
  const spans: [number, number][] = [];
  const words: Record<string, number> = { first: 1, second: 2, third: 3, fourth: 4, fifth: 5, sixth: 6, seventh: 7, eighth: 8, ninth: 9, tenth: 10 };
  function put(key: keyof MarketScope, value: string | number) {
    if (scope[key] != null && scope[key] !== value) throw new Error('Conflicting market coordinates. Choose one exact market.');
    Object.assign(scope, { [key]: value });
  }
  for (const m of text.matchAll(/\b(first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|\d+(?:st|nd|rd|th)?)[ -]+(half|quarter|set|innings?)\b/g)) {
    if ([...text.matchAll(/\bfirst[ -]+5[ -]+innings\b/g)].some((x) => x.index! <= m.index! && m.index! < x.index! + x[0].length)) continue;
    const n = words[m[1]] ?? Number.parseInt(m[1], 10);
    const [suffix, limit] = m[2] === 'half' ? ['h', 2] : m[2] === 'quarter' ? ['q', 4] : m[2] === 'set' ? ['s', 5] : ['i', 9];
    if (n > Number(limit)) throw new Error('That period is unsupported. Choose a supported market period.');
    put('period', `${n}${suffix}`); spans.push([m.index!, m.index! + m[0].length]);
  }
  for (const m of text.matchAll(/\b([1-9])([hqs])\b|\b([hq])([1-9])\b/g)) {
    const n = Number(m[1] ?? m[4]), unit = m[2] ?? m[3];
    if (n > ({h:2,q:4,s:5}[unit]!)) throw new Error('That period is unsupported. Choose a supported market period.');
    put('period', `${n}${unit}`); spans.push([m.index!,m.index!+m[0].length]);
  }
  for (const m of text.matchAll(/\b(?:first[ -]+(?:five|5)[ -]+innings|1i5|full[ -]+(?:match|game)|match[ -]+(?:goals|games|sets)|regulation)\b/g)) {
    put('period', m[0].includes('inning') || m[0] === '1i5' ? '1i5' : m[0] === 'regulation' ? 'reg' : 'full');
    spans.push([m.index!, m.index! + (m[0].startsWith('match ') ? 6 : m[0].length)]);
  }
  if (/\bhome[ -]+runs?\b/.test(text)) throw new Error('Home-run totals are unavailable until their model and settlement contract are verified.');
  if (/\b(shots|fouls|rebounds|assists|strikeouts|touchdowns|punts|player props?|odd[ /_-]+even|winning margins?)\b/.test(text)) throw new Error('That market statistic is not available in the validated inventory.');
  if (/\b(correct[ _-]+score|result[ _-]+(?:total|btts)|result\s*(?:and|&)\s*(?:totals?|btts)|clean[ _-]+sheets?|win[ _-]+to[ _-]+nil|team[ _-]+to[ _-]+score|ht[ /_-]+ft|half[ -]*time[ /_-]+full[ -]*time)\b/.test(text)) throw new Error('That market family is not available in the verified shared inventory yet.');
  for (const m of text.matchAll(/\b(?:btts|both teams to score)\s+(yes|no)\b/g)) {
    put('selection', m[1]); spans.push([m.index!+m[0].length-m[1].length,m.index!+m[0].length]);
  }
  for (const m of text.matchAll(/\b(home|away)[ -]+team\b|\b(home|away)(?=\s+(?:over|under|total)\b)|\b(match)[ -]+total\b/g)) {
    put('entity', m[1] ?? m[2] ?? 'all'); spans.push([m.index!, m.index! + m[0].length]);
  }
  let market: string | undefined;
  const handicap = /\b(home|away)(?:[ -]+team)?(?:\s+(?:handicap|spread))?\s*([+-]\d+(?:\.\d+)?)\b/.exec(text);
  if (handicap) {
    if (scope.entity != null && scope.entity !== handicap[1]) throw new Error('Conflicting handicap sides. Choose home or away.');
    delete scope.entity;
    put('selection', handicap[1]); put('line', Number(handicap[2])); put('entity', 'all');
    market = 'handicap'; spans.push([handicap.index, handicap.index + handicap[0].length]);
  } else if (/\b(handicaps?|spreads?)\b/.test(text) && /(?<![\w.])[+-]\d+(?:\.\d+)?\b/.test(text)) {
    throw new Error('Which side should receive that handicap: home or away?');
  }
  function masked() {
    const chars = [...text];
    for (const [a, b] of spans) for (let i = a; i < b; i++) chars[i] = ' ';
    return chars.join('');
  }
  const visible = masked();
  const stats: Record<string, string> = { goals: 'points', points: 'points', runs: 'points', games: 'games', sets: 'sets', corners: 'corners', cards: 'cards' };
  for (const m of visible.matchAll(/\b(goals|points|runs|games|sets|corners|cards)\b/g)) {
    if (['games', 'sets'].includes(m[0]) && !(
      /\b(?:match|total|totals)\s*$/.test(text.slice(0, m.index)) ||
      /\b(?:over|under)\s*\d+(?:\.\d+)?\s*$/.test(text.slice(0, m.index)) ||
      /^\s+(?:over|under)\b/.test(text.slice(m.index! + m[0].length)))) continue;
    put('stat', stats[m[0]]); spans.push([m.index!, m.index! + m[0].length]);
  }
  let clean = masked().replace(/\s+/g, ' ').trim();
  if (market === 'handicap' && !/\b(handicaps?|spreads?)\b/.test(clean)) clean += ' handicap';
  if (Object.keys(scope).length && !/\b(total|totals|over|under|handicaps?|spreads?|btts|winner|moneyline|win|double chance|draw no bet|dnb|dc|1x2|match result|correct score|result_total|clean sheet|team to score|win to nil)\b/.test(clean)) clean += ' total';
  return { text: clean, scope, market };
}
