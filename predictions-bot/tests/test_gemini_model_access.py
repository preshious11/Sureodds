"""Phase 2C.1B: mocked discovery/probes/protocol/errors; no live Gemini or sports calls."""
from dataclasses import asdict, replace
import json
from unittest.mock import Mock

import pytest

import diagnose_gemini_audit as diag
from predbot.ticket_audit.gemini import (GeminiClient, GeminiHTTPError, Settings, UsageGuard,
    google_error, sanitize_google_text)
from predbot.ticket_audit.model_access import DEFAULT_MODEL, LEGACY_FREE_SEARCH_MODEL, REVIEWED_MODELS, generation_candidates
from predbot.ticket_audit.schemas import AuditUnavailable, NormalizedSelection, NormalizedTicket
from predbot.ticket_audit.local import LocalResolver
from predbot.ticket_audit.service import AuditService
from predbot.ticket_audit.research import Researcher
from tests.test_ticket_audit_foundation import no_providers, client, response, NOW, ticket


def model(name, methods=None, **extras):
    return {'name': 'models/' + name, 'displayName': 'Display ' + name, 'version': 'test-version',
            'supportedGenerationMethods': methods or ['generateContent', 'countTokens'],
            'inputTokenLimit': 1048576, 'outputTokenLimit': 65536, 'thinking': True, **extras}


def free_settings(**kw):
    return Settings(**{'api_key': 'test-secret', 'model': DEFAULT_MODEL, 'enabled': True,
                       'free_tier_confirmed': True, **kw})


def fake_response(data, status=200):
    return Mock(status_code=status, json=Mock(return_value=data))


def test_discovery_preserves_each_record_and_does_not_claim_entitlement(tmp_path):
    rows = [model(LEGACY_FREE_SEARCH_MODEL), model(DEFAULT_MODEL), model('embedding', ['embedContent'])]
    c = client(tmp_path, {'models': rows}, settings=free_settings(model='unknown-configured-model'))
    assert c.models() == rows  # Discovery works independently of configured-model eligibility.
    assert c.transport.request.call_count == 1
    assert c.transport.request.call_args.args[:2] == ('GET', 'https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000')
    assert c.usage.snapshot(NOW)['requests'] == 1


def test_pagination_is_bounded_and_counted(tmp_path):
    c = client(tmp_path, settings=free_settings())
    c.transport.request.side_effect = [fake_response({'models': [model(DEFAULT_MODEL)], 'nextPageToken': 'page 2'}),
                                       fake_response({'models': [model(LEGACY_FREE_SEARCH_MODEL)]})]
    assert len(c.models()) == 2
    assert 'pageToken=page%202' in c.transport.request.call_args.args[1]
    assert c.usage.snapshot(NOW)['requests'] == 2


def test_infinite_model_pagination_stops_at_three_pages(tmp_path):
    c = client(tmp_path, settings=free_settings())
    c.transport.request.side_effect = [fake_response({'models': [], 'nextPageToken': str(i)}) for i in range(3)]
    with pytest.raises(AuditUnavailable, match='model_pagination_limit'):
        c.models()
    assert c.transport.request.call_count == 3


@pytest.mark.parametrize('data', [{}, {'models': None}, {'models': [None]},
    {'models': [{'name': 'model'}]}, {'models': [], 'nextPageToken': 42}])
def test_malformed_catalog_fail_closed(tmp_path, data):
    c = client(tmp_path, data, settings=free_settings())
    with pytest.raises(AuditUnavailable, match='malformed_model'):
        c.models()


def test_discovery_never_leaks_credentials(tmp_path):
    rows = [model(DEFAULT_MODEL, displayName='test-secret', description='not captured')]
    c = client(tmp_path, {'models': rows}, settings=free_settings())
    data = c.models()
    assert 'test-secret' not in repr(data) and 'description' not in data[0]


@pytest.mark.parametrize('name,methods', [('gemini-embedding-2', ['embedContent']),
    ('gemini-3.8-flash-tts', ['generateContent']), ('gemini-3.1-flash-image', ['generateContent']),
    ('gemini-3-flash-preview', ['generateContent']), ('gemini-flash-latest', ['generateContent']),
    ('gemini-3.5-flash', ['generateContent']), ('gemini-3.1-flash-lite', ['generateContent']),
    ('unknown-future-free-model', ['generateContent']), (DEFAULT_MODEL, ['countTokens'])])
def test_candidate_filter_excludes_nonreviewed_paid_alias_preview_deprecated_or_wrong_method(name, methods):
    assert generation_candidates([model(name, methods)]) == []


def test_candidate_discovery_intersects_real_records_and_prefers_free_search_then_lite():
    rows = [model(DEFAULT_MODEL), model('gemini-2.5-flash-lite'), model('gemini-3.8-flash'),
            model('gemini-2.5-flash'), model('gemini-pro-latest')]
    assert generation_candidates(rows) == ['gemini-2.5-flash-lite', LEGACY_FREE_SEARCH_MODEL, DEFAULT_MODEL, 'gemini-3.8-flash']
    assert generation_candidates([model(DEFAULT_MODEL)]) == [DEFAULT_MODEL]


def test_visible_2_5_can_404_then_newer_model_succeeds_and_probing_stops(tmp_path, monkeypatch):
    settings = free_settings()
    usage = UsageGuard(tmp_path / 'probe-usage.sqlite')
    calls, clients = [], []
    def factory(s, u):
        transport = Mock()
        if s.model == LEGACY_FREE_SEARCH_MODEL:
            transport.request.return_value = fake_response({'error': {'status': 'NOT_FOUND',
                'message': 'This model is no longer available to new users. Use a newer model.'}}, 404)
        else:
            transport.request.return_value = fake_response(response('OK'))
        calls.append(s.model)
        c = GeminiClient(s, u, transport, lambda: NOW)
        clients.append(c)
        return c
    monkeypatch.setattr(diag, 'GeminiClient', factory)
    result = diag.probe_candidates(settings, usage, [model(LEGACY_FREE_SEARCH_MODEL), model(DEFAULT_MODEL), model('gemini-3.8-flash')])
    assert result['selected_model'] == DEFAULT_MODEL
    assert calls == [LEGACY_FREE_SEARCH_MODEL, DEFAULT_MODEL]  # No later unnecessary candidate.
    assert result['attempts'][0]['category'] == 'PROJECT_NOT_ENTITLED'
    assert result['attempts'][0]['provider_status'] == 'NOT_FOUND'
    assert settings.model == DEFAULT_MODEL  # Diagnostic does not mutate application configuration.
    for c in clients:
        body = c.transport.request.call_args.kwargs['json']
        assert body['generationConfig']['maxOutputTokens'] == 16
        assert 'tools' not in body and 'responseJsonSchema' not in body['generationConfig']
        assert not any('inlineData' in p for p in body['contents'][0]['parts'])
    assert usage.snapshot(NOW)['requests'] == 2


@pytest.mark.parametrize('status,message', [(403, 'Billing is required'), (429, 'Quota exhausted'),
    (401, 'API key not valid'), (400, 'Request contains an invalid argument.')])
def test_probe_does_not_cycle_models_for_auth_billing_quota_or_invalid_request(tmp_path, monkeypatch, status, message):
    mock_client = Mock()
    mock_client.generate.side_effect = google_error(fake_response({'error': {'message': message}}, status), '')
    factory = Mock(return_value=mock_client)
    monkeypatch.setattr(diag, 'GeminiClient', factory)
    result = diag.probe_candidates(free_settings(), UsageGuard(tmp_path / 'usage.sqlite'),
                                   [model(LEGACY_FREE_SEARCH_MODEL), model(DEFAULT_MODEL)])
    assert result['selected_model'] is None and len(result['attempts']) == 1 and factory.call_count == 1


def test_already_failed_candidate_is_skipped_without_request(tmp_path, monkeypatch):
    c = Mock()
    c.generate.return_value = ('OK', {}, {})
    factory = Mock(return_value=c)
    monkeypatch.setattr(diag, 'GeminiClient', factory)
    result = diag.probe_candidates(free_settings(), UsageGuard(tmp_path / 'usage.sqlite'),
        [model(LEGACY_FREE_SEARCH_MODEL), model(DEFAULT_MODEL)], known_failures=[LEGACY_FREE_SEARCH_MODEL])
    assert result['selected_model'] == DEFAULT_MODEL and factory.call_args.args[0].model == DEFAULT_MODEL


@pytest.mark.parametrize('status,message,category', [
    (404, 'This model models/gemini-2.5-flash-lite is no longer available to new users.', 'PROJECT_NOT_ENTITLED'),
    (404, 'Not found.', 'NOT_FOUND'), (404, 'Model is not available.', 'MODEL_UNAVAILABLE'),
    (404, 'Not found for API version v1beta or not supported for generateContent.', 'API_VERSION_OR_MODEL_SUPPORT'),
    (403, 'Cloud billing must be enabled.', 'BILLING_REQUIRED'),
    (403, 'Free tier is not available for this capability.', 'FREE_TIER_UNAVAILABLE'),
    (401, 'API key not valid. Please pass a valid API key.', 'AUTHENTICATION'),
    (429, 'Daily quota exceeded.', 'QUOTA'), (429, 'Too many requests.', 'RATE_LIMIT'),
    (400, 'Invalid model specified.', 'INVALID_MODEL'),
    (400, 'Request contains an invalid argument.', 'MALFORMED_REQUEST'), (503, 'Service overloaded.', 'SERVICE_UNAVAILABLE')])
def test_sanitized_google_diagnostic_preserves_exact_useful_message(status, message, category):
    error = google_error(fake_response({'error': {'status': 'TEST_STATUS', 'message': message}}, status), '')
    assert error.diagnostic()['http_status'] == status and error.message == message and error.category == category
    assert error.provider_status == 'TEST_STATUS'
    assert message not in repr(error)  # Service errors remain fixed-code; diagnostics expose sanitized data separately.


@pytest.mark.parametrize('value', ['test-secret', 'https://example.test?key=test-secret',
    'x-goog-api-key: other-secret', 'Authorization: Bearer other-token', 'access_token=other-token',
    'AIza12345678901234567890123456789012345'])
def test_google_message_redacts_keys_auth_and_tokens(value):
    result = sanitize_google_text('Useful error. ' + value, 'test-secret')
    assert 'Useful error.' in result
    for token in ('test-secret', 'other-secret', 'other-token', 'AIza12345678901234567890123456789012345'):
        assert token not in result


def test_non_json_http_error_is_controlled_and_does_not_echo_body():
    r = Mock(status_code=502)
    r.json.side_effect = ValueError('private raw body')
    error = google_error(r, 'secret')
    assert error.category == 'SERVICE_UNAVAILABLE' and error.message == '' and 'private' not in repr(error.diagnostic())


@pytest.mark.parametrize('body', ['{"status":"OK"}', '{ "status": "OK" }'])
def test_tiny_structured_output_validates_schema_and_configured_model(tmp_path, body):
    c = client(tmp_path, response(body), settings=free_settings())
    assert diag.validate_structured(c)['structured_valid']
    url = c.transport.request.call_args.args[1]
    config = c.transport.request.call_args.kwargs['json']['generationConfig']
    assert url.endswith('models/' + DEFAULT_MODEL + ':generateContent')
    assert config['thinkingConfig'] == {'thinkingLevel': 'MINIMAL'}
    assert config['maxOutputTokens'] == 128 and 'tools' not in c.transport.request.call_args.kwargs['json']


@pytest.mark.parametrize('body', ['not JSON', '[]', '{}', '{"status":"UNKNOWN"}',
    '{"status":true,"confidence":"HIGH"}', '{"status":"OK","confidence":"HIGH","probability":.9}'])
def test_malformed_tiny_structure_stops_without_retry(tmp_path, body):
    c = client(tmp_path, response(body), settings=free_settings())
    with pytest.raises(AuditUnavailable, match='malformed_structured_output'):
        diag.validate_structured(c)
    assert c.transport.request.call_count == 1 and c.usage.snapshot(NOW)['failures'] == 1


def test_selected_model_search_paid_gate_before_http_and_quota_reservation(tmp_path, db):
    c = client(tmp_path, response('No request allowed'), settings=free_settings())
    with pytest.raises(AuditUnavailable, match='SEARCH_GROUNDING_PAID_ONLY'):
        c.generate('search', grounded=True)
    with pytest.raises(AuditUnavailable, match='SEARCH_GROUNDING_PAID_ONLY'):
        c._request('POST', 'models/' + DEFAULT_MODEL + ':generateContent', {}, grounded=True)
    c.transport.request.assert_not_called()
    assert c.usage.snapshot(NOW)['requests'] == 0
    audit = AuditService(LocalResolver(db.path), Researcher(c)).audit(ticket(), NOW)
    assert 'SEARCH_GROUNDING_PAID_ONLY' in audit.selections[0].limitations
    c.transport.request.assert_not_called()


def test_grounding_parser_uses_metadata_and_preserves_provenance():
    metadata = {'groundingChunks': [{'web': {'uri': 'https://nba.com/news/test', 'title': 'Official NBA'}}],
                'groundingSupports': [{'segment': {'text': 'A verified date'}, 'groundingChunkIndices': [0]}],
                'webSearchQueries': ['NBA regular season start date'], 'searchEntryPoint': {'renderedContent': 'attribution'}}
    summary = diag.grounding_summary(metadata)
    assert summary['sources'] == [metadata['groundingChunks'][0]['web']]
    assert summary['supports'] == metadata['groundingSupports']
    assert summary['search_queries'] == metadata['webSearchQueries']


@pytest.mark.parametrize('metadata', [{}, None, {'answer': 'https://nba.com/this-is-not-grounding'},
    {'groundingChunks': [], 'webSearchQueries': ['query']}, {'groundingChunks': [None]},
    {'groundingChunks': 'not a list'}])
def test_missing_grounding_metadata_is_not_accepted(metadata):
    with pytest.raises(AuditUnavailable):
        diag.grounding_summary(metadata)


def normalized_synthetic(input_type):
    legs = (NormalizedSelection(home='Arsenal', away='Chelsea', market_family='result3', selection='home', submitted_odds=1.75),
            NormalizedSelection(home='Barcelona', away='Sevilla', market_family='total', selection='over', line=2.5, submitted_odds=1.62))
    return NormalizedTicket(input_type, None, None, 2.84, legs, 'HIGH')


@pytest.mark.parametrize('kind', ['image', 'text'])
def test_existing_ticket_extraction_for_selected_model_and_two_legs(kind, tmp_path):
    t = normalized_synthetic(kind)
    data = asdict(t)
    c = client(tmp_path, response(json.dumps(data)), settings=free_settings())
    extracted = c.extract(image=('image/png', diag.synthetic_image())) if kind == 'image' else c.extract(diag.TEXT_SLIP)
    assert diag.validate_ticket(extracted, kind)['unknowns_preserved']
    call = c.transport.request.call_args
    assert call.args[1].endswith('models/' + DEFAULT_MODEL + ':generateContent') and 'tools' not in call.kwargs['json']
    assert c.usage.snapshot(NOW)['requests'] == 1


@pytest.mark.parametrize('change', ['teams', 'line', 'odds', 'invisible', 'combined'])
def test_synthetic_extraction_validation_rejects_changes_or_invented_fields(change):
    t = normalized_synthetic('image')
    if change == 'combined':
        t = replace(t, submitted_combined_odds=3)
    else:
        changes = {'teams': {'home': 'Other'}, 'line': {'line': 3.5}, 'odds': {'submitted_odds': 2.0},
                   'invisible': {'sport': 'football'}}[change]
        t = replace(t, selections=(replace(t.selections[0], **changes), t.selections[1]))
    with pytest.raises(AuditUnavailable):
        diag.validate_ticket(t, 'image')


def test_selected_model_quota_guard_still_prevents_second_call(tmp_path):
    c = client(tmp_path, response('OK'), settings=free_settings(max_requests_per_day=1))
    c.generate('Reply OK', max_output_tokens=16)
    c.usage = UsageGuard(tmp_path / 'audit-usage.sqlite3')
    with pytest.raises(AuditUnavailable, match='local_quota_exhausted'):
        c.generate('Reply OK', max_output_tokens=16)
    assert c.transport.request.call_count == 1


def test_configured_legacy_model_is_not_automatically_replaced(tmp_path):
    c = client(tmp_path, settings=free_settings(model=LEGACY_FREE_SEARCH_MODEL))
    c.transport.request.return_value = fake_response({'error': {'status': 'NOT_FOUND', 'message': 'No longer available to new users.'}}, 404)
    with pytest.raises(GeminiHTTPError):
        c.generate('OK')
    assert c.transport.request.call_count == 1
    assert c.settings.model == LEGACY_FREE_SEARCH_MODEL
    assert LEGACY_FREE_SEARCH_MODEL in c.transport.request.call_args.args[1]


@pytest.mark.parametrize('message', ['Billing is required.', 'Free tier is not available for this capability.',
                                   'This model is no longer available to new users.'])
def test_billing_and_entitlement_errors_still_make_no_sports_fallback(tmp_path, db, message):
    c = client(tmp_path, settings=free_settings(model=LEGACY_FREE_SEARCH_MODEL))
    c.transport.request.return_value = fake_response({'error': {'status': 'PERMISSION_DENIED', 'message': message}}, 403)
    audit = AuditService(LocalResolver(db.path), Researcher(c)).audit(ticket(), NOW)
    assert audit.selections[0].verdict == 'INSUFFICIENT_EVIDENCE' and c.transport.request.call_count == 1


def test_diagnostic_without_live_probes_nothing_and_writes_no_env(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(diag.Settings, 'from_environment', lambda *a: free_settings())
    p = tmp_path / 'unused-ledger.sqlite'
    assert diag.main(['probe', '--discover', '--usage-file', str(p)]) == 0
    assert not p.exists() and 'PLAN_ONLY' in capsys.readouterr().out


def test_diagnostic_retains_sanitized_google_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(diag.Settings, 'from_environment', lambda *a: free_settings())
    fake = Mock()
    fake.generate.side_effect = google_error(fake_response({'error': {'status': 'INVALID_ARGUMENT',
                                              'message': 'Invalid request with test-secret'}}, 400), 'test-secret')
    monkeypatch.setattr(diag, 'GeminiClient', lambda *a: fake)
    assert diag.main(['probe', '--live', '--confirm-free-tier', '--usage-file', str(tmp_path / 'usage.sqlite')]) == 2
    out = capsys.readouterr().out
    assert 'INVALID_ARGUMENT' in out and 'Invalid request' in out and 'test-secret' not in out


def test_candidate_probe_has_hard_three_request_budget(tmp_path, monkeypatch):
    c = Mock()
    c.generate.side_effect = google_error(fake_response({'error': {'status': 'NOT_FOUND', 'message': 'Unavailable model'}}, 404), '')
    factory = Mock(return_value=c)
    monkeypatch.setattr(diag, 'GeminiClient', factory)
    rows = [model(m) for m in REVIEWED_MODELS]
    result = diag.probe_candidates(free_settings(), UsageGuard(tmp_path / 'quota.sqlite'), rows)
    assert result['selected_model'] is None and len(result['attempts']) == 3 and factory.call_count == 3


def test_invalid_candidate_metadata_does_not_trigger_guessing():
    assert generation_candidates([None, {'name': DEFAULT_MODEL, 'supportedGenerationMethods': None}]) == []
