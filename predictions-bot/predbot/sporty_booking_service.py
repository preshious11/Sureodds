"""Private relay contract for the Worker; all SportyBet semantics stay in one adapter.

Not a public endpoint. Hosting/authentication of the private service binding is a
staging prerequisite. No server, scheduler, provider generation or Telegram here.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import hmac
import json
import logging
import sqlite3
import threading
from typing import Callable

from predbot.sporty_booking import Selection, SportyBooking, CODE

log = logging.getLogger(__name__)


class BookingService:
    """At-most-once durable claims; pass a dedicated scratch ledger, never bot DB."""
    def __init__(self, ledger: sqlite3.Connection, adapter_factory: Callable[[], SportyBooking],
                 *, enabled: bool = False):
        self.ledger, self.factory, self.enabled = ledger, adapter_factory, enabled
        self.lock = threading.Lock()
        self.capacity = threading.BoundedSemaphore(2)
        ledger.execute('CREATE TABLE IF NOT EXISTS booking_claims (fingerprint TEXT PRIMARY KEY)')
        ledger.commit()

    def handle(self, request: dict) -> dict:
        failure = {'status':'unverified'}
        if not self.enabled:
            return {'status':'disabled'}
        if not self.capacity.acquire(blocking=False):
            return failure
        stage = 'validation'
        try:
            raw = request.get('manifest_json')
            fingerprint = request.get('fingerprint')
            if not isinstance(raw,str) or len(raw.encode('utf-8'))>32768:
                return failure
            if hashlib.sha256(raw.encode('utf-8')).hexdigest()!=fingerprint:
                return failure
            rows = json.loads(raw)
            if not isinstance(rows,list) or not 1<=len(rows)<=5:
                return failure
            selections = [Selection.from_candidate(row) for row in rows]
            adapter = self.factory()
            adapter.validate_selections(selections)
            stage = 'ledger_claim'
            with self.lock:
                claimed = self.ledger.execute('INSERT OR IGNORE INTO booking_claims VALUES (?)',(fingerprint,)).rowcount
                self.ledger.commit()
            if not claimed:
                return failure  # ambiguous/crashed POSTs are NEVER reclaimed
            stage = 'creation_and_readback'
            result = adapter.create_booking_code(selections)  # changed odds stop; no override
            if (not result.programmatic_verified or result.status!='owner_confirmation_required'
                    or not CODE.fullmatch(result.code)
                    or [r.original for r in result.selections]!=selections):
                return failure  # current adapter necessarily takes this path
            now = datetime.now(timezone.utc).timestamp()*1000
            return {'status':'verified','fingerprint':fingerprint,'code':result.code,
                    'selection_count':len(selections),'exact_membership_verified':True,
                    'expires_at_ms':min(now+120000,min(
                        datetime.fromisoformat(s.start_utc.replace('Z','+00:00')).timestamp()*1000
                        for s in selections))}
        except Exception:  # optional boundary: never leak upstream text or crash delivery
            log.warning('Optional SportyBet operation withheld | stage=%s',stage)
            return failure
        finally:
            self.capacity.release()


class BookingHTTPApplication:
    """WSGI adapter for a private authenticated HTTPS reverse proxy.

    Caller must enforce an HTTP request deadline/size at the reverse proxy too.
    This endpoint is Kovqi-owned, NOT a claimed SportyBet endpoint.
    """
    def __init__(self, service: BookingService, token: str):
        if not isinstance(token,str) or len(token)<32 or not all(33<=ord(c)<=126 for c in token):
            raise ValueError('private service token must be at least 32 characters')
        self.service, self.token = service, token

    def __call__(self, environ, start_response):
        status='200 OK'; result={'status':'unverified'}
        if environ.get('PATH_INFO')!='/v1/bookings' or environ.get('REQUEST_METHOD')!='POST':
            status='404 Not Found'
        elif not hmac.compare_digest(str(environ.get('HTTP_AUTHORIZATION','')).encode(),f'Bearer {self.token}'.encode()):
            status='401 Unauthorized'
        elif environ.get('CONTENT_TYPE','').split(';')[0]!='application/json':
            status='415 Unsupported Media Type'
        else:
            try:
                size=int(environ.get('CONTENT_LENGTH','0'))
                if not 0<size<=65536:
                    status='413 Content Too Large'
                else:
                    request=json.loads(environ['wsgi.input'].read(size))
                    if isinstance(request,dict): result=self.service.handle(request)
            except (ValueError,TypeError,KeyError,OSError,RecursionError):
                status='400 Bad Request'
        body=json.dumps(result,separators=(',',':')).encode()
        start_response(status,[('Content-Type','application/json'),('Content-Length',str(len(body))),('Cache-Control','no-store')])
        return [body]
