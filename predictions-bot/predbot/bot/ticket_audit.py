"""Private ephemeral orchestration. Bounded workers; confirmation precedes every audit."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
import json
import logging
import re
import secrets
import threading
from predbot import ticket_audit_switch

from predbot.bot import audit_format
from predbot.ticket_audit import booking as bk
from predbot.ticket_audit.runtime import build_backend
from predbot.ticket_audit.diagnostics import diagnosed, note, note_settings, note_quota, failure_details
from predbot.ticket_audit.schemas import (AuditUnavailable, TicketResourceLimit, instant, normalized,
                                         number, ticket_from_json)

log = logging.getLogger(__name__)
PARAMETER = 'ticket_audit'
PREFIX = 'ta|'
TTL = timedelta(minutes=15)
INTRO = ('🎟 Ticket Audit\n\nSend me your ticket and I’ll check each selection.\n\n'
         '• Paste the ticket\n• Send a screenshot\n\n'
         'I’ll use today’s prediction data where available and free public evidence where supported.\n'
         'No booking-code input yet. You’ll review the selections before I check them.')
UNAVAILABLE = 'Ticket Audit is temporarily unavailable. Try again later.'
UNREADABLE = '😅 I couldn’t read that ticket clearly.\n\nTry a clearer screenshot or paste the selections as text.'
SUPPORTED = 'Send a screenshot (PNG or JPEG) or paste the ticket selections. Other files, videos and voice notes aren’t supported.'
RESOURCE_LIMIT = 'This ticket exceeds the safe input size or parsing limits. Try a smaller image, shorter text, or a booking code.'
INCOMPLETE = 'I couldn’t extract the complete ticket. Send a clearer, complete screenshot, paste the full ticket, or use a booking code.'
BOOKING = 'Booking codes aren’t supported yet. Send a screenshot or paste the ticket selections.'
INTRO_CODES = (INTRO.replace('No booking-code input yet. ', '')
               .replace('• Send a screenshot', '• Send a screenshot\n• Send a booking code'))
CHOOSE_BOOKMAKER = 'Which bookmaker is this code from?'
CODE_MESSAGES = {
    bk.MALFORMED_CODE: 'That doesn’t look like a valid booking code. Check it and try again, or send a screenshot.',
    bk.UNKNOWN_BOOKMAKER: 'I couldn’t tell which bookmaker that code is from. Send the code again and pick the bookmaker.',
    bk.UNSUPPORTED_BOOKMAKER: 'That bookmaker isn’t supported for code import yet. You can still send a screenshot.',
    bk.NOT_FOUND: 'I couldn’t find that booking code. Check the code and bookmaker and try again.',
    bk.EXPIRED: 'That booking code has expired. Send a screenshot of the ticket instead.',
    bk.EMPTY: 'That booking code has no selections I can check. Send a screenshot instead.',
    bk.RESPONSE_TOO_LARGE: 'Ticket response is too large to process safely.',
    bk.PARSER_LIMIT: 'Ticket response exceeds safe parsing depth or work limits.',
    bk.QUOTA_EXHAUSTED: 'Code import has reached its limit for now. Try again later, or send a screenshot instead.',
    bk.TIMEOUT: 'The bookmaker took too long to respond. Try again in a moment, or send a screenshot.',
    bk.PROVIDER_UNAVAILABLE: 'Code import is temporarily unavailable. You can send a screenshot instead.',
    bk.PROVIDER_CHANGED: 'Code import is temporarily unavailable. You can send a screenshot instead.',
    bk.DISABLED: BOOKING}
ACTIVE = {'WAITING', 'EXTRACTING', 'CLARIFYING', 'REVIEWING', 'PENDING_REVIEW', 'AUDITING', 'COMPLETE',
          'CHOOSING_BOOKMAKER', 'DECODING'}
BUSY = ('EXTRACTING', 'DECODING', 'REVIEWING', 'AUDITING')
FIELD_LABELS = {'home': 'Home/team 1', 'away': 'Away/team 2', 'sport': 'Sport', 'competition': 'Competition',
    'kickoff': 'Kick-off (date, time and UTC offset)', 'market_family': 'Market (e.g. 1X2, moneyline, total, handicap)',
    'selection': 'Pick (home, away, draw, over, under, yes, no)', 'line': 'Line',
    'period': 'Period (e.g. full match)', 'stat': 'Counts (score, corners, cards, sets, games)',
    'entity': 'Applies to (both teams, home, away, or player name)'}
KEYS = {'home':'home','home/team 1':'home','away':'away','away/team 2':'away','sport':'sport',
    'competition':'competition','league':'competition','kick-off':'kickoff','kickoff':'kickoff',
    'market':'market_family','pick':'selection','selection':'selection','line':'line','period':'period',
    'counts':'stat','statistic':'stat','applies to':'entity'}
SPORTS = {'soccer':'football', 'american football':'american_football','nfl':'american_football',
          'nba':'basketball','mlb':'baseball','ufc':'mma'}
MARKETS = {'1x2':'result3','three-way':'result3','moneyline':'result2','two-way':'result2',
    'totals':'total','over/under':'total','spread':'handicap','double chance':'dc','draw no bet':'dnb'}
SIDES = {'result3': {'home','away','draw'},'result2':{'home','away'}, 'total':{'over','under'},
    'handicap':{'home','away'},'btts':{'yes','no'},'dc':{'1x','x2','12'},'dnb':{'home','away'}}


def missing(s):
    fields = [k for k in ('home','away','sport','competition','kickoff','market_family','selection','period','stat','entity') if not getattr(s,k)]
    if s.market_family in SIDES and s.selection not in SIDES[s.market_family] and 'selection' not in fields:
        fields.append('selection')
    if s.market_family in ('total','handicap') and number(s.line) is None: fields.append('line')
    if s.market_family in ('win','match result') and 'market_family' not in fields: fields.append('market_family')
    if s.home and s.away and normalized(s.home) == normalized(s.away) and 'away' not in fields:
        fields.append('away')
    return fields


def clarify(ticket, text):
    index = next((i for i,s in enumerate(ticket.selections) if missing(s)), None)
    if index is None: return ticket
    s = ticket.selections[index]
    needed = set(missing(s))
    updates = {}
    for line in text.splitlines():
        label, separator, value = line.partition(':')
        key = KEYS.get(normalized(label.split('(')[0]))
        if not separator or key is None or not value.strip() or key in updates: raise ValueError
        value = value.strip()
        if len(value) > 200: raise ValueError
        if key not in needed:  # never silently replace a correctly extracted field
            if normalized(value) != normalized(str(getattr(s,key))): raise ValueError
            continue
        updates[key] = value
    if not updates: raise ValueError
    if 'sport' in updates: updates['sport'] = SPORTS.get(normalized(updates['sport']), normalized(updates['sport']))
    if 'market_family' in updates: updates['market_family'] = MARKETS.get(normalized(updates['market_family']),normalized(updates['market_family']))
    if 'line' in updates:
        v = number(updates['line'])
        if v is None or abs(v) > 10000: raise ValueError
        updates['line'] = float(v)
    if 'kickoff' in updates:
        at = instant(updates['kickoff'])
        if at is None: raise ValueError
        updates['kickoff'] = at.isoformat()
    if 'period' in updates:
        updates['period'] = {'full match':'full','full game':'full','regulation':'reg','first half':'1h','second half':'2h'}.get(normalized(updates['period']),normalized(updates['period']))
    if 'stat' in updates: updates['stat'] = {'score':'points'}.get(normalized(updates['stat']),normalized(updates['stat']))
    if 'entity' in updates: updates['entity'] = {'both teams':'all','match':'all'}.get(normalized(updates['entity']),normalized(updates['entity']))
    updated = replace(s,**updates)
    if 'selection' in updates:
        side = normalized(updates['selection'])
        side = 'home' if side == normalized(updated.home) else 'away' if side == normalized(updated.away) else side
        updated = replace(updated,selection=side)
    if not set(missing(s)) - set(missing(updated)):
        raise ValueError
    legs = list(ticket.selections);legs[index] = updated
    return replace(ticket,selections=tuple(legs))


@dataclass
class Session:
    key: tuple[str,str]
    token: str
    expires: datetime
    state: str = 'WAITING'
    ticket: object = None
    review_id: str | None = None
    retries: int = 0
    last_message: int = -1
    pending_code: str | None = None  # memory only, until the bookmaker is chosen
    booking: object = None  # BookingCodeResult provenance; never part of NormalizedTicket


class TicketAuditController:
    def __init__(self,tg,cfg,backend_factory=None,clock=None,runner=None,booking_factory=None):
        self.tg,self.cfg = tg,cfg
        self.factory = backend_factory or (lambda: build_backend(cfg))
        self.booking_factory = booking_factory or (lambda: bk.build_booking_service(cfg))
        self._booking_service,self._booking_built = None,False
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.runner = runner
        self.sessions = OrderedDict()
        self.rates = OrderedDict()
        self.lock = threading.RLock()
        self.backend_lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(2)
        self.backend = None

    @staticmethod
    def key(message):
        chat = message.get('chat') or {}
        user = message.get('from') or {}
        uid,cid = str(user.get('id')),str(chat.get('id'))
        return (uid,cid) if chat.get('type') == 'private' and uid.isdigit() and cid.isdigit() else None

    def _prune(self):
        now = self.clock()
        for key,s in list(self.sessions.items()):
            if s.expires <= now:
                s.state,s.ticket,s.pending_code,s.booking = 'EXPIRED',None,None,None
                self.sessions.pop(key,None)
        for key,attempts in list(self.rates.items()):
            self.rates[key] = [at for at in attempts if now-at < timedelta(hours=1)]
            if not self.rates[key]: self.rates.pop(key,None)

    def active(self,key):
        with self.lock:
            self._prune()
            return bool(key in self.sessions and self.sessions[key].state in ACTIVE)

    def leave(self,key):
        with self.lock:
            s = self.sessions.pop(key,None)
            if s: s.state,s.ticket,s.pending_code,s.booking = 'CANCELLED',None,None,None

    def start(self,message):
        if not ticket_audit_switch.ENABLED: return False
        key = self.key(message)
        if key is None: return False
        with self.lock:
            self._prune()
            self.leave(key)
            if len(self.sessions) >= 500:
                self._send(UNAVAILABLE,key[1]);return True
            self.sessions[key] = Session(key,secrets.token_urlsafe(18),self.clock()+TTL)
        self._send(INTRO_CODES if self._booking() else INTRO,key[1])
        return True

    def _send(self,text,chat,markup=None):
        try: return self.tg.send(text,chat_id=chat,reply_markup=markup)
        except Exception as exc:
            log.warning('Ticket Audit delivery failed | error_type=%s',type(exc).__name__)
            return None

    def _ack(self,cb,text):
        try: self.tg.answer_callback_query(str(cb.get('id') or ''),text=text)
        except Exception as exc: log.warning('Ticket Audit acknowledgement failed | error_type=%s',type(exc).__name__)

    def _current(self,s,state=None,token=None):
        return (self.sessions.get(s.key) is s and s.expires > self.clock() and s.state in ACTIVE
                and (state is None or s.state == state) and (token is None or s.token == token))

    def _booking(self):
        """Lazily built booking-code service, or None when disabled. Opens no network."""
        with self.backend_lock:
            if not self._booking_built:
                try: self._booking_service = self.booking_factory()
                except Exception as exc:
                    log.warning('Ticket Audit booking-code setup failed | error_type=%s',type(exc).__name__)
                    self._booking_service = None
                self._booking_built = True
            return self._booking_service

    def _backend(self):
        with self.backend_lock:
            if self.backend is None: self.backend = self.factory()
            return self.backend

    def _submit(self,job):
        if not ticket_audit_switch.ENABLED: return False
        if not self.slots.acquire(blocking=False): return False
        def run():
            try: job()
            finally: self.slots.release()
        try:
            if self.runner: self.runner(run)
            else: threading.Thread(target=run,daemon=True,name='ticket-audit').start()
        except Exception:
            self.slots.release()
            return False
        return True

    def message(self,msg):
        if not ticket_audit_switch.ENABLED: return False
        key = self.key(msg)
        if key is None: return False
        with self.lock:
            self._prune()
            s = self.sessions.get(key)
            if not s or s.state not in ACTIVE: return False
            mid = msg.get('message_id')
            if type(mid) is int:
                if mid <= s.last_message: return True
                s.last_message = mid
            if s.state in BUSY:
                self._send('I’m checking your ticket. Please wait a moment.',key[1]);return True
            text = (msg.get('text') or '').strip()
            if s.state == 'PENDING_REVIEW':
                self._send('Use Audit Ticket, Send Again or Cancel below the review.',key[1]);return True
            if s.state == 'CLARIFYING':
                if not text:
                    self._send('Reply with the labelled details above, or use Send Again for a new ticket.',key[1]);return True
                try:
                    updated = clarify(s.ticket,text)
                    if updated == s.ticket: raise ValueError
                    s.ticket,s.retries = updated,0
                except (ValueError,TypeError):
                    s.retries += 1
                    if s.retries >= 2:
                        self._reset(s)
                        self._send('I still can’t identify that selection safely. Please resend the full ticket with more detail.',key[1])
                        return True
                self._review_or_clarify(s)
                return True
            if text and re.fullmatch(r'[A-Za-z0-9_-]{4,32}',text) and (any(c.isdigit() for c in text) or (text.isupper() and len(text)>=6)):
                self._booking_code(s,text);return True
            if s.state == 'CHOOSING_BOOKMAKER':  # a screenshot/ticket replaces the pending code
                s.state,s.pending_code,s.review_id = 'WAITING',None,None
            image = self._image(msg)
            if not text and image is None:
                self._send(SUPPORTED,key[1]);return True
            if not text and image is False:
                self._send(SUPPORTED,key[1]);return True
            if not text and image == 'large':
                self._send('Please send a smaller screenshot (up to 5 MB).',key[1]);return True
            if len(text)>8000:
                self._send('Please send ticket text under 8,000 characters, or use a booking code.',key[1]);return True
            attempts = self.rates.get(key)
            if (attempts is not None and len(attempts)>=5) or (attempts is None and len(self.rates)>=500):
                self._send('Please wait before sending another ticket. Try again later.',key[1]);return True
            attempts = self.rates.setdefault(key,[])
            s.state = 'EXTRACTING'
            s.token,s.review_id = secrets.token_urlsafe(18),None
            s.expires = self.clock()+TTL
            token = s.token
            def extract():
                self._extract(s,token,text,image if isinstance(image,str) and image != 'large' else None)
            if not self._submit(extract):
                s.state = 'WAITING'
                self._send('Ticket Audit is busy. Try again in a moment.',key[1]);return True
            attempts.append(self.clock())
            return True

    def _booking_code(self,s,text):
        """Code-only input: ask ONLY for the bookmaker. Retrieval starts after the choice."""
        service = self._booking()
        if service is None:
            self._send(BOOKING,s.key[1]);return
        try: code = bk.normalize_code(text)
        except bk.BookingCodeFailure:
            self._send(CODE_MESSAGES[bk.MALFORMED_CODE],s.key[1]);return
        s.state,s.pending_code,s.ticket,s.booking = 'CHOOSING_BOOKMAKER',code,None,None
        s.token,s.review_id,s.expires = secrets.token_urlsafe(18),None,self.clock()+TTL
        names = [(k,bk.BOOKMAKERS[k]) for k in service.bookmakers()]
        rows = [[{'text':name,'callback_data':PREFIX+s.token+'|bk:'+k} for k,name in names[i:i+3]]
                for i in range(0,len(names),3)]
        rows += [[{'text':'More','callback_data':PREFIX+s.token+'|bk:other'}],
                 [{'text':'❌ Cancel','callback_data':PREFIX+s.token+'|cancel'}]]
        mid = self._send(CHOOSE_BOOKMAKER,s.key[1],{'inline_keyboard':rows})
        if mid: s.review_id = str(mid)
        else: self._reset(s)

    def _choose_bookmaker(self,s,cb,choice,message):
        """Called under self.lock with a current, owned CHOOSING_BOOKMAKER session."""
        key = s.key
        if str(message.get('message_id'))!=s.review_id:
            self._ack(cb,'Use the buttons on your latest message.');return
        code,s.pending_code = s.pending_code,None
        if choice not in bk.BOOKMAKERS or not code:
            self._reset(s);self._ack(cb,'Not supported yet.')
            self._send(CODE_MESSAGES[bk.UNSUPPORTED_BOOKMAKER],key[1]);return
        attempts = self.rates.get(key)
        if (attempts is not None and len(attempts)>=5) or (attempts is None and len(self.rates)>=500):
            self._reset(s);self._ack(cb,'Please wait.')
            self._send('Please wait before sending another ticket. Try again later.',key[1]);return
        attempts = self.rates.setdefault(key,[])
        s.state,s.review_id = 'DECODING',None
        token = s.token
        self._ack(cb,'Loading your ticket…')
        if not self._submit(lambda:self._decode(s,token,choice,code)):
            self._reset(s)
            self._send('Ticket Audit is busy. Try again in a moment.',key[1]);return
        attempts.append(self.clock())

    def _decode(self,s,token,bookmaker,code):
        """bookmaker + code -> resolver -> NormalizedTicket -> the SAME review. No Gemini here."""
        try:
            with self.lock:
                if not self._current(s,'DECODING',token): return
            self._send('🔎 Loading your booking code…',s.key[1])
            result = self._booking().resolve(bookmaker,code,self.clock())
            code = None
            ticket = bk.to_ticket(result) if result.valid else None
        except TicketResourceLimit as exc:
            status = bk.RESPONSE_TOO_LARGE if str(exc) == 'ticket_input_too_large' else bk.PARSER_LIMIT
            result,ticket = bk.BookingCodeResult(bookmaker,'','',status),None
        except Exception as exc:
            log.warning('Ticket Audit booking-code decode failed | error_type=%s',type(exc).__name__)
            result,ticket = None,None
        status = result.status if result is not None else bk.PROVIDER_UNAVAILABLE
        with self.lock:
            if not self._current(s,'DECODING',token): return
            if ticket is None:
                self._reset(s)
            else:
                s.ticket,s.booking,s.state = ticket,result,'REVIEWING'
        if ticket is None:
            self._send(CODE_MESSAGES.get(status,CODE_MESSAGES[bk.PROVIDER_UNAVAILABLE]),s.key[1]);return
        # Decoded legs never go to clarification: gaps are shown and audited as insufficient.
        self._deliver_review(s,token,audit_format.booking_review(ticket,result))

    @staticmethod
    def _image(msg):
        if msg.get('photo'):
            photos = [p for p in msg['photo'] if isinstance(p,dict) and isinstance(p.get('file_id'),str)]
            eligible = [p for p in photos if type(p.get('file_size')) is not int or p['file_size']<=5_000_000]
            if not eligible: return 'large'
            best = max(eligible,key=lambda p:(int(p.get('width') or 0)*int(p.get('height') or 0),p.get('file_size') or 0))
            return best['file_id']
        doc = msg.get('document')
        if doc:
            if doc.get('mime_type') not in ('image/png','image/jpeg'): return False
            if type(doc.get('file_size')) is int and doc['file_size']>5_000_000: return 'large'
            return doc.get('file_id') or False
        return None

    def _reset(self,s):
        s.state,s.ticket,s.review_id,s.retries = 'WAITING',None,None,0
        s.pending_code,s.booking = None,None
        s.token = secrets.token_urlsafe(18)

    @diagnosed('backend_init')
    def _extract(self,s,token,text,file_id):
        image = None
        try:
            with self.lock:
                if not self._current(s,'EXTRACTING',token): return
            backend = self._backend()
            note_settings(backend.client.settings)
            note_quota(backend.client.usage.path, backend.client.clock())
            with self.lock:
                if not self._current(s,'EXTRACTING',token): return
            if file_id:
                note(stage='telegram_image_download')
                image = self.tg.download_audit_image(file_id)
                with self.lock:
                    if not self._current(s,'EXTRACTING',token): return
                mime = 'image/png' if image.startswith(b'\x89PNG\r\n\x1a\n') else 'image/jpeg'
                note(mime_type=mime, image_size_bytes=len(image))
                result = backend.client.extract(image=(mime,image))
            else: result = backend.client.extract(text=text)
            note(stage='ticket_revalidation')
            result = ticket_from_json(json.dumps(asdict(result)))
            with self.lock:
                if not self._current(s,'EXTRACTING',token): return
                s.ticket = result
            note(stage='ticket_review')
            self._review_or_clarify(s,token)
        except Exception as exc:
            diagnostics = failure_details(exc)
            log.warning('Ticket Audit extraction failed | error_type=%s | diagnostics=%s',
                        type(exc).__name__,json.dumps(diagnostics,sort_keys=True),
                        extra={'audit_diagnostics':diagnostics})
            with self.lock:
                if not self._current(s,'EXTRACTING',token): return
                self._reset(s)
                unavailable = isinstance(exc,AuditUnavailable) and str(exc) not in (
                    'malformed_structured_output','extraction_type_mismatch','invalid_image_input','safety_refusal_or_incomplete_output','empty_response')
            self._send(RESOURCE_LIMIT if isinstance(exc,TicketResourceLimit) else
                       INCOMPLETE if str(exc) == 'incomplete_extraction' else
                       'Ticket text exceeds the safe model input size. Try a booking code.' if str(exc) == 'input_too_large' else
                       UNAVAILABLE if unavailable else UNREADABLE,s.key[1])
        finally:
            image = None  # only memory; no file IDs/bytes retained in session

    def _review_or_clarify(self,s,token=None):
        token = token or s.token
        with self.lock:
            if not self._current(s,token=token): return
            ticket = s.ticket
            first = next(((i,missing(leg)) for i,leg in enumerate(ticket.selections) if missing(leg)),None)
            s.state = 'CLARIFYING' if first else 'REVIEWING'
        if first:
            i,keys = first
            text = (f'Selection {i+1}: {audit_format.fixture(ticket.selections[i]) or audit_format.pick(ticket.selections[i])}\n\n'
                'I need a few details to identify this selection safely. Reply with the missing details on labelled lines:\n'
                + '\n'.join(FIELD_LABELS[k]+':' for k in keys) + '\n\nOr resend the full ticket using Send Again.')
            with self.lock:
                if not self._current(s,'CLARIFYING',token): return
                markup = self._markup(s,confirm=False)
            self._send(text,s.key[1],markup);return
        self._deliver_review(s,token,audit_format.review(ticket))

    def _deliver_review(self,s,token,pages):
        for page in pages[:-1]:
            with self.lock:
                if not self._current(s,'REVIEWING',token): return
            if not self._send(page,s.key[1]):
                with self.lock:
                    if self._current(s,'REVIEWING',token): self._reset(s)
                return
        with self.lock:
            if not self._current(s,'REVIEWING',token): return
            markup = self._markup(s)
        mid = self._send(pages[-1],s.key[1],markup)
        with self.lock:
            if not self._current(s,'REVIEWING',token): return
            if not mid:
                self._reset(s);return
            s.review_id,s.state = str(mid),'PENDING_REVIEW'
        log.info('Ticket Audit review | normalized_selection_count=%s | telegram_review_message_count=%s',
                 len(s.ticket.selections), len(pages))

    @staticmethod
    def _markup(s,confirm=True):
        rows = [[{'text':'✅ Audit Ticket','callback_data':PREFIX+s.token+'|confirm'}]] if confirm else []
        rows.extend([[{'text':'✏️ Send Again','callback_data':PREFIX+s.token+'|resend'}],
                     [{'text':'❌ Cancel','callback_data':PREFIX+s.token+'|cancel'}]])
        return {'inline_keyboard':rows}

    def callback(self,cb):
        if not ticket_audit_switch.ENABLED: return
        message = cb.get('message') or {}
        key = self.key({**message,'from':cb.get('from')})
        if key is None:
            self._ack(cb,'Open Ticket Audit in your private chat with the bot.');return
        data = str(cb.get('data') or '').split('|')
        with self.lock:
            self._prune();s = self.sessions.get(key)
            if len(data)!=3 or not s or not self._current(s) or data[0]!='ta' or data[1]!=s.token:
                self._ack(cb,'This review has expired. Open Ticket Audit again.');return
            action = data[2]
            if action.startswith('bk:') and s.state == 'CHOOSING_BOOKMAKER':
                self._choose_bookmaker(s,cb,action[3:],message);return
            if action in ('resend','cancel'):
                if action == 'cancel':
                    self.leave(key);self._ack(cb,'Cancelled.');self._send('Ticket Audit cancelled.',key[1])
                else:
                    self._reset(s);self._ack(cb,'Send your replacement ticket.')
                    self._send('Send a new booking code or screenshot, or paste the ticket selections.' if self._booking()
                               else 'Send a new screenshot or paste the ticket selections.',key[1])
                return
            if action!='confirm':
                self._ack(cb,'Use the buttons on your current review.');return
            if s.state in ('AUDITING','COMPLETE'):
                self._ack(cb,'Already checked — see your result above.' if s.state=='COMPLETE' else 'I’m already checking this ticket.');return
            if s.state!='PENDING_REVIEW' or str(message.get('message_id'))!=s.review_id:
                self._ack(cb,'Please finish reviewing your current ticket.');return
            s.state = 'AUDITING'
            token = s.token
            self._ack(cb,'Checking your ticket…')
            self._send('🔍 Checking your ticket…',key[1])
            if not self._submit(lambda:self._audit(s,token)):
                s.state = 'PENDING_REVIEW'
                self._send('Ticket Audit is busy. Try Audit Ticket again in a moment.',key[1])

    def _audit(self,s,token):
        try:
            with self.lock:
                if not self._current(s,'AUDITING',token): return
                ticket = s.ticket
            result = self._backend().service.audit(ticket,self.clock())
            pages = audit_format.results(ticket,result)
            with self.lock:
                if not self._current(s,'AUDITING',token): return
                s.state,s.ticket = 'COMPLETE',None  # consume before delivery; never rerun on a failed send
            delivered = 0
            for page in pages:
                with self.lock:
                    if not self._current(s,'COMPLETE',token): return
                if not self._send(page,s.key[1]):
                    self._send('I couldn’t deliver the complete result. Please send a new ticket to try again.',s.key[1]);break
                delivered += 1
            log.info('Ticket Audit result | selection_count=%s | telegram_result_message_count=%s | complete=%s',
                     len(result.selections), delivered, delivered == len(pages))
        except Exception as exc:
            log.warning('Ticket Audit execution failed | error_type=%s',type(exc).__name__)
            with self.lock:
                if self._current(s,'AUDITING',token):
                    s.state,s.ticket = 'COMPLETE',None
                else: return
            self._send(RESOURCE_LIMIT if isinstance(exc,TicketResourceLimit) else UNAVAILABLE,s.key[1])
