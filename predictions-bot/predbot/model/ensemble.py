"""Per-event prediction model.

For each event we build:
  market      de-vigged consensus probability for every selection (always)
  score_model Poisson grid (football/hockey/baseball/handball) or Gaussian
              margin model (basketball/american football) fitted to the main
              lines and, for football, blended with API-Football goal rates
  elo         rating-based result probabilities (weight grows with data)
  apif        API-Football's own 1X2 percentages when present

Sources are blended in logit space, shrunk toward the market (we are humble by
default), then passed through the learned calibration.
"""
from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from predbot.markets.catalog import selection_label, spec
from predbot.model import features
from predbot.model.calibration import Calibrator
from predbot.model.devig import blend_logit, devig, single_side_prob
from predbot.model.elo import Elo
from predbot.model.margin import MarginModel
from predbot.model.poisson import lambdas_from_market, price_line
from predbot.providers.base import Event, MarketLine

log = logging.getLogger(__name__)

GRID_SPORTS = {"football", "hockey", "baseball", "handball"}
MARGIN_SPORTS = {"basketball", "american_football"}
SCORE_MODEL_WEIGHT = {"football": 0.60, "hockey": 0.45, "baseball": 0.35, "handball": 0.35,
                      "basketball": 0.50, "american_football": 0.45}


@dataclass
class Scored:
    event: Event
    line: MarketLine
    p_market: float
    p_model: float
    sources: Dict[str, float]
    agreement: float
    ev: float
    edge: float
    label: str
    liquidity: float
    rank_score: float = 0.0
    tier: str = ""
    notes: Dict = field(default_factory=dict)

    @property
    def price(self) -> float:
        return self.line.price


class EventModel:
    def __init__(self, event: Event, elo: Elo, calibrator: Calibrator, shrink: float = 0.8):
        self.ev = event
        self.elo = elo
        self.cal = calibrator
        self.shrink = shrink
        self.sport = event.sport
        self.p_market: Dict[Tuple, float] = {}
        self.market_complete: Dict[Tuple, bool] = {}
        self.grid_lambdas: Optional[Tuple[float, float]] = None
        self.margin_model: Optional[MarginModel] = None
        self.elo_probs: Optional[Tuple[float, float, float]] = None
        self.elo_games = 0
        self.apif_probs: Optional[Tuple[float, float, float]] = None
        self.notes: Dict = {}
        self._market_index: Dict[Tuple, List[MarketLine]] = {}
        self._build_market_index()
        self._build()

    # ---------------------------------------------------------------- build
    def _build_market_index(self) -> None:
        for ml in self.ev.markets:
            key = (ml.market, ml.period, ml.entity, ml.stat)
            self._market_index.setdefault(key, []).append(ml)

    def _build(self) -> None:
        self._market_probs()
        self._score_model()
        self.elo_probs, self.elo_games = self.elo.probs(self.sport, self.ev.home, self.ev.away)
        pred = self.ev.extra.get("prediction")
        if pred:
            self.apif_probs = features.apif_result_probs(pred)
            self.notes["apif"] = features.apif_summary(pred)

    @staticmethod
    def _mean_price(ml: MarketLine) -> float:
        if ml.book_prices:
            return sum(ml.book_prices) / len(ml.book_prices)
        return ml.price

    def _market_probs(self) -> None:
        groups: Dict[Tuple, List[MarketLine]] = {}
        for ml in self.ev.markets:
            groups.setdefault(ml.market_key(), []).append(ml)
        for mk, lines in groups.items():
            ways = spec(mk[0]).ways
            sels = {ml.selection for ml in lines}
            complete = False
            if ways == 3 and mk[0] == "result3":
                complete = sels >= {"home", "draw", "away"}
            elif ways == 2:
                complete = len(sels) == 2 and any(
                    {a, b} <= sels for a, b in (("home", "away"), ("over", "under"), ("yes", "no"), ("even", "odd")))
            elif mk[0] == "dc":
                complete = False  # derive from result3 instead
            if complete:
                probs = devig({ml.selection: self._mean_price(ml) for ml in lines})
                for ml in lines:
                    if ml.selection in probs:
                        self.p_market[ml.key()] = probs[ml.selection]
                        self.market_complete[ml.key()] = True
            for ml in lines:
                if ml.key() in self.p_market:
                    continue
                if ml.fair_price:
                    self.p_market[ml.key()] = 1.0 / ml.fair_price
                    self.market_complete[ml.key()] = True
                else:
                    self.p_market[ml.key()] = single_side_prob(self._mean_price(ml), ways if ways else 0)
                    self.market_complete[ml.key()] = False

    def _find(self, market: str, period: str = "full", entity: str = "all", stat: str = "points",
              selection: Optional[str] = None) -> List[MarketLine]:
        key = (market, period, entity, stat)
        results = self._market_index.get(key, [])
        if selection is None:
            return results
        return [m for m in results if m.selection == selection]

    def _main_line(self, market: str, entity: str = "all") -> Optional[Tuple[float, float]]:
        """Return (line, p_over_or_cover_for_home) for the most balanced line."""
        best = None
        for over in self._find(market, "full", entity, "points", "over" if market == "total" else "home"):
            if over.line is None:
                continue
            p = self.p_market.get(over.key())
            if p is None:
                continue
            bal = abs(p - 0.5)
            if best is None or bal < best[0]:
                best = (bal, over.line, p)
        return (best[1], best[2]) if best else None

    def _score_model(self) -> None:
        res = {m.selection: self.p_market[m.key()] for m in self._find("result3") if m.key() in self.p_market}
        two = {m.selection: self.p_market[m.key()] for m in self._find("result2") + self._find("dnb")
               if m.key() in self.p_market}
        tot = self._main_line("total")
        if self.sport in GRID_SPORTS:
            ph, pd, pa = res.get("home"), res.get("draw"), res.get("away")
            if ph is None and two:
                ph, pa = two.get("home"), two.get("away")
                pd = None
            if ph is None and tot is None:
                return
            lam = lambdas_from_market(ph, pd, pa, tot[0] if tot else None, tot[1] if tot else None, self.sport)
            if lam is None:
                return
            lh, la = lam
            pred = self.ev.extra.get("prediction")
            if pred and self.sport == "football":
                stats = features.apif_lambdas(pred)
                if stats:
                    sh, sa, conf = stats
                    w = 0.35 * conf
                    lh, la = (1 - w) * lh + w * sh, (1 - w) * la + w * sa
                    self.notes["lambda_stats"] = (round(sh, 2), round(sa, 2), round(conf, 2))
            self.grid_lambdas = (lh, la)
            self.notes["lambda"] = (round(lh, 2), round(la, 2))
        elif self.sport in MARGIN_SPORTS:
            spread = self._main_line("handicap")
            ph = two.get("home")
            if spread is None and ph is None and tot is None:
                return
            self.margin_model = MarginModel.from_market(
                self.sport, ph, spread[0] if spread else None, spread[1] if spread else None,
                tot[0] if tot else None, tot[1] if tot else None)
            self.notes["margin"] = (round(self.margin_model.mu, 1), round(self.margin_model.mu_t, 1))

    # -------------------------------------------------------------- pricing
    def _result_prob_from(self, probs: Tuple[float, float, float], ml: MarketLine) -> Optional[float]:
        h, d, a = probs
        m, s = ml.market, ml.selection
        if ml.period not in ("full", "reg") or ml.stat != "points":
            return None
        if m == "result3":
            return {"home": h, "draw": d, "away": a}.get(s)
        if m in ("result2", "dnb"):
            if h + a <= 0:
                return None
            return {"home": h / (h + a), "away": a / (h + a)}.get(s)
        if m == "dc":
            return {"home_draw": h + d, "away_draw": a + d, "home_away": h + a}.get(s)
        if m == "handicap" and ml.line is not None and abs(ml.line) < 1e-9:
            return {"home": h / (h + a), "away": a / (h + a)}.get(s) if h + a > 0 else None
        return None

    def score(self, ml: MarketLine) -> Optional[Scored]:
        p_mkt = self.p_market.get(ml.key())
        if p_mkt is None:
            return None
        sources: Dict[str, float] = {"market": p_mkt}
        weights: Dict[str, float] = {"market": 1.0}

        # score model
        p_sm = None
        if self.grid_lambdas and self.sport in GRID_SPORTS:
            p_sm = price_line(ml, self.grid_lambdas[0], self.grid_lambdas[1], self.sport)
        elif self.margin_model:
            p_sm = self.margin_model.prob(ml)
        if p_sm is not None and 0 < p_sm < 1:
            sources["score_model"] = p_sm
            weights["score_model"] = SCORE_MODEL_WEIGHT.get(self.sport, 0.4)

        # elo
        if self.elo_probs:
            p_elo = self._result_prob_from(self.elo_probs, ml)
            if p_elo is not None:
                sources["elo"] = p_elo
                weights["elo"] = 0.30 * min(1.0, self.elo_games / 15.0)

        # API-Football
        if self.apif_probs:
            p_ap = self._result_prob_from(self.apif_probs, ml)
            if p_ap is not None:
                sources["apif"] = p_ap
                weights["apif"] = 0.35

        blended = blend_logit(sources, weights) or p_mkt
        p_final = p_mkt + self.shrink * (blended - p_mkt)
        # market-only lines (no model view) are shaded to the market entirely
        if len(sources) == 1:
            p_final = p_mkt
        p_final = self.cal.apply(p_final)
        p_final = max(0.02, min(0.98, p_final))

        vals = list(sources.values())
        agreement = 1.0 - min(1.0, statistics.pstdev(vals) / 0.10) if len(vals) >= 2 else 0.35
        if not self.market_complete.get(ml.key(), False):
            agreement *= 0.7
        ev = p_final * ml.price - 1.0
        edge = p_final - p_mkt
        liquidity = min(1.0, ml.n_books / 3.0)
        label = selection_label(ml, self.ev.home, self.ev.away, self.sport)
        return Scored(event=self.ev, line=ml, p_market=p_mkt, p_model=p_final, sources=sources,
                      agreement=agreement, ev=ev, edge=edge, label=label, liquidity=liquidity,
                      notes=dict(self.notes))

    def score_all(self) -> List[Scored]:
        out = []
        for ml in self.ev.markets:
            s = self.score(ml)
            if s:
                out.append(s)
        return out
