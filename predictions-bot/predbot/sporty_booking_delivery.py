"""Owner-only Telegram delivery of verified SportyBet booking codes.

Exactly one recipient: the single configured admin (bot owner), PRIVATE chat
only, identity confirmed via getChat before anything is sent. Never a group,
never the public channel/mirror, never subscriber DMs, never any database,
never ticket_publications. No automatic retries; receipts and failures carry
no tokens and no upstream message text.
"""
from __future__ import annotations

import re

import requests

CODE = re.compile(r'[A-Z0-9]{4,16}')
VERIFIED_STATUS = 'exact_membership_verified'
_MAX_TEXT = 4096


def booking_message(*, day, revision, selection_count, code, verification) -> str:
    """Compose the owner message; raises ValueError on any unsafe input."""
    if not isinstance(code, str) or not CODE.fullmatch(code):
        raise ValueError('invalid code')
    if type(selection_count) is not int or isinstance(selection_count, bool) \
            or not 1 <= selection_count <= 5:
        raise ValueError('invalid selection count')
    lines = [
        'SportyBet booking code',
        f'Top Picks date: {day}',
        f'Prediction revision: {revision}',
        f'Selections: {selection_count}',
        f'Code: {code}',
        f'Verification: {verification}',
        'No wager has been placed. Confirm the selections in the SportyBet app '
        'before any further publication.',
    ]
    composed = '\n'.join(lines)
    if len(composed) > _MAX_TEXT:
        raise ValueError('message too long')
    return composed


def _telegram_post(url, payload, timeout):
    return requests.post(url, json=payload, timeout=timeout, allow_redirects=False)


def _verified_owner_id(cfg, poster):
    admins = getattr(cfg, 'telegram_admin_ids', None) or []
    token = getattr(cfg, 'telegram_token', '') or ''
    if len(admins) != 1 or not str(admins[0]).strip().lstrip('+').isdigit() \
            or int(str(admins[0]).lstrip('+')) <= 0 or not token:
        return None
    owner = str(int(str(admins[0]).lstrip('+')))
    base = f'https://api.telegram.org/bot{token}'
    try:
        probe = poster(base + '/getChat', {'chat_id': owner}, (5, 12))
        data = probe.json() if getattr(probe, 'status_code', None) == 200 else None
    except (requests.RequestException, ValueError, TypeError, AttributeError):
        return None
    if (not isinstance(data, dict) or data.get('ok') is not True
            or not isinstance(data.get('result'), dict)
            or data['result'].get('type') != 'private'
            or str(data['result'].get('id')) != owner):
        return None
    return owner


def preflight_owner_delivery(cfg, *, post=None):
    """Confirm a single private owner destination before creating a code."""
    poster = post or _telegram_post
    if _verified_owner_id(cfg, poster) is None:
        return {'ready': False, 'failure': 'owner_chat_unverified'}
    return {'ready': True, 'recipient': 'owner_private'}


def send_owner_booking_result(cfg, *, day, revision, selection_count, code,
                              verification, post=None):
    """Send one verified result only to the owner's verified private chat."""
    admins = getattr(cfg, 'telegram_admin_ids', None) or []
    token = getattr(cfg, 'telegram_token', '') or ''
    if len(admins) != 1 or not str(admins[0]).strip().lstrip('+').isdigit() \
            or int(str(admins[0]).lstrip('+')) <= 0 or not token:
        return {'sent': False, 'failure': 'owner_not_configured'}
    if verification != VERIFIED_STATUS:
        return {'sent': False, 'failure': 'verification_required'}
    try:
        message = booking_message(day=day, revision=revision,
                                  selection_count=selection_count, code=code,
                                  verification=verification)
    except ValueError:
        return {'sent': False, 'failure': 'invalid_result'}
    poster = post or _telegram_post
    owner = _verified_owner_id(cfg, poster)
    if owner is None:
        return {'sent': False, 'failure': 'owner_chat_unverified'}
    base = f'https://api.telegram.org/bot{token}'
    try:
        reply = poster(base + '/sendMessage',
                       {'chat_id': owner, 'text': message,
                        'disable_web_page_preview': True}, (5, 15))
        payload = reply.json() if getattr(reply, 'status_code', None) == 200 else None
    except (requests.RequestException, ValueError, TypeError, AttributeError):
        return {'sent': False, 'failure': 'send_rejected'}
    if (getattr(reply, 'status_code', None) != 200 or not isinstance(payload, dict)
            or payload.get('ok') is not True
            or not isinstance(payload.get('result'), dict)):
        return {'sent': False, 'failure': 'send_rejected'}
    return {'sent': True, 'recipient': 'owner_private',
            'message_id': payload['result'].get('message_id')}
