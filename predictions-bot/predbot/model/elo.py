"""Elo ratings learned from every final result the bot observes.

Cold start: every team is 1500 with 0 games; the ensemble gives Elo weight
proportional to how many games both teams have been rated on, so it only
starts to matter once the bot has settled a few weeks of fixtures.
"""
from __future__ import annotations

import math
from typing import Optional, Tuple

from predbot.storage.db import Database
from predbot.util.names import normalize

K_FACTOR = {"football": 20.0, "basketball": 20.0, "american_football": 22.0,
            "baseball": 8.0, "tennis": 28.0, "mma": 30.0}
HOME_ADV = {"football": 65.0, "basketball": 80.0, "american_football": 60.0,
            "baseball": 30.0, "tennis": 0.0, "mma": 0.0}
BASE_DRAW = {"football": 0.26}


class Elo:
    def __init__(self, db: Database):
        self.db = db

    @staticmethod
    def team_key(name: str) -> str:
        return normalize(name)

    def get(self, sport: str, team: str) -> Tuple[float, int]:
        r, g = self.db.get_rating(sport, self.team_key(team))
        return (r if r is not None else 1500.0), g

    def expected_home(self, sport: str, home: str, away: str) -> Tuple[float, int]:
        """Return (P(home beats away, draws split), min games rated)."""
        rh, gh = self.get(sport, home)
        ra, ga = self.get(sport, away)
        diff = rh + HOME_ADV.get(sport, 50.0) - ra
        e = 1.0 / (1.0 + 10 ** (-diff / 400.0))
        return e, min(gh, ga)

    def probs(self, sport: str, home: str, away: str) -> Tuple[Optional[Tuple[float, float, float]], int]:
        """(p_home, p_draw, p_away), confidence games. None when no data."""
        e, games = self.expected_home(sport, home, away)
        if games == 0:
            return None, 0
        rh, _ = self.get(sport, home)
        ra, _ = self.get(sport, away)
        diff = rh + HOME_ADV.get(sport, 50.0) - ra
        base_draw = BASE_DRAW.get(sport, 0.0)
        if base_draw:
            p_draw = max(0.10, base_draw - 0.00030 * abs(diff))
            p_home = (1 - p_draw) * e
            p_away = (1 - p_draw) * (1 - e)
            return (p_home, p_draw, p_away), games
        return (e, 0.0, 1 - e), games

    def update(self, sport: str, home: str, away: str, home_score: float, away_score: float) -> None:
        rh, gh = self.get(sport, home)
        ra, ga = self.get(sport, away)
        e, _ = self.expected_home(sport, home, away)
        diff = rh + HOME_ADV.get(sport, 50.0) - ra
        s = 1.0 if home_score > away_score else 0.0 if away_score > home_score else 0.5
        margin = abs(home_score - away_score)
        mov = math.log(margin + 1) * (2.2 / ((diff * (1 if s > 0.5 else -1)) * 0.001 + 2.2)) if margin > 0 else 1.0
        k = K_FACTOR.get(sport, 20.0) * max(0.8, min(2.5, mov))
        delta = k * (s - e)
        self.db.set_rating(sport, self.team_key(home), rh + delta, gh + 1)
        self.db.set_rating(sport, self.team_key(away), ra - delta, ga + 1)
