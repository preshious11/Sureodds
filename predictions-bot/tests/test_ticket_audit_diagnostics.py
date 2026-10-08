"""Extraction failure diagnostics; real audit gates/client, no external HTTP."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
import base64
import json
import logging
import os
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
import requests

from predbot.ticket_audit.gemini import Settings
from predbot.ticket_audit.runtime import build_backend
from predbot.ticket_audit.schemas import AuditUnavailable
from predbot.ticket_audit.diagnostics import scope, note_quota, failure_details
from predbot.config import load_config
from predbot.bot.ticket_audit import UNAVAILABLE, UNREADABLE
from tests.test_ticket_audit_telegram import ui, extra_firewall, input_message
from tests.test_ticket_audit_foundation import no_providers, ticket, selection, response, NOW, db_dump


def failure(caplog):
    records=[r for r in caplog.records if r.getMessage().startswith('Ticket Audit extraction failed |')]
    assert len(records)==1
    return records[0].audit_diagnostics


def test_real_disabled_config_screenshot_failure_reports_pre_http_root(ui,monkeypatch,caplog):
    monkeypatch.setattr(Settings,'from_environment',lambda *a:Settings('MOCK-SECRET','gemini-3.5-flash-lite',False,False))
    ui.controller.factory=lambda:build_backend(ui.cfg)
    ui.controller.start(input_message())
    with caplog.at_level(logging.WARNING):
        ui.controller.message(input_message(photo=[{'file_id':'private-file-id'}]))
    d=failure(caplog)
    assert d['stage']=='backend_config'
    assert d['reason']=='private_audit_disabled'
    assert d['model']=='gemini-3.5-flash-lite'
    assert d['api_key_present'] is True
    assert d['gemini_enabled'] is False and d['free_tier_confirmed'] is False
    assert d['http_attempted'] is False and d['quota_reserved'] is False
    assert d['image_size_bytes'] is None
    ui.client.transport.request.assert_not_called()
    assert 'MOCK-SECRET' not in caplog.text and 'private-file-id' not in caplog.text


def screenshot(ui,caplog,blob=b'\x89PNG\r\n\x1a\nFAKE-IMAGE-BYTES'):
    ui.tg.download_audit_image=Mock(return_value=blob)
    ui.controller.start(input_message())
    with caplog.at_level(logging.WARNING):
        ui.controller.message(input_message(photo=[{'file_id':'PRIVATE-FILE-ID'}]))


@pytest.mark.parametrize('key,enabled,confirmed,model,reason',[
    ('',True,True,'gemini-3.5-flash-lite','private_audit_disabled'),
    ('SECRET',False,True,'gemini-3.5-flash-lite','private_audit_disabled'),
    ('SECRET',True,False,'gemini-3.5-flash-lite','private_audit_disabled'),
    ('SECRET',True,True,'gemini-2.5-flash','unsupported_private_audit_model'),
    ('SECRET',True,True,'PRIVATE-SECRET-IN-MODEL','unsupported_private_audit_model')])
def test_runtime_config_rejections_do_not_download_or_reserve(ui,monkeypatch,caplog,key,enabled,confirmed,model,reason):
    monkeypatch.setattr(Settings,'from_environment',lambda *a:Settings(key,model,enabled,confirmed))
    ui.controller.factory=lambda:build_backend(ui.cfg)
    screenshot(ui,caplog)
    d=failure(caplog)
    assert d['reason']==reason and d['stage']=='backend_config'
    assert d['http_attempted'] is False and d['quota_reserved'] is False
    assert d['api_key_present']==bool(key) and d['quota_limit']==20
    assert d['quota_used']==0 and d['quota_state']=='ledger_missing'
    ui.tg.download_audit_image.assert_not_called()
    assert 'PRIVATE-SECRET-IN-MODEL' not in caplog.text
    assert ui.tg.sends[-1]['text']==UNAVAILABLE


def test_terminal_startup_loads_key_into_environment_but_preserves_false_flags(ui,tmp_path,caplog):
    path=tmp_path/'input.env'
    path.write_text('GEMINI_API_KEY=SECRET-LOCAL-KEY\nGEMINI_AUDIT_MODEL=gemini-3.5-flash-lite\n'
        'AUDIT_GEMINI_ENABLED=false\nAUDIT_GEMINI_FREE_TIER_CONFIRMED=false\n'
        f'DB_PATH={ui.cfg.db_path}\n',encoding='utf-8')
    with patch.dict(os.environ,{},clear=True):
        cfg=load_config(str(path))
        assert bool(os.environ.get('GEMINI_API_KEY'))
        ui.controller.factory=lambda:build_backend(cfg)
        screenshot(ui,caplog)
    d=failure(caplog)
    assert d['api_key_in_environment'] is True and d['api_key_present'] is True
    assert d['reason']=='private_audit_disabled' and d['gemini_enabled'] is False
    assert 'SECRET-LOCAL-KEY' not in caplog.text


def test_terminal_environment_false_overrides_true_env_file(ui,tmp_path,caplog):
    path=tmp_path/'input.env'
    path.write_text('GEMINI_API_KEY=SECRET-FILE-KEY\nGEMINI_AUDIT_MODEL=gemini-3.5-flash-lite\n'
        'AUDIT_GEMINI_ENABLED=true\nAUDIT_GEMINI_FREE_TIER_CONFIRMED=true\n',encoding='utf-8')
    with patch.dict(os.environ,{'AUDIT_GEMINI_ENABLED':'false'},clear=True):
        ui.cfg.env_file=str(path);ui.controller.factory=lambda:build_backend(ui.cfg)
        screenshot(ui,caplog)
    d=failure(caplog)
    assert d['api_key_present'] is True and d['gemini_enabled'] is False and d['free_tier_confirmed'] is True
    assert d['reason']=='private_audit_disabled' and d['http_attempted'] is False


def test_exhausted_twenty_request_guard_stops_before_http_and_does_not_consume_twenty_first(ui,caplog):
    for _ in range(20):ui.client.usage.reserve(20,False,NOW)
    before=ui.client.usage.snapshot(NOW)
    screenshot(ui,caplog)
    d=failure(caplog)
    assert d['reason']=='local_quota_exhausted' and d['stage']=='quota_reservation'
    assert d['quota_used']==20 and d['quota_limit']==20
    assert d['http_attempted'] is False and d['quota_reserved'] is False
    assert d['mime_type']=='image/png' and d['image_size_bytes']>0
    assert ui.client.usage.snapshot(NOW)==before
    ui.client.transport.request.assert_not_called()
    assert ui.tg.sends[-1]['text']==UNAVAILABLE


@pytest.mark.parametrize('limit',[0,101])
def test_invalid_guard_configuration_has_no_http_or_quota_consumption(ui,caplog,limit):
    ui.client.settings=replace(ui.client.settings,max_requests_per_day=limit)
    screenshot(ui,caplog)
    d=failure(caplog)
    assert d['reason']=='invalid_local_budget' and d['quota_limit']==limit
    assert d['http_attempted'] is False and d['quota_reserved'] is False
    assert ui.client.usage.snapshot(NOW)['requests']==0


@pytest.mark.parametrize('status,reason,category',[(400,'gemini_request_rejected','MALFORMED_REQUEST'),
    (403,'key_or_capability_rejected','AUTHENTICATION_OR_PERMISSION'),(404,'model_unavailable','NOT_FOUND'),
    (429,'gemini_rate_limited','RATE_LIMIT'),(500,'gemini_service_unavailable','SERVICE_UNAVAILABLE')])
@pytest.mark.parametrize('mime,blob',[('image/png',b'\x89PNG\r\n\x1a\nPRIVATE-IMAGE'),
    ('image/jpeg',b'\xff\xd8\xffPRIVATE-JPEG')])
def test_http_failure_logs_safe_stage_encoding_status_and_consumption(ui,caplog,status,reason,category,mime,blob):
    secret=ui.client.settings.api_key
    # Arbitrary provider text may echo a user's ticket or image; never log it.
    raw=f'PRIVATE-TICKET-TEXT PRIVATE-IMAGE {secret} Authorization: Bearer secret-header'
    ui.client.transport.request.return_value=Mock(status_code=status,json=Mock(return_value={'error':{'message':raw}}))
    screenshot(ui,caplog,blob)
    d=failure(caplog)
    assert d['stage']=='gemini_http' and d['reason']==reason
    assert d['http_attempted'] is True and d['http_status']==status and d['quota_reserved'] is True
    assert d['quota_used']==1 and d['quota_limit']==20
    assert d['mime_type']==mime and d['image_size_bytes']==len(blob)
    assert d['provider_category']==category and d['provider_message']
    body=ui.client.transport.request.call_args.kwargs['json']
    image=body['contents'][0]['parts'][1]['inlineData']
    assert image['mimeType']==mime and base64.b64decode(image['data'])==blob
    assert 'tools' not in body and 'maxItems' not in body['generationConfig']['responseFormat']['text']['schema']['properties']['selections']
    assert 'gemini-3.5-flash-lite:generateContent' in ui.client.transport.request.call_args.args[1]
    assert ui.client.transport.request.call_count==1 and ui.client.usage.snapshot(NOW)['requests']==1
    for value in ('PRIVATE-TICKET-TEXT','PRIVATE-IMAGE','PRIVATE-FILE-ID','Authorization',secret,'secret-header',base64.b64encode(blob).decode()):
        assert value not in caplog.text
    assert ui.tg.sends[-1]['text']==UNAVAILABLE


@pytest.mark.parametrize('error,reason',[(requests.Timeout('PRIVATE-URL-AND-TICKET'),'gemini_timeout'),
    (requests.ConnectionError('PRIVATE-URL-AND-TICKET'),'gemini_transport_or_json_failure')])
def test_timeout_or_connection_failure_attempted_and_reserved_without_known_http_status(ui,caplog,error,reason):
    ui.client.transport.request.side_effect=error
    screenshot(ui,caplog)
    d=failure(caplog)
    assert d['stage']=='gemini_http' and d['reason']==reason and d['http_status'] is None
    assert d['http_attempted'] is True and d['quota_reserved'] is True
    assert ui.client.usage.snapshot(NOW)['requests']==1
    assert 'PRIVATE-URL-AND-TICKET' not in caplog.text


@pytest.mark.parametrize('kind,reason,stage',[('json','gemini_transport_or_json_failure','gemini_response_json'),
    ('envelope','malformed_response','gemini_response'),('blocked','malformed_response','gemini_response'),
    ('truncated','incomplete_extraction','gemini_response'),('empty','empty_response','gemini_response'),
    ('ticket','malformed_structured_output','ticket_parse'),('wrong_type','extraction_type_mismatch','ticket_parse')])
def test_http_200_internal_parsing_and_validation_are_distinguishable(ui,caplog,kind,reason,stage):
    r=ui.client.transport.request.return_value
    if kind=='json': r.json.side_effect=ValueError('PRIVATE-RAW-RESPONSE')
    elif kind=='envelope':r.json.return_value={}
    elif kind=='blocked':r.json.return_value={'promptFeedback':{'blockReason':'SAFETY'}}
    elif kind=='truncated':r.json.return_value=response('incomplete',reason='MAX_TOKENS')
    elif kind=='empty':r.json.return_value=response('')
    elif kind=='ticket':r.json.return_value=response('{"private":"PRIVATE-TICKET-TEXT"}')
    else:r.json.return_value=response(json.dumps(asdict(replace(ticket(),input_type='text'))))
    screenshot(ui,caplog)
    d=failure(caplog)
    assert d['reason']==reason and d['stage']==stage
    assert d['http_attempted'] is True and d['http_status']==200 and d['quota_reserved'] is True
    assert ui.client.usage.snapshot(NOW)['requests']==1
    assert 'PRIVATE-RAW-RESPONSE' not in caplog.text and 'PRIVATE-TICKET-TEXT' not in caplog.text
    if kind=='blocked':assert d['prompt_blocked'] is True
    if kind=='truncated':assert d['finish_reason']=='MAX_TOKENS'


def test_malformed_image_fails_local_validation_before_request(ui,caplog):
    screenshot(ui,caplog,b'NOT-AN-IMAGE-PRIVATE-CONTENT')
    d=failure(caplog)
    assert d['reason']=='invalid_image_input' and d['stage']=='gemini_validation'
    assert d['http_attempted'] is False and d['quota_reserved'] is False
    assert ui.client.usage.snapshot(NOW)['requests']==0
    assert ui.tg.sends[-1]['text']==UNREADABLE
    assert 'PRIVATE-CONTENT' not in caplog.text


def test_payload_resource_limit_is_reported_after_http(ui,caplog):
    t=replace(ticket(),input_type='image',source_bookmaker='x'*1_000_001)
    ui.client.transport.request.return_value.json.return_value=response(json.dumps(asdict(t)))
    screenshot(ui,caplog)
    d=failure(caplog)
    assert d['reason']=='ticket_input_too_large' and d['stage']=='ticket_parse'
    assert d['http_status']==200 and d['quota_reserved'] is True
    assert 'safe input size or parsing limits' in ui.tg.sends[-1]['text']


def test_reservation_failure_logs_readable_guard_without_attempting_provider(ui,caplog,monkeypatch):
    monkeypatch.setattr(ui.client.usage,'reserve',Mock(side_effect=AuditUnavailable('usage_guard_unavailable')))
    screenshot(ui,caplog)
    d=failure(caplog)
    assert d['reason']=='usage_guard_unavailable' and d['stage']=='quota_reservation'
    assert d['http_attempted'] is False and d['quota_reserved'] is False
    ui.client.transport.request.assert_not_called()


def test_failure_counter_error_retains_original_provider_cause(ui,caplog,monkeypatch):
    ui.client.transport.request.return_value=Mock(status_code=403,json=Mock(return_value={'error':{'status':'PERMISSION_DENIED'}}))
    monkeypatch.setattr(ui.client.usage,'count',Mock(side_effect=AuditUnavailable('usage_guard_unavailable')))
    screenshot(ui,caplog)
    d=failure(caplog)
    assert d['reason']=='usage_guard_unavailable' and d['provider_reason']=='key_or_capability_rejected'
    assert d['http_status']==403 and d['http_attempted'] is True and d['quota_reserved'] is True


@pytest.mark.parametrize('error',[AuditUnavailable('PRIVATE-RAW-TICKET'),ValueError('PRIVATE-RAW-TICKET')])
def test_unknown_exception_text_never_logged(ui,caplog,error):
    ui.client.extract=Mock(side_effect=error)
    screenshot(ui,caplog)
    d=failure(caplog)
    assert d['reason'] in ('unknown_audit_reason','unexpected_error')
    assert 'PRIVATE-RAW-TICKET' not in caplog.text


def test_concurrent_shared_client_diagnostics_do_not_mix_image_metadata(ui,caplog):
    blobs={'png':b'\x89PNG\r\n\x1a\nSMALL','jpeg':b'\xff\xd8\xffLARGER-PRIVATE-IMAGE'}
    ui.tg.download_audit_image=lambda file_id:blobs[file_id]
    def request(*args,**kwargs):
        mime=kwargs['json']['contents'][0]['parts'][1]['inlineData']['mimeType']
        return Mock(status_code=400 if mime=='image/png' else 429,json=Mock(return_value={'error':{}}))
    ui.client.transport.request.side_effect=request
    def submit(uid,file_id):
        msg=input_message(photo=[{'file_id':file_id}]);msg['chat']['id']=uid;msg['from']['id']=uid
        ui.controller.start(msg);ui.controller.message(msg)
    with caplog.at_level(logging.WARNING),ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(submit,42,'png'),pool.submit(submit,43,'jpeg')]
        for future in futures:future.result()
    ds=[r.audit_diagnostics for r in caplog.records if r.getMessage().startswith('Ticket Audit extraction failed |')]
    assert len(ds)==2
    assert {(d['mime_type'],d['image_size_bytes'],d['http_status']) for d in ds}=={
        ('image/png',len(blobs['png']),400),('image/jpeg',len(blobs['jpeg']),429)}
    assert all(d['quota_reserved'] and d['http_attempted'] for d in ds)
    assert ui.client.usage.snapshot(NOW)['requests']==2


def test_request_context_does_not_leak_previous_http_attempt_into_config_failure(ui,caplog,monkeypatch):
    ui.client.transport.request.side_effect=requests.Timeout('private')
    screenshot(ui,caplog)
    assert failure(caplog)['http_attempted'] is True
    caplog.clear();ui.controller.backend=None
    monkeypatch.setattr(Settings,'from_environment',lambda *a:Settings('SECRET','gemini-3.5-flash-lite',False,True))
    ui.controller.factory=lambda:build_backend(ui.cfg)
    screenshot(ui,caplog)
    d=failure(caplog)
    assert d['reason']=='private_audit_disabled' and d['http_attempted'] is False and d['http_status'] is None


def test_readonly_quota_diagnostic_never_creates_missing_ledger_or_changes_existing(ui,tmp_path):
    absent=tmp_path/'absent.sqlite3'
    with scope('quota_reservation'):
        note_quota(absent,NOW)
        d=failure_details(AuditUnavailable('local_quota_exhausted'))
    assert not absent.exists() and d['quota_used']==0 and d['quota_state']=='ledger_missing'
    before=Path(ui.client.usage.path).read_bytes()
    with scope('quota_reservation'):
        note_quota(ui.client.usage.path,NOW)
    assert Path(ui.client.usage.path).read_bytes()==before


def test_bad_quota_snapshot_is_safe_and_does_not_replace_reason(tmp_path):
    path=tmp_path/'bad.sqlite3';path.write_bytes(b'PRIVATE-INVALID-DATABASE')
    with scope('quota_reservation'):
        note_quota(path,NOW)
        d=failure_details(AuditUnavailable('usage_guard_unavailable'))
    assert d['quota_state']=='unavailable' and d['quota_used'] is None
    assert d['reason']=='usage_guard_unavailable' and path.read_bytes()==b'PRIVATE-INVALID-DATABASE'


def test_failed_screenshot_never_changes_prediction_database(ui,db,caplog):
    before=db_dump(db)
    ui.client.transport.request.side_effect=requests.Timeout('PRIVATE-TICKET')
    screenshot(ui,caplog)
    assert db_dump(db)==before
