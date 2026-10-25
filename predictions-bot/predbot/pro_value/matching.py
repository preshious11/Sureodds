"""Match SportyBet events to OddsAPI events: normalised team names plus kickoff tolerance. Unique matches only."""
from __future__ import annotations

import difflib
import re
import unicodedata
from typing import Dict, List, Sequence

STOP = {"fc", "cf", "afc", "sc", "ac", "as", "club", "cd", "ud", "sd", "de", "the", "fk", "if", "bk", "sk", "il",
        "sv", "ss", "ssc", "us", "vfb", "rb", "bv", "ca", "cs", "og", "fa", "ks", "kv"}


def norm_team(name: str) -> str:
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    return " ".join(t for t in re.findall(r"[a-z0-9]+", s) if t not in STOP)


def same_team(a: str, b: str) -> bool:
    na, nb = norm_team(a), norm_team(b)
    if not na or not nb:
        return False
    if na == nb or na in nb or nb in na:
        return True
    return difflib.SequenceMatcher(None, na, nb).ratio() >= 0.8


def match_events(odds_events: Sequence[Dict], sporty_events: Sequence[Dict], window_min: int = 60) -> List[Dict]:
    """Pair each OddsAPI event with the one SportyBet event that has the same two teams and a kickoff within
    ``window_min`` minutes. An event with zero or several candidates is NOT matched (ambiguity is never guessed).

    odds_events: {id, home_team, away_team, commence_ts} (commence_ts in epoch seconds)
    sporty_events: {event_id, home_team, away_team, kickoff_ts} (epoch seconds)
    """
    out = []
    for oe in odds_events:
        cands = [s for s in sporty_events
                 if abs(s["kickoff_ts"] - oe["commence_ts"]) <= window_min * 60
                 and same_team(s["home_team"], oe["home_team"]) and same_team(s["away_team"], oe["away_team"])]
        if len(cands) == 1:
            out.append({"odds": oe, "sporty": cands[0]})
    return out


def match_key(home: str, away: str, kickoff_ts: int) -> str:
    return f"{norm_team(home)}|{norm_team(away)}|{int(kickoff_ts)}"
