"""Independent football strength model: team ratings fitted from historical RESULTS only.

Nothing here reads a bookmaker price. The fit is a time-weighted Poisson attack/defence model (the Maher /
Dixon-Coles family), solved by iterative scaling in pure Python:

    E[home goals] = mu_home * attack[home] * defence[away]
    E[away goals] = mu_away * attack[away] * defence[home]

* ``attack[i]``  > 1: team i scores more than an average team; ``defence[j]`` > 1: team j concedes more.
* ``mu_home`` / ``mu_away``: league baseline goals for the home and the away side - their ratio IS the home effect.
* Opponent strength is built in: a goal against a weak defence raises the scorer's attack less than one against a
  strong defence (each team's update divides by the exposure its opponents implied).
* Recency: a match ``t`` days old weighs ``0.5 ** (t / half_life_days)``.
* Small samples: every attack and defence has a Gamma(k, k) prior of mean 1, so the update is
  ``(k + weighted goals) / (k + weighted exposure)`` - a team with few matches stays near average instead of
  inheriting an extreme rating from noise. ``k`` (``prior_strength``) is a fitting hyper-parameter chosen by backtest.

``fit_strength`` refuses any training match that kicked off at or after ``as_of`` (``LeakageError``): a prediction can
only ever use results that were already final. The expected goals feed ``ScoreGrid`` / ``price_line`` unchanged, so every
supported market comes from ONE score distribution.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Tuple

SECONDS_PER_DAY = 86400.0


class LeakageError(ValueError):
    """A training match is not strictly before the prediction instant."""


@dataclass(frozen=True)
class StrengthParams:
    half_life_days: float = 365.0      # provisional: choose with tools/backtest_football.py
    prior_strength: float = 8.0        # provisional: choose with tools/backtest_football.py
    iterations: int = 60
    tolerance: float = 1e-7
    min_effective_matches: float = 5.0  # weighted matches a team needs before its rating is trusted


@dataclass
class FittedStrength:
    competition_id: str
    as_of: datetime
    attack: Dict[str, float]
    defence: Dict[str, float]
    mu_home: float
    mu_away: float
    effective_matches: Dict[str, float]
    matches: int
    params: StrengthParams
    iterations: int = 0

    def status(self, home_id: str, away_id: str) -> str:
        """'ok' or why no independent estimate exists for this pairing."""
        for team in (home_id, away_id):
            if team not in self.attack:
                return "unknown_team"
            if self.effective_matches.get(team, 0.0) < self.params.min_effective_matches:
                return "insufficient_history"
        return "ok"

    def lambdas(self, home_id: str, away_id: str) -> Optional[Tuple[float, float]]:
        if self.status(home_id, away_id) != "ok":
            return None
        lh = self.mu_home * self.attack[home_id] * self.defence[away_id]
        la = self.mu_away * self.attack[away_id] * self.defence[home_id]
        return max(0.05, lh), max(0.05, la)


def fit_strength(matches: Iterable, as_of: datetime, params: StrengthParams = StrengthParams(),
                 competition_id: str = "") -> Optional[FittedStrength]:
    """Fit ratings from ``matches`` (objects with kickoff, home_id, away_id, home_goals, away_goals).

    ``None`` when there is nothing to fit. Raises ``LeakageError`` if any match is not strictly before ``as_of``."""
    rows: List[Tuple[str, str, int, int, float]] = []
    for m in matches:
        if m.kickoff >= as_of:
            raise LeakageError(f"training match at {m.kickoff.isoformat()} is not before {as_of.isoformat()}")
        age_days = (as_of - m.kickoff).total_seconds() / SECONDS_PER_DAY
        rows.append((m.home_id, m.away_id, m.home_goals, m.away_goals, 0.5 ** (age_days / params.half_life_days)))
    if not rows:
        return None
    teams = sorted({r[0] for r in rows} | {r[1] for r in rows})
    wsum = sum(r[4] for r in rows)
    attack = {t: 1.0 for t in teams}
    defence = {t: 1.0 for t in teams}
    mu_h = max(0.1, sum(r[4] * r[2] for r in rows) / wsum)
    mu_a = max(0.1, sum(r[4] * r[3] for r in rows) / wsum)
    k = params.prior_strength
    effective: Dict[str, float] = {t: 0.0 for t in teams}
    for h, a, _, _, w in rows:
        effective[h] += w
        effective[a] += w
    it = 0
    for it in range(1, params.iterations + 1):
        gf = {t: 0.0 for t in teams}; ex_att = {t: 0.0 for t in teams}
        ga = {t: 0.0 for t in teams}; ex_def = {t: 0.0 for t in teams}
        for h, a, hg, ag, w in rows:
            gf[h] += w * hg; ex_att[h] += w * mu_h * defence[a]        # home side scores against the away defence
            gf[a] += w * ag; ex_att[a] += w * mu_a * defence[h]
            ga[a] += w * hg; ex_def[a] += w * mu_h * attack[h]         # away defence concedes to the home attack
            ga[h] += w * ag; ex_def[h] += w * mu_a * attack[a]
        new_attack = {t: (k + gf[t]) / (k + ex_att[t]) for t in teams}
        new_defence = {t: (k + ga[t]) / (k + ex_def[t]) for t in teams}
        # keep the scale identifiable: mean attack = mean defence = 1, the baselines absorb the scale
        ma = sum(new_attack.values()) / len(teams)
        md = sum(new_defence.values()) / len(teams)
        new_attack = {t: v / ma for t, v in new_attack.items()}
        new_defence = {t: v / md for t, v in new_defence.items()}
        num_h = sum(w * hg for _, _, hg, _, w in rows)
        den_h = sum(w * new_attack[h] * new_defence[a] for h, a, _, _, w in rows)
        num_a = sum(w * ag for _, _, _, ag, w in rows)
        den_a = sum(w * new_attack[a] * new_defence[h] for h, a, _, _, w in rows)
        new_mu_h, new_mu_a = max(0.1, num_h / den_h), max(0.1, num_a / den_a)
        change = max(max(abs(new_attack[t] - attack[t]) for t in teams), max(abs(new_defence[t] - defence[t]) for t in teams),
                     abs(new_mu_h - mu_h), abs(new_mu_a - mu_a))
        attack, defence, mu_h, mu_a = new_attack, new_defence, new_mu_h, new_mu_a
        if change < params.tolerance:
            break
    return FittedStrength(competition_id=competition_id, as_of=as_of, attack=attack, defence=defence, mu_home=mu_h,
                          mu_away=mu_a, effective_matches=effective, matches=len(rows), params=params, iterations=it)
