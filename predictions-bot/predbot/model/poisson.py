"""Score-grid model for low-scoring sports (football, hockey, baseball, handball).

A Dixon-Coles adjusted independent Poisson grid P(home=i, away=j). Expected
goals (lambda) come from two places and are blended by the ensemble:
  * the market  - solved so the grid reproduces the de-vigged 1X2 and main
                  Over/Under line (see `lambdas_from_market`)

From one grid we price *every* score-settled market consistently: 1X2, DNB,
DC, totals on any line (incl. Asian quarter lines), team totals, BTTS, Asian
handicaps, correct score, odd/even, HT/FT, result & total combos, clean sheet,
win-to-nil, winning margin and first-half variants (scaled lambdas).
"""
from __future__ import annotations

import math
from functools import lru_cache
from typing import Dict, List, Optional, Tuple

from predbot.providers.base import MarketLine

MAX_GOALS = {"football": 10, "hockey": 12, "handball": 60, "baseball": 20}
DEFAULT_RHO = {"football": -0.10, "hockey": -0.05, "baseball": 0.0, "handball": 0.0}
PERIOD_SCALE = {"full": 1.0, "reg": 1.0, "1h": 0.44, "2h": 0.56, "1p": 0.33, "2p": 0.33, "3p": 0.34, "1i5": 0.55}


@lru_cache(maxsize=4096)
def _pois(k: int, lam: float) -> float:
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return math.exp(-lam + k * math.log(lam) - math.lgamma(k + 1))


def _tau(i: int, j: int, lh: float, la: float, rho: float) -> float:
    """Dixon-Coles low-score correction."""
    if i == 0 and j == 0:
        return 1 - lh * la * rho
    if i == 0 and j == 1:
        return 1 + lh * rho
    if i == 1 and j == 0:
        return 1 + la * rho
    if i == 1 and j == 1:
        return 1 - rho
    return 1.0


class ScoreGrid:
    def __init__(self, lam_home: float, lam_away: float, rho: float = -0.1, max_goals: int = 10):
        self.lh = max(0.05, lam_home)
        self.la = max(0.05, lam_away)
        self.rho = rho
        self.n = max_goals
        grid: List[List[float]] = []
        tot = 0.0
        for i in range(self.n + 1):
            row = []
            for j in range(self.n + 1):
                p = _pois(i, self.lh) * _pois(j, self.la) * _tau(i, j, self.lh, self.la, rho)
                p = max(p, 0.0)
                row.append(p)
                tot += p
            grid.append(row)
        self.grid = [[p / tot for p in row] for row in grid]
        self._result: Optional[Tuple[float, float, float]] = None
        self._total_dist: Dict[str, Dict[int, float]] = {}

    # -- basic outcome probabilities -------------------------------------
    def p(self, i: int, j: int) -> float:
        if 0 <= i <= self.n and 0 <= j <= self.n:
            return self.grid[i][j]
        return 0.0

    def result(self) -> Tuple[float, float, float]:
        # the grid is immutable: compute 1X2 once, reuse for every market
        if self._result is None:
            h = d = a = 0.0
            for i in range(self.n + 1):
                for j in range(self.n + 1):
                    v = self.grid[i][j]
                    if i > j:
                        h += v
                    elif i == j:
                        d += v
                    else:
                        a += v
            self._result = (h, d, a)
        return self._result

    def total_dist(self, entity: str = "all") -> Dict[int, float]:
        cached = self._total_dist.get(entity)
        if cached is not None:
            return cached
        out: Dict[int, float] = {}
        for i in range(self.n + 1):
            for j in range(self.n + 1):
                k = i + j if entity == "all" else i if entity == "home" else j
                out[k] = out.get(k, 0.0) + self.grid[i][j]
        self._total_dist[entity] = out
        return out

    def margin_dist(self) -> Dict[int, float]:
        out: Dict[int, float] = {}
        for i in range(self.n + 1):
            for j in range(self.n + 1):
                out[i - j] = out.get(i - j, 0.0) + self.grid[i][j]
        return out

    # -- line markets: returns (p_win, p_push) ----------------------------
    def over_under(self, line: float, side: str, entity: str = "all") -> Tuple[float, float]:
        dist = self.total_dist(entity)
        return _line_outcome(dist, line, side)

    def handicap(self, line: float, selection: str) -> Tuple[float, float]:
        """P(selected team + line beats opponent). Asian semantics."""
        margin = self.margin_dist()
        if selection == "away":
            margin = {-k: v for k, v in margin.items()}
        # selected margin + line > 0 wins
        return _line_outcome(margin, -line, "over")

    def btts(self) -> float:
        return sum(self.grid[i][j] for i in range(1, self.n + 1) for j in range(1, self.n + 1))

    def odd_even(self, entity: str = "all") -> Tuple[float, float]:
        dist = self.total_dist(entity)
        even = sum(v for k, v in dist.items() if k % 2 == 0)
        return even, 1 - even

    def clean_sheet(self, entity: str) -> float:
        return sum(self.grid[i][0] for i in range(self.n + 1)) if entity == "home" else sum(self.grid[0][j] for j in range(self.n + 1))

    def win_to_nil(self, entity: str) -> float:
        if entity == "home":
            return sum(self.grid[i][0] for i in range(1, self.n + 1))
        return sum(self.grid[0][j] for j in range(1, self.n + 1))

    def team_scores(self, entity: str) -> float:
        return 1.0 - self.clean_sheet("away" if entity == "home" else "home")

    def result_total(self, team: str, side: str, line: float) -> float:
        p = 0.0
        for i in range(self.n + 1):
            for j in range(self.n + 1):
                r = "home" if i > j else "away" if j > i else "draw"
                if r != team:
                    continue
                t = i + j
                if (side == "over" and t > line) or (side == "under" and t < line):
                    p += self.grid[i][j]
        return p

    def result_btts(self, team: str, yes: bool) -> float:
        p = 0.0
        for i in range(self.n + 1):
            for j in range(self.n + 1):
                r = "home" if i > j else "away" if j > i else "draw"
                if r == team and ((i > 0 and j > 0) == yes):
                    p += self.grid[i][j]
        return p

    def winning_margin(self, team: str, band: str) -> float:
        md = self.margin_dist()
        if team == "draw":
            return md.get(0, 0.0)
        sign = 1 if team == "home" else -1
        p = 0.0
        for k, v in md.items():
            m = sign * k
            if m <= 0:
                continue
            if band.endswith("+") and m >= float(band[:-1]):
                p += v
            elif "-" in band:
                lo, hi = band.split("-")
                if float(lo) <= m <= float(hi):
                    p += v
            elif band.replace(".", "").isdigit() and m == float(band):
                p += v
        return p


def _line_outcome(dist: Dict[int, float], line: float, side: str) -> Tuple[float, float]:
    """Return (p_win, p_push) for over/under `line` on an integer distribution,
    handling Asian quarter lines (x.25 / x.75) as a split stake."""
    frac = round(abs(line - math.floor(line)), 2)
    if frac in (0.25, 0.75):
        w1, p1 = _line_outcome(dist, line - 0.25, side)
        w2, p2 = _line_outcome(dist, line + 0.25, side)
        return 0.5 * (w1 + w2), 0.5 * (p1 + p2)
    win = push = 0.0
    for k, v in dist.items():
        if k == line:
            push += v
        elif (side == "over" and k > line) or (side == "under" and k < line):
            win += v
    return win, push


def effective_prob(p_win: float, p_push: float) -> float:
    """Probability used for EV/ranking: a push returns the stake, so treat it
    as half a win in a 'does this selection pay out' sense."""
    return p_win + 0.5 * p_push


# --------------------------------------------------------------------------
def lambdas_from_market(p_home: Optional[float], p_draw: Optional[float], p_away: Optional[float],
                        total_line: Optional[float], p_over: Optional[float], sport: str = "football",
                        rho: Optional[float] = None) -> Optional[Tuple[float, float]]:
    """Solve (lam_home, lam_away) so the grid reproduces the market.

    total from the O/U line, split from the result market. Any missing input
    falls back to sport priors."""
    rho = DEFAULT_RHO.get(sport, 0.0) if rho is None else rho
    n = MAX_GOALS.get(sport, 10)
    prior_total = {"football": 2.6, "hockey": 6.0, "baseball": 8.5, "handball": 55.0}.get(sport, 2.6)

    # 1) total lambda
    if total_line is not None and p_over is not None and 0.02 < p_over < 0.98:
        lo, hi = 0.2, prior_total * 3
        for _ in range(40):
            mid = (lo + hi) / 2
            g = ScoreGrid(mid / 2, mid / 2, rho, n)
            pw, pp = g.over_under(total_line, "over")
            if effective_prob(pw, pp) < p_over:
                lo = mid
            else:
                hi = mid
        lam_total = (lo + hi) / 2
    else:
        lam_total = prior_total

    # 2) split by result probabilities (p_home - p_away is monotonic in the share)
    if p_home is None or p_away is None:
        return lam_total * 0.55, lam_total * 0.45
    target = p_home - p_away
    lo, hi = 0.1, 0.9
    for _ in range(40):
        r = (lo + hi) / 2
        g = ScoreGrid(lam_total * r, lam_total * (1 - r), rho, n)
        h, d, a = g.result()
        if (h - a) < target:
            lo = r
        else:
            hi = r
    r = (lo + hi) / 2
    return lam_total * r, lam_total * (1 - r)


@lru_cache(maxsize=256)
def grid_for_period(lh: float, la: float, period: str, sport: str) -> ScoreGrid:
    """Memoised: the same (lambdas, period, sport) always yields the same grid,
    so an event's markets share one grid instead of rebuilding it per market."""
    s = PERIOD_SCALE.get(period, 1.0)
    return ScoreGrid(lh * s, la * s, DEFAULT_RHO.get(sport, 0.0), MAX_GOALS.get(sport, 10))


def price_line(ml: MarketLine, lh: float, la: float, sport: str) -> Optional[float]:
    """Model probability for any score-settled MarketLine, or None if the
    market is not expressible on the goal grid (e.g. corners without a model)."""
    if ml.stat != "points" or ml.is_player_prop:
        return None
    g = grid_for_period(lh, la, ml.period, sport)
    m, s = ml.market, ml.selection
    if m == "result3":
        h, d, a = g.result()
        return {"home": h, "draw": d, "away": a}.get(s)
    if m in ("result2", "dnb"):
        h, d, a = g.result()
        if sport in ("football", "hockey", "handball") and ml.period in ("full", "reg", "1h", "2h") and m == "dnb":
            return {"home": h / (h + a), "away": a / (h + a)}.get(s)
        if sport in ("hockey", "baseball") and ml.period == "full":
            # overtime / extra innings: draw resolved ~ by strength
            share = h / (h + a)
            return {"home": h + d * share, "away": a + d * (1 - share)}.get(s)
        return {"home": h / (h + a), "away": a / (h + a)}.get(s)
    if m == "dc":
        h, d, a = g.result()
        return {"home_draw": h + d, "away_draw": a + d, "home_away": h + a}.get(s)
    if m == "btts":
        p = g.btts()
        return p if s == "yes" else 1 - p
    if m == "total" and ml.line is not None:
        pw, pp = g.over_under(ml.line, s, ml.entity)
        return effective_prob(pw, pp)
    if m == "handicap" and ml.line is not None and s in ("home", "away"):
        pw, pp = g.handicap(ml.line, s)
        return effective_prob(pw, pp)
    if m == "odd_even":
        even, odd = g.odd_even(ml.entity)
        return even if s == "even" else odd
    if m == "correct_score":
        try:
            i, j = [int(x) for x in s.split("-")]
        except ValueError:
            return None
        return g.p(i, j)
    if m == "ht_ft":
        ht, ft = (s.split("/") + [""])[:2]
        g1 = grid_for_period(lh, la, "1h", sport)
        g2 = grid_for_period(lh, la, "2h", sport)
        p = 0.0
        for i in range(g1.n + 1):
            for j in range(g1.n + 1):
                r1 = "home" if i > j else "away" if j > i else "draw"
                if r1 != ht:
                    continue
                p1 = g1.grid[i][j]
                if p1 < 1e-7:
                    continue
                # second half result must combine to the wanted FT result
                for k in range(g2.n + 1):
                    for l_ in range(g2.n + 1):
                        fh, fa = i + k, j + l_
                        r2 = "home" if fh > fa else "away" if fa > fh else "draw"
                        if r2 == ft:
                            p += p1 * g2.grid[k][l_]
        return p
    if m == "result_total" and ml.line is not None and "_" in s:
        t, ou = s.split("_", 1)
        return g.result_total(t, ou, ml.line)
    if m == "result_btts" and "_" in s:
        t, yn = s.split("_", 1)
        return g.result_btts(t, yn == "yes")
    if m == "clean_sheet":
        p = g.clean_sheet(ml.entity)
        return p if s == "yes" else 1 - p
    if m == "win_to_nil":
        p = g.win_to_nil(ml.entity)
        return p if s == "yes" else 1 - p
    if m == "team_to_score":
        p = g.team_scores(ml.entity)
        return p if s == "yes" else 1 - p
    if m == "winning_margin" and "_" in s:
        t, band = s.split("_", 1)
        return g.winning_margin(t, band)
    return None
