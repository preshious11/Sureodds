"""Offline presentation only. No network, prediction changes or publication writes."""
from __future__ import annotations

import base64
from datetime import datetime
from io import BytesIO
import json
import logging
import math
import os
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from .format import combined_odds

ASSETS = Path(__file__).resolve().parents[2] / 'assets' / 'kovqi'
DISCLAIMER = 'Bet only what you can afford to lose.'
log = logging.getLogger(__name__)


def group_link(value):
    value = (value or '').strip()
    if re.fullmatch(r'https://t\.me/(?:\+[A-Za-z0-9_-]{8,}|joinchat/[A-Za-z0-9_-]{8,}|[A-Za-z][A-Za-z0-9_]{4,31})', value):
        return value
    log.warning('Kovqi image: TELEGRAM_GROUP_INVITE_URL missing or invalid; omitting promotion')
    return ''


def ticket_identity(day, picks):
    return json.dumps([day, [[p.pick_id, p.fixture_id, p.market, p.selection, p.price,
                              p.home, p.away, p.label, p.kickoff_utc] for p in picks]], separators=(',', ':'), ensure_ascii=False)


def render_ticket(day, picks, link='', assets=ASSETS, sample=False):
    from PIL import Image, ImageDraw, ImageFont
    import qrcode
    if not 1 <= len(picks) <= 3:
        raise ValueError('Bet of the Day needs 1-3 selections')
    datetime.strptime(day, '%Y-%m-%d')
    for p in picks:
        if not all((p.pick_id, p.fixture_id, p.market, p.selection, p.home, p.away, p.label)):
            raise ValueError('incomplete structured ticket identity')
        if not math.isfinite(p.price) or p.price <= 1:
            raise ValueError('invalid decimal odds')
        datetime.fromisoformat(p.kickoff_utc.replace('Z', '+00:00')).astimezone(ZoneInfo('Africa/Lagos'))
    odds = combined_odds(p.price for p in picks)
    link = group_link(link)
    font_path = str(assets / 'BarlowCondensed-Bold.ttf')
    font = lambda size: ImageFont.truetype(str(assets / 'Barlow-Regular.ttf') if size <= 38 else font_path, size)
    width, height = 1000, 870 + len(picks) * 240 + (140 if link else 0)
    im = Image.new('RGB', (width, height), '#101820')
    d = ImageDraw.Draw(im)
    lime, white, muted = '#C6F36B', '#FFFFFF', '#AAC0CD'
    d.polygon([(0,0),(230,0),(0,230)], fill='#253d25')
    d.polygon([(1000,height),(740,height),(1000,height-320)], fill='#253d25')
    # A quiet pitch motif stays behind the footer, away from fixture text.
    d.rounded_rectangle((600,height-220,960,height+40),radius=10,outline='#26353B',width=2)
    d.line((780,height-220,780,height+40),fill='#26353B',width=2)
    d.ellipse((720,height-150,840,height-30),outline='#26353B',width=2)
    # Crop the supplied approved logo; no generated or re-typeset brand mark.
    with Image.open(assets / 'logo-source.png') as source:
        logo = source.convert('RGB').crop((150, 425, 1110, 815))
        logo.thumbnail((360, 148))
        im.paste(logo, (60, 45))
    d.text((65, 200), 'Football. Smarter.', font=font(28), fill=muted)
    d.text((690, 80), datetime.strptime(day,'%Y-%m-%d').strftime('%d %b %Y').upper(), font=font(30), fill=white)
    d.text((690, 120), 'AFRICA / LAGOS', font=font(22), fill=muted)
    d.rounded_rectangle((55,270,945,395), radius=18, fill=lime)
    d.text((85,283), 'BET OF THE DAY', font=font(84), fill='#101820')
    d.text((65,420), 'HANDPICKED MATCHES   /   SMART ANALYSIS', font=font(28), fill=muted)
    if sample:
        d.text((65,238), 'SAMPLE DATA - PREVIEW ONLY', font=font(24), fill=lime)

    def fit(text, x, y, max_width, size, color, max_lines=2):
        # Wrap all content; reject extreme strings instead of clipping real selections.
        for current in range(size, 19, -1):
            f = font(current)
            lines, line = [], ''
            for word in str(text).split():
                candidate = (line + ' ' + word).strip()
                if d.textlength(candidate, font=f) > max_width:
                    if line: lines.append(line)
                    line = word
                else: line = candidate
            if line: lines.append(line)
            if len(lines) <= max_lines and all(d.textlength(s,font=f)<=max_width for s in lines):
                for i,s in enumerate(lines): d.text((x,y+i*(current+5)),s,font=f,fill=color)
                return
        raise ValueError('ticket text exceeds readable layout')

    for i,p in enumerate(picks):
        y = 480 + i*240
        d.rounded_rectangle((55,y,945,y+220),radius=22,fill='#0A1219',outline='#334651',width=2)
        fit(f'{p.home}  vs  {p.away}',80,y+18,650,34,white)
        kickoff = datetime.fromisoformat(p.kickoff_utc.replace('Z','+00:00')).astimezone(ZoneInfo('Africa/Lagos'))
        fit(f'{p.league}  |  {kickoff:%H:%M} WAT',80,y+110,650,25,muted,1)
        fit(p.label,80,y+148,650,26,lime,2)
        d.text((790,y+70),f'{p.price:.2f}',font=font(54),fill=lime)
    y = 500 + len(picks)*240
    d.text((65,y),'COMBINED ODDS',font=font(30),fill=white)
    d.text((65,y+42),f'{odds:.2f}',font=font(90),fill=lime)
    if link:
        y += 155
        d.rounded_rectangle((55,y,945,y+135),radius=22,outline=lime,width=3)
        d.text((80,y+15),'JOIN OUR TELEGRAM COMMUNITY',font=font(30),fill=white)
        fit(link.removeprefix('https://'),80,y+65,670,32,lime,1)
        qr = qrcode.make(link).convert('RGB').resize((110,110),Image.Resampling.NEAREST)
        im.paste(qr,(810,y+12))
    d.text((65,height-100),DISCLAIMER,font=font(32),fill=muted)
    out = BytesIO()
    im = im.resize((800, round(height * .8)), Image.Resampling.LANCZOS)
    im.save(out,format='JPEG',quality=65,optimize=True)
    return out.getvalue()


def attach_image(snapshot):
    """Bound optional media; oversize SQL rows use existing parameterized REST sync."""
    snapshot.bet_image = None
    if snapshot.status != 'COMPLETE' or not snapshot.top_picks:
        return
    try:
        link = group_link(os.getenv('TELEGRAM_GROUP_INVITE_URL'))
        jpeg = render_ticket(snapshot.date,snapshot.top_picks,link)
        media = {'date': snapshot.date, 'identity': ticket_identity(snapshot.date,snapshot.top_picks),
                 'link':link, 'jpeg':base64.b64encode(jpeg).decode('ascii')}
        snapshot.bet_image = media
        # Existing d1_sync diverts >90KB rows to REST (2MB row limit).
        if len(media['jpeg']) > 160000 or len(json.dumps(snapshot.to_dict(),ensure_ascii=True).encode()) > 1500000:
            raise ValueError('image plus snapshot exceeds safe D1 sync budget')
    except Exception as exc:
        snapshot.bet_image = None
        log.warning('Kovqi image unavailable; using text ticket: %s',type(exc).__name__)
