"""Allowlisted, request-local diagnostics. No payloads, identifiers or exception text."""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
import os
import sqlite3
from zoneinfo import ZoneInfo

from .model_access import REVIEWED_MODELS
from .schemas import AuditUnavailable

_CURRENT = ContextVar('ticket_audit_diagnostics', default=None)
REASONS = frozenset({
    'incomplete_extraction', 'ticket_input_too_large', 'ticket_parser_depth_limit', 'ticket_parser_node_limit',
    'unsupported_private_audit_model', 'private_audit_disabled', 'usage_path_must_be_separate',
    'unrelated_source_usage_database', 'invalid_local_budget', 'local_quota_exhausted',
    'usage_guard_unavailable', 'gemini_disabled', 'missing_key', 'free_tier_unconfirmed',
    'unsupported_free_model', 'SEARCH_GROUNDING_PAID_ONLY', 'structured_free_capability_unavailable',
    'image_free_capability_unavailable', 'input_too_large', 'search_schema_combination_not_validated',
    'unsupported_structured_schema', 'invalid_image_input', 'invalid_output_budget',
    'gemini_rate_limited', 'key_or_capability_rejected', 'model_unavailable',
    'gemini_service_unavailable', 'gemini_request_rejected', 'gemini_timeout',
    'gemini_transport_or_json_failure', 'malformed_response', 'safety_refusal_or_incomplete_output',
    'empty_response', 'malformed_structured_output', 'extraction_type_mismatch',
    # research.py / free_research.py (the research/fact-check path) - previously
    # missing here, so every one of these legitimate failures logged as the
    # opaque 'unknown_audit_reason' fallback instead of its real cause.
    'unsupported_research_coordinates', 'research_coordinate_mismatch', 'empty_grounding_metadata',
    'no_supported_sources', 'malformed_research_output', 'incomplete_or_started_fixture',
    'research_busy', 'research_wait_timeout', 'invalid_research_time', 'ticket_audit_disabled',
    'unsupported_free_research', 'unreviewed_public_source', 'malformed_public_source',
    'malformed_or_unavailable_public_source', 'public_source_unavailable', 'public_source_timeout',
    'public_source_too_large', 'public_source_budget_exhausted', 'public_source_budget_unavailable',
    'public_fixture_missing', 'public_fixture_ambiguous', 'conflicting_public_results',
    'insufficient_public_history', 'conflicting_source_routes',
})
# Provider messages can echo a submitted ticket or an image. Log a fixed summary,
# never arbitrary provider text, even when its credentials have been redacted.
PROVIDER_MESSAGES = {
    'BILLING_REQUIRED': 'Provider reports billing is required; no fallback attempted.',
    'PROJECT_NOT_ENTITLED': 'Provider reports this project is not entitled to the model.',
    'FREE_TIER_UNAVAILABLE': 'Provider reports free-tier access is unavailable.',
    'AUTHENTICATION': 'Provider rejected authentication.',
    'API_VERSION_OR_MODEL_SUPPORT': 'Provider reports API-version or model support failure.',
    'QUOTA': 'Provider reports quota exhaustion.',
    'RATE_LIMIT': 'Provider rate-limited the request.',
    'MODEL_UNAVAILABLE': 'Provider reports the model is unavailable.',
    'NOT_FOUND': 'Provider could not find the requested resource.',
    'INVALID_MODEL': 'Provider reports an invalid model.',
    'MALFORMED_REQUEST': 'Provider rejected the request as invalid.',
    'AUTHENTICATION_OR_PERMISSION': 'Provider rejected authentication or permission.',
    'SERVICE_UNAVAILABLE': 'Provider service is unavailable.',
    'REQUEST_REJECTED': 'Provider rejected the request.',
}


def _initial():
    return {'stage': 'backend_init', 'model': None, 'mime_type': None, 'image_size_bytes': None,
        'api_key_present': None, 'api_key_in_environment': bool(os.environ.get('GEMINI_API_KEY')),
        'gemini_enabled': None, 'free_tier_confirmed': None, 'quota_day': None,
        'quota_used': None, 'quota_limit': None, 'quota_state': 'unknown',
        'quota_reserved': False, 'http_attempted': False, 'http_status': None}


def note(**values):
    current = _CURRENT.get()
    if current is not None:
        current.update(values)


def note_settings(settings):
    model = settings.model
    note(model=model if model in REVIEWED_MODELS else 'unreviewed_model',
         api_key_present=bool(settings.api_key), gemini_enabled=bool(settings.enabled),
         free_tier_confirmed=bool(settings.free_tier_confirmed),
         quota_limit=settings.max_requests_per_day if type(settings.max_requests_per_day) is int else None)


def note_quota(path, now=None):
    """Best-effort read-only snapshot; never create/reset a ledger or hide a failure."""
    try:
        now = now or datetime.now(timezone.utc)
        day = now.astimezone(ZoneInfo('America/Los_Angeles')).date().isoformat()
        path = Path(path).resolve()
        note(quota_day=day)
        if not path.is_file():
            note(quota_used=0, quota_state='ledger_missing')
            return
        with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=0.1) as db:
            db.execute('PRAGMA query_only=ON')
            row = db.execute('SELECT requests FROM audit_usage WHERE day=?', (day,)).fetchone()
        used = row[0] if row else 0
        note(quota_used=used if type(used) is int and used >= 0 else None, quota_state='read')
    except Exception:
        note(quota_used=None, quota_state='unavailable')


def note_provider(error):
    category = getattr(error, 'category', None)
    if category in PROVIDER_MESSAGES:
        note(provider_category=category, provider_message=PROVIDER_MESSAGES[category],
             provider_reason=str(error) if str(error) in REASONS else 'unknown_audit_reason')


def note_response(data):
    """Bounded response flags only; never candidate text or safety explanation."""
    feedback = data.get('promptFeedback')
    candidates = data.get('candidates')
    first = candidates[0] if isinstance(candidates, list) and candidates else None
    finish = first.get('finishReason') if isinstance(first, dict) else None
    known = {'STOP', 'MAX_TOKENS', 'SAFETY', 'RECITATION', 'LANGUAGE', 'BLOCKLIST',
             'PROHIBITED_CONTENT', 'SPII', 'MALFORMED_FUNCTION_CALL', 'OTHER'}
    note(prompt_blocked=bool(feedback.get('blockReason')) if isinstance(feedback, dict) else False,
         finish_reason=finish if isinstance(finish, str) and finish in known else 'OTHER' if finish else None)


def failure_details(error):
    current = _CURRENT.get()
    details = dict(current if current is not None else getattr(error, 'audit_diagnostics', _initial()))
    # AuditUnavailable is a contract of fixed codes, but still allowlist at the
    # log boundary so an injected/unknown exception cannot leak submitted data.
    reason = str(error) if isinstance(error, AuditUnavailable) else None
    details['reason'] = reason if reason in REASONS else 'unknown_audit_reason' if reason else 'unexpected_error'
    note_provider(error)
    if current is not None:
        details.update(current)
    return details


@contextmanager
def scope(stage):
    token = _CURRENT.set(_initial()) if _CURRENT.get() is None else None
    note(stage=stage)
    try:
        yield
    except Exception as error:
        error.audit_diagnostics = failure_details(error)
        raise
    finally:
        if token is not None:
            _CURRENT.reset(token)


def diagnosed(stage):
    def decorate(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            with scope(stage):
                return fn(*args, **kwargs)
        return wrapped
    return decorate
