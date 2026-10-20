"""Build the offline crest bundle from a pinned GitHub tree. Never used at runtime.

python tools/build_club_catalog.py --tree <GitHub recursive tree JSON> --download
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha1, sha256
from io import BytesIO
import json
from pathlib import Path
import re
import sys
import time
from urllib.parse import quote
from urllib.request import urlopen

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from predbot.bot.club_badges import name_key

REPO = 'luukhopman/football-logos'
ASSETS = Path(__file__).resolve().parents[1] / 'assets/kovqi/badges'
TOKENS = {'fc','afc','cf','sc','sk','fk','ac','as','bsc','sv','ssc','ogc','rcd'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tree',type=Path,required=True)
    parser.add_argument('--download',action='store_true',help='Explicit maintenance-only network access')
    args = parser.parse_args()
    tree = json.loads(args.tree.read_text(encoding='utf-8'))
    revision = tree['sha']
    if not re.fullmatch('[0-9a-f]{40}',revision):
        raise ValueError('invalid pinned revision')
    if not args.download:
        parser.error('--download must be explicit; this tool never runs in production')
    source_rows = [r for r in tree['tree'] if r['path'].startswith('logos/') and r['path'].endswith('.png')]
    destination = ASSETS / 'catalog'
    destination.mkdir(parents=True,exist_ok=True)

    def build(row):
        from PIL import Image, ImageDraw
        source = row['path']
        url = f'https://raw.githubusercontent.com/{REPO}/{revision}/'+quote(source)
        file = 'catalog/'+sha256(source.encode()).hexdigest()[:20]+'.png'
        for attempt in range(3):
            try:
                with urlopen(url,timeout=30) as response:
                    content = response.read(500000)
                digest = sha1(b'blob '+str(len(content)).encode()+b'\0'+content).hexdigest()
                if digest != row['sha']:
                    raise ValueError('catalog source hash mismatch')
                with Image.open(BytesIO(content)) as source_image:
                    image = source_image.convert('RGBA')
                # Remove only connected white exterior canvas, not crest interior.
                if image.getchannel('A').getextrema() == (255,255):
                    for corner in ((0,0),(image.width-1,0),(0,image.height-1),(image.width-1,image.height-1)):
                        if min(image.getpixel(corner)[:3]) >= 245:
                            ImageDraw.floodfill(image,corner,(255,255,255,0),thresh=8)
                bounds = image.getbbox()
                if not bounds:
                    raise ValueError('empty source badge')
                image = image.crop(bounds)
                image.thumbnail((112,112),Image.Resampling.LANCZOS)
                image.save(ASSETS/file,optimize=True)
                return {'name':Path(source).stem,'league':Path(source).parent.name,'file':file,
                        'source':url,'source_blob':row['sha']}
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(attempt+1)

    with ThreadPoolExecutor(max_workers=8) as executor:
        records = list(executor.map(build,source_rows))
    aliases = {}
    for record in records:
        key = name_key(record['name'])
        variants = {key,' '.join(word for word in key.split() if word not in TOKENS)}
        for variant in variants:
            if variant:
                aliases.setdefault(variant,[]).append(record['file'])
    # Explicit project overrides take precedence; no approximate-name matching.
    overrides = json.loads((ASSETS/'overrides.json').read_text(encoding='utf-8'))
    for alias,file in overrides.items():
        aliases[name_key(alias)] = [file]
    reviewed = json.loads((ASSETS/'reviewed_aliases.json').read_text(encoding='utf-8'))
    for alias,canonical in reviewed.items():
        matches = [r['file'] for r in records if name_key(r['name']) == name_key(canonical)]
        if len(matches) == 1:
            aliases[name_key(alias)] = matches
    for alias in aliases:
        aliases[alias] = sorted(set(aliases[alias]))
    index = {'source':f'https://github.com/{REPO}','revision':revision,
             'clubs':records,'aliases':dict(sorted(aliases.items()))}
    (ASSETS/'index.json').write_text(json.dumps(index,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(f'Bundled {len(records)} clubs; {sum(len(v)==1 for v in aliases.values())} unambiguous aliases.')


if __name__ == '__main__':
    main()
