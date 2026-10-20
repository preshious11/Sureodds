"""Generate a labeled fictional preview; never publishes or reads production data."""
import argparse
from dataclasses import replace
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from predbot.bot.ticket_image import ASSETS, render_ticket
from predbot.cache.models import PickRecord


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot',type=Path,help='Read an existing structured ticket JSON for a preview only')
    parser.add_argument('--link',default=os.getenv('TELEGRAM_GROUP_INVITE_URL',''))
    parser.add_argument('--catalog-samples',action='store_true',help='Also render future-date samples with other catalog clubs')
    args = parser.parse_args()
    day = datetime.now(ZoneInfo('Africa/Lagos')).date().isoformat()
    destination = ASSETS / 'previews'
    destination.mkdir(exist_ok=True)
    if args.snapshot:
        data = json.loads(args.snapshot.read_text(encoding='utf-8-sig'))
        picks = [PickRecord.from_dict(p) for p in data['top_picks']]
        link = args.link or data.get('link','')
        path = destination / 'current-ticket-redesign.jpg'
        path.write_bytes(render_ticket(data['date'],picks,link))
        print(path)
        day = data['date']
    else:
        link = args.link
    sample = [('Sample United','Example City',1.28),
              ('Demo Athletic','Sample Rovers',1.25),
              ('Example Town','Demo FC',1.26)]
    picks = [PickRecord(pick_id=i, fixture_id=f'sample:{i}', market='result3',selection='home',
                        home=home,away=away,label='Home team to win',price=price,
                        league='Sample League',kickoff_utc=f'{day}T17:00:00Z')
             for i,(home,away,price) in enumerate(sample,1)]
    for count in (1,2,3):
        path = destination / f'sample-{count}-selection.jpg'
        path.write_bytes(render_ticket(day,picks[:count],link,sample=True))
        print(path)
    stress = [replace(picks[0],home='Club Deportivo Universidad de Concepción',
                      away='Brighton & Hove Albion Women',
                      league='International Continental Championship · Qualification',
                      label='Home team double chance and over one and a half total goals'),
              replace(picks[1],home='Fenerbahçe',away='ŠK Slavia Praha',league='',price=12.75)]
    path = destination / 'sample-long-names.jpg'
    path.write_bytes(render_ticket(day,stress,link,sample=True))
    print(path)
    (ASSETS / 'sample-preview.jpg').write_bytes((destination / 'sample-3-selection.jpg').read_bytes())
    if args.catalog_samples:
        future_day = (datetime.strptime(day,'%Y-%m-%d').date()+timedelta(days=1)).isoformat()
        examples = [('Arsenal','Fulham','Premier League','result3','home','Arsenal to Win',1.28),
                    ('Bayern Munich','Borussia Dortmund','Bundesliga','btts','yes','Both teams to score: Yes',1.25),
                    ('Napoli','Inter Milan','Serie A','dc','away_draw','Inter Milan or Draw',1.26)]
        future = [PickRecord(pick_id=9000+i,fixture_id=f'sample-future:{i}',sport='football',
                             home=h,away=a,league=league,market=m,selection=s,label=label,price=price,
                             kickoff_utc=f'{future_day}T17:00:00Z')
                  for i,(h,a,league,m,s,label,price) in enumerate(examples,1)]
        for count in (1,2,3):
            path = destination / f'future-ticket-{count}.jpg'
            path.write_bytes(render_ticket(future_day,future[:count],link,sample=True))
            print(path)


if __name__ == '__main__':
    main()
