"""Synthetic/mocked diagnostics only. No live credentials, codes or provider calls.

Only the conversion example has a published field shape. Other payloads below
exercise defensive inspection; they do NOT establish Betloy's actual schemas.
"""
import json
from pathlib import Path
from unittest.mock import Mock
import uuid

import pytest
import requests

import diagnose_betloy as b


@pytest.fixture(autouse=True)
def no_live_http(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Betloy tests must never use live HTTP')
    monkeypatch.setattr(requests.Session, 'request', forbidden)


@pytest.fixture
def credential(monkeypatch):
    # An ephemeral test string, never a live account credential or committed key.
    value = 'synthetic-' + uuid.uuid4().hex
    monkeypatch.setenv('BETLOY_API_KEY', value)
    return value


def response(status=200, payload=None, mime='application/json', retry=None):
    r = Mock(status_code=status, headers={'Content-Type': mime})
    if retry:
        r.headers['Retry-After'] = retry
    r.json.return_value = {'success': True, 'data': {}} if payload is None else payload
    return r


def client(credential, r=None):
    session = Mock()
    session.request.return_value = r or response()
    return b.Validator(credential, session), session


def test_missing_key_stops_before_http(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv('BETLOY_API_KEY', raising=False)
    session = Mock()
    assert b.main(['health'], session=session, env_file=tmp_path / '.env') == 2
    assert capsys.readouterr().out == 'BETLOY_API_KEY is not configured.\n'
    session.request.assert_not_called()


def test_reads_only_named_key_without_loading_other_configuration(credential, monkeypatch, tmp_path):
    monkeypatch.delenv('BETLOY_API_KEY')
    path = tmp_path / '.env'
    path.write_text('BETLOY_API_KEY="' + credential + '"\nUNRELATED_DIAGNOSTIC_TEST=value\n', encoding='utf-8')
    assert b.read_key(path) == credential
    assert 'UNRELATED_DIAGNOSTIC_TEST' not in b.os.environ


def test_empty_process_key_does_not_fall_back_to_dotenv(credential, monkeypatch, tmp_path):
    path = tmp_path / '.env'
    path.write_text('BETLOY_API_KEY=' + credential, encoding='utf-8')
    monkeypatch.setenv('BETLOY_API_KEY', '')
    with pytest.raises(b.ValidationError, match='not configured'):
        b.read_key(path)


@pytest.mark.parametrize('suffix', ['\n', '\r\n'])
def test_header_injection_rejected_without_disclosure(credential, monkeypatch, suffix):
    monkeypatch.setenv('BETLOY_API_KEY', credential + suffix + 'Injected: header')
    with pytest.raises(b.ValidationError) as error:
        b.read_key()
    assert credential not in str(error.value)


def test_bearer_header_https_timeout_no_redirect_no_retry(credential):
    validator, session = client(credential)
    result = validator.metadata('health')
    session.request.assert_called_once_with('GET', 'https://betloy.com/api/v1/health',
                                           headers={'Authorization': 'Bearer ' + credential, 'Accept': 'application/json'},
                                           params=None, timeout=(10, 20), allow_redirects=False)
    assert result.ok and credential not in repr(validator) + b.render(result)


def test_successful_health_json_is_only_transport_evidence(credential, capsys):
    validator, session = client(credential, response(payload={'status': 'healthy'}))
    assert b.main(['health', '--live'], session=session) == 0
    output = capsys.readouterr().out
    assert 'JSON transport only' in output and 'status' in output and credential not in output


def test_usage_fields_are_preserved_not_invented(credential, capsys):
    # Illustrative only: no documented usage JSON schema is available.
    payload = {'success': True, 'data': {'example_plan': 'trial', 'example_remaining': 7, 'example_reset': None}}
    assert b.parse_usage(payload) == payload['data']
    assert 'monthly_allowance' not in b.parse_usage(payload)
    _, session = client(credential, response(payload=payload))
    assert b.main(['usage', '--live'], session=session) == 0
    assert 'example_remaining' in capsys.readouterr().out


def test_catalogue_and_explicit_nigeria_filter():
    catalogue = [{'key': 'example-ng', 'region': 'Nigeria'}, {'key': 'another-ng', 'region': 'NG'},
                 {'key': 'example-gh', 'region': 'Ghana'}, {'key': 'global', 'region': 'Global'},
                 {'key': 'unclassified'}, {'key': 'regional', 'region': 'West Africa'}]
    assert b.parse_bookmakers({'success': True, 'data': catalogue}) == catalogue
    assert b.nigeria_bookmakers(catalogue) == catalogue[:2]
    assert b.parse_bookmakers(catalogue) == catalogue
    assert all('can_convert_to' not in row for row in catalogue)


@pytest.mark.parametrize('payload', [None, 'html', 5, {'data': {}}, {'data': ['wrong']}, {'success': False}])
def test_malformed_catalogue_does_not_claim_coverage(payload):
    with pytest.raises(b.ValidationError):
        b.parse_bookmakers(payload)


def test_catalogue_command_reports_only_explicit_nigeria(credential, capsys):
    _, session = client(credential, response(payload={'data': [{'region': 'Nigeria'}, {'region': 'Ghana'}]}))
    assert b.main(['bookmakers', '--live'], session=session) == 0
    output = capsys.readouterr().out
    assert 'Catalogue rows: 2; explicit Nigeria-region rows: 1' in output


def test_public_catalogue_key_preserved_but_credential_reflection_hidden(credential):
    payload = {'data': [{'key': 'public-bookmaker-id', 'region': 'Nigeria'},
                        {'key': credential, 'region': 'Nigeria', 'api_key': credential}]}
    validator, _ = client(credential, response(payload=payload))
    probe = validator.metadata('bookmakers')
    assert probe.data['data'][0]['key'] == 'public-bookmaker-id'
    assert probe.data['data'][1]['key'] == '[REDACTED]'
    assert credential not in json.dumps(probe.data)


def test_decode_inspector_keeps_opaque_fields_without_fabricating_selections():
    payload = {'success': True, 'data': {'opaque_field': [{'unspecified_field': 'example'}]}}
    assert b.parse_decode(payload) == payload['data']
    assert 'canonical_event_id' not in b.parse_decode(payload)


def conversion_payload():
    # Codes are generated at test runtime and cannot be mistaken for evidence.
    return {'success': True, 'data': {
        'converted_code': uuid.uuid4().hex[:7].upper(), 'from_bookie': 'Example Source', 'to_bookie': 'Example Destination',
        'total_odds': 4.5, 'number_of_events': 3, 'status': 'completed'}}


def test_conversion_example_parser_never_implies_leg_matching_or_verification():
    payload = conversion_payload()
    out = b.parse_conversion(payload)
    assert out['converted_code'] == payload['data']['converted_code']
    assert out['total_odds'] == 4.5 and out['number_of_events'] == 3
    assert out['flagged'] is None and out['partial_visibility'] == 'UNKNOWN'
    assert out['destination_verification'] == 'UNVERIFIED'
    assert 'source_leg_count' not in out and 'canonical_event_id' not in out


def test_partial_flags_are_preserved_without_substitution_or_assuming_item_schema():
    payload = conversion_payload()
    payload['flagged'] = ['opaque unmatched leg']
    out = b.parse_conversion(payload)
    assert out['flagged'] == ['opaque unmatched leg'] and out['partial_visibility'] == 'VISIBLE'
    payload['flagged'] = []
    assert b.parse_conversion(payload)['flagged'] == []


@pytest.mark.parametrize('payload', [{'success': False}, {'success': True}, {'success': True, 'data': {'converted_code': ''}}])
def test_conversion_missing_required_shape_fails_closed(payload):
    with pytest.raises(b.ValidationError):
        b.parse_conversion(payload)


def test_conversion_body_uses_documented_names_and_runtime_values():
    code = uuid.uuid4().hex[:7].upper()
    assert b.conversion_body(code, 101, 202) == {'booking_code': code, 'from_bookie_id': 101, 'to_bookie_id': 202}


@pytest.mark.parametrize('source,destination', [(0, 2), (1, -1), ('one', 2), (True, 2)])
def test_invalid_runtime_ids_are_rejected(source, destination):
    with pytest.raises(b.ValidationError):
        b.conversion_body(uuid.uuid4().hex[:7], source, destination)


@pytest.mark.parametrize('status,payload,expected', [
    (400, {'code': 'SYNTHETIC_INVALID_CODE', 'message': 'Example invalid code'}, 'http_error'),
    (401, {'error': {'code': 'SYNTHETIC_AUTH', 'message': 'Example auth failure'}}, 'authentication_failed'),
    (402, {'code': 'SYNTHETIC_QUOTA', 'message': 'Example quota exhausted'}, 'quota_exhausted'),
    (429, {'code': 'SYNTHETIC_LIMIT', 'message': 'Example rate limit'}, 'rate_limited'),
    (500, {'code': 'SYNTHETIC_SERVER'}, 'server_error'),
    (503, {'message': 'Example unavailable'}, 'server_error'),
    (200, {'success': False, 'code': 'SYNTHETIC_FAILURE'}, 'provider_error'),
])
def test_error_taxonomy_preserves_observed_codes_without_retries(credential, status, payload, expected):
    validator, session = client(credential, response(status, payload, retry='30' if status == 429 else None))
    result = validator.metadata('usage')
    assert not result.ok and result.error == expected
    assert result.error_code == b.parse_error(payload)[0]
    assert result.message == b.parse_error(payload)[1]
    if status == 429:
        assert result.retry_after == '30'
    session.request.assert_called_once()


@pytest.mark.parametrize('status', [200, 404, 500, 302])
def test_html_is_not_api_or_authentication_success(credential, status):
    validator, session = client(credential, response(status, mime='text/html'))
    result = validator.metadata('health')
    assert not result.ok and result.error == 'non_json_response' and result.data is None
    session.request.assert_called_once()


def test_malformed_json_is_safe(credential):
    r = response()
    r.json.side_effect = ValueError(credential)
    validator, _ = client(credential, r)
    result = validator.metadata('health')
    assert result.error == 'malformed_json' and credential not in repr(result)


@pytest.mark.parametrize('exception,error', [(requests.Timeout, 'timeout'), (requests.ConnectionError, 'transport_error')])
def test_timeout_and_transport_failures_never_echo_or_retry(credential, exception, error):
    validator, session = client(credential)
    session.request.side_effect = exception('Authorization: Bearer ' + credential)
    result = validator.metadata('health')
    assert result.error == error and credential not in b.render(result)
    session.request.assert_called_once()


def test_nested_secret_headers_codes_and_encoded_reflections_are_redacted(credential):
    key = credential + '/+='
    raw = {'authorization': 'Bearer ' + key, 'api_key': key, 'original_code': uuid.uuid4().hex[:7],
           'nested': [key, b.quote(key, safe=''), 'Bearer random-opaque-secret', {'email': 'private@example.invalid'}],
           key: 'field name reflection'}
    sanitized = b.redact(raw, key)
    serialized = json.dumps(sanitized)
    assert key not in serialized and b.quote(key, safe='') not in serialized
    assert 'random-opaque-secret' not in serialized and 'private@example.invalid' not in serialized
    assert sanitized['original_code'] == '[REDACTED]'


def test_provider_echoes_never_reach_output(credential, capsys, caplog):
    payload = {'success': False, 'code': credential, 'message': 'Bearer ' + credential,
               'data': {'nested': [{'api_key': credential}]}}
    _, session = client(credential, response(400, payload))
    assert b.main(['health', '--live'], session=session) == 1
    assert credential not in capsys.readouterr().out + caplog.text


@pytest.mark.parametrize('endpoint', ['https://evil.invalid', '../convert', 'convert', 'decode', 'scan', 'analyze'])
def test_network_allowlist_blocks_credential_exfiltration_and_paid_calls(credential, endpoint):
    validator, session = client(credential)
    with pytest.raises(b.ValidationError):
        validator.metadata(endpoint)
    session.request.assert_not_called()


def test_running_without_a_mode_is_network_free(credential, capsys):
    session = Mock()
    assert b.main([], session=session) == 0
    assert 'usage:' in capsys.readouterr().out
    session.request.assert_not_called()


@pytest.mark.parametrize('mode', ['convert', 'decode'])
@pytest.mark.parametrize('live', [False, True])
def test_paid_modes_require_flag_and_honor_validation_stop(credential, capsys, mode, live):
    session = Mock()
    args = [mode] + (['--live'] if live else [])
    assert b.main(args, session=session) == 2
    output = capsys.readouterr().out
    assert ('HTTP 404/HTML' if live else '--live is required') in output
    assert 'may consume credits' in output and credential not in output
    session.request.assert_not_called()


def test_sandbox_probe_uses_exact_documented_query_without_claiming_conversion_support(credential, capsys):
    _, session = client(credential)
    assert b.main(['sandbox', '--live'], session=session) == 0
    assert session.request.call_args.kwargs['params'] == {'test': 'true'}
    assert 'cannot prove' in capsys.readouterr().out
    session.request.assert_called_once()


def test_failed_metadata_stops_without_automatic_followup(credential, capsys):
    _, session = client(credential, response(404, mime='text/html'))
    assert b.main(['health', '--live'], session=session) == 1
    assert 'STOP: no further calls' in capsys.readouterr().out
    session.request.assert_called_once()


@pytest.mark.parametrize('mode', ['health', 'usage', 'bookmakers', 'sandbox'])
def test_disputed_metadata_billing_also_requires_explicit_intent(credential, capsys, mode):
    session = Mock()
    assert b.main([mode], session=session) == 2
    assert 'disputed billing requires --live' in capsys.readouterr().out
    session.request.assert_not_called()


def test_utility_import_and_execution_are_isolated_from_production():
    root = Path(__file__).resolve().parents[1]
    for path in [root / 'main.py', root / 'cli.py', *root.joinpath('predbot').rglob('*.py')]:
        assert 'diagnose_betloy' not in path.read_text(encoding='utf-8')
    source = root.joinpath('diagnose_betloy.py').read_text(encoding='utf-8')
    assert 'Database(' not in source and 'load_config(' not in source
    assert 'get_daily_snapshot' not in source and 'build_picks' not in source
