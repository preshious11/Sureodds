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

def team_badge(name, assets=ASSETS):
    from PIL import Image
    from .club_badges import badge_file
    file = badge_file(name,assets)
    if file is None:
        return None
    try:
        with Image.open(file) as image:
            badge = image.convert('RGBA')
        bounds = badge.getbbox()
        return badge.crop(bounds) if bounds else None
    except (OSError, ValueError):
        log.warning('Kovqi verified club badge unavailable; using text fixture')
        return None


def group_link(value):
    value = (value or '').strip()
    if re.fullmatch(r'https://t\.me/(?:\+[A-Za-z0-9_-]{8,}|joinchat/[A-Za-z0-9_-]{8,}|[A-Za-z][A-Za-z0-9_]{4,31})', value):
        return value
    log.warning('Kovqi image: TELEGRAM_GROUP_INVITE_URL missing or invalid; omitting promotion')
    return ''


def ticket_identity(day, picks):
    return json.dumps([day, [[p.pick_id, p.fixture_id, p.market, p.selection, p.price,
                              p.home, p.away, p.label, p.kickoff_utc] for p in picks]], separators=(',', ':'), ensure_ascii=False)


def transparent_logo(assets=ASSETS):
    """Matte the approved source pixels, preserving its actual letterforms."""
    from PIL import Image, ImageChops
    with Image.open(assets / 'logo-source.png') as source:
        logo = source.convert('RGB').crop((150, 425, 1110, 815))
    red, green, blue = logo.split()
    strength = ImageChops.lighter(ImageChops.lighter(red, green), blue)
    logo.putalpha(strength.point(lambda v: max(0, min(255, (v - 42) * 255 // 130))))
    return logo.crop(logo.getbbox())


def render_ticket(day, picks, link='', assets=ASSETS, sample=False):
    from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageFilter, ImageEnhance
    import qrcode
    if not 1 <= len(picks) <= 3:
        raise ValueError('Bet of the Day needs 1-3 selections')
    date = datetime.strptime(day, '%Y-%m-%d')
    for p in picks:
        if not all((p.pick_id, p.fixture_id, p.market, p.selection, p.home, p.away, p.label)):
            raise ValueError('incomplete structured ticket identity')
        if not math.isfinite(p.price) or p.price <= 1:
            raise ValueError('invalid decimal odds')
    odds = combined_odds(p.price for p in picks)
    link = group_link(link)
    width, height = 1080, 1350
    lime, white, muted = '#BEF53B', '#F5F8FA', '#ADC5D3'
    fonts = {}

    def font(size, weight='regular'):
        name = {'regular':'Barlow-Regular.ttf', 'bold':'Barlow-SemiBold.ttf',
                'display':'BarlowCondensed-Bold.ttf','script':'KaushanScript-Regular.ttf',
                'headline':'Anton-Regular.ttf'}[weight]
        key = (size, weight)
        if key not in fonts:
            fonts[key] = ImageFont.truetype(str(assets / name), size)
        return fonts[key]

    # Required fonts fail visibly into the existing text-ticket fallback.
    for weight in ('regular', 'bold', 'display'):
        font(24, weight)
    try:
        font(24,'script')
        script_weight = 'script'
    except OSError:
        script_weight = 'display'
    try:
        font(24,'headline')
        headline_weight = 'headline'
    except OSError:
        headline_weight = 'display'
    try:
        with Image.open(assets / 'stadium-background.png') as background:
            im = ImageOps.fit(background.convert('RGB'), (width,height), method=Image.Resampling.LANCZOS).filter(ImageFilter.GaussianBlur(1.25))
    except (OSError, ValueError):
        log.warning('Kovqi optional stadium background unavailable; using local gradient')
        im = Image.new('RGB', (width,height), '#070D10')
        gradient = ImageDraw.Draw(im)
        for y in range(height):
            light = max(0, (y - 850) // 30)
            gradient.line((0,y,width,y), fill=(5,12+light,13))
    im = ImageEnhance.Brightness(im).enhance(.78).convert('RGBA')
    layer = Image.new('RGBA', im.size)
    ld = ImageDraw.Draw(layer)
    ld.polygon([(0,0),(182,0),(0,182)],fill=(143,222,20,225))
    ld.polygon([(0,182),(182,0),(232,0),(0,232)],fill=(37,87,12,135))
    ld.polygon([(1080,430),(1006,505),(1006,620),(1080,546)],fill=(82,139,18,100))
    im = Image.alpha_composite(im,layer)
    d = ImageDraw.Draw(im)

    def layout(text, max_width, size, weight='regular', max_lines=2, minimum=22):
        for current in range(size,minimum-1,-1):
            f = font(current,weight)
            lines, line = [], ''
            for word in str(text).split():
                candidate = (line + ' ' + word).strip()
                if d.textlength(candidate,font=f) > max_width:
                    if line:
                        lines.append(line)
                    line = word
                else:
                    line = candidate
            if line:
                lines.append(line)
            if len(lines) <= max_lines and all(d.textlength(s,font=f) <= max_width for s in lines):
                return lines, f, current + 6
        raise ValueError('ticket text exceeds readable layout')

    def block(plan, x, y, color):
        lines, f, step = plan
        for i, line in enumerate(lines):
            d.text((x,y+i*step),line,font=f,fill=color,anchor='lt')
        return len(lines)*step

    def tracked(text, xy, size, color, spacing=3):
        x,y = xy
        f = font(size,'bold')
        for letter in text:
            d.text((x,y),letter,font=f,fill=color,anchor='lt')
            x += d.textlength(letter,font=f)+spacing

    logo = transparent_logo(assets)
    logo.thumbnail((286,116),Image.Resampling.LANCZOS)
    im.alpha_composite(logo,(82,65))
    d = ImageDraw.Draw(im)
    d.text((139,190),'Football. Smarter.',font=font(23),fill=muted,anchor='lt')
    tracked('BET OF THE DAY',(781,65),18,white,2)
    # Calendar icon, drawn locally rather than relying on emoji fonts.
    d.rounded_rectangle((785,108,810,132),radius=3,outline=muted,width=2)
    d.line((785,116,810,116),fill=muted,width=2)
    d.line((791,103,791,111),fill=muted,width=3)
    d.line((804,103,804,111),fill=muted,width=3)
    d.text((826,108),date.strftime('%d %b %Y').upper(),font=font(24,'bold'),fill=muted,anchor='lt')
    if sample:
        d.text((785,151),'SAMPLE · NOT A LIVE TICKET',font=font(15,'bold'),fill=lime,anchor='lt')

    # Reference composition: crown, split-color headline and handwritten aside.
    d.polygon([(77,295),(68,261),(91,274),(106,249),(120,274),(144,261),(135,295)],fill=lime)
    heading = font(112,headline_weight)
    d.text((68,312),'BET',font=heading,fill=white,anchor='lt')
    bet_width = d.textlength('BET ',font=heading)
    d.text((68+bet_width,312),'OF THE DAY',font=heading,fill=lime,anchor='lt')
    for i,line in enumerate(('Smart','Bets.','Real Value.')):
        d.text((811 if i<2 else 778,257+i*52),line,font=font(43,script_weight),fill=lime,anchor='lt')
    d.line((813,426,978,402),fill=lime,width=4)
    tracked('HANDPICKED MATCHES  |  SMART ANALYSIS  |  BETTER ODDS',(72,440),18,muted,1.4)

    count = len(picks)
    plans = []
    for p in picks:
        kickoff = datetime.fromisoformat(p.kickoff_utc.replace('Z','+00:00')).astimezone(ZoneInfo('Africa/Lagos'))
        def club_name(text,span):
            try:
                return layout(text,span,27,'bold',1,21)
            except ValueError:
                return layout(text,span,26,'bold',3,21)
        home,away = club_name(p.home,176),club_name(p.away,190)
        if count == 3:
            if len(home[0]) == 3:
                home = layout(p.home,176,23,'bold',3,21)
            if len(away[0]) == 3:
                away = layout(p.away,190,23,'bold',3,21)
        metadata = layout(f'{p.league or "Football"}  |  {kickoff:%H:%M} WAT',474,21,max_lines=2,minimum=20)
        market_name = {'result3':'1X2','result2':'MATCH WINNER','total':'O/U','dc':'DOUBLE CHANCE',
                       'btts':'BTTS','double_chance':'DOUBLE CHANCE'}.get(p.market,p.market.replace('_',' ').upper())
        market = layout(market_name,280,20,'bold',2,18)
        prediction = layout(p.label,224,25,'bold',4,21)
        if len(prediction[0]) == 4:
            prediction = layout(p.label,224,21,'bold',4,21)
        fixture_height = max(len(home[0])*home[2],len(away[0])*away[2])
        needed = max(fixture_height+len(metadata[0])*metadata[2]+34,
                     len(market[0])*market[2]+len(prediction[0])*prediction[2]+34)
        row_height = max(166 if count == 1 else 150,needed)
        home_badge,away_badge = team_badge(p.home,assets),team_badge(p.away,assets)
        plans.append((p,home,away,metadata,market,prediction,row_height,home_badge,away_badge))
    rows_height = sum(plan[6] for plan in plans)
    ticket_top = max(476,min(504,1124-rows_height-122))
    ticket_bottom = ticket_top + rows_height + 122
    if ticket_bottom > 1124:
        # Overlong content uses the established text fallback rather than clipping.
        raise ValueError('ticket text exceeds poster safe area')
    # One cohesive ticket, with restrained depth and a distinct total strip.
    panel = Image.new('RGBA',im.size)
    pd = ImageDraw.Draw(panel)
    pd.rounded_rectangle((55,ticket_top+8,1025,ticket_bottom+8),radius=27,fill=(0,0,0,100))
    pd.rounded_rectangle((56,ticket_top,1024,ticket_bottom),radius=27,fill=(7,17,23,244),outline=(61,83,96,230),width=2)
    im = Image.alpha_composite(im,panel)
    d = ImageDraw.Draw(im)
    y = ticket_top
    for index,(p,home,away,metadata,market,prediction,row_height,home_badge,away_badge) in enumerate(plans):
        fixture_height = max(len(home[0])*home[2],len(away[0])*away[2])
        fixture_y = y+(row_height-fixture_height-len(metadata[0])*metadata[2]-14)//2
        for badge,bx in ((home_badge,82),(away_badge,573)):
            if badge is not None:
                badge.thumbnail((58,72),Image.Resampling.LANCZOS)
                im.alpha_composite(badge,(bx+(58-badge.width)//2,fixture_y+(max(50,fixture_height)-badge.height)//2))
        d = ImageDraw.Draw(im)
        block(home,151,fixture_y,white)
        d.text((340,fixture_y+5),'vs',font=font(20),fill=muted,anchor='lt')
        block(away,376,fixture_y,white)
        block(metadata,151,fixture_y+fixture_height+14,muted)
        d.rounded_rectangle((644,y+14,1000,y+row_height-14),radius=18,fill='#101D25',outline='#283C48',width=1)
        right_height = len(market[0])*market[2]+len(prediction[0])*prediction[2]+13
        right_y = y+(row_height-right_height)//2
        market_height = block(market,664,right_y,lime)
        block(prediction,664,right_y+market_height+13,white)
        price = f'{p.price:.2f}'
        price_font = font(39,'display')
        while d.textlength(price,font=price_font)>96:
            if price_font.size <= 25:
                raise ValueError('odds exceed poster safe area')
            price_font = font(price_font.size-1,'display')
        d.text((981,y+row_height//2+10),price,font=price_font,fill=lime,anchor='rm')
        y += row_height
        if index < count-1:
            d.line((82,y,998,y),fill='#293B45',width=1)
    d.line((82,y,998,y),fill='#3B515C',width=2)
    # The reference gives the summary its own inset card inside the ticket.
    d.rounded_rectangle((74,y+14,1006,y+110),radius=20,fill='#071218',outline='#2E4651',width=1)
    d.rounded_rectangle((87,y+30,119,y+76),radius=3,outline=lime,width=3)
    d.rectangle((95,y+26,111,y+33),fill='#0A151C',outline=lime,width=2)
    for offset in (43,53,63):
        d.line((95,y+offset,111,y+offset),fill=lime,width=2)
    tracked('COMBINED ODDS',(143,y+25),21,white,2.3)
    d.text((142,y+48),f'{odds:.2f}',font=font(58,'display'),fill=lime,anchor='lt')
    d.line((490,y+25,490,y+94),fill='#2E4651',width=1)
    # The reference's optional stake space is decorative; no amount is invented.
    for cy in (y+55,y+65,y+75):
        d.ellipse((523,cy-9,555,cy+3),outline=muted,width=2)
        d.arc((523,cy-15,555,cy-1),0,180,fill=muted,width=2)
    d.text((576,y+39),'STAKE',font=font(20),fill=muted,anchor='lt')
    d.text((576,y+66),'(Optional)',font=font(20),fill=muted,anchor='lt')
    d.text((781,y+36),'Good luck!',font=font(31,script_weight),fill=lime,anchor='lt')
    d.line((793,y+92,965,y+73),fill=lime,width=3)

    footer_y = ticket_bottom+135
    if link:
        cta_y = ticket_bottom+24
        d.rounded_rectangle((56,cta_y,1024,cta_y+142),radius=27,fill='#050E11',outline=lime,width=3)
        d.ellipse((82,cta_y+35,150,cta_y+103),fill=lime)
        d.polygon([(96,cta_y+64),(137,cta_y+49),(127,cta_y+85),(116,cta_y+75),(109,cta_y+82),(110,cta_y+69)],fill='#08140C')
        tracked('JOIN OUR TELEGRAM COMMUNITY',(176,cta_y+31),19,white,1.4)
        block(layout(link.removeprefix('https://'),510,28,'bold',2,22),176,cta_y+68,lime)
        d.line((708,cta_y+30,708,cta_y+112),fill='#81AD30',width=1)
        for offset,line in enumerate(('Scan to join','or click the link')):
            d.text((884,cta_y+51+offset*23),line,font=font(16),fill=muted,anchor='lt')
        # Integer module size and an untouched four-module quiet zone.
        qr = qrcode.QRCode(box_size=1,border=4,error_correction=qrcode.constants.ERROR_CORRECT_M)
        qr.add_data(link)
        qr.make(fit=True)
        code = qr.make_image(fill_color='black',back_color='white').convert('RGB')
        scale = max(1,116//code.width)
        code = code.resize((code.width*scale,code.height*scale),Image.Resampling.NEAREST)
        im.paste(code,(749+(116-code.width)//2,cta_y+(142-code.height)//2))
        footer_y = min(cta_y+190,height-48)
    else:
        log.info('Kovqi community section omitted: no configured invitation URL')
    d = ImageDraw.Draw(im)
    disclaimer_font = font(25,'bold')
    footer_start = (1080-d.textlength(DISCLAIMER,font=disclaimer_font)-42)/2
    d.line((64,footer_y+11,footer_start-28,footer_y+11),fill='#708A98',width=1)
    d.polygon([(footer_start+12,footer_y-1),(footer_start+24,footer_y+4),
               (footer_start+22,footer_y+21),(footer_start+12,footer_y+29),
               (footer_start+2,footer_y+21),(footer_start,footer_y+4)],outline=muted,width=2)
    d.text((footer_start+42,footer_y-1),DISCLAIMER,font=disclaimer_font,fill=muted,anchor='lt')
    footer_end = footer_start+42+d.textlength(DISCLAIMER,font=disclaimer_font)
    d.line((footer_end+28,footer_y+11,1016,footer_y+11),fill='#708A98',width=1)
    # Keep a consistent bottom margin instead of leaving a large empty field
    # when the real ticket contains fewer selections than the reference.
    im = im.crop((0,0,width,min(height,footer_y+76)))
    # Keep the existing Worker/D1 media ceiling, without changing delivery code.
    for quality in (88,84,80,76,72,68,64,60,56,52):
        out = BytesIO()
        im.convert('RGB').save(out,format='JPEG',quality=quality,optimize=True,progressive=True,subsampling=2)
        jpeg = out.getvalue()
        if len(jpeg) <= 120000:
            return jpeg
    raise ValueError('poster exceeds existing media byte budget')


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
