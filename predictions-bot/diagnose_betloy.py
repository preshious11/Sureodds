"""Phase 2B.1 only: isolated diagnostics, never imported by the sports bot.

Official reference: https://betloy.com/api (consulted 2026-10-07).
The documented health URL returned HTML/404 during validation. Paid operations
are deliberately blocked until Betloy supplies a working, consistent contract.
Mock parser tests are NOT evidence of live endpoint schemas or capabilities.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from urllib.parse import quote, quote_plus

import requests

from predbot.config import _parse_env_file, default_env_path

API_BASE = 'https://betloy.com/api/v1/'
REFERENCE = 'https://betloy.com/api'
METADATA = frozenset({'health', 'usage', 'bookmakers'})
LIVE_BLOCKER = ('Live validation stopped: the documented health endpoint returned HTTP 404/HTML, '
                'and official API access/cost information conflicts. Resolve the contract with Betloy first.')
SENSITIVE_FIELDS = re.compile(r'key|token|authorization|password|secret|email|original_code|booking_code|source_code', re.I)


class ValidationError(ValueError):
    """Only fixed, safe messages: never attach a request, credential or raw exception."""


def read_key(env_file: Path | None = None):
    key = os.environ.get('BETLOY_API_KEY')
    if key is None:
        path = env_file if env_file is not None else default_env_path()
        try:
            key = _parse_env_file(path).get('BETLOY_API_KEY') if path.is_file() else None
        except (OSError, UnicodeError):
            raise ValidationError('Could not read the local environment file.') from None
    if not isinstance(key, str) or not key.strip():
        raise ValidationError('BETLOY_API_KEY is not configured.')
    if '\r' in key or '\n' in key:
        raise ValidationError('BETLOY_API_KEY has an invalid header value.')
    return key.strip()


def redact(value, key, *, catalogue=False):
    """Redact nested credentials, source codes and common encoded key reflections."""
    if isinstance(value, dict):
        return {redact(str(k), key): '[REDACTED]' if SENSITIVE_FIELDS.search(str(k)) and not (
                    catalogue and k == 'key' and isinstance(value.get('region'), str)) else redact(v, key, catalogue=catalogue)
                for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, key, catalogue=catalogue) for v in value]
    if isinstance(value, str):
        for secret in sorted({key, quote(key, safe=''), quote_plus(key), json.dumps(key)[1:-1]}, key=len, reverse=True):
            if secret:
                value = value.replace(secret, '[REDACTED]')
        return re.sub(r'(?i)\bbearer\s+[^\s"<>]+', 'Bearer [REDACTED]', value)
    return value


@dataclass(frozen=True)
class Probe:
    endpoint: str
    observed_at: str
    http_status: int | None
    content_type: str | None
    data: dict | list | None
    error: str | None
    error_code: str | None = None
    message: str | None = None
    retry_after: str | None = None

    @property
    def ok(self):
        return self.error is None and self.http_status == 200


def parse_error(payload):
    """Preserve only fields that exist; a nested error envelope is a synthetic test shape."""
    if not isinstance(payload, dict):
        return None, None
    error = payload.get('error')
    source = error if isinstance(error, dict) else payload
    code, message = source.get('code'), source.get('message')
    return (str(code)[:120] if isinstance(code, (str, int)) else None,
            message[:240] if isinstance(message, str) else None)


def parse_response(endpoint, response, key, observed_at):
    status = response.status_code
    content_type = redact(response.headers.get('Content-Type', ''), key)
    retry_after = redact(response.headers.get('Retry-After'), key)
    # An HTML page at HTTP 200 is never API success. JSON MIME type is mandatory.
    if 'application/json' not in content_type.lower() and '+json' not in content_type.lower():
        return Probe(endpoint, observed_at, status, content_type, None, 'non_json_response', retry_after=retry_after)
    try:
        payload = response.json()
    except (ValueError, TypeError):
        return Probe(endpoint, observed_at, status, content_type, None, 'malformed_json', retry_after=retry_after)
    if not isinstance(payload, (dict, list)):
        return Probe(endpoint, observed_at, status, content_type, None, 'malformed_response', retry_after=retry_after)
    payload = redact(payload, key, catalogue=endpoint == 'bookmakers')
    code, message = parse_error(payload)
    error = {401: 'authentication_failed', 402: 'quota_exhausted', 429: 'rate_limited'}.get(status)
    if status >= 500:
        error = 'server_error'
    elif status != 200 and error is None:
        error = 'redirect_refused' if 300 <= status < 400 else 'http_error'
    if isinstance(payload, dict) and payload.get('success') is False and error is None:
        error = 'provider_error'
    return Probe(endpoint, observed_at, status, content_type, payload, error, code, message, retry_after)


class Validator:
    """Fixed HTTPS origin, metadata-only network access, no redirects or retries."""
    def __init__(self, key, session=None):
        if not isinstance(key, str) or not key.strip():
            raise ValidationError('BETLOY_API_KEY is not configured.')
        if '\r' in key or '\n' in key:
            raise ValidationError('BETLOY_API_KEY has an invalid header value.')
        self._key = key
        self._session = session if session is not None else requests.Session()

    def __repr__(self):
        return 'Validator(credentials=[REDACTED])'

    def metadata(self, endpoint, *, test=False):
        if endpoint not in METADATA:
            raise ValidationError('Only documented metadata endpoints are enabled.')
        at = datetime.now(timezone.utc).isoformat()
        try:
            response = self._session.request('GET', API_BASE + endpoint,
                                            headers={'Authorization': 'Bearer ' + self._key,
                                                     'Accept': 'application/json'},
                                            params={'test': 'true'} if test else None,
                                            timeout=(10, 20), allow_redirects=False)
            return parse_response(endpoint, response, self._key, at)
        except requests.Timeout:
            return Probe(endpoint, at, None, None, None, 'timeout')
        except requests.RequestException:
            return Probe(endpoint, at, None, None, None, 'transport_error')


def unwrap_data(payload):
    if not isinstance(payload, (dict, list)) or (isinstance(payload, dict) and payload.get('success') is False):
        raise ValidationError('No successful structured response available.')
    return payload.get('data', payload) if isinstance(payload, dict) else payload


def parse_usage(payload):
    """Keep actual usage field names. No invented plan/allowance/reset defaults."""
    data = unwrap_data(payload)
    if not isinstance(data, dict):
        raise ValidationError('Usage response is not an object; schema remains unverified.')
    return data


def parse_bookmakers(payload):
    """Provisional list/envelope reader. No capabilities inferred from a name."""
    data = unwrap_data(payload)
    if not isinstance(data, list) or not all(isinstance(row, dict) for row in data):
        raise ValidationError('Bookmaker response is not a record list; schema remains unverified.')
    return data


def nigeria_bookmakers(records):
    """Only an explicit Nigeria/NG region qualifies; global support is unknown."""
    return [row for row in records if isinstance(row.get('region'), str) and
            row['region'].strip().casefold() in ('nigeria', 'ng')]


def parse_decode(payload):
    """Inspect structured data without inventing a decode/selection field schema."""
    data = unwrap_data(payload)
    if not isinstance(data, (dict, list)):
        raise ValidationError('Decode response is not structured; schema remains unverified.')
    return data


def parse_conversion(payload):
    """Only documented conversion example fields; absent flagged data stays UNKNOWN."""
    if not isinstance(payload, dict) or payload.get('success') is not True:
        raise ValidationError('Conversion success was not established.')
    data = payload.get('data')
    if not isinstance(data, dict) or not isinstance(data.get('converted_code'), str) or not data['converted_code'].strip():
        raise ValidationError('No destination code in the documented response shape.')
    fields = ('conversion_id', 'converted_code', 'from_bookie', 'to_bookie', 'total_odds', 'number_of_events', 'status')
    out = {field: data[field] for field in fields if field in data}
    flagged = payload.get('flagged')
    out['flagged'] = flagged if isinstance(flagged, list) else None
    out['partial_visibility'] = 'VISIBLE' if isinstance(flagged, list) else 'UNKNOWN'
    # No silent inference of source-leg count, match count, canonical IDs or verification.
    out['destination_verification'] = 'UNVERIFIED'
    return out


def conversion_body(code, source, destination):
    """Build the official example's shape only. No codes/IDs are embedded here."""
    if not isinstance(code, str) or not code.strip() or any(c in code for c in '\r\n'):
        raise ValidationError('Supply a booking code at runtime.')
    if any(type(value) is not int or value <= 0 for value in (source, destination)):
        raise ValidationError('Bookmaker IDs must be positive integers confirmed by Betloy.')
    return {'booking_code': code.strip(), 'from_bookie_id': source, 'to_bookie_id': destination}


def render(probe):
    lines = [f'Endpoint: GET {API_BASE}{probe.endpoint}', f'Observed at: {probe.observed_at}',
             f'HTTP: {probe.http_status if probe.http_status is not None else "not received"}',
             f'Result: {"PASS (JSON transport only)" if probe.ok else "FAIL — " + str(probe.error)}']
    if probe.content_type:
        lines.append(f'Content-Type: {probe.content_type}')
    if probe.error_code is not None:
        lines.append(f'Provider code: {probe.error_code}')
    if probe.message:
        lines.append(f'Provider message: {probe.message}')
    if probe.retry_after:
        lines.append(f'Retry-After: {probe.retry_after} (no automatic retry)')
    if probe.data is not None:
        lines.append('Response structure: ' + ('object fields ' + ', '.join(probe.data) if isinstance(probe.data, dict) else 'list'))
    return '\n'.join(lines)


def main(argv=None, *, session=None, env_file=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', nargs='?', choices=sorted(METADATA | {'sandbox', 'decode', 'convert'}))
    parser.add_argument('--live', action='store_true', help='Explicit network/possible-credit intent; current conversion safety blocker still applies.')
    parser.add_argument('--code', help='Booking code supplied at runtime; never stored. Interactive entry is safer than shell history.')
    parser.add_argument('--from-bookie-id', type=int)
    parser.add_argument('--to-bookie-id', type=int)
    args = parser.parse_args(argv)
    if args.mode is None:
        parser.print_help()
        return 0  # Running the tool with no mode never makes a request.
    try:
        key = read_key(env_file)
        if args.mode in ('decode', 'convert'):
            print('This operation may consume credits. Documented conversion cost: 1 unit; decode cost is disputed.')
            if not args.live:
                print('STOP: --live is required. No request sent.')
                return 2
            print(LIVE_BLOCKER)
            if args.mode == 'decode':
                print('Decode parameters are also undocumented. No request sent.')
            return 2
        # Pricing contradicts the reference FAQ; do not assert actual billing is free.
        print('BETLOY VALIDATION\nMetadata probe: reference FAQ says free; pricing disagrees. Actual billing is unverified.')
        if not args.live:
            print('STOP: disputed billing requires --live even for metadata/test probes. No request sent.')
            return 2
        validator = Validator(key, session)
        probe = validator.metadata('health' if args.mode == 'sandbox' else args.mode, test=args.mode == 'sandbox')
        print(render(probe))
        if not probe.ok:
            print('STOP: no further calls, host guessing, paid retries or conversion.')
            return 1
        if args.mode == 'sandbox':
            print('Test query sent: test=true. A JSON health response alone cannot prove deterministic/free conversion sandbox behavior.')
        elif args.mode == 'usage':
            print('Usage fields (provider names preserved): ' + json.dumps(parse_usage(probe.data), ensure_ascii=True))
        elif args.mode == 'bookmakers':
            catalogue = parse_bookmakers(probe.data)
            nigeria = nigeria_bookmakers(catalogue)
            print(f'Catalogue rows: {len(catalogue)}; explicit Nigeria-region rows: {len(nigeria)}')
            print('Nigeria records (capabilities not inferred): ' + json.dumps(nigeria, ensure_ascii=True))
        return 0
    except ValidationError as exc:
        print(str(exc))
        return 2
    except Exception:
        # Never expose a transport exception containing headers, URLs or credentials.
        print('Validation failed safely. No further request sent.')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
