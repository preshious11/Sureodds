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
         "fk", "cs", "rc", "sv", "vfb", "vfl", "tsg", "1", "sporting"}

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


def same_team(a: str, b: str, threshold: float = 0.82) -> bool:
    return similarity(a, b) >= threshold
