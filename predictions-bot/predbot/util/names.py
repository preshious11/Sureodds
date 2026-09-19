"""Team-name normalisation and fuzzy matching (used to de-duplicate the same
fixture across providers and to match results back to picks)."""
from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

#: Generic club-name affixes only, never a word that distinguishes one real
#: club from another (e.g. "united"/"city"/"real"/"atletico" must stay OUT of
#: this list - stripping them made "Manchester United" == "Manchester City"
#: and "Real Madrid" == "Atletico Madrid" under same_team()).
_STOP = {"fc", "cf", "sc", "afc", "club", "de", "the", "cd", "ac", "as", "us", "ss", "bk", "if", "sk",
         "fk", "cs", "rc", "sv", "vfb", "vfl", "tsg", "1", "sporting", "rcd", "ud", "sd"}

#: Sports whose US-style names come as "BOS Red Sox" / "LA Dodgers" / "Athletics" on one provider
#: and "Boston Red Sox" / "Los Angeles Dodgers" / "Oakland Athletics" on another.
NICKNAME_SPORTS = {"baseball", "basketball", "american_football"}

#: A reserve/youth-team qualifier is never noise to strip: "Barcelona" and
#: "Barcelona B" are different clubs, not the same club spelled differently.
_RESERVE_SUFFIXES = {"b", "ii", "iii", "reserves", "reserve", "u23", "u21", "u20", "u19", "u18", "youth"}


def normalize(name: str) -> str:
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    s = s.lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    tokens = [t for t in s.split() if t not in _STOP]
    return " ".join(tokens) if tokens else s.strip()


def similarity(a: str, b: str) -> float:
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    ta, tb = set(na.split()), set(nb.split())
    extra = (ta - tb) | (tb - ta)
    if extra and extra <= _RESERVE_SUFFIXES:
        return 0.0
    if ta and tb and len(ta) > 1 and len(tb) > 1 and (ta <= tb or tb <= ta):
        return 0.92
    return SequenceMatcher(None, na, nb).ratio()


def strip_abbreviation(name: str) -> str:
    """Drop a leading/trailing all-caps 2-3 letter city code ("BOS Red Sox" -> "Red Sox", "LA Dodgers" ->
    "Dodgers"); a name that is nothing but the code is left alone."""
    tokens = (name or "").split()
    if len(tokens) >= 2 and tokens[0].isalpha() and tokens[0].isupper() and len(tokens[0]) <= 3:
        tokens = tokens[1:]
    if len(tokens) >= 2 and tokens[-1].isalpha() and tokens[-1].isupper() and len(tokens[-1]) <= 3:
        tokens = tokens[:-1]
    return " ".join(tokens)


def canonical_team(name: str, sport: str = "") -> str:
    """A spelling-independent token for a team, used to build fixture keys. Football keeps the normalised
    name; US-style sports reduce to the nickname (the last word, "red sox"/"white sox" keep two)."""
    if sport not in NICKNAME_SPORTS:
        return normalize(name)
    tokens = normalize(strip_abbreviation(name)).split()
    if not tokens:
        return normalize(name)
    return " ".join(tokens[-2:]) if tokens[-1] == "sox" and len(tokens) > 1 else tokens[-1]


def same_team(a: str, b: str, threshold: float = 0.82, sport: str = "") -> bool:
    """Fuzzy same-club test. ``sport`` (optional) switches on the US-sports rules: city abbreviations are
    stripped, and when one name's words are the END of the other's ("Rangers" / "Texas Rangers",
    "Athletics" / "Oakland Athletics") they are the same team. The threshold is never lowered."""
    if similarity(a, b) >= threshold:
        return True
    if sport in NICKNAME_SPORTS:
        sa, sb = strip_abbreviation(a), strip_abbreviation(b)
        if similarity(sa, sb) >= threshold:
            return True
        ta, tb = normalize(sa).split(), normalize(sb).split()
        if ta and tb:
            short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
            return long_[-len(short):] == short
    return False
