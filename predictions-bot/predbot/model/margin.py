"""Gaussian margin & total model for high-scoring sports.

  margin = home - away ~ N(mu, sigma_m)
  total  = home + away ~ N(mu_t, sigma_t)
  home   = (total + margin)/2, away = (total - margin)/2 (used for team totals)

mu / mu_t are solved from the main spread and total lines *and their prices*
(e.g. a -3.5 spread priced -120 on the favourite implies mu > 3.5).
"""
from __future__ import annotations

import math
from typing import Optional, Tuple

from predbot.providers.base import MarketLine

SIGMA_MARGIN = {"basketball": 12.0, "american_football": 13.5, "tennis": 0.0}
SIGMA_TOTAL = {"basketball": 18.0, "american_football": 14.0}
PRIOR_TOTAL = {"basketball": 225.0, "american_football": 45.0}
PERIOD_SCALE = {"full": 1.0, "reg": 1.0, "1h": 0.5, "2h": 0.5, "1q": 0.25, "2q": 0.25, "3q": 0.25, "4q": 0.25}


def _phi(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _inv_phi(p: float) -> float:
    # Acklam's rational approximation, good to ~1e-9
    p = min(max(p, 1e-9), 1 - 1e-9)
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02, 1.383577518672690e+02,
         -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02, 6.680131188771972e+01,
         -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00, -2.549732539343734e+00,
         4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00, 3.754408661907416e+00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)


class MarginModel:
    def __init__(self, sport: str, mu: float, mu_total: float):
        self.sport = sport
        self.mu = mu
        self.mu_t = mu_total
        self.sm = SIGMA_MARGIN.get(sport, 12.0)
        self.st = SIGMA_TOTAL.get(sport, 18.0)

    @classmethod
    def from_market(cls, sport: str, p_home_ml: Optional[float], spread_home: Optional[float],
                    p_cover_home: Optional[float], total_line: Optional[float], p_over: Optional[float]):
        sm = SIGMA_MARGIN.get(sport, 12.0)
        st = SIGMA_TOTAL.get(sport, 18.0)
        mu: Optional[float] = None
        if spread_home is not None:
            # P(margin + spread > 0) = p_cover -> mu = -spread + sm * z
            z = _inv_phi(p_cover_home) if p_cover_home else 0.0
            mu = -spread_home + sm * z
        elif p_home_ml is not None:
            mu = sm * _inv_phi(p_home_ml)
        if mu is None:
            mu = 0.0
        if total_line is not None:
            z = _inv_phi(p_over) if p_over else 0.0
            mu_t = total_line + st * z
        else:
            mu_t = PRIOR_TOTAL.get(sport, 200.0)
        return cls(sport, mu, mu_t)

    # continuous scores: a "push" happens only on integer lines; we use a
    # continuity correction of +-0.5 to approximate discrete pushes.
    def _line_prob(self, mean: float, sigma: float, line: float, side: str) -> float:
        frac = round(abs(line - math.floor(line)), 2)
        if frac in (0.25, 0.75):
            return 0.5 * (self._line_prob(mean, sigma, line - 0.25, side) + self._line_prob(mean, sigma, line + 0.25, side))
        if frac == 0.0:
            # integer line: P(win) + 0.5 P(push)
            over = 1 - _phi((line + 0.5 - mean) / sigma)
            push = _phi((line + 0.5 - mean) / sigma) - _phi((line - 0.5 - mean) / sigma)
            p = over + 0.5 * push
        else:
            p = 1 - _phi((line - mean) / sigma)
        return p if side == "over" else 1 - p

    def prob(self, ml: MarketLine) -> Optional[float]:
        if ml.stat != "points" or ml.is_player_prop:
            return None
        s = PERIOD_SCALE.get(ml.period, 1.0)
        mu, mu_t = self.mu * s, self.mu_t * s
        sm, st = self.sm * math.sqrt(s), self.st * math.sqrt(s)
        m, sel = ml.market, ml.selection
        if m in ("result2", "dnb", "result3"):
            ph = 1 - _phi((0 - mu) / sm)
            if m == "result3":
                # a genuine tie is rare in these sports; approximate with +-0.5
                tie = _phi((0.5 - mu) / sm) - _phi((-0.5 - mu) / sm)
                return {"home": ph - tie / 2, "away": 1 - ph - tie / 2, "draw": tie}.get(sel)
            return ph if sel == "home" else 1 - ph
        if m == "handicap" and ml.line is not None:
            mean = mu if sel == "home" else -mu
            return self._line_prob(mean, sm, -ml.line, "over")
        if m == "total" and ml.line is not None:
            if ml.entity == "all":
                return self._line_prob(mu_t, st, ml.line, sel)
            sign = 1 if ml.entity == "home" else -1
            mean = (mu_t + sign * mu) / 2
            sigma = math.sqrt(st * st + sm * sm) / 2
            return self._line_prob(mean, sigma, ml.line, sel)
        if m == "odd_even":
            return 0.5
        if m == "winning_margin" and "_" in sel:
            t, band = sel.split("_", 1)
            sign = 1 if t == "home" else -1
            if band.endswith("+"):
                lo, hi = float(band[:-1]), 1e9
            elif "-" in band:
                lo, hi = [float(x) for x in band.split("-")]
            else:
                lo = hi = float(band)
            a = _phi((sign * (lo - 0.5) - mu) / sm) if sign > 0 else 1 - _phi((-(lo - 0.5) - mu) / sm)
            b = _phi((sign * (hi + 0.5) - mu) / sm) if sign > 0 else 1 - _phi((-(hi + 0.5) - mu) / sm)
            return abs(b - a)
        return None
