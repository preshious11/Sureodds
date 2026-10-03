"""Team-name normalisation and fuzzy matching (used to de-duplicate the same
fixture across providers and to match results back to picks)."""
from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from functools import lru_cache

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


@lru_cache(maxsize=65536)
def normalize(name: str) -> str:
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    s = s.lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    tokens = [t for t in s.split() if t not in _STOP]
    return " ".join(tokens) if tokens else s.strip()


@lru_cache(maxsize=262144)
def similarity(a: str, b: str, floor: float = 0.0) -> float:
    """Name similarity in [0, 1]. ``floor`` is an optimisation only: when the cheap upper bounds of the ratio
    already prove it is below ``floor``, 0.0 is returned without running the full matcher - so a caller that
    only asks "is it >= floor?" (same_team) gets the identical answer."""
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
    sm = SequenceMatcher(None, na, nb)
    if floor and (sm.real_quick_ratio() < floor or sm.quick_ratio() < floor):
        return 0.0
    return sm.ratio()


def is_doubles(home: str, away: str) -> bool:
    """A tennis doubles fixture: a side is a pair ("Dabrowski / Stefani"). Neither SharpAPI's ``/odds`` rows nor
    the Odds API carry a doubles flag or participant count, so the pair separator is the indicator."""
    return " / " in (home or "") or " / " in (away or "")


def strip_abbreviation(name: str) -> str:
    """Drop a leading/trailing all-caps 2-3 letter city code ("BOS Red Sox" -> "Red Sox", "LA Dodgers" ->
    "Dodgers"); a name that is nothing but the code is left alone."""
    tokens = (name or "").split()
    if len(tokens) >= 2 and tokens[0].isalpha() and tokens[0].isupper() and len(tokens[0]) <= 3:
        tokens = tokens[1:]
    if len(tokens) >= 2 and tokens[-1].isalpha() and tokens[-1].isupper() and len(tokens[-1]) <= 3:
        tokens = tokens[:-1]
    return " ".join(tokens)


#: Last words that name an institution or a kind of place, not a team: "Ohio State" and "Kent State" do not share a
#: nickname. A name ending in one of these (or in a single letter, "Texas A&M") has no nickname to reduce to.
GENERIC_TAILS = {"state", "university", "college", "tech", "institute", "polytechnic", "city", "county"}


def canonical_team(name: str, sport: str = "") -> str:
    """A spelling-independent token for a team, used to build fixture keys and participant identity. Football keeps
    the normalised name; US-style sports reduce to the nickname (the last word, "red sox"/"white sox" keep two) -
    but only when that last word IS a nickname. A name that ends in a generic institutional word ("Ohio State",
    "Kent State", "Texas A&M") keeps its full normalised form, otherwise every "<place> State" would be one team."""
    if sport not in NICKNAME_SPORTS:
        return normalize(name)
    tokens = normalize(strip_abbreviation(name)).split()
    if not tokens:
        return normalize(name)
    if len(tokens) > 1 and (tokens[-1] in GENERIC_TAILS or len(tokens[-1]) == 1):
        return " ".join(tokens)
    return " ".join(tokens[-2:]) if tokens[-1] == "sox" and len(tokens) > 1 else tokens[-1]


def same_team(a: str, b: str, threshold: float = 0.82, sport: str = "") -> bool:
    """Fuzzy same-club test. ``sport`` (optional) switches on the US-sports rules: city abbreviations are
    stripped, and when one name's words are the END of the other's ("Rangers" / "Texas Rangers",
    "Athletics" / "Oakland Athletics") they are the same team. The threshold is never lowered."""
    if similarity(a, b, threshold) >= threshold:
        return True
    if sport in NICKNAME_SPORTS:
        sa, sb = strip_abbreviation(a), strip_abbreviation(b)
        if similarity(sa, sb, threshold) >= threshold:
            return True
        ta, tb = normalize(sa).split(), normalize(sb).split()
        if ta and tb:
            short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
            if len(short) == 1 and (short[0] in GENERIC_TAILS or len(short[0]) == 1):
                return False
            return long_[-len(short):] == short
    return False
