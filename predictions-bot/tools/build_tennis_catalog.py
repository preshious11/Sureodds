"""Build the offline tennis tour/tournament badge bundle. Never used at
runtime (same discipline as build_club_catalog.py / build_espn_catalog.py).

Tennis has no per-team crest - picks name players, not teams - so this is a
small, hand-curated set keyed by TOUR/TOURNAMENT text instead: the generic
ATP and WTA tour marks, plus the three Grand Slams with a Commons-hosted,
openly-licensed logo available (Australian Open, French Open/Roland Garros,
US Open). Wimbledon's crossed-rackets mark is NOT on Wikimedia Commons under
a free license (unlike the others, it's not mirrored there at all, which
usually means it's treated as a non-free trademark) - it is deliberately
left OUT rather than bundling anything of uncertain rights. An "ATP
Wimbledon"/"WTA Wimbledon" league string still gets a crest: it falls back
to the generic ATP/WTA tour badge via tennis_badge_file()'s longest-alias
substring match in club_badges.py.

python tools/build_tennis_catalog.py --download
"""
import argparse
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import sys
import time
from urllib.request import Request, urlopen

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from predbot.bot.club_badges import name_key

ASSETS = Path(__file__).resolve().parents[1] / 'assets/kovqi/badges'

#: Each source was checked individually via the Wikimedia API's
#: imageinfo/extmetadata (LicenseShortName) before being listed here -
#: Public Domain or CC0 only. aliases are the league-text keywords
#: tennis_badge_file() matches against (see its docstring for the
#: longest-match rule).
SOURCES = [
    {'name':'ATP Tour','aliases':['atp'],
     'url':'https://upload.wikimedia.org/wikipedia/commons/thumb/3/3f/ATP_Tour_logo.svg/500px-ATP_Tour_logo.svg.png',
     'license':'Public domain','credit':'Wikimedia Commons, File:ATP Tour logo.svg'},
    {'name':'WTA Tour','aliases':['wta'],
     'url':'https://upload.wikimedia.org/wikipedia/en/0/0e/WTA_Tour_logo.png',
     'license':'Public domain','credit':'English Wikipedia, File:WTA Tour logo.png'},
    {'name':'Australian Open','aliases':['australian open','ao'],
     'url':'https://upload.wikimedia.org/wikipedia/commons/thumb/b/b4/Australian_Open_Logo_2017.svg/500px-Australian_Open_Logo_2017.svg.png',
     'license':'Public domain','credit':'Wikimedia Commons, File:Australian Open Logo 2017.svg'},
    {'name':'French Open','aliases':['french open','roland garros','roland-garros'],
     'url':'https://upload.wikimedia.org/wikipedia/commons/2/2e/RG-Logo.png',
     'license':'CC0','credit':'Wikimedia Commons, File:RG-Logo.png'},
    {'name':'US Open','aliases':['us open'],
     'url':'https://upload.wikimedia.org/wikipedia/commons/thumb/c/cd/Usopen-horizontal-logo.svg/500px-Usopen-horizontal-logo.svg.png',
     'license':'Public domain','credit':'Wikimedia Commons, File:Usopen-horizontal-logo.svg'},
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--download',action='store_true',help='Explicit maintenance-only network access')
    args = parser.parse_args()
    if not args.download:
        parser.error('--download must be explicit; this tool never runs in production')
    destination = ASSETS / 'catalog'
    destination.mkdir(parents=True,exist_ok=True)

    from PIL import Image
    records = []
    for entry in SOURCES:
        req = Request(entry['url'],headers={'User-Agent':'SureOddsAssetBuild/1.0'})
        with urlopen(req,timeout=30) as response:
            content = response.read(2_000_000)
        with Image.open(BytesIO(content)) as source_image:
            image = source_image.convert('RGBA')
        bounds = image.getbbox()
        if not bounds:
            raise ValueError(f'empty source badge for {entry["name"]}')
        image = image.crop(bounds)
        image.thumbnail((112,112),Image.Resampling.LANCZOS)
        file = f'catalog/tennis-{name_key(entry["name"]).replace(" ","-")}.png'
        image.save(ASSETS/file,optimize=True)
        records.append({'name':entry['name'],'file':file,'source':entry['url'],
                         'license':entry['license'],'credit':entry['credit'],
                         'source_sha256':sha256(content).hexdigest(),'aliases':entry['aliases']})

    aliases = {}
    for record in records:
        for alias in record['aliases']:
            key = name_key(alias)
            if key:
                aliases.setdefault(key,[]).append(record['file'])
    for alias in aliases:
        aliases[alias] = sorted(set(aliases[alias]))
    index = {'built_at':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),
             'badges':[{k:v for k,v in r.items() if k != 'aliases'} for r in records],
             'aliases':dict(sorted(aliases.items()))}
    (ASSETS/'index_tennis.json').write_text(json.dumps(index,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(f'Bundled {len(records)} tennis tour/tournament badges -> index_tennis.json')


if __name__ == '__main__':
    main()
