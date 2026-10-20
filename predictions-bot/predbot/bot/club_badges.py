"""Presentation-only local crest catalog. No network or fuzzy club matching."""
import json
import logging
from pathlib import Path
import re
import unicodedata

log = logging.getLogger(__name__)


def name_key(name):
    plain = unicodedata.normalize('NFKD',name.casefold()).encode('ascii','ignore').decode()
    return re.sub(r'[^a-z0-9]+',' ',plain).strip()


def badge_file(name, assets):
    root = Path(assets) / 'badges'
    try:
        index = json.loads((root / 'index.json').read_text(encoding='utf-8'))
        candidates = index['aliases'].get(name_key(name),[])
        # Exact reviewed aliases only. Ambiguity must never select the first club.
        if len(candidates) != 1:
            return None
        path = (root / candidates[0]).resolve()
        if not path.is_relative_to(root.resolve()):
            return None
        return path
    except (OSError, ValueError, TypeError, KeyError):
        log.warning('Kovqi club catalog unavailable; omitting crests')
        return None
