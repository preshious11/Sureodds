"""Turn the API-Football /predictions payload into model inputs:

  * p_apif  - their 1X2 percentages (a bookmaker-independent signal)
  * lambdas - expected goals from season goal rates (home at home / away away),
              blended with last-5 form
  * strength - comparison block (form/att/def/h2h) used to nudge the split
"""
from __future__ import annotations

from typing import Dict, Optional, Tuple


def _pct(v) -> Optional[float]:
    try:
        return float(str(v).replace("%", "").strip()) / 100.0
    except (TypeError, ValueError):
        return None


def _num(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def apif_result_probs(pred: Dict) -> Optional[Tuple[float, float, float]]:
    pc = (pred.get("predictions") or {}).get("percent") or {}
    h, d, a = _pct(pc.get("home")), _pct(pc.get("draw")), _pct(pc.get("away"))
    if h is None or d is None or a is None:
        return None
    tot = h + d + a
    if tot <= 0:
        return None
    return h / tot, d / tot, a / tot


def apif_lambdas(pred: Dict) -> Optional[Tuple[float, float, float]]:
    """(lam_home, lam_away, confidence 0-1) from season averages and last-5."""
    teams = pred.get("teams") or {}
    th, ta = teams.get("home") or {}, teams.get("away") or {}

    def avg(team: Dict, direction: str, venue: str) -> Optional[float]:
        try:
            return _num(team["league"]["goals"][direction]["average"][venue])
        except (KeyError, TypeError):
            return None

    def played(team: Dict) -> int:
        try:
            return int(team["league"]["fixtures"]["played"]["total"] or 0)
        except (KeyError, TypeError, ValueError):
            return 0

    hf, ha = avg(th, "for", "home"), avg(th, "against", "home")
    af, aa = avg(ta, "for", "away"), avg(ta, "against", "away")
    if None in (hf, ha, af, aa):
        return None
    lam_h = 0.5 * (hf + aa)
    lam_a = 0.5 * (af + ha)

    # last-5 form nudge (goals for average)
    def last5(team: Dict, direction: str) -> Optional[float]:
        try:
            return _num(team["last_5"]["goals"][direction]["average"])
        except (KeyError, TypeError):
            return None

    l5h, l5a = last5(th, "for"), last5(ta, "for")
    if l5h is not None:
        lam_h = 0.8 * lam_h + 0.2 * l5h
    if l5a is not None:
        lam_a = 0.8 * lam_a + 0.2 * l5a
    games = min(played(th), played(ta))
    conf = max(0.0, min(1.0, games / 12.0))
    return max(0.2, lam_h), max(0.2, lam_a), conf


def apif_summary(pred: Dict) -> Dict:
    p = pred.get("predictions") or {}
    cmp_ = pred.get("comparison") or {}
    return {
        "advice": p.get("advice"),
        "winner": (p.get("winner") or {}).get("name"),
        "under_over": p.get("under_over"),
        "form": cmp_.get("form"), "att": cmp_.get("att"), "def": cmp_.get("def"),
        "h2h": cmp_.get("h2h"), "total": cmp_.get("total"),
    }
