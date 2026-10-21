"""Presentation-only local crest catalog. No network or fuzzy club matching."""
import json
import logging
from pathlib import Path
import re
import unicodedata

log = logging.getLogger(__name__)

#: One index file per team-roster sport (built by tools/build_club_catalog.py
#: for football, tools/build_espn_catalog.py for the rest). Tennis has no
#: per-team crest (players, not teams) and is looked up separately, by
#: league/tournament text, via tennis_badge_file() below.
SPORT_INDEX = {
    'football': 'index.json',
    'basketball': 'index_nba.json',
    'american_football': 'index_nfl.json',
    'baseball': 'index_mlb.json',
}


def name_key(name):
    plain = unicodedata.normalize('NFKD',name.casefold()).encode('ascii','ignore').decode()
    return re.sub(r'[^a-z0-9]+',' ',plain).strip()


def _resolve(root, candidate):
    path = (root / candidate).resolve()
    if not path.is_relative_to(root.resolve()):
        return None
    return path


def badge_file(name, assets, index='index.json'):
    root = Path(assets) / 'badges'
    try:
        data = json.loads((root / index).read_text(encoding='utf-8'))
        candidates = data['aliases'].get(name_key(name),[])
        # Exact reviewed aliases only. Ambiguity must never select the first club.
        if len(candidates) != 1:
            return None
        return _resolve(root, candidates[0])
    except (OSError, ValueError, TypeError, KeyError):
        log.warning('Kovqi club catalog (%s) unavailable; omitting crests',index)
        return None


def team_badge_file(sport, name, assets):
    """Team-name crest lookup for any roster sport SPORT_INDEX knows about.
    An unlisted sport (tennis, mma, other) always returns None here - tennis
    has its own tennis_badge_file() instead; mma/other have no catalog."""
    index = SPORT_INDEX.get(sport)
    if index is None:
        return None
    return badge_file(name,assets,index)


def tennis_badge_file(league, assets):
    """League/tournament badge, keyed on the league TEXT rather than a team
    name - tennis picks name players, not teams, so there's no club crest to
    look up. Matches the LONGEST catalog alias that appears as a whole word
    run inside the (normalized) league text, so a specific Slam alias
    ("wimbledon") wins over a generic tour alias ("atp") when a league
    string like "ATP Wimbledon" contains both. Only singleton (unambiguous)
    alias entries are ever used, same discipline as badge_file()."""
    root = Path(assets) / 'badges'
    try:
        data = json.loads((root / 'index_tennis.json').read_text(encoding='utf-8'))
        text = name_key(league)
        if not text:
            return None
        best = None
        for alias,candidates in data['aliases'].items():
            if len(candidates) != 1 or not alias:
                continue
            if re.search(rf'\b{re.escape(alias)}\b',text) and (best is None or len(alias) > len(best[0])):
                best = (alias,candidates[0])
        return _resolve(root,best[1]) if best else None
    except (OSError, ValueError, TypeError, KeyError):
        log.warning('Kovqi tennis catalog unavailable; omitting crest')
        return None
