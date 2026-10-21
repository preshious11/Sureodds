import { describe, it, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import {
  parseRequest, scoped, normalizeCandidate, serializeRequest, deserializeRequest,
  refine, availableTotalLines, clearTotalLine, respond, __resetPoolCacheForTests,
} from '../src/custom_picks.ts';
import { buildProTicket, buildProPicks } from '../src/pro_picks.ts';

beforeEach(() => __resetPoolCacheForTests());

const cases = JSON.parse(readFileSync(new URL('../../predictions-bot/tests/fixtures/market_requests.json', import.meta.url)));
const day = '2030-06-01', zone = 'Africa/Lagos';
const now = Date.parse('2030-06-01T12:00:00Z');
function payload(c, overrides = {}) {
  return { ...c, canonical_event_id: 'fixture', prediction_date: day, timezone: zone,
    generated_at: '2030-06-01T12:00:00Z', start_utc: '2030-06-01T20:00:00Z',
    home: 'Arsenal', away: 'Chelsea', price: 1.5, p_model: .8,
    sport: c.text.includes('basketball') ? 'basketball' : c.text.includes('tennis') ? 'tennis' : 'football',
    // Clears pro_picks.ts's own PRO_MIN_BOOKS (3) by default so these fixtures
    // (shared with Custom Picks' own tests, which never gate on this field)
    // also work for Pro Picks assertions below.
    bookmaker_count: 3,
    ...overrides };
}
function normalized(c, overrides = {}) {
  return normalizeCandidate(payload(c, overrides), day, zone, now, now);
}
function request(text) {
  const p = parseRequest(text);
  assert.equal(p.ok, true, p.text);
  return p.request;
}

describe('exact coordinates shared with Python', () => {
  for (const c of cases) it(c.text, () => {
    const req = request(c.text), candidate = normalized(c);
    assert.ok(candidate);
    assert.ok(scoped(candidate, req));
    const restored = deserializeRequest(JSON.parse(JSON.stringify(serializeRequest(req))));
    assert.ok(scoped(candidate, restored));
    for (const [key, value] of [['period', c.period === 'full' ? '1h' : 'full'],
      ['stat', c.stat === 'games' ? 'sets' : 'games'], ['entity', c.entity === 'home' ? 'away' : 'home']]) {
      assert.equal(scoped({ ...candidate, [key]: value }, restored), false, key);
    }
    if (req.totalLine || req.marketScope?.line != null) {
      assert.equal(scoped({ ...candidate, line: c.line + .5 }, req), false);
      assert.equal(scoped({ ...candidate, selection: 'not-the-requested-side' }, req), false);
    }
  });
});

it('scope survives quality refinement and line clarification', () => {
  let req = refine(request('home team over 1.5 goals'), request('make it safer'));
  assert.equal(req.marketScope.entity, 'home');
  req = refine(req, request('first half instead'));
  assert.equal(req.marketScope.entity, 'home');
  assert.equal(req.marketScope.period, '1h');
  assert.equal(req.totalLine.line, 1.5);
  const other = refine(req, request('btts instead'));
  assert.equal(other.marketScope, null);
  assert.equal(other.totalLine, null);
});

it('line alternatives preserve team, period and statistic and reject wrong scope', () => {
  const req = request('first-half home team over 1.5 goals');
  const home = normalized(cases[1], { period: '1h', line: 2.5 });
  const match = normalized(cases[0], { period: '1h' });
  const full = normalized(cases[1], { line: 2.5 });
  const available = availableTotalLines([home, match, full], req, now);
  assert.deepEqual(available, [{ selection: 'over', line: 2.5 }]);
  const next = clearTotalLine(req);
  assert.ok(scoped(home, next));
  assert.equal(scoped(match, next), false);
  assert.deepEqual(availableTotalLines([home], request('away +1.5 handicap'), now), []);
});

it('old serialized requests without new coordinates remain compatible', () => {
  const raw = serializeRequest(request('over 2.5'));
  delete raw.marketScope;
  const req = deserializeRequest(raw);
  assert.ok(scoped(normalized(cases[0]), req));
  assert.equal(scoped(normalized(cases[0], { entity: 'home' }), req), false);
});

it('known ambiguous legacy archive contracts are never supplied to Custom or Pro', () => {
  for (const market_name of ['1St Inning Total Runs', 'Total Home Runs', 'Total Punts', 'Total Touchdowns']) {
    assert.equal(normalized(cases[0], { market_name }), null);
  }
  const picks = [normalized(cases[1]), normalized(cases[0])];
  const ticket = buildProTicket(picks, now);
  assert.equal(ticket.picks.length, 1, 'same-fixture alternatives must not become two Pro legs');
});

it('a missing exact contract returns unavailable instead of another scope', async () => {
  __resetPoolCacheForTests();
  const deps = {
    getPool: async () => ({ status: 'COMPLETE', generated_at: '2030-06-01T12:00:00Z' }),
    getCandidates: async () => [{ payload_json: JSON.stringify(payload(cases[0])) }],
  };
  const { result } = await respond(deps, '1 picks home team over 2.5 goals', zone, day, now, null);
  assert.equal(result.reason, 'line_unavailable');
  assert.doesNotMatch(result.text, /Arsenal vs Chelsea/);
});

it('Custom and Pro share the same disabled-family inventory guard', async () => {
  const deps = {
    getPool: async () => ({status:'COMPLETE',generated_at:'2030-06-01T12:00:00Z'}),
    getCandidates: async () => [{payload_json:JSON.stringify(payload(cases[0],{
      market:'odd_even',selection:'odd',p_model:.95,line:null,
    }))}],
  };
  const result=await buildProPicks(deps,day,zone,now);
  assert.equal(result.reason,'no_candidates');
  const custom=await respond(deps,'1 picks',zone,day,now,null);
  assert.notEqual(custom.result.reason,'success');
});

for (const text of ['third half over 1.5 goals', 'home team away +1.5 handicap', 'over 2.5 home runs', '+1.5 handicap']) {
  it(`rejects ambiguous coordinates: ${text}`, () => assert.equal(parseRequest(text).ok, false));
}

it('preserves known tennis games aliases and explicit BTTS side', () => {
  const req=request('tennis match games over 21.5');
  const c=normalized(cases[5], {stat:'points'});
  assert.ok(scoped(c,req));
  assert.equal(scoped({...c,stat:'sets'},req),false);
  assert.equal(request('btts no').marketScope.selection,'no');
  assert.equal(request('first 5 innings over 4.5 runs').marketScope.period,'1i5');
  assert.equal(request('1h over 0.5 goals').marketScope.period,'1h');
  assert.equal(request('home over 1.5 goals').marketScope.entity,'home');
});
