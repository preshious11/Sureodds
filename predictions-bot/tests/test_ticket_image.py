from dataclasses import replace
from io import BytesIO
import json

import pytest
from PIL import Image

from predbot.bot.ticket_image import (ASSETS, DISCLAIMER, attach_image, group_link,
                                      render_ticket, ticket_identity)
from predbot.bot.format import combined_odds
from predbot.cache.models import DailySnapshot, PickRecord


def pick(i=1):
    return PickRecord(pick_id=i, fixture_id=f'sample:{i}', market='result3', selection='home',
                      home=f'Sample United {i}', away=f'Example City {i}', label='Home team to win',
                      league='Sample League', price=1.28, kickoff_utc='2026-10-19T17:00:00Z')


@pytest.mark.parametrize('n', [1, 2, 3])
def test_render_and_transfer(n, monkeypatch):
    monkeypatch.setenv('TELEGRAM_GROUP_INVITE_URL', 'https://t.me/SampleCommunity')
    picks = [pick(i+1) for i in range(n)]
    snapshot = DailySnapshot(date='2026-10-19', top_picks=picks)
    before = [p.to_dict() for p in picks]
    attach_image(snapshot)
    media = snapshot.bet_image
    assert json.loads(media['identity'])[1][0][4] == picks[0].price
    assert media['link'] == 'https://t.me/SampleCommunity'
    assert snapshot.to_dict()['top_combined_odds'] == combined_odds(p.price for p in picks)
    assert before == [p.to_dict() for p in picks]
    image = Image.open(BytesIO(render_ticket(snapshot.date,picks,media['link'])))
    assert image.width == 800 and image.height < 2000
    assert len(media['jpeg']) <= 160000


def test_assets_and_extreme_text_fail_safely(tmp_path):
    with pytest.raises((OSError, FileNotFoundError)):
        render_ticket('2026-10-19',[pick()],assets=tmp_path)
    with pytest.raises(ValueError):
        render_ticket('2026-10-19',[replace(pick(),home='X'*500)])
    # Long but reasonable club names wrap without truncation.
    render_ticket('2026-10-19',[replace(pick(),home='Club Deportivo Universidad de Concepcion')])
    render_ticket('2026-10-19',[replace(pick(),label='Home team double chance and over one and a half total goals in the match')])


@pytest.mark.parametrize('asset',['logo-source.png','Barlow-Regular.ttf','BarlowCondensed-Bold.ttf'])
def test_malformed_assets_are_rejected(tmp_path, asset):
    import shutil
    destination = tmp_path / 'assets'
    shutil.copytree(ASSETS,destination)
    (destination / asset).write_bytes(b'corrupt asset')
    with pytest.raises(OSError):
        render_ticket('2026-10-19',[pick()],assets=destination)


def test_missing_library_or_render_failure_keeps_predictions(monkeypatch):
    import predbot.bot.ticket_image as module
    snapshot = DailySnapshot(date='2026-10-19',top_picks=[pick()])
    monkeypatch.setattr(module,'render_ticket',lambda *a: (_ for _ in ()).throw(OSError('broken asset')))
    attach_image(snapshot)
    assert snapshot.bet_image is None
    assert snapshot.top_picks[0].pick_id == 1


@pytest.mark.parametrize('value',['','https://example.com/foo','https://t.me/foo?bad=1','https://t.me/a','javascript:bad'])
def test_invalid_links(value):
    assert group_link(value) == ''


def test_identity_includes_authoritative_fields():
    p = pick()
    assert ticket_identity('2026-10-19',[p]) != ticket_identity('2026-10-19',[replace(p,price=1.3)])
    assert DISCLAIMER == 'Bet only what you can afford to lose.'


def test_draws_actual_content_and_disclaimer_inside_image(monkeypatch):
    from PIL import ImageDraw
    original = ImageDraw.ImageDraw.text
    drawn = []
    def capture(self, xy, text, *args, **kwargs):
        drawn.append((xy,text))
        return original(self,xy,text,*args,**kwargs)
    monkeypatch.setattr(ImageDraw.ImageDraw,'text',capture)
    data = render_ticket('2026-10-19',[pick()])
    text = '\n'.join(s for _,s in drawn)
    assert 'Sample United 1 vs Example City 1' in text
    assert 'Home team to win' in text and '1.28' in text and '18:00 WAT' in text
    image = Image.open(BytesIO(data))
    assert any(s == DISCLAIMER and 0 < xy[1] * .8 < image.height for xy,s in drawn)


def test_media_survives_existing_d1_parameterized_transfer(tmp_path):
    import importlib.util
    from pathlib import Path
    script = Path(__file__).resolve().parents[2] / 'workers/scripts/d1_sync.py'
    spec = importlib.util.spec_from_file_location('ticket_sync',script)
    sync = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sync)
    snapshot = DailySnapshot(date='2026-10-19',top_picks=[pick()])
    attach_image(snapshot)
    payload = json.dumps(snapshot.to_dict())
    # Force the already-supported REST route, as on a broad production slate.
    payload = payload[:-1] + ', "padding": "' + 'x'*90000 + '"}'
    rest = tmp_path / 'rest'
    sync.emit_statements(str(tmp_path),'p1_prediction_cache',
                         'INSERT INTO prediction_cache(payload_json) VALUES ',[(payload,)],
                         lambda r: "('" + r[0].replace("'","''") + "')", str(rest))
    item = json.loads(next(rest.glob('*.jsonl')).read_text().strip())
    transferred = json.loads(item['params'][0])
    assert transferred['bet_image'] == snapshot.bet_image
