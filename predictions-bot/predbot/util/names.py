"""Team-name normalisation and fuzzy matching (used to de-duplicate the same
fixture across providers and to match results back to picks)."""
from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

_STOP = {"fc", "cf", "sc", "afc", "club", "de", "the", "cd", "ac", "as", "us", "ss", "bk", "if", "sk",
         "fk", "cs", "rc", "sv", "vfb", "vfl", "tsg", "1", "sporting", "athletic", "atletico", "united",
         "city", "real"}


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
    if ta and tb and (ta <= tb or tb <= ta):
        return 0.92
    return SequenceMatcher(None, na, nb).ratio()


def same_team(a: str, b: str, threshold: float = 0.82) -> bool:
    return similarity(a, b) >= threshold
