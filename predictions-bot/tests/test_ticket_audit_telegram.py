"""Private V1 regressions: real extraction/service, mocked transports, network firewall."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import timedelta
import base64
import json
import socket
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from predbot.bot import audit_format, callbacks
from predbot.bot.commands import CommandLoop
from predbot.bot.telegram import Telegram, TelegramImageError
from predbot.bot.ticket_audit import TicketAuditController, INTRO, UNAVAILABLE, UNREADABLE, RESOURCE_LIMIT
from predbot.config import Config
from predbot.ticket_audit.analysis import EvidenceAnalyzer
from predbot.ticket_audit.composition import finish, local_probability
from predbot.ticket_audit.gemini import Settings, UsageGuard
from predbot.ticket_audit.local import LocalResolver
from predbot.ticket_audit.runtime import build_backend
from predbot.ticket_audit.schemas import AuditUnavailable, LocalEvidenceMatch
from predbot.ticket_audit.service import AuditService
from tests.test_community_menu import MenuTelegram, BOT_USERNAME, COMMUNITY, message_update
from tests.test_ticket_audit_foundation import (no_providers, local, selection, ticket, client,
    response, NOW, db_dump, evidence, forbidden)
from tests.test_ticket_audit_free_research import setup_source
import diagnose_gemini_audit as diag

KEY = ('42','42')


@pytest.fixture(autouse=True)
def extra_firewall(monkeypatch):
    from predbot.jobs import settle, live_results
    for obj,name in ((settle,'run'),(settle,'_prefetch'),(live_results,'run'),(live_results,'_ask')):
        monkeypatch.setattr(obj,name,forbidden)
    monkeypatch.setattr(socket.socket,'connect',forbidden)


def input_message(text=None, mid=1, **media):
    msg = message_update(text or '',user_id='42')['message']
    msg['message_id'] = mid
    msg.update(media)
    return msg


def tap(controller, action='confirm', user='42', chat='42', token=None, mid=None):
    s = controller.sessions.get(KEY)
    return {'id':'tap', 'from':{'id':int(user)},
        'data':f'ta|{token or s.token}|{action}',
        'message':{'message_id':mid or (s.review_id if s else '123'),
            'chat':{'id':int(chat),'type':'private' if not chat.startswith('-') else 'supergroup'}}}


@pytest.fixture
def ui(db,tmp_path):
    cfg = Config(db_path=db.path, timezone='UTC',telegram_chat_id=COMMUNITY,telegram_admin_ids=['42'])
    tg = MenuTelegram()
    c = client(tmp_path,response(json.dumps(asdict(replace(ticket(),input_type='text')))),
        settings=Settings('MOCK-KEY','gemini-3.5-flash-lite',True,True))
    service = AuditService(LocalResolver(db.path,'UTC'))
    backend = SimpleNamespace(client=c,service=service)
    factory = Mock(return_value=backend)
    controller = TicketAuditController(tg,cfg,factory,clock=lambda:NOW,runner=lambda job:job())
    return SimpleNamespace(tg=tg,cfg=cfg,client=c,service=service,factory=factory,controller=controller)


def prepare(ui,t=None):
    if t is not None:
        ui.client.transport.request.return_value.json.return_value = response(json.dumps(asdict(replace(t,input_type='text'))))
    ui.controller.start(input_message())
    ui.controller.message(input_message('Arsenal vs Chelsea: over 2.5, football, EPL, June 1 2030 at 15:00 UTC'))
    return ui.controller.sessions[KEY]


def joined(ui):
    return '\n\n'.join(s['text'] for s in ui.tg.sends)


def test_menu_adds_same_bot_private_link_and_preserves_all_existing_buttons():
    rows = callbacks.community_menu_markup(BOT_USERNAME)['inline_keyboard']
    assert rows[4] == [{'text':'🔍 Ticket Audit','url':f'https://t.me/{BOT_USERNAME}?start=ticket_audit'}]
    assert [b['callback_data'] for row in rows[:3] for b in row] == ['pg|top|0','pg|daily|0','pg|all|0','pg|stats|0','pg|yesterday|0']
    assert rows[3][0]['url'].endswith('?start=custom_picks')


def test_start_lazy_no_subscription_no_generation_no_http(ui,db):
    before = db_dump(db)
    loop = CommandLoop(ui.tg,db,ui.cfg,{})
    loop.instance='test'
    loop._ticket_audit = ui.controller
    loop._handle_update(message_update('/start ticket_audit',user_id='42'))
    assert ui.controller.active(KEY) and ui.tg.sends[-1]['text']==INTRO
    ui.factory.assert_not_called()
    ui.client.transport.request.assert_not_called()
    assert db_dump(db)==before


@pytest.mark.parametrize('kind',['group','supergroup','channel'])
def test_nonprivate_entry_redirects_without_intake(ui,db,kind):
    loop = CommandLoop(ui.tg,db,ui.cfg,{})
    loop._ticket_audit=ui.controller
    update = message_update('/start ticket_audit',COMMUNITY,kind,user_id='42')
    if kind=='channel': update['channel_post']=update.pop('message')
    before = db_dump(db)
    loop._handle_update(update)
    assert not ui.controller.sessions
    assert ui.tg.sends[-1]['reply_markup']['inline_keyboard'][0][0]['url'].endswith('?start=ticket_audit')
    assert db_dump(db)==before
    ui.factory.assert_not_called()


@pytest.mark.parametrize('media',[{'text':'Arsenal over 2.5'}, {'photo':[{'file_id':'group-image','width':100,'height':100}]}])
@pytest.mark.parametrize('kind',['group','supergroup','channel'])
def test_group_text_images_never_enter_ticket_or_custom_modes(ui,db,media,kind):
    prepare(ui)
    before = ui.client.transport.request.call_count
    loop=CommandLoop(ui.tg,db,ui.cfg,{})
    loop._ticket_audit=ui.controller
    msg={'chat':{'id':int(COMMUNITY),'type':kind},'from':{'id':42},**media}
    loop._handle_update({'message':msg})
    assert ui.client.transport.request.call_count==before
    assert ui.controller.sessions[KEY].state=='PENDING_REVIEW'


@pytest.mark.parametrize('command',['/start','/start other','/start custom_picks','/stop'])
def test_mode_switch_invalidates_audit_but_preserves_original_behavior(ui,db,command):
    s=prepare(ui); old=tap(ui.controller)
    loop=CommandLoop(ui.tg,db,ui.cfg,{})
    loop.instance='test';loop._ticket_audit=ui.controller
    loop._custom_users.add(KEY)
    loop._handle_update(message_update(command,user_id='42'))
    assert not ui.controller.active(KEY)
    ui.controller.callback(old)
    assert 'expired' in ui.tg.answers[-1]['text']
    assert (KEY in loop._custom_users)==(command=='/start custom_picks')
    assert bool(db.subscribers_active())==(command in ('/start','/start other'))


def test_audit_entry_exits_custom_mode_and_old_recovery_cannot_run(ui,db):
    from predbot.custom_recovery import CALLBACK_PREFIX
    loop=CommandLoop(ui.tg,db,ui.cfg,{})
    loop._ticket_audit=ui.controller;loop._custom_users.add(KEY)
    loop._custom_picks.select=Mock(side_effect=AssertionError('old recovery forbidden'))
    loop._handle_update(message_update('/start ticket_audit',user_id='42'))
    assert KEY not in loop._custom_users
    cb=tap(ui.controller);cb['data']=CALLBACK_PREFIX+'old'
    loop._dispatch_callback(cb)
    loop._custom_picks.select.assert_not_called()
    assert 'Custom Picks' in ui.tg.answers[-1]['text']


def test_private_help_mentions_audit_without_public_clutter(ui,db):
    loop=CommandLoop(ui.tg,db,ui.cfg,{})
    assert '/start ticket_audit' in loop.handle('/help','42','42')
    assert '/start ticket_audit' not in loop.handle('/help','42',COMMUNITY)


def test_text_extraction_review_has_no_evidence_call_before_confirm(ui,db):
    spy=Mock(wraps=ui.service.audit);ui.service.audit=spy
    before=db_dump(db);s=prepare(ui)
    assert s.state=='PENDING_REVIEW' and 'over 2.5' in joined(ui)
    assert '@' not in ui.tg.sends[-1]['text'] and 'Combined odds' not in joined(ui)
    assert [r[0]['text'] for r in ui.tg.sends[-1]['reply_markup']['inline_keyboard']]==['✅ Audit Ticket','✏️ Send Again','❌ Cancel']
    spy.assert_not_called()
    assert ui.client.transport.request.call_count==1 and db_dump(db)==before
    body=ui.client.transport.request.call_args.kwargs['json']
    assert 'tools' not in body and 'maxItems' not in body['generationConfig']['responseFormat']['text']['schema']['properties']['selections']
    assert 'first_name' not in json.dumps(body) and 'chat_id' not in json.dumps(body)


def test_review_preserves_submitted_individual_and_combined_odds_without_recalculation(ui):
    t=replace(ticket(selection(submitted_odds=1.75),selection(home='Barcelona',away='Sevilla',submitted_odds=1.62)),
        submitted_combined_odds=2.84)
    prepare(ui,t)
    assert '@ 1.75' in joined(ui) and '@ 1.62' in joined(ui) and 'Combined odds: 2.84' in joined(ui)
    assert '2.835' not in joined(ui)


@pytest.mark.parametrize('bad',['not json','{}','[]'])
def test_malformed_extraction_friendly_and_no_audit(ui,bad):
    ui.client.transport.request.return_value.json.return_value=response(bad)
    ui.controller.start(input_message());ui.controller.message(input_message('Not a readable ticket at all'))
    assert ui.tg.sends[-1]['text']==UNREADABLE
    assert ui.controller.sessions[KEY].state=='WAITING'
    assert ui.client.transport.request.call_count==1


@pytest.mark.parametrize('code',['ABCD12','1234567','MYBOOKCODE'])
def test_booking_code_only_never_calls_betloy_or_gemini(ui,code):
    ui.controller.start(input_message());ui.controller.message(input_message(code))
    assert 'Booking codes' in ui.tg.sends[-1]['text']
    ui.factory.assert_not_called()


def test_pasted_large_ticket_reaches_review_without_leg_limit(ui):
    prepare(ui,ticket(*[selection() for _ in range(100)]))
    assert ui.controller.sessions[KEY].state == 'PENDING_REVIEW'
    assert len(ui.controller.sessions[KEY].ticket.selections) == 100


@pytest.mark.parametrize('change,reply',[({'away':None},'Away/team 2: Chelsea'),
    ({'market_family':None},'Market (e.g. 1X2, moneyline, total, handicap): total'),
    ({'line':None},'Line: 2.5'),({'kickoff':None},'Kick-off (date, time and UTC offset): 2030-06-01T15:00:00Z'),
    ({'sport':None},'Sport: football'),({'period':None,'stat':None,'entity':None},'Period: full match\nCounts: score\nApplies to: both teams'),
    ({'away':'Arsenal'},'Away: Chelsea')])
def test_missing_or_ambiguous_fields_clarified_deterministically_without_research(ui,change,reply):
    s=prepare(ui,ticket(selection(**change)))
    assert s.state=='CLARIFYING'
    assert not any(b.get('callback_data','').endswith('|confirm') for row in ui.tg.sends[-1]['reply_markup']['inline_keyboard'] for b in row)
    ui.controller.message(input_message(reply,mid=2))
    assert s.state=='PENDING_REVIEW'
    assert ui.client.transport.request.call_count==1


def test_clarification_two_failed_replies_request_full_resend(ui):
    s=prepare(ui,ticket(selection(market_family='win')))
    ui.controller.message(input_message('Market: win',mid=2))
    assert s.state=='CLARIFYING'
    ui.controller.message(input_message('guess it please',mid=3))
    assert s.state=='WAITING' and s.ticket is None
    assert 'resend the full ticket' in ui.tg.sends[-1]['text']
    assert ui.client.transport.request.call_count==1


@pytest.mark.parametrize('kind',['photo','document'])
def test_screenshot_real_gemini_image_contract_memory_only_no_metadata(ui,tmp_path,kind):
    png=diag.synthetic_image(diag.NATURAL_TEXT_SLIP)
    ui.tg.download_audit_image=Mock(return_value=png)
    ui.client.transport.request.return_value.json.return_value=response(json.dumps(asdict(replace(ticket(),input_type='image'))))
    media=({'photo':[{'file_id':'small','width':10,'height':10,'file_size':300},
        {'file_id':'best','width':1000,'height':1000,'file_size':10000},
        {'file_id':'oversize','width':2000,'height':2000,'file_size':6_000_000}]} if kind=='photo'
        else {'document':{'file_id':'best','mime_type':'image/png','file_size':10000,'file_name':'private-name.png'}})
    before={str(p) for p in tmp_path.rglob('*')}
    ui.controller.start(input_message());ui.controller.message(input_message(**media))
    ui.tg.download_audit_image.assert_called_once_with('best')
    assert ui.controller.sessions[KEY].state=='PENDING_REVIEW'
    body=ui.client.transport.request.call_args.kwargs['json']
    inline=body['contents'][0]['parts'][1]['inlineData']
    assert inline['mimeType']=='image/png' and base64.b64decode(inline['data'])==png
    assert 'private-name' not in json.dumps(body) and 'file_id' not in json.dumps(body)
    assert {str(p) for p in tmp_path.rglob('*')}==before
    assert 'file_id' not in asdict(ui.controller.sessions[KEY]) and 'image' not in asdict(ui.controller.sessions[KEY])


@pytest.mark.parametrize('media',[{'document':{'file_id':'pdf','mime_type':'application/pdf'}},
    {'voice':{'file_id':'voice'}},{'video':{'file_id':'video'}},
    {'photo':[{'file_id':'huge','file_size':5_000_001}]}])
def test_unsupported_or_large_images_no_google_request(ui,media):
    ui.controller.start(input_message());ui.controller.message(input_message(**media))
    ui.factory.assert_not_called()
    assert 'screenshot' in ui.tg.sends[-1]['text']


def test_image_download_failure_no_extraction_and_sanitized(ui,caplog):
    ui.tg.download_audit_image=Mock(side_effect=TelegramImageError('secret path'))
    ui.controller.start(input_message());ui.controller.message(input_message(photo=[{'file_id':'x'}]))
    assert ui.tg.sends[-1]['text']==UNREADABLE
    ui.client.transport.request.assert_not_called()
    assert 'secret path' not in caplog.text


@pytest.mark.parametrize('error',[AuditUnavailable('gemini_rate_limited'),AuditUnavailable('model_unavailable'),
    AuditUnavailable('daily_budget_exhausted'),AuditUnavailable('private_audit_disabled')])
def test_gemini_unavailable_fail_closed_no_retry(ui,error):
    ui.client.extract=Mock(side_effect=error)
    prepare(ui)
    assert ui.tg.sends[-1]['text']==UNAVAILABLE
    assert ui.client.extract.call_count==1


def test_gemini_image_failure_friendly_no_retry(ui):
    ui.tg.download_audit_image=Mock(return_value=b'\x89PNG\r\n\x1a\nbytes')
    ui.client.transport.request.side_effect=requests.Timeout('SECRET-URL')
    ui.controller.start(input_message());ui.controller.message(input_message(photo=[{'file_id':'x'}]))
    assert ui.tg.sends[-1]['text']==UNAVAILABLE
    assert ui.client.transport.request.call_count==1


@pytest.mark.parametrize('action',['resend','cancel'])
def test_review_resend_cancel_no_audit_invalidate_old_callbacks(ui,action):
    s=prepare(ui);old=tap(ui.controller)
    ui.controller.callback(tap(ui.controller,action))
    assert s.ticket is None
    ui.controller.callback(old)
    assert 'expired' in ui.tg.answers[-1]['text']
    assert (s.state=='WAITING') if action=='resend' else not ui.controller.active(KEY)
    assert ui.client.transport.request.call_count==1


@pytest.mark.parametrize('user,chat',[('99','42'),('42','99'),('42',COMMUNITY)])
def test_callbacks_bound_to_original_private_user_chat(ui,user,chat):
    s=prepare(ui)
    ui.controller.callback(tap(ui.controller,user=user,chat=chat))
    assert s.state=='PENDING_REVIEW'
    assert not any('Ticket Audit\n\n1 selections' in sent['text'] for sent in ui.tg.sends)


def test_callback_expiry_restart_and_wrong_review_id(ui):
    s=prepare(ui);cb=tap(ui.controller)
    ui.controller.callback(tap(ui.controller,mid='999'))
    assert s.state=='PENDING_REVIEW'
    replacement=TicketAuditController(ui.tg,ui.cfg,ui.factory,clock=lambda:NOW)
    replacement.callback(cb)
    assert 'expired' in ui.tg.answers[-1]['text']
    ui.controller.clock=lambda:NOW+timedelta(minutes=16)
    ui.controller.callback(cb)
    assert not ui.controller.sessions and s.ticket is None
    assert len(cb['data'].encode())<=64


def test_duplicate_and_concurrent_confirmation_execute_once(ui,db):
    s=prepare(ui);jobs=[];ui.controller.runner=jobs.append
    spy=Mock(wraps=ui.service.audit);ui.service.audit=spy
    cb=tap(ui.controller);before=db_dump(db)
    with ThreadPoolExecutor(max_workers=8) as pool: list(pool.map(lambda _:ui.controller.callback(cb),range(12)))
    assert len(jobs)==1 and s.state=='AUDITING'
    assert sum(x['text']=='🔍 Checking your ticket…' for x in ui.tg.sends)==1
    jobs.pop()()
    ui.controller.callback(cb)
    assert s.state=='COMPLETE' and s.ticket is None
    assert spy.call_count==1 and db_dump(db)==before


def test_resend_during_queued_audit_old_job_cannot_run_or_publish(ui):
    s=prepare(ui);jobs=[];ui.controller.runner=jobs.append
    cb=tap(ui.controller);ui.controller.callback(cb)
    ui.controller.callback(tap(ui.controller,'resend'))
    ui.controller.message(input_message('New complete ticket selection',mid=2))
    assert s.state=='EXTRACTING' and len(jobs)==2
    old=jobs.pop(0);old()
    assert s.state=='EXTRACTING'
    jobs.pop()()
    assert s.state=='PENDING_REVIEW'
    ui.controller.callback(cb)
    assert 'expired' in ui.tg.answers[-1]['text']


def test_cancel_during_running_service_suppresses_result(ui):
    prepare(ui)
    original=ui.service.audit
    def audit(t,now):
        ui.controller.callback(tap(ui.controller,'cancel'))
        return original(t,now)
    ui.service.audit=audit
    ui.controller.callback(tap(ui.controller))
    assert not ui.controller.active(KEY)
    assert 'Evidence: ' not in joined(ui)


def test_cancel_during_image_download_skips_google_extraction(ui):
    def download(file_id):
        ui.controller.leave(KEY)
        return b'\x89PNG\r\n\x1a\nimage'
    ui.tg.download_audit_image=download
    ui.controller.start(input_message());ui.controller.message(input_message(photo=[{'file_id':'x'}]))
    ui.client.transport.request.assert_not_called()
    assert not ui.controller.active(KEY)


def test_old_running_audit_cannot_publish_into_replacement_audit(ui):
    prepare(ui);first=tap(ui.controller);original=ui.service.audit;jobs=[]
    def first_audit(t,now):
        ui.controller.callback(tap(ui.controller,'resend'))
        ui.controller.runner=jobs.append
        ui.controller.message(input_message('Replacement ticket to extract',mid=2))
        jobs.pop(0)()
        ui.controller.callback(tap(ui.controller))
        return original(t,now)
    ui.service.audit=first_audit
    ui.controller.callback(first)
    assert ui.controller.sessions[KEY].state=='AUDITING'
    assert 'Evidence:' not in joined(ui)
    ui.service.audit=original
    jobs.pop(0)()
    assert joined(ui).count('Evidence:')==1


def test_background_extraction_keeps_existing_private_view_commands_responsive(ui,db):
    jobs=[];ui.controller.runner=jobs.append
    loop=CommandLoop(ui.tg,db,ui.cfg,{},service=Mock())
    loop._ticket_audit=ui.controller
    loop._send_view=Mock()
    loop._handle_update(message_update('/start ticket_audit',user_id='42'))
    loop._handle_update({'message':input_message('Complete ticket for background extraction')})
    for cmd in ('/top','/daily','/stats'):loop._handle_update(message_update(cmd,user_id='42'))
    assert loop._send_view.call_count==3 and len(jobs)==1
    assert ui.controller.sessions[KEY].state=='EXTRACTING'
    jobs.pop()()


def test_confirmation_runs_on_bounded_daemon_thread_by_default(ui):
    import threading
    done=threading.Event();thread_details=[];prepare(ui)
    original=ui.service.audit
    def audit(t,now):
        thread_details.append((threading.current_thread().name,threading.current_thread().daemon))
        result=original(t,now);done.set();return result
    ui.service.audit=audit;ui.controller.runner=None
    ui.controller.callback(tap(ui.controller))
    assert done.wait(timeout=2) and thread_details==[('ticket-audit',True)]


def test_failed_telegram_result_send_consumes_audit_and_never_reexecutes(ui):
    prepare(ui);cb=tap(ui.controller);original_send=ui.tg.send
    spy=Mock(wraps=ui.service.audit);ui.service.audit=spy
    def send(text,**kwargs):
        return None if text.startswith('🎟 Ticket Audit\n\n1 selections') else original_send(text,**kwargs)
    ui.tg.send=send
    ui.controller.callback(cb);ui.controller.callback(cb)
    assert ui.controller.sessions[KEY].state=='COMPLETE' and spy.call_count==1
    assert 'couldn’t deliver' in joined(ui)


def test_rate_map_stays_bounded_and_does_not_block_existing_user(ui):
    ui.controller.start(input_message())
    ui.controller.rates.update({(str(i),str(i)):[NOW] for i in range(500)})
    msg=input_message('Another full ticket');msg['from']['id']=999;msg['chat']['id']=999
    ui.controller.start(msg);ui.controller.message(msg)
    assert len(ui.controller.rates)==500
    ui.controller.message(input_message('Existing user full ticket'))
    assert ui.client.transport.request.call_count==1


def test_duplicate_update_extraction_once_and_new_ticket_uses_new_token(ui):
    s=prepare(ui);old=tap(ui.controller)
    ui.controller.message(input_message('same delivered update',mid=1))
    assert ui.client.transport.request.call_count==1
    ui.controller.callback(old)
    ui.controller.message(input_message('another complete ticket to review',mid=2))
    ui.controller.callback(old)
    assert 'expired' in ui.tg.answers[-1]['text'] and s.state=='PENDING_REVIEW'


def test_bounded_workers_decline_third_extraction_without_google(ui):
    jobs=[];ui.controller.runner=jobs.append
    for uid in ('42','43','44'):
        msg=input_message('complete ticket text')
        msg['from']['id']=int(uid);msg['chat']['id']=int(uid)
        ui.controller.start(msg);ui.controller.message(msg)
    assert len(jobs)==2 and ui.controller.sessions[('44','44')].state=='WAITING'
    assert 'busy' in ui.tg.sends[-1]['text']
    ui.client.transport.request.assert_not_called()
    for job in jobs: job()


def test_intake_per_user_hourly_limit_survives_cancel_and_new_entry(ui):
    for i in range(5):
        ui.controller.start(input_message());ui.controller.message(input_message('complete ticket text',mid=i+1))
        ui.controller.callback(tap(ui.controller,'cancel'))
    ui.controller.start(input_message());ui.controller.message(input_message('complete ticket text',mid=6))
    assert ui.client.transport.request.call_count==5
    assert 'wait' in ui.tg.sends[-1]['text']


def test_confirm_local_first_no_remote_evidence_and_zero_prediction_db_changes(ui,local,db):
    resolver,s,_=local
    free=Mock();free.cached.side_effect=AssertionError('local must bypass free research')
    free.research.side_effect=AssertionError('local must bypass free research')
    ui.service.resolver=resolver;ui.service.researcher=free
    before=db_dump(db);prepare(ui,ticket(s));ui.controller.callback(tap(ui.controller))
    assert 'Today’s data' in joined(ui) and 'Model probability:' in joined(ui)
    assert 'Evidence: 1/1 selections checked' in joined(ui)
    assert db_dump(db)==before and ui.client.transport.request.call_count==1
    free.cached.assert_not_called();free.research.assert_not_called()


def test_full_order_local_then_cache_then_free_source_then_analyzer(ui,local):
    resolver,s,_=local;calls=[]
    original=resolver.resolve
    resolver.resolve=lambda sel,now:(calls.append(('local',sel.home)),original(sel,now))[1]
    cached=selection(home='Liverpool');uncached=selection(home='Everton')
    re=Mock();re.cached.side_effect=lambda sel,now:(calls.append(('cache',sel.home)),evidence(sel) if sel.home=='Liverpool' else None)[1]
    re.research.side_effect=lambda sel,now:(calls.append(('research',sel.home)),evidence(sel))[1]
    analyzer=Mock();analyzer.analyze_ticket.side_effect=lambda a,now:(calls.append(('analyze',None)),a)[1]
    ui.service.resolver=resolver;ui.service.researcher=re;ui.service.analyzer=analyzer
    prepare(ui,ticket(s,cached,uncached));assert calls==[]
    ui.controller.callback(tap(ui.controller))
    assert calls[:3]==[('local',s.home),('local','Liverpool'),('local','Everton')]
    assert calls[-2:]==[('research','Everton'),('analyze',None)]
    assert re.research.call_count==1
    assert 'Today’s data' in joined(ui) and 'Public data' in joined(ui)
    assert joined(ui).count('Model probability:')==1


@pytest.mark.parametrize('error',[None,requests.Timeout('secret-source'),requests.ConnectionError('secret-source')])
def test_actual_free_resolver_partial_evidence_or_failure_never_fails_local_leg(ui,local,tmp_path,error):
    resolver,s,_=local
    remote=selection(home='Liverpool',away='Everton')
    source,adapter,transport=setup_source(tmp_path,error=error)
    ui.service.resolver=resolver;ui.service.researcher=source
    prepare(ui,ticket(s,remote));ui.controller.callback(tap(ui.controller))
    assert 'Evidence: 1/2 selections checked' in joined(ui)
    assert 'Limited data' in joined(ui) and 'Insufficient Evidence' in joined(ui)
    assert transport.get.call_count==1 and 'secret-source' not in joined(ui)


def test_all_insufficient_no_forced_weakest_or_analysis(ui,tmp_path):
    ui.service.analyzer=EvidenceAnalyzer(ui.client)
    prepare(ui,ticket(selection(sport='unsupported')))
    ui.controller.callback(tap(ui.controller))
    assert 'Evidence: 0/1' in joined(ui) and 'Weakest' not in joined(ui)
    assert 'Overall: ⚪ Insufficient Evidence' in joined(ui)
    assert ui.client.transport.request.call_count==1


def test_malformed_analysis_preserves_deterministic_local_result_and_quota(ui,local):
    resolver,s,_=local;ui.service.resolver=resolver
    prepare(ui,ticket(s))
    ui.client.transport.request.return_value.json.return_value=response('{"probability":0.99,"safer_bet":"invented"}')
    ui.service.analyzer=EvidenceAnalyzer(ui.client)
    ui.controller.callback(tap(ui.controller))
    assert 'Evidence: 1/1' in joined(ui) and '99%' not in joined(ui) and 'invented' not in joined(ui)
    assert ui.client.transport.request.call_count==2 and ui.client.usage.snapshot(NOW)['failures']==1
    body=ui.client.transport.request.call_args.kwargs['json']
    assert 'tools' not in body and 'Use only the supplied evidence for current factual claims.' in body['contents'][0]['parts'][0]['text']


def test_quota_exhaustion_is_durable_across_restart_zero_final_request(ui,tmp_path):
    ui.client.settings=replace(ui.client.settings,max_requests_per_day=1)
    prepare(ui);ui.controller.callback(tap(ui.controller,'cancel'))
    ui.client.usage=UsageGuard(tmp_path/'audit-usage.sqlite3')
    prepare(ui)
    assert ui.tg.sends[-1]['text']==UNAVAILABLE
    assert ui.client.transport.request.call_count==1


def leg(ui,verdict='REASONABLE',level='GOOD_LOCAL',p=.7):
    match=LocalEvidenceMatch('EXACT',metrics={'p_model':p,'p_market':.6,'ev':.1},evaluated_at=NOW.isoformat())
    r=Mock();r.resolve.return_value=match
    a=AuditService(r).audit(ticket(),NOW).selections[0]
    return replace(a,verdict=verdict,evidence_level=level)


def aggregate(ui,*legs):
    a=ui.service.audit(ticket(),NOW)
    return replace(a,selections=tuple(legs))


@pytest.mark.parametrize('verdicts,overall',[
    (['STRONG'],'STRONG'),(['STRONG','STRONG'],'REASONABLE'),(['REASONABLE'],'REASONABLE'),
    (['STRONG','RISKY'],'RISKY'),(['STRONG','VERY_RISKY'],'VERY_RISKY'),
    (['STRONG','INSUFFICIENT_EVIDENCE'],'RISKY'),(['INSUFFICIENT_EVIDENCE'],'INSUFFICIENT_EVIDENCE'),
    (['STRONG']*6,'RISKY')])
def test_deterministic_overall_rule(ui,verdicts,overall):
    a=aggregate(ui,*[leg(ui,v,'STRONG_LOCAL' if v=='STRONG' else 'GOOD_LOCAL') for v in verdicts])
    assert finish(a).overall_verdict==overall


def test_weakest_verdict_then_evidence_strength_then_actual_local_probability(ui):
    a=aggregate(ui,leg(ui,'STRONG'),leg(ui,'VERY_RISKY',p=.6),leg(ui,'RISKY',p=.2),leg(ui,'VERY_RISKY',p=.5))
    assert finish(a).weakest_selections==(3,)
    a=aggregate(ui,leg(ui,'RISKY','STRONG_LOCAL',p=.2),leg(ui,'RISKY','GOOD_LOCAL',p=.8))
    assert finish(a).weakest_selections==(1,)


def test_probability_requires_real_exact_local_metric_no_fake_research_percentage(ui):
    a=leg(ui,p=.71)
    assert local_probability(a)==.71
    assert local_probability(replace(a,local_probability=.99)) is None
    b=replace(a,evidence_level='RESEARCHED',evidence=replace(a.evidence,evidence_source='RESEARCH'))
    assert local_probability(b) is None
    out='\n'.join(audit_format.results(ticket(),aggregate(ui,b)))
    assert 'Public data' in out and '%' not in out


def test_rendering_escapes_all_fields_and_omits_injected_alternative(ui):
    a=leg(ui)
    s=selection(home='<script>&evil',away='<b>away',competition='<x>',raw_text='<bad>')
    a=replace(a,normalized_selection=s,safer_alternative={'selection':'made-up unsafe bet'},concerns=('raw technical error',))
    out='\n'.join(audit_format.results(ticket(s),aggregate(ui,a)))
    assert '<script>' not in out and '&lt;script&gt;&amp;' in out
    assert 'made-up' not in out and 'Safer alternative' not in out and 'technical' not in out


def test_long_twenty_leg_output_bounded_grouping_no_flood_or_combined_probability(ui):
    s=selection(home='Long Home '+('x'*400),away='Long Away '+('y'*400))
    t=ticket(*[s]*20)
    a=aggregate(ui,*[replace(leg(ui),normalized_selection=s) for _ in range(20)])
    pages=audit_format.results(t,a)
    # Packed by size, whole legs only: bounded pages, far fewer than one per leg.
    assert all(len(p)<=3500 for p in pages) and 3<=len(pages)<10
    assert ''.join(pages[1:-1]).count('Long Home')==20  # every leg, footer excluded
    assert '20 selections' in pages[0] and 'Overall:' in pages[-1]
    assert 'Combined odds' not in '\n'.join(pages)
    assert 'accumulator probability' not in '\n'.join(pages)
    reviews=audit_format.review(t)
    assert all(len(p)<=3500 for p in reviews)


@pytest.mark.parametrize('settings',[Settings('', 'gemini-3.5-flash-lite',True,True),
    Settings('key','gemini-2.5-flash',True,True),Settings('key','gemini-3.5-flash-lite',False,True),
    Settings('key','gemini-3.5-flash-lite',True,False)])
def test_runtime_rejects_unconfigured_or_wrong_model_before_sidecars(ui,tmp_path,monkeypatch,settings):
    monkeypatch.setattr(Settings,'from_environment',lambda *a:settings)
    before={str(p) for p in tmp_path.rglob('*')}
    with pytest.raises(AuditUnavailable):build_backend(ui.cfg)
    assert {str(p) for p in tmp_path.rglob('*')}==before


def test_runtime_uses_owned_sidecars_and_only_approved_research_no_http(ui,tmp_path,monkeypatch,db):
    monkeypatch.setattr(Settings,'from_environment',lambda *a:Settings('MOCK','gemini-3.5-flash-lite',True,True))
    before=db_dump(db)
    backend=build_backend(ui.cfg)
    assert backend.client.usage.path==tmp_path/'ticket_audit_usage.sqlite3'
    assert backend.service.resolver.path==db.path or str(backend.service.resolver.path)==str(db.path)
    assert len(backend.service.researcher.adapters)==1
    assert db_dump(db)==before


def file_response(status=200,payload=None,chunks=None,headers=None):
    r=Mock(status_code=status,headers=headers or {})
    r.json.return_value=payload
    r.iter_content.return_value=chunks or []
    r.__enter__=Mock(return_value=r);r.__exit__=Mock(return_value=False)
    return r


@pytest.mark.parametrize('signature',[b'\x89PNG\r\n\x1a\n',b'\xff\xd8\xff'])
def test_telegram_download_only_fixed_host_memory_and_bounded_timeouts(signature):
    tg=Telegram('secret','42');tg.session=Mock()
    tg.session.get.side_effect=[file_response(payload={'ok':True,'result':{'file_path':'photos/photo.jpg','file_size':20}}),
        file_response(chunks=[signature,b'bytes'])]
    assert tg.download_audit_image('image-id')==signature+b'bytes'
    calls=tg.session.get.call_args_list
    assert calls[0].kwargs['params']=={'file_id':'image-id'}
    assert calls[1].args[0]=='https://api.telegram.org/file/botsecret/photos/photo.jpg'
    assert all(c.kwargs['allow_redirects'] is False and c.kwargs['timeout']==(5,10) for c in calls)


@pytest.mark.parametrize('path',['https://evil.example/image','../evil','photos/../evil','/abs','photos/a?token=secret'])
def test_telegram_image_path_rejected_before_download(path):
    tg=Telegram('secret','42');tg.session=Mock()
    tg.session.get.return_value=file_response(payload={'ok':True,'result':{'file_path':path}})
    with pytest.raises(TelegramImageError):tg.download_audit_image('id')
    assert tg.session.get.call_count==1


@pytest.mark.parametrize('response',[file_response(status=302),file_response(chunks=[b'not an image']),
    file_response(headers={'Content-Length':'5000001'}),file_response(chunks=[b'\x89PNG\r\n\x1a\n',b'x'*5_000_000])])
def test_telegram_malformed_redirect_or_oversized_download_fails_closed(response):
    tg=Telegram('secret','42');tg.session=Mock()
    tg.session.get.side_effect=[file_response(payload={'ok':True,'result':{'file_path':'photos/a.png'}}),response]
    with pytest.raises(TelegramImageError):tg.download_audit_image('id')


def test_telegram_image_error_never_exposes_token_url():
    tg=Telegram('secret','42');tg.session=Mock()
    tg.session.get.side_effect=requests.ConnectionError(tg.base)
    with pytest.raises(TelegramImageError) as exc:tg.download_audit_image('id')
    assert 'secret' not in str(exc.value)


def test_telegram_image_wallclock_budget_even_when_source_trickles(monkeypatch):
    import predbot.bot.telegram as transport
    tg=Telegram('secret','42');tg.session=Mock()
    tg.session.get.side_effect=[file_response(payload={'ok':True,'result':{'file_path':'photos/a.png'}}),
        file_response(chunks=[b'\x89PNG\r\n\x1a\n',b'bytes'])]
    clock=iter([0,1,21]);monkeypatch.setattr(transport.time,'monotonic',lambda:next(clock))
    with pytest.raises(TelegramImageError,match='image_timeout'):tg.download_audit_image('id')
