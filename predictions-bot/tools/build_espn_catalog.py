"""Build an offline team-crest bundle for a roster sport from ESPN's public
team-logo CDN. Never used at runtime (same discipline as build_club_catalog.py).

python tools/build_espn_catalog.py --sport nba --download
python tools/build_espn_catalog.py --sport nfl --download
python tools/build_espn_catalog.py --sport mlb --download
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
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

#: ESPN's "site.api" sport/league path + team-logo CDN sport slug, per bot sport key.
#: SPORT_INDEX in club_badges.py is the single source of truth for which sport
#: maps to which built index file; this script only needs the ESPN-side names.
SPORTS = {
    'nba': {'api': 'basketball/nba', 'cdn': 'nba', 'index': 'index_nba.json'},
    'nfl': {'api': 'football/nfl', 'cdn': 'nfl', 'index': 'index_nfl.json'},
    'mlb': {'api': 'baseball/mlb', 'cdn': 'mlb', 'index': 'index_mlb.json'},
}


def fetch_json(url):
    req = Request(url,headers={'User-Agent':'Mozilla/5.0'})
    with urlopen(req,timeout=30) as response:
        return json.loads(response.read())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sport',choices=sorted(SPORTS),required=True)
    parser.add_argument('--download',action='store_true',help='Explicit maintenance-only network access')
    args = parser.parse_args()
    if not args.download:
        parser.error('--download must be explicit; this tool never runs in production')
    cfg = SPORTS[args.sport]
    roster = fetch_json(f'https://site.api.espn.com/apis/site/v2/sports/{cfg["api"]}/teams?limit=50')
    teams = roster['sports'][0]['leagues'][0]['teams']
    destination = ASSETS / 'catalog'
    destination.mkdir(parents=True,exist_ok=True)

    def build(entry):
        team = entry['team']
        display_name = team['displayName']  # e.g. "Washington Wizards"
        abbr = team['abbreviation'].lower()
        url = f'https://a.espncdn.com/i/teamlogos/{cfg["cdn"]}/500/{abbr}.png'
        file = f'catalog/espn-{cfg["cdn"]}-{abbr}.png'
        for attempt in range(3):
            try:
                req = Request(url,headers={'User-Agent':'Mozilla/5.0'})
                with urlopen(req,timeout=30) as response:
                    content = response.read(2_000_000)
                from PIL import Image
                with Image.open(BytesIO(content)) as source_image:
                    image = source_image.convert('RGBA')
                bounds = image.getbbox()
                if not bounds:
                    raise ValueError('empty source badge')
                image = image.crop(bounds)
                image.thumbnail((112,112),Image.Resampling.LANCZOS)
                image.save(ASSETS/file,optimize=True)
                return {'name':display_name,'abbreviation':team['abbreviation'],'file':file,
                        'source':url,'source_sha256':sha256(content).hexdigest()}
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(attempt+1)

    with ThreadPoolExecutor(max_workers=8) as executor:
        records = list(executor.map(build,teams))
    aliases = {}
    for record in records:
        key = name_key(record['name'])
        # Both the full display name ("washington wizards") and the bare
        # nickname ("wizards") resolve - the bot's home/away strings are
        # usually the full name, but this keeps the lookup forgiving the
        # same way build_club_catalog.py's TOKENS-stripped variant does.
        nickname = key.split()[-1] if ' ' in key else key
        for variant in (key,nickname,name_key(record['abbreviation'])):
            if variant:
                aliases.setdefault(variant,[]).append(record['file'])
    for alias in aliases:
        aliases[alias] = sorted(set(aliases[alias]))
    index = {'source':f'https://a.espncdn.com/i/teamlogos/{cfg["cdn"]}/',
             'built_at':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),
             'clubs':records,'aliases':dict(sorted(aliases.items()))}
    (ASSETS/cfg['index']).write_text(json.dumps(index,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    unambiguous = sum(len(v) == 1 for v in aliases.values())
    print(f'Bundled {len(records)} {args.sport.upper()} teams; {unambiguous} unambiguous aliases -> {cfg["index"]}')


if __name__ == '__main__':
    main()
