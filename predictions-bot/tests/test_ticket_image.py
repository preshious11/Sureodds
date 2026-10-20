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
    assert image.width == 1080 and 1000 <= image.height <= 1350
    assert len(media['jpeg']) <= 160000


def test_assets_and_extreme_text_fail_safely(tmp_path):
    with pytest.raises((OSError, FileNotFoundError)):
        render_ticket('2026-10-19',[pick()],assets=tmp_path)
    with pytest.raises(ValueError):
        render_ticket('2026-10-19',[replace(pick(),home='X'*500)])
    # Long but reasonable club names wrap without truncation.
    render_ticket('2026-10-19',[replace(pick(),home='Club Deportivo Universidad de Concepcion')])
    render_ticket('2026-10-19',[replace(pick(),label='Home team double chance and over one and a half total goals in the match')])


@pytest.mark.parametrize('asset',['logo-source.png','Barlow-Regular.ttf','BarlowCondensed-Bold.ttf','Barlow-SemiBold.ttf'])
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
    normalized = ' '.join(text.split())
    assert 'Sample United 1 vs Example City 1' in normalized
    assert 'Home team to win' in normalized and '1.28' in text and '18:00 WAT' in normalized
    image = Image.open(BytesIO(data))
    assert any(s == DISCLAIMER and 0 < xy[1] < image.height for xy,s in drawn)


def test_logo_has_transparency_without_replacing_brand_pixels():
    from predbot.bot.ticket_image import transparent_logo
    logo = transparent_logo()
    assert logo.mode == 'RGBA'
    assert logo.getchannel('A').getextrema() == (0,255)
    assert any(g > 180 and r > 100 and b < 100 and a == 255
               for _,(r,g,b,a) in logo.getcolors(logo.width*logo.height))


@pytest.mark.parametrize('asset',['stadium-background.png'])
def test_optional_decorative_asset_failure_keeps_image(tmp_path, asset):
    import shutil
    destination = tmp_path / 'assets'
    shutil.copytree(ASSETS,destination)
    (destination / asset).write_bytes(b'broken optional background')
    image = Image.open(BytesIO(render_ticket('2026-10-19',[pick()],assets=destination)))
    assert image.width == 1080 and 1000 <= image.height <= 1350


def test_unicode_and_missing_competition():
    p = replace(pick(),home='Fenerbahçe',away='SK Slavia Praha',league='')
    assert render_ticket('2026-10-20',[p])


def test_three_long_fixtures_stay_inside_fixed_canvas():
    picks = [replace(pick(i),home='Club Deportivo Universidad de Concepción',
                     away='Brighton & Hove Albion Women',
                     league='International Continental Championship · Qualification',
                     label='Home team double chance and over one and a half total goals')
             for i in (1,2,3)]
    assert render_ticket('2026-10-20',picks,'https://t.me/SampleCommunity')


def test_verified_badge_lookup_never_guesses(tmp_path):
    from predbot.bot.ticket_image import team_badge
    assert team_badge('Sabah FK') is not None
    assert team_badge('Fenerbahçe') is not None
    assert team_badge('Sabah FC') is None  # different club, not an alias
    assert team_badge('Sample United') is None
    assert team_badge('Borussia Dortmund',tmp_path) is None


def test_known_fixture_with_crests_preserves_data():
    p = replace(pick(),home='Sabah FK',away='Borussia Dortmund',label='Borussia Dortmund to Win',price=1.3)
    assert len(render_ticket('2026-10-20',[p],'https://t.me/SampleCommunity')) <= 120000


def test_bad_badge_and_missing_script_are_decorative_only(tmp_path):
    import shutil
    destination = tmp_path / 'assets'
    shutil.copytree(ASSETS,destination)
    (destination / 'badges' / 'sabah-fk.png').write_bytes(b'bad badge')
    (destination / 'KaushanScript-Regular.ttf').unlink()
    p = replace(pick(),home='Sabah FK',away='Borussia Dortmund')
    assert render_ticket('2026-10-20',[p],assets=destination)


def test_catalog_covers_clubs_outside_current_ticket():
    from predbot.bot.ticket_image import team_badge
    for club in ('Arsenal','Fulham','Barcelona','Bayern Munich','Napoli','Inter Milan','Nice'):
        assert team_badge(club) is not None,club
    index = json.loads((ASSETS/'badges/index.json').read_text(encoding='utf-8'))
    assert len(index['clubs']) >= 300


def test_catalog_rejects_ambiguous_and_unsafe_paths(tmp_path):
    from predbot.bot.club_badges import badge_file
    directory = tmp_path/'badges'
    directory.mkdir()
    (directory/'index.json').write_text(json.dumps({'aliases':{
        'united':['a.png','b.png'],'unsafe':['../other.png'],'a club':['catalog/a.png']}}))
    assert badge_file('United',tmp_path) is None
    assert badge_file('Unsafe',tmp_path) is None
    assert badge_file('A CLUB',tmp_path) == (directory/'catalog/a.png').resolve()


@pytest.mark.parametrize('day',['2030-06-01','2031-11-20'])
def test_future_generation_uses_same_production_store_path(day,db,monkeypatch):
    from datetime import datetime,timezone
    from predbot.cache.fetcher import UnifiedFetcher
    from predbot.config import Config
    import requests
    import urllib.request
    def forbidden(*args,**kwargs):
        raise AssertionError('daily renderer must use local assets')
    monkeypatch.setattr(requests.Session,'request',forbidden)
    monkeypatch.setattr(urllib.request,'urlopen',forbidden)
    p = replace(pick(7384),home='Arsenal',away='Fulham',price=1.78,
                label='Arsenal to Win',kickoff_utc=f'{day}T17:00:00Z')
    snapshot = DailySnapshot(date=day,top_picks=[p])
    fetcher = UnifiedFetcher(None,db,Config())
    fetcher._store(snapshot,datetime.fromisoformat(day).replace(tzinfo=timezone.utc))
    saved = json.loads(db.query('SELECT payload_json FROM prediction_cache WHERE local_date=?',(day,))[0]['payload_json'])
    assert saved['top_picks'][0]['pick_id'] == 7384
    assert saved['top_picks'][0]['price'] == 1.78
    assert saved['bet_image']['date'] == day
    assert saved['bet_image']['jpeg'].startswith('/9j/')


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
