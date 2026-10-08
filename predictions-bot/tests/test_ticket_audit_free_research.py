"""Phase 2C.2 regression firewall. No external HTTP in pytest."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import timedelta
import ast
import base64
import json
from pathlib import Path
import sqlite3
import struct
import threading
from unittest.mock import Mock
import zlib

import pytest
import requests

import diagnose_gemini_audit as diag
from predbot.ticket_audit.analysis import EvidenceAnalyzer
from predbot.ticket_audit.free_research import FreeResearchResolver, OpenFootballAdapter, PublicSourceBudget
from predbot.ticket_audit.gemini import Settings
from predbot.ticket_audit.model_access import DEFAULT_MODEL
from predbot.ticket_audit.research import ResearchCache, research_key
from predbot.ticket_audit.schemas import (AuditUnavailable, LocalEvidenceMatch,
    ticket_from_json, ticket_json_schema)
from predbot.ticket_audit.service import AuditService
from tests.test_ticket_audit_foundation import (no_providers, local, selection,
    ticket, client, response, NOW, evidence, db_dump)


def source_data(s=None):
    s = s or selection()
    matches = [{'date': '2030-06-01', 'team1': s.home, 'team2': s.away}]
    for i in range(5):
        matches.extend([
            {'date': f'2030-05-{25-i:02}', 'team1': s.home, 'team2': f'Opponent {i}', 'score': {'ft': [3, 1]}},
            {'date': f'2030-05-{25-i:02}', 'team1': f'Other {i}', 'team2': s.away, 'score': {'ft': [2, 1]}}])
    return {'name': 'English Premier League 2029/30', 'matches': matches}


def setup_source(tmp_path, data=None, status=200, error=None, budget=None):
    transport = Mock()
    r = Mock(status_code=status)
    r.iter_content.return_value = [json.dumps(data if data is not None else source_data()).encode()]
    transport.get.return_value = r
    if error:
        transport.get.side_effect = error
    b = budget or PublicSourceBudget(tmp_path / 'sources.sqlite3')
    adapter = OpenFootballAdapter(b, transport)
    return FreeResearchResolver([adapter]), adapter, transport


def missing_resolver():
    r = Mock()
    r.resolve.return_value = LocalEvidenceMatch('MISSING')
    return r


def free_client(tmp_path, text, error=None):
    return client(tmp_path, response(text), error=error,
        settings=Settings('MOCK-SECRET', DEFAULT_MODEL, True, True))


def extracted_payload(input_type='text'):
    legs = [asdict(selection(sport=None, competition=None, kickoff=None, market_family=None,
        selection='home', line=None, submitted_odds=1.75, period=None, stat=None, entity=None)),
        asdict(selection(sport=None, competition=None, home='Barcelona', away='Sevilla', kickoff=None,
        submitted_odds=1.62, period=None, stat=None, entity=None))]
    return {'input_type': input_type, 'source_bookmaker': None, 'source_code': None,
        'submitted_combined_odds': 2.84, 'selections': legs, 'extraction_confidence': 'HIGH', 'extraction_warnings': []}


def test_minimal_and_nested_shapes_match_current_rest_contract(tmp_path):
    c = free_client(tmp_path, '{"status":"OK"}')
    assert diag.validate_structured(c)['structured_valid']
    body = c.transport.request.call_args.kwargs['json']
    text = body['generationConfig']['responseFormat']['text']
    assert text == {'mimeType': 'APPLICATION_JSON', 'schema': {
        'type': 'object', 'properties': {'status': {'type': 'string'}}, 'required': ['status']}}
    assert 'responseMimeType' not in body['generationConfig'] and 'responseJsonSchema' not in body['generationConfig']
    c.transport.request.return_value.json.return_value = response('{"items":[{"status":"OK","missing":null}]}')
    assert diag.validate_schema_shapes(c)['schema_shapes_valid']
    assert c.transport.request.call_count == 2


@pytest.mark.parametrize('kind', ['text', 'image'])
def test_natural_ticket_extraction_preserves_all_missing_fields(tmp_path, kind):
    c = free_client(tmp_path, json.dumps(extracted_payload(kind)))
    png = diag.synthetic_image(diag.NATURAL_TEXT_SLIP)
    result = c.extract(diag.NATURAL_TEXT_SLIP) if kind == 'text' else c.extract(image=('image/png', png))
    assert diag.validate_ticket(result, kind, natural=True)['unknowns_preserved']
    body = c.transport.request.call_args.kwargs['json']
    assert body['contents'][0]['role'] == 'user' and 'tools' not in body
    assert 'maxItems' not in body['generationConfig']['responseFormat']['text']['schema']['properties']['selections']
    if kind == 'image':
        inline = body['contents'][0]['parts'][1]['inlineData']
        assert inline['mimeType'] == 'image/png' and base64.b64decode(inline['data'], validate=True) == png


def test_wire_schema_and_parser_have_no_product_selection_limit():
    assert 'maxItems' not in ticket_json_schema()['properties']['selections']
    p = extracted_payload()
    p['selections'] = [p['selections'][0]] * 100
    assert len(ticket_from_json(p).selections) == 100
    p['selections'].append(p['selections'][0])
    assert len(ticket_from_json(p).selections) == 101


def test_synthetic_image_is_valid_png_not_just_signature():
    p = diag.synthetic_image(diag.NATURAL_TEXT_SLIP)
    at, compressed, dimensions = 8, b'', None
    while at < len(p):
        n = struct.unpack('>I', p[at:at+4])[0]
        kind, payload = p[at+4:at+8], p[at+8:at+8+n]
        assert zlib.crc32(kind + payload) & 0xffffffff == struct.unpack('>I', p[at+8+n:at+12+n])[0]
        if kind == b'IHDR': dimensions = struct.unpack('>IIBBBBB', payload)
        if kind == b'IDAT': compressed += payload
        at += n + 12
    width, height, depth, colour, *_ = dimensions
    assert depth == 8 and colour == 0 and len(zlib.decompress(compressed)) == (width + 1) * height


@pytest.mark.parametrize('bad', ['not JSON', '{}', '[]', '{"selections":[]}'])
def test_bad_extraction_never_retries_or_researches(tmp_path, bad):
    c = free_client(tmp_path, bad)
    with pytest.raises(AuditUnavailable): c.extract(diag.NATURAL_TEXT_SLIP)
    assert c.transport.request.call_count == 1


@pytest.mark.parametrize('error', [requests.Timeout('secret'), requests.ConnectionError('secret')])
def test_gemini_transport_failure_no_fallback(tmp_path, error):
    c = free_client(tmp_path, '', error)
    with pytest.raises(AuditUnavailable) as exc: c.extract(diag.NATURAL_TEXT_SLIP)
    assert 'secret' not in str(exc.value) and c.transport.request.call_count == 1


def test_http400_no_retry_and_sanitized(tmp_path):
    c = free_client(tmp_path, '')
    c.transport.request.return_value = Mock(status_code=400, json=Mock(return_value={
        'error': {'status': 'INVALID_ARGUMENT', 'message': 'Request contains an invalid argument.'}}))
    with pytest.raises(AuditUnavailable): c.extract(diag.NATURAL_TEXT_SLIP)
    assert c.transport.request.call_count == 1 and c.usage.snapshot(NOW)['failures'] == 1


@pytest.mark.parametrize('schema',[{}, {'type':'bad'}, {'type':'object','$ref':'remote'},
    {'type':'object','required':['missing']}, {'type':'array','items':False},
    {'type':'array','maxItems':True}, {'type':'string','pattern':'unreviewed'}])
def test_bad_schema_rejected_before_http_or_quota(tmp_path,schema):
    c=free_client(tmp_path,'unused')
    with pytest.raises(AuditUnavailable,match='unsupported_structured_schema'):c.generate('test',schema)
    c.transport.request.assert_not_called()
    assert c.usage.snapshot(NOW)['requests']==0


@pytest.mark.parametrize('image',[('image/png',b'notPNG'),('image/jpeg',b'notJPEG'),
    ('image/gif',b'GIF89a'),('image/png',b''),('image/png',b'x'*5_000_001)])
def test_bad_images_never_consume_requests(tmp_path,image):
    c=free_client(tmp_path,'unused')
    with pytest.raises(AuditUnavailable,match='invalid_image_input'):c.extract(image=image)
    c.transport.request.assert_not_called()


@pytest.mark.parametrize('changes', [{'sport': s} for s in ('tennis', 'basketball', 'baseball', 'american_football', 'mma')]
    + [dict(competition='Championship'), dict(period='first'), dict(stat='corners'), dict(entity='home'),
       dict(home='Arsenal Women'), dict(home='Arsenal FC'), dict(kickoff=None), dict(selection='bad'), dict(line=None)])
def test_unsupported_or_wrong_fixture_never_falls_back(tmp_path, changes):
    r, _, t = setup_source(tmp_path)
    s = selection(**changes)
    with pytest.raises(AuditUnavailable): r.research(s, NOW)
    # Wrong exact names require one permitted dataset request to establish absence.
    assert t.get.call_count <= (1 if 'home' in changes else 0)


@pytest.mark.parametrize('market,selected,line,category', [('total','over',2.5,'totals'),
    ('btts','yes',None,'both_score'), ('result3','home',None,'form'), ('dc','1x',None,'form'),
    ('dnb','away',None,'form'), ('handicap','home',-1.5,'margin')])
def test_market_evidence_and_provenance(tmp_path, market, selected, line, category):
    r, a, t = setup_source(tmp_path)
    s = selection(market_family=market, selection=selected, line=line)
    e = r.research(s, NOW)
    assert {f.category for f in e.facts} == {category}
    assert all(f.source == a.source_id and f.source_url == a.url(s) and f.event_date and
        f.observed_at == NOW.isoformat() and f.freshness == 'RECENT' and f.source_quality == 'C' for f in e.facts)
    assert e.research_confidence == 'LIMITED' and e.grounding_metadata == {} and not e.interpretations
    assert not any(f.category in ('injury', 'availability', 'probability') for f in e.facts)
    assert e.verdict == 'INSUFFICIENT_EVIDENCE' and 'sha256' in e.retrieval_metadata
    assert 'rows' not in e.retrieval_metadata
    opts = t.get.call_args.kwargs
    assert opts['allow_redirects'] is False and opts['timeout'] == (5,10) and 'Authorization' not in opts['headers']


def test_market_line_date_keys_and_fixture_dataset_reuse(tmp_path):
    r, a, t = setup_source(tmp_path)
    s = selection()
    one = r.research(s, NOW)
    assert r.cached(s, NOW) == one
    for changed in (replace(s, line=3.5), replace(s, selection='under'), replace(s, market_family='btts',selection='yes',line=None)):
        assert research_key(s, a.source_id) != research_key(changed, a.source_id)
        assert r.cached(changed, NOW) is not None  # fixture cache needs no HTTP
    assert t.get.call_count == 1
    assert '3.5' in r.cached(replace(s,line=3.5),NOW).facts[0].claim
    assert r.cached(replace(s,home='Arsenal U21'),NOW) is None
    assert r.cached(replace(s,kickoff='2030-06-02T15:00:00Z'),NOW) is None
    assert r.cached(s,NOW+timedelta(hours=3)) is None  # kickoff expires all market data
    with pytest.raises(AuditUnavailable): r.research(s,NOW+timedelta(hours=3))
    assert t.get.call_count == 1


def test_concurrent_markets_singleflight_one_dataset(tmp_path):
    r, _, t = setup_source(tmp_path)
    barrier, entered, release = threading.Barrier(6), threading.Event(), threading.Event()
    original = t.get.return_value
    def get(*args, **kw):
        entered.set()
        assert release.wait(5)
        return original
    t.get.side_effect = get
    def load(i):
        barrier.wait(timeout=5)
        return r.research(selection(line=2.5+i/10),NOW)
    with ThreadPoolExecutor(max_workers=6) as pool:
        fs = [pool.submit(load,i) for i in range(6)]
        assert entered.wait(5)
        release.set()
        values = [f.result(timeout=5) for f in fs]
    assert t.get.call_count == 1 and len(values) == 6
    values[0].fixture['home'] = 'mutated'
    assert r.cached(selection(),NOW).fixture['home'] == 'Arsenal'


def test_local_all_legs_first_cache_then_source_then_analysis(tmp_path, local, db):
    resolver, local_s, _ = local
    source, _, t = setup_source(tmp_path)
    order = []
    original = resolver.resolve
    resolver.resolve = lambda s,n: (order.append('local'), original(s,n))[1]
    source.research(selection(),NOW)  # cached leg, future audit no network
    t.get.reset_mock()
    analyzer = Mock()
    analyzer.analyze_ticket.side_effect = lambda audit,now: (order.append('analysis'),audit)[1]
    before = db_dump(db)
    result = AuditService(resolver,source,analyzer=analyzer).audit(ticket(local_s,selection()),NOW)
    assert order == ['local','local','analysis'] and t.get.call_count == 0
    assert result.selections[0].local_probability is not None and result.selections[1].local_probability is None
    assert result.selections[0].evidence.evidence_source == 'LOCAL'
    assert result.selections[1].evidence.evidence_source == 'RESEARCH'
    assert result.selections[1].verdict == 'INSUFFICIENT_EVIDENCE'
    assert result.overall_verdict == 'RISKY' and db_dump(db) == before


def test_twenty_same_fixture_markets_reuse_one_external_slot(tmp_path):
    source, _, t = setup_source(tmp_path)
    result = AuditService(missing_resolver(),source).audit(ticket(*(selection(line=1+i/10) for i in range(20))),NOW)
    assert t.get.call_count == 1 and all(leg.key_facts for leg in result.selections)
    assert not any('budget exhausted' in lim for leg in result.selections for lim in leg.limitations)


@pytest.mark.parametrize('status', [301,302,401,403,404,429,500])
def test_source_unavailable_is_insufficient_no_redirect_or_retry(tmp_path,status):
    r, _, t = setup_source(tmp_path,status=status)
    result = AuditService(missing_resolver(),r).audit(ticket(),NOW)
    assert result.selections[0].verdict == 'INSUFFICIENT_EVIDENCE'
    assert result.selections[0].local_probability is None
    assert t.get.call_count == 1 and t.get.return_value.close.call_count == 1
    AuditService(missing_resolver(),r).audit(ticket(),NOW)
    assert t.get.call_count == 1  # negative-cache cooldown


@pytest.mark.parametrize('error', [requests.Timeout(),requests.ConnectionError()])
def test_source_timeout_preserves_insufficient_and_no_llm_guess(tmp_path,error):
    r,_,t = setup_source(tmp_path,error=error)
    c = free_client(tmp_path,'unused')
    result = AuditService(missing_resolver(),r,analyzer=EvidenceAnalyzer(c)).audit(ticket(),NOW)
    assert result.overall_verdict == 'INSUFFICIENT_EVIDENCE' and t.get.call_count == 1
    c.transport.request.assert_not_called()


def test_conflicting_records_are_rejected(tmp_path):
    data = source_data()
    data['matches'].append({**data['matches'][1], 'score': {'ft':[0,3]}})
    r,_,_ = setup_source(tmp_path,data)
    result = AuditService(missing_resolver(),r).audit(ticket(),NOW)
    assert result.selections[0].verdict == 'INSUFFICIENT_EVIDENCE'
    assert 'conflicting_public_results' in result.selections[0].limitations


def test_current_public_dataset_supports_both_score_representations(tmp_path):
    data=source_data()
    for row in data['matches'][1::2]: row['score']=row['score']['ft']
    r,_,t=setup_source(tmp_path,data)
    result=r.research(selection(),NOW)
    assert len(result.facts)==10 and {f.value for f in result.facts}=={3,4}
    assert t.get.call_count==1


def test_ambiguous_fixture_and_adapter_routes_fail_closed(tmp_path):
    data = source_data()
    data['matches'].append({**data['matches'][0],'date':'2030-06-02'})
    r,a,t = setup_source(tmp_path,data)
    with pytest.raises(AuditUnavailable,match='public_fixture_ambiguous'): r.research(selection(),NOW)
    another = Mock(source_id='other',supports=Mock(return_value=True))
    r = FreeResearchResolver([a,another])
    with pytest.raises(AuditUnavailable,match='conflicting_source_routes'): r.research(selection(),NOW)
    another.fetch_fixture.assert_not_called()


@pytest.mark.parametrize('change', ['no_scores','future_scores','same_day','empty','invalid_score','wrong_league','wrong_shape'])
def test_unusable_source_data_never_creates_facts(tmp_path,change):
    data = source_data()
    if change == 'no_scores':
        for row in data['matches']: row.pop('score',None)
    elif change == 'future_scores':
        for row in data['matches'][1:]: row['date']='2030-06-02'
    elif change == 'same_day':
        for row in data['matches'][1:]: row['date']='2030-06-01'
    elif change == 'empty': data['matches']=[]
    elif change == 'invalid_score': data['matches'][1]['score']={'ft':[True,2]}
    elif change == 'wrong_league': data['name']='Other League'
    elif change == 'wrong_shape': data['matches']=[None]
    r,_,_ = setup_source(tmp_path,data)
    result=AuditService(missing_resolver(),r).audit(ticket(),NOW)
    assert result.overall_verdict=='INSUFFICIENT_EVIDENCE' and not result.selections[0].key_facts


def test_source_size_and_stream_timeout_bounded(tmp_path,monkeypatch):
    r,a,t=setup_source(tmp_path)
    t.get.return_value.iter_content.return_value=[b'x'*1_000_001]
    with pytest.raises(AuditUnavailable,match='too_large'):r.research(selection(),NOW)
    r,a,t=setup_source(tmp_path)
    times=iter([0,21])
    monkeypatch.setattr('predbot.ticket_audit.free_research.time.monotonic',lambda:next(times))
    with pytest.raises(AuditUnavailable,match='timeout'):r.research(selection(),NOW)


def test_slow_trickle_yields_for_deadline_and_margin_names_its_participant(tmp_path,monkeypatch):
    r,a,t=setup_source(tmp_path)
    e=r.research(selection(market_family='handicap',selection='home',line=-1.5),NOW)
    assert any('Arsenal goal margin 2' in f.claim for f in e.facts)
    assert any('Chelsea goal margin -1' in f.claim for f in e.facts)
    t.get.return_value.iter_content.assert_called_once_with(chunk_size=1)
    r,a,t=setup_source(tmp_path)
    t.get.return_value.iter_content.return_value=[b'{',b' ',b' ']
    times=iter([0,9,18,27])
    monkeypatch.setattr('predbot.ticket_audit.free_research.time.monotonic',lambda:next(times))
    with pytest.raises(AuditUnavailable,match='public_source_timeout'):r.research(selection(),NOW)
    assert t.get.return_value.close.call_count==1


def test_source_budget_survives_restart_and_blocks_before_http(tmp_path):
    path=tmp_path/'budget.sqlite3'
    budget=PublicSourceBudget(path,daily=1,hourly=1)
    r,a,t=setup_source(tmp_path,budget=budget)
    r.research(selection(),NOW)
    r,a,t=setup_source(tmp_path,budget=PublicSourceBudget(path,daily=1,hourly=1))
    with pytest.raises(AuditUnavailable,match='budget_exhausted'):r.research(selection(),NOW)
    t.get.assert_not_called()
    assert PublicSourceBudget(path,daily=1,hourly=1).reserve(a.source_id,NOW+timedelta(days=1)) is None


def test_budget_rejects_prediction_or_google_database(tmp_path,db):
    with pytest.raises(AuditUnavailable,match='unrelated'):PublicSourceBudget(db.path)
    c=free_client(tmp_path,'unused')
    with pytest.raises(AuditUnavailable,match='unrelated'):PublicSourceBudget(tmp_path/'audit-usage.sqlite3')


@pytest.mark.parametrize('url',['https://espn.com/test','http://raw.githubusercontent.com/test',
    OpenFootballAdapter.base+'../../secrets',OpenFootballAdapter.base+'2030-31/other.json'])
def test_unreviewed_urls_never_use_transport(tmp_path,url):
    _,a,t=setup_source(tmp_path)
    with pytest.raises(AuditUnavailable,match='unreviewed'):a._download(url,NOW)
    t.get.assert_not_called()


def analyzed_payload(verdict='INSUFFICIENT_EVIDENCE',facts=None,codes=None):
    return {'legs':[{'index':0,'verdict':verdict,'fact_ids':facts or ['0:0'],
        'risk_codes':codes or ['LIMITED_SOURCE','MISSING_AVAILABILITY']}]}


def test_analysis_only_supplied_evidence_no_fact_or_probability_generation(tmp_path):
    r,_,_=setup_source(tmp_path)
    before=AuditService(missing_resolver(),r).audit(ticket(),NOW)
    c=free_client(tmp_path,json.dumps(analyzed_payload()))
    result=EvidenceAnalyzer(c).analyze_ticket(before,NOW)
    leg=result.selections[0]
    assert leg.key_facts==before.selections[0].key_facts and leg.provenance==before.selections[0].provenance
    assert leg.local_probability is None and leg.market_probability is None and leg.safer_alternative is None
    body=c.transport.request.call_args.kwargs['json'];prompt=body['contents'][0]['parts'][0]['text']
    assert 'Use only the supplied evidence for current factual claims.' in prompt and 'tools' not in body
    assert leg.key_facts[0].claim in prompt and leg.key_facts[0].source_url in prompt
    assert c.transport.request.call_count==1


@pytest.mark.parametrize('change',['probability','injury','alternative','new_fact','wrong_ref','cross_leg_ref',
    'positive_limited','duplicate_leg','missing_leg','extra_leg','bad_code','unsupported_conflict','unsupported_ev'])
def test_analysis_rejects_invented_or_unsupported_output(tmp_path,change):
    r,_,_=setup_source(tmp_path)
    audit=AuditService(missing_resolver(),r).audit(ticket(),NOW)
    data=analyzed_payload();leg=data['legs'][0]
    if change in ('probability','injury','alternative','new_fact'):leg[change]='invented'
    elif change=='wrong_ref':leg['fact_ids']=['0:99']
    elif change=='cross_leg_ref':leg['fact_ids']=['1:0']
    elif change=='positive_limited':leg['verdict']='REASONABLE'
    elif change=='duplicate_leg':data['legs']*=2
    elif change=='missing_leg':data['legs']=[]
    elif change=='extra_leg':data['legs'].append({**leg,'index':1})
    elif change=='bad_code':leg['risk_codes']=['No injuries']
    elif change=='unsupported_conflict':leg['risk_codes']=['CONFLICT']
    elif change=='unsupported_ev':leg['risk_codes']=['NEGATIVE_LOCAL_EV']
    c=free_client(tmp_path,json.dumps(data))
    result=EvidenceAnalyzer(c).analyze_ticket(audit,NOW)
    assert result.selections[0].verdict == audit.selections[0].verdict
    assert result.selections[0].key_facts == audit.selections[0].key_facts
    assert 'Gemini analysis unavailable for this batch' in result.selections[0].limitations
    assert c.transport.request.call_count==1


def test_analysis_failure_keeps_deterministic_leg_assessments(tmp_path,local):
    resolver,s,_=local
    c=free_client(tmp_path,'',requests.Timeout())
    result=AuditService(resolver,analyzer=EvidenceAnalyzer(c)).audit(ticket(s),NOW)
    assert result.selections[0].local_probability is not None
    assert any('analysis unavailable' in lim for a in result.selections for lim in a.limitations)


def test_successful_local_and_mixed_researched_legs_preserve_numeric_metrics(tmp_path,local):
    resolver,s,_=local
    unknown=selection(home='Unknown')
    researcher=Mock()
    researcher.cached.side_effect=lambda sel,now:evidence(sel)
    c=free_client(tmp_path,json.dumps({'legs':[{'index':0,'verdict':'REASONABLE','fact_ids':[],'risk_codes':[]},
        {'index':1,'verdict':'RISKY','fact_ids':['1:0'],'risk_codes':['SMALL_SAMPLE']}]}))
    result=AuditService(resolver,researcher,analyzer=EvidenceAnalyzer(c)).audit(ticket(s,unknown),NOW)
    assert result.selections[0].local_probability is not None and result.selections[1].local_probability is None
    assert result.selections[1].evidence_level=='RESEARCHED' and result.selections[1].verdict=='RISKY'
    researcher.research.assert_not_called()
    assert c.transport.request.call_count==1


def test_static_import_firewall_has_no_production_provider_engine_or_telegram_dependencies():
    root=Path(__file__).resolve().parents[1]/'predbot'/'ticket_audit'
    banned=('predbot.providers','predbot.model','predbot.jobs','predbot.cache','predbot.bot','predbot.custom_picks',
        'predbot.storage','telegram','openai')
    for file in root.glob('*.py'):
        for node in ast.walk(ast.parse(file.read_text(encoding='utf-8'))):
            names=[n.name for n in node.names] if isinstance(node,ast.Import) else [node.module or ''] if isinstance(node,ast.ImportFrom) else []
            assert not any(name.startswith(banned) for name in names),(file,names)
