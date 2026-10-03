"""Independent (non-market) match models behind one sport-agnostic interface.

An independent model answers "what does OUR analysis say?" from historical information only - never from today's
bookmaker prices. The prediction pipeline asks ``IndependentModels.estimate(event)`` and gets back either an
``IndependentEstimate`` (which can price any market it supports) or ``None`` plus an explicit status:

    ok                                  an estimate exists
    unavailable:no_independent_model    this sport has no independent model yet (tennis, baseball, ...)
    unavailable:unresolved_participant  the event has no canonical participants
    unavailable:no_history              no stored results for this competition
    unavailable:unknown_team            a side has no results in the store
    unavailable:insufficient_history    a side has too few (recency-weighted) results
    unavailable:unsupported_market      the estimate exists but cannot price this market

The status is stored with every prediction, so a market-derived fallback is never mistaken for an independent estimate.
A new sport adds one class implementing ``IndependentModel`` and registers it; nothing else changes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Optional, Protocol, Tuple

from predbot.history.store import HistoryStore
from predbot.model.football_strength import FittedStrength, StrengthParams, fit_strength
from predbot.model.poisson import price_line
from predbot.providers.base import Event, MarketLine
from predbot.util.timeutil import utcnow

#: football markets the score distribution prices coherently AND the backtest validates (full match only: period
#: scaling of the grid is not validated, so half markets stay market-derived)
FOOTBALL_MARKETS = frozenset({"result3", "dc", "dnb", "total", "btts", "handicap"})


@dataclass
class IndependentEstimate:
    source: str
    sport: str
    lambdas: Tuple[float, float]
    detail: Dict = field(default_factory=dict)
    markets: frozenset = FOOTBALL_MARKETS

    def price(self, ml: MarketLine) -> Optional[float]:
        """The model's probability for this line, or ``None`` when the line is outside what the model supports."""
        if ml.market not in self.markets or ml.period not in ("full", "reg") or ml.stat != "points" or ml.is_player_prop:
            return None
        if ml.market == "total" and ml.entity != "all":
            return None
        p = price_line(ml, self.lambdas[0], self.lambdas[1], self.sport)
        return None if p is None or not 0.0 < p < 1.0 else p


class IndependentModel(Protocol):
    sport: str
    source: str

    def estimate(self, event: Event) -> Tuple[Optional[IndependentEstimate], str]:
        ...


class FootballIndependentModel:
    sport = "football"
    source = "football_poisson_v1"

    def __init__(self, db, params: StrengthParams = StrengthParams(), as_of: Optional[datetime] = None):
        self.store, self.params, self.as_of = HistoryStore(db), params, as_of or utcnow()
        self._fits: Dict[str, Optional[FittedStrength]] = {}

    def fit_for(self, competition_id: str) -> Optional[FittedStrength]:
        """One fit per competition per run, from stored results strictly before ``as_of``."""
        if competition_id not in self._fits:
            matches = self.store.results(sport=self.sport, competition_id=competition_id, before=self.as_of)
            self._fits[competition_id] = fit_strength(matches, self.as_of, self.params, competition_id)
        return self._fits[competition_id]

    def estimate(self, event: Event) -> Tuple[Optional[IndependentEstimate], str]:
        if not (event.home_id and event.away_id and event.canonical_competition_id):
            return None, "unavailable:unresolved_participant"
        fit = self.fit_for(event.canonical_competition_id)
        if fit is None:
            return None, "unavailable:no_history"
        status = fit.status(event.home_id, event.away_id)
        if status != "ok":
            return None, "unavailable:" + status
        lam = fit.lambdas(event.home_id, event.away_id)
        detail = {"competition": event.canonical_competition_id, "training_matches": fit.matches,
                  "home_effective_matches": round(fit.effective_matches[event.home_id], 1),
                  "away_effective_matches": round(fit.effective_matches[event.away_id], 1),
                  "attack": [round(fit.attack[event.home_id], 3), round(fit.attack[event.away_id], 3)],
                  "defence": [round(fit.defence[event.home_id], 3), round(fit.defence[event.away_id], 3)],
                  "half_life_days": self.params.half_life_days, "prior_strength": self.params.prior_strength}
        return IndependentEstimate(self.source, self.sport, lam, detail), "ok"


class IndependentModels:
    """Sport -> independent model. A sport with no entry reports an explicit ``no_independent_model`` status."""

    def __init__(self, models: Optional[Dict[str, IndependentModel]] = None):
        self.models = dict(models or {})

    def register(self, model: IndependentModel) -> None:
        self.models[model.sport] = model

    def estimate(self, event: Event) -> Tuple[Optional[IndependentEstimate], str]:
        model = self.models.get(event.sport)
        if model is None:
            return None, "unavailable:no_independent_model"
        return model.estimate(event)

    @classmethod
    def default(cls, db, cfg=None, as_of: Optional[datetime] = None) -> "IndependentModels":
        params = StrengthParams(half_life_days=getattr(cfg, "strength_half_life_days", StrengthParams.half_life_days),
                                prior_strength=getattr(cfg, "strength_prior", StrengthParams.prior_strength),
                                min_effective_matches=getattr(cfg, "strength_min_matches", StrengthParams.min_effective_matches))
        return cls({"football": FootballIndependentModel(db, params, as_of)})
