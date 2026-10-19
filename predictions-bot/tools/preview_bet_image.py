"""Generate a labeled fictional preview; never publishes or reads production data."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from predbot.bot.ticket_image import ASSETS, render_ticket
from predbot.cache.models import PickRecord


def main():
    sample = [('Sample United','Example City',1.28),
              ('Demo Athletic','Sample Rovers',1.25),
              ('Example Town','Demo FC',1.26)]
    picks = [PickRecord(pick_id=i, fixture_id=f'sample:{i}', market='result3',selection='home',
                        home=home,away=away,label='Home team to win',price=price,
                        league='Sample League',kickoff_utc='2026-10-19T17:00:00Z')
             for i,(home,away,price) in enumerate(sample,1)]
    path = ASSETS / 'sample-preview.jpg'
    path.write_bytes(render_ticket('2026-10-19',picks,sample=True))
    print(path)


if __name__ == '__main__':
    main()
