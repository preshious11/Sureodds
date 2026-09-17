"""Temporary capability discovery (deleted after use).

Live calls made (recorded for the audit):
  1. SharpAPI GET /sports                          (sport catalog, no odds)
  2. SportGameOdds GET /sports                     (sport catalog, no objects billed)
  3. SharpAPI GET /odds?sport=tennis&limit=8       (tennis field-shape verification)
API-Football: 0 calls (daily quota exhausted server-side; capabilities from docs metadata).
"""
import json
import os
import sys

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from predbot.config import load_config  # noqa: E402

cfg = load_config()
out = {}


def get(label, url, headers, params=None):
    try:
        r = requests.get(url, headers=headers, params=params or {}, timeout=25)
        body = r.json() if r.headers.get('content-type', '').startswith('application/json') else r.text[:200]
    except Exception as e:  # noqa: BLE001
        out[label] = {'error': f'{type(e).__name__}: {e}'}
        return
    out[label] = {'status': r.status_code, 'body': body}


get('sharp_sports', 'https://api.sharpapi.io/api/v1/sports', {'X-API-Key': cfg.sharpapi_key})
get('sgo_sports', 'https://api.sportsgameodds.com/v2/sports', {'X-Api-Key': cfg.sgo_key}, {'limit': 100})
get('sharp_tennis_odds', 'https://api.sharpapi.io/api/v1/odds', {'X-API-Key': cfg.sharpapi_key},
    {'sport': 'tennis', 'is_live': 'false', 'limit': 8, 'market': 'moneyline,spread,total'})

print(json.dumps(out, indent=2, default=str), file=open('_discover_out.json', 'w', encoding='utf-8'))
print('written')