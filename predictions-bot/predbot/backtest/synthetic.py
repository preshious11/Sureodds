"""SYNTHETIC football leagues with known strengths - for validating the backtest machinery only.

Results drawn from a generator say nothing about real football: a score here can only show that the fitting code
recovers strengths, that the walk-forward is leak-free and that the scoring code behaves. Every report built from this
module must be labelled synthetic. The "market" is the TRUE probability perturbed by log-normal noise (a market that
knows the truth up to noise), the "oracle" is the exact generating probability.
"""
from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone
from typing import List, Tuple

from predbot.backtest.football import BTMatch, grid_probs


def _poisson(rng: random.Random, lam: float) -> int:
    limit, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


def _round_robin(teams: List[str]) -> List[List[Tuple[str, str]]]:
    n, rounds = len(teams), []
    ring = list(teams)
    for r in range(n - 1):
        rounds.append([(ring[i], ring[n - 1 - i]) for i in range(n // 2)])
        ring = [ring[0]] + [ring[-1]] + ring[1:-1]
    return rounds


def synthetic_league(seed: int = 1, teams: int = 20, seasons: int = 4, competition: str = "synthetic",
                     mu: float = 1.35, home_factor: float = 1.30, drift: float = 0.012, market_noise: float = 0.10):
    """(matches, oracle) where oracle[i] is the exact (home, draw, away) probability of matches[i]."""
    rng = random.Random(seed)
    names = [f"T{i:02d}" for i in range(teams)]
    attack = {t: rng.gauss(0, 0.28) for t in names}
    defence = {t: rng.gauss(0, 0.22) for t in names}
    day = datetime(2019, 8, 10, 15, 0, tzinfo=timezone.utc)
    matches, oracle = [], []
    for s in range(seasons):
        order = names[:]
        rng.shuffle(order)
        rounds = _round_robin(order)
        schedule = rounds + [[(a, h) for h, a in r] for r in rounds]
        for r in schedule:
            for h, a in r:
                lh = mu * home_factor * math.exp(attack[h] + defence[a])
                la = mu * math.exp(attack[a] + defence[h])
                true, _, _ = grid_probs(lh, la)
                noisy = [p * math.exp(rng.gauss(0, market_noise)) for p in true]
                z = sum(noisy)
                matches.append(BTMatch(day, competition, h, a, _poisson(rng, lh), _poisson(rng, la),
                                       tuple(x / z for x in noisy)))
                oracle.append(true)
            day += timedelta(days=7)
            for t in names:                                   # strengths drift slowly: recency should help
                attack[t] += rng.gauss(0, drift)
                defence[t] += rng.gauss(0, drift)
        day += timedelta(days=70)
    return matches, oracle
