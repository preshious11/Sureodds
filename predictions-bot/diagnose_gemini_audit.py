"""Explicit live Gemini-only validation. Never called by production startup.

No arguments/--live omitted: prints a plan and makes zero requests. Test inputs are
synthetic; never print raw user slips, provider responses, headers or exceptions.
"""
from dataclasses import replace
from datetime import datetime, timezone
import argparse
import json
from pathlib import Path
import struct
import zlib

from predbot.ticket_audit.gemini import GeminiClient, GeminiHTTPError, Settings, UsageGuard, sanitize_google_metadata
from predbot.ticket_audit.model_access import REVIEWED_MODELS, generation_candidates
from predbot.ticket_audit.schemas import AuditUnavailable

ROOT = Path(__file__).resolve().parent
TEXT_SLIP = ('TEST BETSLIP\nARSENAL VS CHELSEA\nRESULT3 ARSENAL TO WIN\nODDS 1.75\n'
             'BARCELONA VS SEVILLA\nTOTAL OVER 2.5\nODDS 1.62\nTOTAL ODDS 2.84')
NATURAL_TEXT_SLIP = ('Arsenal vs Chelsea\nArsenal to Win @ 1.75\n\n'
                     'Barcelona vs Sevilla\nOver 2.5 @ 1.62\n\nTotal Odds: 2.84')


def probe_candidates(settings, usage, records, known_failures=(), max_probes=3):
    """Explicit diagnostic-only discovery; normal client never switches models."""
    if not 1 <= max_probes <= 3:
        raise AuditUnavailable('invalid_probe_budget')
    candidates = [m for m in generation_candidates(records) if m not in known_failures]
    attempts = []
    for model in candidates[:max_probes]:
        client = GeminiClient(replace(settings, model=model), usage)
        try:
            text, _, tokens = client.generate('Reply with exactly: OK', max_output_tokens=16)
            if text.strip() != 'OK':
                raise AuditUnavailable('unexpected_probe_output')
            attempts.append({'model': model, 'http_status': 200, 'generation_succeeded': True, 'tokens': tokens})
            return {'selected_model': model, 'attempts': attempts, 'candidates': candidates}
        except GeminiHTTPError as exc:
            attempts.append({'model': model, 'generation_succeeded': False, **exc.diagnostic()})
            # Only an unavailable model permits another diagnostic candidate. Auth,
            # billing, quota, malformed requests and transport failures stop the run.
            if exc.http_status != 404 or exc.category in ('BILLING_REQUIRED', 'FREE_TIER_UNAVAILABLE'):
                break
        except AuditUnavailable as exc:
            attempts.append({'model': model, 'generation_succeeded': False, 'reason': str(exc)})
            break
    return {'selected_model': None, 'attempts': attempts, 'candidates': candidates}


def validate_structured(client):
    schema = {'type': 'object', 'properties': {'status': {'type': 'string'}},
              'required': ['status']}
    text, _, tokens = client.generate('Return status OK.',
                                      schema=schema, max_output_tokens=128)
    try:
        data = json.loads(text)
        if data != {'status': 'OK'}:
            raise ValueError
    except (ValueError, TypeError):
        client.usage.count('failures', client.clock())
        raise AuditUnavailable('malformed_structured_output') from None
    return {'http_status': 200, 'structured_valid': True, 'output': data, 'tokens': tokens}


def grounding_summary(grounding):
    """Actual grounding metadata required; URLs in answer text are not proof."""
    if not isinstance(grounding, dict):
        raise AuditUnavailable('empty_grounding_metadata')
    chunks, supports, queries = (grounding.get(k, []) for k in ('groundingChunks', 'groundingSupports', 'webSearchQueries'))
    if not isinstance(chunks, list) or not isinstance(supports, list) or not isinstance(queries, list):
        raise AuditUnavailable('malformed_grounding_metadata')
    sources = [c['web'] for c in chunks if isinstance(c, dict) and isinstance(c.get('web'), dict) and c['web'].get('uri')]
    if not sources or not queries:
        raise AuditUnavailable('empty_grounding_metadata')
    return {'grounding_metadata_keys': sorted(grounding), 'sources': sources, 'supports': supports,
            'search_queries': queries, 'search_entry_point': grounding.get('searchEntryPoint')}


def validate_schema_shapes(client):
    schema = {'type': 'object', 'additionalProperties': False, 'properties': {
        'items': {'type': 'array', 'minItems': 1, 'maxItems': 2, 'items': {
            'type': 'object', 'additionalProperties': False, 'properties': {
                'status': {'type': 'string', 'enum': ['OK']},
                'missing': {'type': ['string', 'null']}}, 'required': ['status', 'missing']}}},
        'required': ['items']}
    output, _, tokens = client.generate('Return one item with status OK and missing null.', schema, max_output_tokens=256)
    try:
        if json.loads(output) != {'items': [{'status': 'OK', 'missing': None}]}:
            raise ValueError
    except (ValueError, TypeError):
        raise AuditUnavailable('malformed_structured_output') from None
    return {'http_status': 200, 'schema_shapes_valid': True, 'tokens': tokens}


def validate_ticket(ticket, input_type, natural=False):
    if ticket.input_type != input_type or len(ticket.selections) != 2 or ticket.submitted_combined_odds != 2.84:
        raise AuditUnavailable('synthetic_ticket_mismatch')
    expected = [('arsenal', 'chelsea', 'result3', 'home', None, 1.75),
                ('barcelona', 'sevilla', 'total', 'over', 2.5, 1.62)]
    for leg, coords in zip(ticket.selections, expected):
        actual = ((leg.home or '').casefold(), (leg.away or '').casefold(), leg.market_family,
                  leg.selection, leg.line, leg.submitted_odds)
        if natural and leg is ticket.selections[0] and leg.market_family is None:
            actual = (*actual[:2], 'result3', *actual[3:])
        if actual != coords:
            raise AuditUnavailable('synthetic_ticket_mismatch')
        if any(getattr(leg, k) is not None for k in ('sport', 'competition', 'kickoff', 'source_event_id',
               'canonical_event_id', 'period', 'stat', 'entity')):
            raise AuditUnavailable('synthetic_unknowns_invented')
    if ticket.source_bookmaker is not None or ticket.source_code is not None:
        raise AuditUnavailable('synthetic_unknowns_invented')
    return {'http_status': 200, 'structured_valid': True, 'input_type': input_type, 'selections': 2,
            'teams_markets_selections_lines_odds_correct': True, 'combined_odds': 2.84, 'unknowns_preserved': True}


def synthetic_image(text=None):
    """Tiny standard-library bitmap font; ephemeral PNG, no files/OCR dependency."""
    shapes = {
        'A': '01110 10001 10001 11111 10001 10001 10001',
        'B': '11110 10001 10001 11110 10001 10001 11110',
        'C': '01111 10000 10000 10000 10000 10000 01111',
        'D': '11110 10001 10001 10001 10001 10001 11110',
        'E': '11111 10000 10000 11110 10000 10000 11111',
        'G': '01111 10000 10000 10111 10001 10001 01111',
        'H': '10001 10001 10001 11111 10001 10001 10001',
        'I': '11111 00100 00100 00100 00100 00100 11111',
        'L': '10000 10000 10000 10000 10000 10000 11111',
        'N': '10001 11001 11001 10101 10011 10011 10001',
        'O': '01110 10001 10001 10001 10001 10001 01110',
        'P': '11110 10001 10001 11110 10000 10000 10000',
        'R': '11110 10001 10001 11110 10100 10010 10001',
        'S': '01111 10000 10000 01110 00001 00001 11110',
        'T': '11111 00100 00100 00100 00100 00100 00100',
        'U': '10001 10001 10001 10001 10001 10001 01110',
        'V': '10001 10001 10001 10001 10001 01010 00100',
        'W': '10001 10001 10001 10101 10101 10101 01010',
        '0': '01110 10001 10011 10101 11001 10001 01110',
        '1': '00100 01100 00100 00100 00100 00100 01110',
        '2': '01110 10001 00001 00010 00100 01000 11111',
        '3': '11110 00001 00001 01110 00001 00001 11110',
        '4': '00010 00110 01010 10010 11111 00010 00010',
        '5': '11111 10000 10000 11110 00001 00001 11110',
        '6': '01110 10000 10000 11110 10001 10001 01110',
        '7': '11111 00001 00010 00100 01000 01000 01000',
        '8': '01110 10001 10001 01110 10001 10001 01110',
        '.': '00000 00000 00000 00000 00000 00100 00100',
        '@': '01110 10001 10111 10101 10111 10000 01111',
        ':': '00000 00100 00100 00000 00100 00100 00000',
    }
    width, height, scale = 760, 395, 5
    pixels = bytearray([255]) * (width * height)
    for row, line_text in enumerate((text or TEXT_SLIP).upper().splitlines()):
        for col, char in enumerate(line_text):
            for gy, line in enumerate(shapes.get(char, '00000 ' * 6 + '00000').split()):
                for gx, bit in enumerate(line):
                    if bit == '1':
                        for dy in range(scale):
                            for dx in range(scale):
                                x, y = 20 + col * 6 * scale + gx * scale + dx, 20 + row * 45 + gy * scale + dy
                                pixels[y * width + x] = 0
    def chunk(kind, payload):
        return struct.pack('>I', len(payload)) + kind + payload + struct.pack('>I', zlib.crc32(kind + payload) & 0xffffffff)
    raw = b''.join(b'\x00' + bytes(pixels[y * width:(y + 1) * width]) for y in range(height))
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 0, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b'')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('models', 'probe', 'text', 'structured', 'schema', 'search', 'image'), nargs='?', default='models')
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--confirm-free-tier', action='store_true', help='Operator confirms Free tier and Cloud Billing disabled')
    parser.add_argument('--usage-file', type=Path, default=ROOT / 'data' / 'ticket_audit_usage.sqlite3')
    parser.add_argument('--model', help='Explicit diagnostic override; never writes .env or switches a running bot')
    parser.add_argument('--discover', action='store_true', help='probe mode only: list and probe reviewed free candidates, stopping on success')
    parser.add_argument('--skip-model', action='append', default=[], help='probe --discover: skip an already failed candidate')
    args = parser.parse_args(argv)
    settings = Settings.from_environment(ROOT / '.env')
    if args.model:
        settings = replace(settings, model=args.model)
    # Do not print an arbitrary configured model (could contain sensitive input).
    print(json.dumps({'execution': 'LIVE' if args.live else 'PLAN_ONLY', 'mode': args.mode,
                      'model': settings.model if settings.model in REVIEWED_MODELS else 'unsupported',
                      'requests_without_live': 0}))
    if not args.live:
        return 0
    if (args.discover or args.skip_model) and args.mode != 'probe':
        parser.error('--discover/--skip-model are probe-only options')
    settings = replace(settings, enabled=True, free_tier_confirmed=settings.free_tier_confirmed or args.confirm_free_tier)
    try:
        # Capability/key gate before creating a ledger.
        if not settings.api_key:
            raise AuditUnavailable('missing_key')
        if not settings.free_tier_confirmed:
            raise AuditUnavailable('free_tier_unconfirmed')
        if not args.usage_file.parent.is_dir():
            raise AuditUnavailable('usage_directory_missing')
        usage = UsageGuard(args.usage_file)
        client = GeminiClient(settings, usage)
        if args.mode == 'models':
            records = client.models()
            result = {'models': records, 'candidates': generation_candidates(records), 'billing_tier_exposed': False}
        elif args.mode == 'probe':
            if args.discover:
                result = probe_candidates(settings, usage, client.models(), args.skip_model)
                if result['selected_model'] is None:
                    print(json.dumps({'result': result, 'usage': usage.snapshot(client.clock())}))
                    return 2
            else:
                text, _, tokens = client.generate('Reply with exactly: OK', max_output_tokens=16)
                if text.strip() != 'OK':
                    raise AuditUnavailable('unexpected_probe_output')
                result = {'http_status': 200, 'generation_succeeded': True, 'tokens': tokens}
        elif args.mode == 'structured':
            result = validate_structured(client)
        elif args.mode == 'schema':
            result = validate_schema_shapes(client)
        elif args.mode in ('text', 'image'):
            ticket = client.extract(image=('image/png', synthetic_image(NATURAL_TEXT_SLIP))) if args.mode == 'image' else client.extract(NATURAL_TEXT_SLIP)
            result = validate_ticket(ticket, args.mode, natural=True)
        else:
            text, grounding, tokens = client.generate(
                'Use Google Search for ONE harmless sports check: according to the official NBA website, '
                'what is the opening date of the 2026-27 NBA regular season? Today is ' + datetime.now(timezone.utc).date().isoformat()
                + '. Give a short sourced answer. Do not predict games or invent probabilities.', grounded=True)
            result = {**grounding_summary(grounding), 'supported_text_characters': len(text), 'tokens': tokens,
                      'model': settings.model, 'timestamp': client.clock().isoformat()}
        output = sanitize_google_metadata({'result': result, 'usage': usage.snapshot(datetime.now(timezone.utc))}, settings.api_key)
        print(json.dumps(output, ensure_ascii=True))
        return 0
    except GeminiHTTPError as exc:
        print(json.dumps({'unavailable': str(exc), 'error': exc.diagnostic()}))
        return 2
    except AuditUnavailable as exc:
        print(json.dumps({'unavailable': str(exc)}))
        return 2
    except (OSError, ValueError):
        print(json.dumps({'unavailable': 'local_configuration_error'}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
