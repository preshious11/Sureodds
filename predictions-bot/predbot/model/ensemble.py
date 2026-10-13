"""Per-event prediction model.

For each event we build:
  market      de-vigged consensus probability for every selection (always)
  score_model Poisson grid (football/baseball) or Gaussian
              margin model (basketball/american football) fitted to the main
              lines
  elo         rating-based result probabilities (weight grows with data)

Sources are blended in logit space, shrunk toward the market (we are humble by
default), then passed through the learned calibration.

Which signals are INDEPENDENT of the soft market consensus (and so can say it is wrong)?
  * ``sharp``  - a sharp book's de-vigged price (Shin), when a configured sharp bookmaker quotes the whole market.
                 It is a different, better-informed price - not an average of "all books".
  * ``elo``    - ratings learned from settled results, once both teams have ``elo_min_games``.
NOT independent: ``market`` (the soft consensus itself) and ``score_model`` (a Poisson / Gaussian grid FITTED to
the market's own main lines - it only makes one match's lines coherent with each other). The score model still
shapes ``p_model`` (smoothing), but it is never evidence and never counts as a confirmation.
"""
from __future__ import annotations

import logging
import math
import statistics
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from predbot.markets.catalog import selection_label, spec
from predbot.model.calibration import Calibrator
from predbot.model.devig import blend_logit, devig, shin_devig, single_side_prob
from predbot.model.elo import Elo
from predbot.model.margin import MarginModel
from predbot.model.poisson import lambdas_from_market, price_line, settlement_line
from predbot.model.payoff import Payoff, requires_distribution
from predbot.providers.base import Event, MarketLine

log = logging.getLogger(__name__)

GRID_SPORTS = {"football", "baseball"}
MARGIN_SPORTS = {"basketball", "american_football"}
SCORE_MODEL_WEIGHT = {"football": 0.60, "baseball": 0.35,
                      "basketball": 0.50, "american_football": 0.45}


def _reliable_pair(prices: Dict[str, float]) -> bool:
    """True when these selections' implied probabilities sum past 100%, i.e.
    a real bookmaker margin - the only case devig() can safely remove.

    Each price here is a *cross-book average* (_mean_price): the two sides of
    a pair can be averaged from different, non-identical sets of bookmakers,
    so their implied probabilities are not guaranteed to sum above 1 even for
    a perfectly healthy market - on thin lines this dips under 100% purely
    from book-to-book disagreement, not genuine arbitrage. devig() then
    trusts that as-is and normalises it, extremising the split into an
    overconfident probability on one side. When the sum doesn't clear 100%
    the caller skips devig() and lets each line fall back to
    single_side_prob() below, which prices it off its own price alone."""
    return sum(1.0 / p for p in prices.values() if p and p > 1.0) > 1.0


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
    p_raw: Optional[float] = None     # blended probability BEFORE calibration (stored as prob_raw)
    #: Sharp/reference fair probability (Shin de-vig of a sharp book's complete market), ``None`` when no configured
    #: sharp book quotes this line. NEVER invented: absent means "no reference", not "reference = market".
    p_fair: Optional[float] = None
    fair_books: List[str] = field(default_factory=list)
    #: EV of the best AVAILABLE (soft) price against ``p_fair`` (EDGE_PRICE); ``None`` without a reference.
    edge_ev: Optional[float] = None
    #: which of the signals that can contradict the soft market actually fed this estimate: subset of {sharp, elo}
    independent: List[str] = field(default_factory=list)
    #: Independent (history-only) estimate and how p_model relates to it. ``p_independent`` is None when no
    #: independent model could price this line; ``independent_status`` says why ('ok' or 'unavailable:<reason>').
    #: ``p_model_basis`` is 'independent' only when p_model IS that estimate; otherwise p_model is the market-derived
    #: value and the basis says so ('market_blend'), so a fallback is never mistaken for an independent prediction.
    p_independent: Optional[float] = None
    independent_status: str = "unavailable:not_configured"
    p_model_basis: str = "market_blend"
    quality: str = ""                 # main | secondary | restricted - filled by selection.evidence
    se: float = 0.0                   # uncertainty of p_model (probability points) - selection.evidence
    p_lcb: Optional[float] = None     # p_model - z*se: the ranking input (NOT a probability to show)

    @property
    def price(self) -> float:
        return self.line.price


class EventModel:
    def __init__(self, event: Event, elo: Elo, calibrator: Calibrator, shrink: float = 0.8,
                 sharp_books=None, elo_min_games: int = 10, edge_price: str = "best",
                 independent=None, independent_mode: str = "shadow"):
        self.independent = independent
        self.independent_mode = independent_mode if independent_mode in ("off", "shadow", "independent") else "shadow"
        self.ind_estimate, self.ind_status = None, "unavailable:not_configured"
        self.sharp_books = [b for b in (sharp_books or []) if b]
        self.elo_min_games = elo_min_games
        self.edge_price = edge_price if edge_price in ("best", "median") else "best"
        self.p_fair: Dict[Tuple, float] = {}
        self.fair_books: Dict[Tuple, List[str]] = {}
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
        self._soft_prices()
        self._market_probs()
        self._score_model()
        self.elo_probs, self.elo_games = self.elo.probs(self.sport, self.ev.home, self.ev.away,
                                                        home_id=self.ev.home_id, away_id=self.ev.away_id)
        if self.independent is not None and self.independent_mode != "off":
            self.ind_estimate, self.ind_status = self.independent.estimate(self.ev)

    def _soft_quotes(self, ml: MarketLine) -> List[float]:
        """Prices from the NON-sharp books: what a punter can actually be offered."""
        return [p for b, p in ml.book_quotes.items() if b not in self.sharp_books and p and p > 1.0]

    def _soft_prices(self) -> None:
        """A sharp book's price is a reference, not an offer: where it contributed to a line's price, the available
        price is re-derived from the soft books alone (median, as PRICE_MODE=median does everywhere else)."""
        if not self.sharp_books:
            return
        for ml in self.ev.markets:
            soft = self._soft_quotes(ml)
            if soft and len(soft) < len(ml.book_quotes):
                ml.price = round(float(statistics.median(soft)), 4)
                ml.bookmaker = "median" if len(soft) > 1 else ml.bookmaker

    def _sharp_fair(self, by_selection: Dict[str, MarketLine]) -> Optional[Tuple[Dict[str, float], List[str]]]:
        """Shin-de-vigged fair probabilities from the sharp book(s) that quote EVERY selection of the market, averaged
        over those books; ``None`` when no sharp book covers the whole market (a partial market cannot be de-vigged)."""
        if not self.sharp_books or not by_selection:
            return None
        per_book, used = [], []
        for book in self.sharp_books:
            prices = {sel: ml.book_quotes.get(book) for sel, ml in by_selection.items()}
            if all(p and p > 1.0 for p in prices.values()):
                per_book.append(shin_devig(prices))
                used.append(book)
        if not per_book:
            return None
        return {sel: sum(pb[sel] for pb in per_book) / len(per_book) for sel in by_selection}, used

    def _record_fair(self, by_selection: Dict[str, MarketLine]) -> None:
        got = self._sharp_fair(by_selection)
        if got:
            probs, used = got
            for sel, ml in by_selection.items():
                self.p_fair[ml.key()] = probs[sel]
                self.fair_books[ml.key()] = used

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
            market, ways = mk[0], spec(mk[0]).ways
            sels = {ml.selection for ml in lines}
            if market == "handicap" and ways == 2:
                # market_key() groups by abs(line), so this bucket can hold
                # MORE than one true pair when both signs of the same
                # magnitude are quoted (e.g. home+2.0/away+2.0 AND
                # home-2.0/away-2.0 - two different, non-complementary
                # propositions, not two sides of one market). A selection-
                # keyed dict comprehension over the whole bucket would let a
                # later pair's price silently overwrite an earlier pair's and
                # broadcast one devig result across every line in the group.
                # Each home line must devig against its own signed mirror
                # (home.line == -away.line) only.
                homes = [ml for ml in lines if ml.selection == "home"]
                aways = [ml for ml in lines if ml.selection == "away"]
                for h in homes:
                    mirror = next((a for a in aways if a.line == -h.line), None)
                    if mirror is None:
                        continue
                    prices = {"home": self._mean_price(h), "away": self._mean_price(mirror)}
                    if not _reliable_pair(prices):
                        continue
                    probs = devig(prices)
                    if "home" in probs:
                        self.p_market[h.key()] = probs["home"]
                        self.market_complete[h.key()] = True
                    if "away" in probs:
                        self.p_market[mirror.key()] = probs["away"]
                        self.market_complete[mirror.key()] = True
                    self._record_fair({"home": h, "away": mirror})
            else:
                complete = False
                if ways == 3 and market == "result3":
                    complete = sels >= {"home", "draw", "away"}
                elif ways == 2:
                    complete = len(sels) == 2 and any(
                        {a, b} <= sels for a, b in (("home", "away"), ("over", "under"), ("yes", "no"), ("even", "odd")))
                elif market == "dc":
                    complete = False  # derive from result3 instead
                if complete:
                    prices = {ml.selection: self._mean_price(ml) for ml in lines}
                    if _reliable_pair(prices):
                        probs = devig(prices)
                        for ml in lines:
                            if ml.selection in probs:
                                self.p_market[ml.key()] = probs[ml.selection]
                                self.market_complete[ml.key()] = True
                    if len({ml.selection for ml in lines}) == len(lines):
                        self._record_fair({ml.selection: ml for ml in lines})
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
            self.grid_lambdas = (lh, la)
            self.notes["lambda"] = (round(lh, 2), round(la, 2))
        elif self.sport in MARGIN_SPORTS:
            spread = self._main_line("handicap")
            ph = two.get("home")
            if ph is None and self.sport == 'american_football' and res.get('home') is not None and res.get('away') is not None:
                decisive = res['home']+res['away']
                ph = res['home']/decisive if decisive > 0 else None
            if spread is None and ph is None and tot is None:
                return
            tie_probability = None
            if self.sport == 'american_football':
                three = {m.selection: m for m in self._find('result3')}
                if set(three) == {'home','draw','away'}:
                    common = set.intersection(*(set(m.book_quotes) for m in three.values()))
                    estimates = []
                    for book in sorted(common):
                        prices = {side: three[side].book_quotes[book] for side in three}
                        if all(math.isfinite(p) and p > 1 for p in prices.values()) and sum(1/p for p in prices.values()) > 1:
                            estimates.append((book, devig(prices)['draw']))
                    if estimates:
                        tie_probability = sum(p for _, p in estimates)/len(estimates)
                        self.notes['nfl_tie_evidence'] = {
                            'source': 'matched_book_three_way', 'period': 'full',
                            'probability': tie_probability, 'books': [b for b, _ in estimates]}
                self.notes['nfl_settlement_contract'] = 'final_margin_v1'
            self.margin_model = MarginModel.from_market(
                self.sport, ph, spread[0] if spread else None, spread[1] if spread else None,
                tot[0] if tot else None, tot[1] if tot else None, tie_probability)
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
        p_fair = self.p_fair.get(ml.key())
        # The soft consensus is only a source while there is no sharp reference: with one, the reference IS the
        # market estimate and the soft books are just where the price is available (not averaged in as equals).
        p_base = p_fair if p_fair is not None else p_mkt
        sources: Dict[str, float] = {"market": p_mkt}
        weights: Dict[str, float] = {"market": 0.0 if p_fair is not None else 1.0}
        if p_fair is not None:
            sources["sharp"], weights["sharp"] = p_fair, 1.0

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
        if self.elo_probs and self.elo_games >= self.elo_min_games:
            p_elo = self._result_prob_from(self.elo_probs, ml)
            if p_elo is not None:
                sources["elo"] = p_elo
                weights["elo"] = 0.30 * min(1.0, self.elo_games / 15.0)

        blended = blend_logit(sources, weights) or p_base
        # self.shrink is how much we shrink the model blend TOWARD the market
        # (MODEL_SHRINK_TO_MARKET): shrink=1.0 -> pure market, shrink=0.0 -> pure model.
        p_final = p_base + (1.0 - self.shrink) * (blended - p_base)
        contributing = [k for k, w in weights.items() if w > 0]
        # market-only lines (no model view) are shaded to the market entirely
        if len(contributing) == 1:
            p_final = p_base
        # independent estimate: priced from history only; it never touches p_market and (in shadow mode) not p_model
        p_ind, ind_status = None, self.ind_status
        if self.ind_estimate is not None:
            p_ind = self.ind_estimate.price(ml)
            ind_status = "ok" if p_ind is not None else "unavailable:unsupported_market"
        basis = "market_blend"
        if self.independent_mode == "independent" and p_ind is not None:
            p_final, basis = p_ind, "independent"
        # Sources/odds speak in decisive-stake price probabilities. Convert to
        # mutually exclusive settlements BEFORE publishing strict-win confidence.
        shape = None
        if basis == 'independent':
            shape = settlement_line(ml, *self.ind_estimate.lambdas, self.sport)
        elif self.grid_lambdas:
            shape = settlement_line(ml, *self.grid_lambdas, self.sport)
        elif self.margin_model:
            shape = self.margin_model.settlement(ml)
        if shape is None and requires_distribution(ml, self.sport):
            return None  # two prices alone cannot identify push/partial masses
        shape = shape or Payoff.binary(p_final)
        quote_raw = max(0.02, min(0.98, p_final))
        quote_final = max(0.02, min(0.98, self.cal.apply(quote_raw)))
        if self.sport == 'american_football':
            # Retain the upper safety cap and all ticket floors. Do not inflate
            # a rare priced outcome to 2% and manufacture a positive return.
            quote_raw = max(0., min(.98, p_final))
            quote_final = max(0., min(.98, self.cal.apply(quote_raw)))
        if (self.sport == 'american_football' and ml.market == 'result3' and ml.selection == 'draw'
                and ml.period == 'full' and self.margin_model
                and self.margin_model.tie_probability is not None):
            # This very same event is the two-way refund mass. A generic 2%
            # numeric floor must not turn a priced rare tie into a false edge.
            quote_raw = quote_final = self.margin_model.tie_probability
            basis, contributing = 'market_tie_reference', ['market']
        fixed = self.margin_model.fixed_tie(ml) if self.margin_model else None
        try:
            raw_payoff = shape.reprice(quote_raw, fixed)
            payoff = shape.reprice(quote_final, fixed)
            market_payoff = shape.reprice(p_mkt, fixed)
        except ValueError:
            return None  # incompatible tie evidence and quote: never invent an advantage
        p_raw, p_final = raw_payoff.won, payoff.won
        if p_ind is not None:
            ind_payoff = settlement_line(ml, *self.ind_estimate.lambdas, self.sport)
            p_ind = ind_payoff.won if ind_payoff else None

        # COHERENCE of the contributing estimates (market vs the score model fitted to it, Elo, sharp). This is not
        # independent confirmation - the score model is derived from the market - so evidence is judged elsewhere
        # (book coverage / dispersion / sharp edge in selection.evidence).
        vals = [sources[k] for k in contributing]
        agreement = 1.0 - min(1.0, statistics.pstdev(vals) / 0.10) if len(vals) >= 2 else 0.35
        if not self.market_complete.get(ml.key(), False):
            agreement *= 0.7
        ev = payoff.ev(ml.price)
        edge = p_final - market_payoff.won
        edge_ev = None
        if p_fair is not None:
            soft = self._soft_quotes(ml)
            if soft:
                # The best SOFT price is the actionable one: a median book always carries its margin, so against a
                # sharp fair price its EV is negative for almost every line and says nothing about THIS line.
                usable = max(soft) if self.edge_price == "best" else float(statistics.median(soft))
                try:
                    edge_ev = shape.reprice(p_fair, fixed).ev(usable)
                except ValueError:
                    return None
        liquidity = min(1.0, ml.distinct_books() / 3.0)
        label = selection_label(ml, self.ev.home, self.ev.away, self.sport)
        notes = dict(self.notes)
        notes["model_basis"] = basis
        notes["settlement"] = payoff.notes(ml.price)
        notes["settlement"]["quote_raw"] = quote_raw
        notes["settlement"]["market_strict_win"] = market_payoff.won
        notes["probability_basis"] = "strict_win_v2"
        if edge_ev is not None:
            notes["reference_price"] = usable
        notes["independent_model"] = {
            "status": ind_status, "mode": self.independent_mode,
            "source": getattr(self.ind_estimate, "source", None),
            "p": None if p_ind is None else round(p_ind, 4),
            "edge_vs_market": None if p_ind is None else round(p_ind - market_payoff.won, 4),
            "ev": None if p_ind is None else round(ind_payoff.ev(ml.price), 4),
            "lambdas": None if self.ind_estimate is None else [round(x, 3) for x in self.ind_estimate.lambdas],
            "detail": None if self.ind_estimate is None else self.ind_estimate.detail}
        return Scored(event=self.ev, line=ml, p_market=p_mkt, p_model=p_final, p_raw=p_raw, sources=sources,
                      p_independent=p_ind, independent_status=ind_status, p_model_basis=basis,
                      agreement=agreement, ev=ev, edge=edge, label=label, liquidity=liquidity,
                      notes=notes, p_fair=p_fair, fair_books=list(self.fair_books.get(ml.key(), [])),
                      edge_ev=edge_ev, independent=([] if basis == 'market_tie_reference' else ["history"] if basis == "independent" else
                                   [k for k in ("sharp", "elo") if k in contributing]))

    def score_all(self) -> List[Scored]:
        out = []
        for ml in self.ev.markets:
            s = self.score(ml)
            if s:
                out.append(s)
        return out
