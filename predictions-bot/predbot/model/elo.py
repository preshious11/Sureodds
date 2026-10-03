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


def expected_home_from(sport: str, rating_home: float, rating_away: float, home_adv: Optional[float] = None) -> float:
    """P(home beats away, draws split) from two ratings - the pure formula every Elo consumer shares.
    ``home_adv`` overrides the sport default (backtests only)."""
    diff = rating_home + (HOME_ADV.get(sport, 50.0) if home_adv is None else home_adv) - rating_away
    return 1.0 / (1.0 + 10 ** (-diff / 400.0))


def probs_from(sport: str, rating_home: float, rating_away: float, home_adv: Optional[float] = None) -> Tuple[float, float, float]:
    """(p_home, p_draw, p_away) from two ratings; two-outcome sports get p_draw = 0."""
    e = expected_home_from(sport, rating_home, rating_away, home_adv)
    diff = rating_home + (HOME_ADV.get(sport, 50.0) if home_adv is None else home_adv) - rating_away
    base_draw = BASE_DRAW.get(sport, 0.0)
    if base_draw:
        p_draw = max(0.10, base_draw - 0.00030 * abs(diff))
        return (1 - p_draw) * e, p_draw, (1 - p_draw) * (1 - e)
    return e, 0.0, 1 - e


def new_ratings(sport: str, rating_home: float, rating_away: float, home_score: float, away_score: float,
                k_factor: Optional[float] = None, home_adv: Optional[float] = None):
    """The two ratings after one result (margin-of-victory scaled K), as pure arithmetic."""
    e = expected_home_from(sport, rating_home, rating_away, home_adv)
    diff = rating_home + (HOME_ADV.get(sport, 50.0) if home_adv is None else home_adv) - rating_away
    s = 1.0 if home_score > away_score else 0.0 if away_score > home_score else 0.5
    margin = abs(home_score - away_score)
    mov = math.log(margin + 1) * (2.2 / ((diff * (1 if s > 0.5 else -1)) * 0.001 + 2.2)) if margin > 0 else 1.0
    k = (K_FACTOR.get(sport, 20.0) if k_factor is None else k_factor) * max(0.8, min(2.5, mov))
    delta = k * (s - e)
    return rating_home + delta, rating_away - delta


class Elo:
    """Persistent Elo ratings. Keyed by CANONICAL PARTICIPANT ID whenever one is supplied, so a club has one rating
    however each provider spells it. Callers that only have a display name fall back to the normalised-name key
    (legacy); the first time an id is used for a team that has a legacy name-keyed rating, that rating is copied
    to the id (idempotent, never overwrites an existing id rating)."""

    def __init__(self, db: Database):
        self.db = db

    @staticmethod
    def team_key(name: str) -> str:
        return normalize(name)

    def key(self, sport: str, name: str, participant_id: str = "") -> str:
        if not participant_id:
            return self.team_key(name)
        r, _ = self.db.get_rating(sport, participant_id)
        if r is None:
            legacy, games = self.db.get_rating(sport, self.team_key(name))
            if legacy is not None:
                self.db.set_rating(sport, participant_id, legacy, games)       # one-time migration of the legacy rating
        return participant_id

    def get(self, sport: str, team: str, participant_id: str = "") -> Tuple[float, int]:
        r, g = self.db.get_rating(sport, self.key(sport, team, participant_id))
        return (r if r is not None else 1500.0), g

    def expected_home(self, sport: str, home: str, away: str, home_id: str = "", away_id: str = "") -> Tuple[float, int]:
        """Return (P(home beats away, draws split), min games rated)."""
        rh, gh = self.get(sport, home, home_id)
        ra, ga = self.get(sport, away, away_id)
        return expected_home_from(sport, rh, ra), min(gh, ga)

    def probs(self, sport: str, home: str, away: str, home_id: str = "", away_id: str = "") -> Tuple[Optional[Tuple[float, float, float]], int]:
        """(p_home, p_draw, p_away), confidence games. None when no data."""
        rh, gh = self.get(sport, home, home_id)
        ra, ga = self.get(sport, away, away_id)
        games = min(gh, ga)
        if games == 0:
            return None, 0
        return probs_from(sport, rh, ra), games

    def update(self, sport: str, home: str, away: str, home_score: float, away_score: float,
               home_id: str = "", away_id: str = "") -> None:
        kh, ka = self.key(sport, home, home_id), self.key(sport, away, away_id)
        rh, gh = self.db.get_rating(sport, kh)
        ra, ga = self.db.get_rating(sport, ka)
        rh, ra = (rh if rh is not None else 1500.0), (ra if ra is not None else 1500.0)
        nh, na = new_ratings(sport, rh, ra, home_score, away_score)
        self.db.set_rating(sport, kh, nh, gh + 1)
        self.db.set_rating(sport, ka, na, ga + 1)
