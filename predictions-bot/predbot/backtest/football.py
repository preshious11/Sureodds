"""Offline walk-forward backtest for football probability models.

Compares, on the SAME matches:
    market      de-vigged closing odds (needs odds in the history)
    elo         canonical-style Elo run sequentially through the results
    poisson     the independent strength model (predbot.model.football_strength)
    poisson+elo geometric pool of the two (weight chosen on a validation window)
    poisson+market, poisson+elo+market   pools with market information (weights chosen on validation)

Temporal validity is structural, not a convention:
* the strength model is re-fitted at the start of each block from matches strictly BEFORE the block
  (``fit_strength`` raises ``LeakageError`` otherwise);
* Elo predicts a match BEFORE it learns that match's result;
* blend weights and hyper-parameters are chosen on a validation window that ends before the test window starts, and
  the headline scores are reported on the test window only.
No weight in this module is invented: every blend weight and strength hyper-parameter is the argmin of a log-loss
computed on past data.
"""
from __future__ import annotations

import math
import random
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from predbot.model.devig import devig
from predbot.model.elo import new_ratings, probs_from
from predbot.model.football_strength import StrengthParams, fit_strength
from predbot.model.poisson import ScoreGrid, DEFAULT_RHO, MAX_GOALS

OUTCOMES = ("home", "draw", "away")
EPS = 1e-12


@dataclass(frozen=True)
class BTMatch:
    kickoff: datetime
    competition: str
    home: str
    away: str
    hg: int
    ag: int
    market: Optional[Tuple[float, float, float]] = None      # de-vigged (home, draw, away), if odds exist
    ou_market: Optional[float] = None                        # de-vigged P(over 2.5), if odds exist

    @property
    def outcome(self) -> int:
        return 0 if self.hg > self.ag else 1 if self.hg == self.ag else 2


@dataclass
class Prediction:
    match: BTMatch
    p1x2: Dict[str, Tuple[float, float, float]]               # model name -> probabilities
    p_over25: Dict[str, float] = field(default_factory=dict)
    p_btts: Dict[str, float] = field(default_factory=dict)


def devig_odds(h: float, d: float, a: float) -> Tuple[float, float, float]:
    p = devig({"home": h, "draw": d, "away": a})
    return p["home"], p["draw"], p["away"]


def from_history(store, competition_ids: Optional[Iterable[str]] = None, book_order=("pinnacle", "bet365", "market_avg"),
                 baseline: Optional[Tuple[str, str]] = None) -> List[BTMatch]:
    """BTMatch list from a HistoryStore.

    ``baseline=(book, kind)`` (kind 'pre' or 'close') attaches that ONE bookmaker's raw odds, de-vigged, as the market
    reference - never a mixture of books; matches without that book carry no market. The odds are evaluation data: no
    model reads ``market`` or ``ou_market`` (see ``walk_forward``). Without ``baseline`` the legacy ``match_odds``
    table is used."""
    out = []
    comps = list(competition_ids) if competition_ids else [None]
    for comp in comps:
        for m in store.results(competition_id=comp):
            quote, ou = None, None
            if baseline is not None:
                book, kind = baseline
                ext = store.odds_ext(m.id)
                if (book, "1x2", kind) in ext:
                    quote = devig_odds(*ext[(book, "1x2", kind)])
                if (book, "ou25", kind) in ext:
                    o, u = ext[(book, "ou25", kind)][:2]
                    ou = devig({"over": o, "under": u})["over"]
            else:
                odds = store.odds(m.id)
                for book in book_order:
                    if book in odds:
                        q = odds[book]
                        quote = devig_odds(q["home"], q["draw"], q["away"])
                        break
            out.append(BTMatch(m.kickoff, m.competition_id, m.home_id, m.away_id, m.home_goals, m.away_goals, quote, ou))
    return sorted(out, key=lambda x: x.kickoff)


# ------------------------------------------------------------------------------------------------ probabilities
def grid_probs(lh: float, la: float) -> Tuple[Tuple[float, float, float], float, float]:
    g = ScoreGrid(lh, la, DEFAULT_RHO["football"], MAX_GOALS["football"])
    h, d, a = g.result()
    over = sum(g.grid[i][j] for i in range(g.n + 1) for j in range(g.n + 1) if i + j > 2.5)
    return (h, d, a), over, g.btts()


def pool(probs: Sequence[Tuple[float, float, float]], weights: Sequence[float]) -> Tuple[float, float, float]:
    """Geometric (log-linear) pool of 1X2 distributions, renormalised. Weights need not sum to one."""
    logs = [sum(w * math.log(max(p[k], EPS)) for p, w in zip(probs, weights)) for k in range(3)]
    mx = max(logs)
    e = [math.exp(x - mx) for x in logs]
    s = sum(e)
    return e[0] / s, e[1] / s, e[2] / s


# ------------------------------------------------------------------------------------------------ walk-forward
RESULT_DELAY = timedelta(hours=3)        # a result is public only after the match has been played


def walk_forward(matches: Sequence[BTMatch], params: StrengthParams, block_days: int = 14,
                 warmup_days: int = 240, score_from: Optional[datetime] = None, until: Optional[datetime] = None,
                 elo_k: Optional[float] = None, elo_home_adv: Optional[float] = None, with_strength: bool = True,
                 skipped: Optional[list] = None) -> List[Prediction]:
    """Predict every match with Elo, the strength model and a climatology baseline using only earlier results.

    * Matches before ``score_from`` (default: first kickoff + ``warmup_days``) are training only.
    * Matches at or after ``until`` are never touched (selection runs stop at the validation boundary).
    * A result is learned only once it is public, i.e. ``RESULT_DELAY`` after its kickoff, so two matches kicking
      off together (or within the match duration) never inform each other.
    * Strength is re-fitted per competition at the start of each ``block_days`` block from matches strictly before it.
    * ``skipped`` (a list) collects (match, reason) for matches that could not be predicted."""
    matches = sorted(matches, key=lambda m: m.kickoff)
    if not matches:
        return []
    start = score_from or matches[0].kickoff + timedelta(days=warmup_days)
    ratings: Dict[str, float] = defaultdict(lambda: 1500.0)
    games: Dict[str, int] = defaultdict(int)
    clim: Dict[str, List[int]] = defaultdict(lambda: [0, 0, 0, 0, 0, 0])     # n, home, draw, away, over2.5, btts
    preds: List[Prediction] = []
    block_start: Optional[datetime] = None
    fits: Dict[str, object] = {}
    learned = 0

    def learn(m: BTMatch) -> None:
        ratings[m.home], ratings[m.away] = new_ratings("football", ratings[m.home], ratings[m.away], m.hg, m.ag,
                                                       elo_k, elo_home_adv)
        games[m.home] += 1
        games[m.away] += 1
        c = clim[m.competition]
        c[0] += 1
        c[1 + m.outcome] += 1
        c[4] += 1 if m.hg + m.ag > 2 else 0
        c[5] += 1 if m.hg > 0 and m.ag > 0 else 0

    for idx, m in enumerate(matches):
        while learned < len(matches) and matches[learned].kickoff + RESULT_DELAY <= m.kickoff:
            learn(matches[learned])
            learned += 1
        if until is not None and m.kickoff >= until:
            break
        if m.kickoff < start:
            continue
        if games[m.home] < 5 or games[m.away] < 5:
            if skipped is not None:
                skipped.append((m, "elo_history"))
            continue
        elo = probs_from("football", ratings[m.home], ratings[m.away], elo_home_adv)
        c = clim[m.competition]
        n = c[0]
        p: Dict[str, Tuple[float, float, float]] = {"elo": elo,
                                                      "climatology": tuple((c[1 + k] + 1) / (n + 3) for k in range(3))}
        over: Dict[str, float] = {"climatology": (c[4] + 1) / (n + 2)}
        btts: Dict[str, float] = {"climatology": (c[5] + 1) / (n + 2)}
        if with_strength:
            if block_start is None or m.kickoff >= block_start + timedelta(days=block_days):
                block_start = m.kickoff
                fits = {}
            if m.competition not in fits:
                train = [x for x in matches[:idx] if x.competition == m.competition and x.kickoff < block_start]
                fits[m.competition] = fit_strength(_as_hist(train), block_start, params, m.competition)
            fit = fits[m.competition]
            lam = fit.lambdas(m.home, m.away) if fit is not None else None
            if lam is None:
                if skipped is not None:
                    skipped.append((m, "no_strength_estimate"))
                continue
            (ph, pd, pa), o, b = grid_probs(*lam)
            p["poisson"], over["poisson"], btts["poisson"] = (ph, pd, pa), o, b
        # reference prices are stored beside the model outputs for comparison and are never read by them
        if m.market is not None:
            p["market"] = m.market
        if m.ou_market is not None:
            over["market"] = m.ou_market
        preds.append(Prediction(m, p, over, btts))
    return preds


@dataclass(frozen=True)
class _H:
    kickoff: datetime
    home_id: str
    away_id: str
    home_goals: int
    away_goals: int


def _as_hist(matches: Iterable[BTMatch]) -> List[_H]:
    return [_H(m.kickoff, m.home, m.away, m.hg, m.ag) for m in matches]


# ------------------------------------------------------------------------------------------------ scoring
def log_loss(preds: Sequence[Tuple[Tuple[float, float, float], int]]) -> float:
    return -sum(math.log(max(p[o], EPS)) for p, o in preds) / len(preds)


def brier(preds: Sequence[Tuple[Tuple[float, float, float], int]]) -> float:
    return sum(sum((p[k] - (1.0 if k == o else 0.0)) ** 2 for k in range(3)) for p, o in preds) / len(preds)


def rps(preds: Sequence[Tuple[Tuple[float, float, float], int]]) -> float:
    tot = 0.0
    for p, o in preds:
        c1, c2 = p[0], p[0] + p[1]
        a1, a2 = (1.0 if o == 0 else 0.0), (1.0 if o <= 1 else 0.0)
        tot += ((c1 - a1) ** 2 + (c2 - a2) ** 2) / 2.0
    return tot / len(preds)


def binary_log_loss(pairs: Sequence[Tuple[float, int]]) -> float:
    return -sum(math.log(max(p if y else 1 - p, EPS)) for p, y in pairs) / len(pairs)


def binary_brier(pairs: Sequence[Tuple[float, int]]) -> float:
    return sum((p - y) ** 2 for p, y in pairs) / len(pairs)


def calibration(preds: Sequence[Tuple[Tuple[float, float, float], int]], bins: int = 10) -> dict:
    """Reliability of every (outcome, probability) pair pooled: per bin the mean predicted and observed frequency, and
    the expected calibration error (count-weighted mean |predicted - observed|)."""
    buckets: Dict[int, List[Tuple[float, float]]] = defaultdict(list)
    for p, o in preds:
        for k in range(3):
            buckets[min(bins - 1, int(p[k] * bins))].append((p[k], 1.0 if k == o else 0.0))
    total = sum(len(v) for v in buckets.values())
    table, ece = [], 0.0
    for b in sorted(buckets):
        v = buckets[b]
        mp, mo = sum(x for x, _ in v) / len(v), sum(y for _, y in v) / len(v)
        table.append({"bin": f"{b / bins:.1f}-{(b + 1) / bins:.1f}", "n": len(v), "predicted": round(mp, 3), "observed": round(mo, 3)})
        ece += len(v) / total * abs(mp - mo)
    return {"ece": round(ece, 4), "bins": table}


def score_models(preds: Sequence[Prediction], names: Sequence[str]) -> Dict[str, dict]:
    out = {}
    for name in names:
        pairs = [(p.p1x2[name], p.match.outcome) for p in preds if name in p.p1x2]
        if not pairs:
            continue
        row = {"n": len(pairs), "log_loss": round(log_loss(pairs), 5), "brier": round(brier(pairs), 5),
               "rps": round(rps(pairs), 5), "calibration": calibration(pairs)}
        ou = [(p.p_over25[name], 1 if p.match.hg + p.match.ag > 2 else 0) for p in preds if name in p.p_over25]
        bt = [(p.p_btts[name], 1 if p.match.hg > 0 and p.match.ag > 0 else 0) for p in preds if name in p.p_btts]
        if ou:
            row["over25_log_loss"], row["over25_brier"] = round(binary_log_loss(ou), 5), round(binary_brier(ou), 5)
        if bt:
            row["btts_log_loss"], row["btts_brier"] = round(binary_log_loss(bt), 5), round(binary_brier(bt), 5)
        out[name] = row
    return out


# ------------------------------------------------------------------------------------------------ blends
def _grid(step: float = 0.25, n: int = 3) -> List[Tuple[float, ...]]:
    vals = [i * step for i in range(int(1 / step) + 1)]
    if n == 2:
        return [(w, 1 - w) for w in vals]
    return [(a, b, round(1 - a - b, 10)) for a in vals for b in vals if a + b <= 1 + 1e-9]


def add_blends(preds: List[Prediction], validation: Sequence[Prediction]) -> Dict[str, dict]:
    """Add pooled models to every prediction. Weights are the log-loss argmin on ``validation`` ONLY."""
    chosen: Dict[str, dict] = {}

    def best(names, grid, label):
        usable = [p for p in validation if all(n in p.p1x2 for n in names)]
        if not usable:
            return None
        scored = []
        for w in grid:
            ll = log_loss([(pool([p.p1x2[n] for n in names], w), p.match.outcome) for p in usable])
            scored.append((ll, w))
        ll, w = min(scored)
        chosen[label] = {"components": list(names), "weights": [round(x, 3) for x in w], "validation_log_loss": round(ll, 5),
                         "validation_n": len(usable)}
        return w

    plan = [(("poisson", "elo"), _grid(0.1, 2), "poisson+elo"),
            (("poisson", "market"), _grid(0.1, 2), "poisson+market"),
            (("elo", "market"), _grid(0.1, 2), "elo+market"),
            (("poisson", "elo", "market"), _grid(0.1, 3), "poisson+elo+market")]
    for names, grid, label in plan:
        w = best(names, grid, label)
        if w is None:
            continue
        for p in preds:
            if all(n in p.p1x2 for n in names):
                p.p1x2[label] = pool([p.p1x2[n] for n in names], w)
    return chosen


# ------------------------------------------------------------------------------------------------ driver
def split_point(preds: Sequence[Prediction], fraction: float) -> int:
    """Index of the first test prediction: ``fraction`` of the way in, moved forward to the next kickoff INSTANT so
    validation and test never share one."""
    cut = int(len(preds) * fraction)
    while 0 < cut < len(preds) and preds[cut].match.kickoff == preds[cut - 1].match.kickoff:
        cut += 1
    return cut


def run_backtest(matches: Sequence[BTMatch], params: StrengthParams, validation_fraction: float = 0.4,
                 block_days: int = 14, warmup_days: int = 240) -> dict:
    """Walk forward, choose blend weights on the early part of the evaluated matches, score the later part."""
    preds = walk_forward(matches, params, block_days, warmup_days)
    if len(preds) < 50:
        return {"error": f"only {len(preds)} predictable matches; need at least 50", "predicted": len(preds)}
    cut = split_point(preds, validation_fraction)
    validation, test = preds[:cut], preds[cut:]
    chosen = add_blends(preds, validation)
    names = ["market", "elo", "poisson", "poisson+elo", "poisson+market", "elo+market", "poisson+elo+market"]
    # fair comparison: the same matches for every model that is reported side by side
    with_market = [p for p in test if "market" in p.p1x2]
    return {"params": {"half_life_days": params.half_life_days, "prior_strength": params.prior_strength},
            "matches_total": len(matches), "predicted": len(preds), "validation_n": len(validation), "test_n": len(test),
            "validation_window": [validation[0].match.kickoff.isoformat(), validation[-1].match.kickoff.isoformat()],
            "test_window": [test[0].match.kickoff.isoformat(), test[-1].match.kickoff.isoformat()],
            "blend_weights_chosen_on_validation": chosen,
            "test_all_matches": score_models(test, [n for n in names if n not in ("market",) and not n.endswith("market")]),
            "test_matches_with_market": score_models(with_market, names) if with_market else {},
            "test_with_market_n": len(with_market)}


def select_hyperparameters(matches: Sequence[BTMatch], half_lives=(120.0, 240.0, 480.0), priors=(2.0, 6.0, 14.0),
                           validation_fraction: float = 0.4, block_days: int = 14, warmup_days: int = 240) -> dict:
    """Pick (half-life, prior) by validation log-loss of the strength model alone; never looks at the test window."""
    results = []
    for hl in half_lives:
        for k in priors:
            preds = walk_forward(matches, StrengthParams(half_life_days=hl, prior_strength=k), block_days, warmup_days)
            cut = split_point(preds, validation_fraction)
            if cut < 30:
                continue
            ll = log_loss([(p.p1x2["poisson"], p.match.outcome) for p in preds[:cut]])
            results.append({"half_life_days": hl, "prior_strength": k, "validation_log_loss": round(ll, 5), "n": cut})
    results.sort(key=lambda r: r["validation_log_loss"])
    return {"grid": results, "best": results[0] if results else None}


# ------------------------------------------------------------------------------------------------ dated protocol
def _ll1(p, o: int) -> float:
    return -math.log(max(p[o], EPS))


def paired_difference(preds: Sequence[Prediction], a: str, b: str) -> Optional[dict]:
    """Per-match 1X2 log-loss of ``a`` minus ``b`` on the matches both predict. Positive mean: ``b`` is better.
    ``z`` is the mean over its standard error (matches are treated as independent)."""
    d = [_ll1(p.p1x2[a], p.match.outcome) - _ll1(p.p1x2[b], p.match.outcome) for p in preds if a in p.p1x2 and b in p.p1x2]
    if len(d) < 2:
        return None
    mean = sum(d) / len(d)
    var = sum((x - mean) ** 2 for x in d) / (len(d) - 1)
    se = math.sqrt(var / len(d))
    identical = abs(mean) < 1e-9 and se < 1e-9                  # the two models are the same distribution
    return {"a": a, "b": b, "n": len(d), "mean_ll_a_minus_b": round(mean, 5), "se": round(se, 5),
            "z": 0.0 if identical or not se else round(mean / se, 2)}


def score_by(preds: Sequence[Prediction], names: Sequence[str], key) -> Dict[str, dict]:
    """Compact per-group scores (n, log-loss, Brier) for every model on the matches of each group."""
    groups: Dict[str, List[Prediction]] = defaultdict(list)
    for p in preds:
        groups[key(p)].append(p)
    out = {}
    for g, ps in sorted(groups.items()):
        row = {}
        for name in names:
            pairs = [(p.p1x2[name], p.match.outcome) for p in ps if name in p.p1x2]
            if pairs:
                row[name] = {"n": len(pairs), "log_loss": round(log_loss(pairs), 4), "brier": round(brier(pairs), 4)}
        out[g] = row
    return out


def binary_reliability(pairs: Sequence[Tuple[float, int]], bins: int = 10) -> dict:
    buckets: Dict[int, List[Tuple[float, int]]] = defaultdict(list)
    for p, y in pairs:
        buckets[min(bins - 1, int(p * bins))].append((p, y))
    table, ece = [], 0.0
    for b in sorted(buckets):
        v = buckets[b]
        mp, mo = sum(x for x, _ in v) / len(v), sum(y for _, y in v) / len(v)
        table.append({"bin": f"{b / bins:.1f}-{(b + 1) / bins:.1f}", "n": len(v), "predicted": round(mp, 3), "observed": round(mo, 3)})
        ece += len(v) / len(pairs) * abs(mp - mo)
    return {"ece": round(ece, 4), "bins": table}


def binary_markets(preds: Sequence[Prediction], names: Sequence[str]) -> Dict[str, dict]:
    """Over/Under 2.5 and BTTS are scored separately and are NOT independent evidence from each other or from 1X2
    (one score distribution produces all three)."""
    out = {}
    for market, getter, outcome in (("over25", lambda p: p.p_over25, lambda m: 1 if m.hg + m.ag > 2 else 0),
                                    ("btts", lambda p: p.p_btts, lambda m: 1 if m.hg > 0 and m.ag > 0 else 0)):
        row = {}
        for name in names:
            pairs = [(getter(p)[name], outcome(p.match)) for p in preds if name in getter(p)]
            if pairs:
                row[name] = {"n": len(pairs), "log_loss": round(binary_log_loss(pairs), 5), "brier": round(binary_brier(pairs), 5),
                             "base_rate": round(sum(y for _, y in pairs) / len(pairs), 3), "reliability": binary_reliability(pairs)}
        out[market] = row
    return out


def disagreement(preds: Sequence[Prediction], a: str = "poisson", b: str = "market", bins: int = 4) -> dict:
    """Does a large gap between model ``a`` and ``b`` (usually the market) carry information? Matches are ranked by the
    largest per-outcome probability gap and cut into equal-count bins; per bin both models' log-loss and the realised
    frequency of the outcome on which ``a`` is more bullish than ``b`` are reported. Evidence only."""
    rows = [p for p in preds if a in p.p1x2 and b in p.p1x2]
    if len(rows) < bins * 10:
        return {"n": len(rows), "note": "too few matches with both predictions"}

    def gap(p):
        return max(abs(p.p1x2[a][k] - p.p1x2[b][k]) for k in range(3))

    rows.sort(key=gap)
    size = len(rows) // bins
    out = []
    for i in range(bins):
        chunk = rows[i * size:(i + 1) * size if i < bins - 1 else len(rows)]
        fav = [max(range(3), key=lambda k: p.p1x2[a][k] - p.p1x2[b][k]) for p in chunk]
        out.append({"gap_range": [round(gap(chunk[0]), 3), round(gap(chunk[-1]), 3)], "n": len(chunk),
                    f"{a}_log_loss": round(log_loss([(p.p1x2[a], p.match.outcome) for p in chunk]), 4),
                    f"{b}_log_loss": round(log_loss([(p.p1x2[b], p.match.outcome) for p in chunk]), 4),
                    f"mean_p_{a}_on_outcome_{a}_favours": round(sum(p.p1x2[a][k] for p, k in zip(chunk, fav)) / len(chunk), 3),
                    f"mean_p_{b}_on_same_outcome": round(sum(p.p1x2[b][k] for p, k in zip(chunk, fav)) / len(chunk), 3),
                    "observed_frequency": round(sum(1 for p, k in zip(chunk, fav) if p.match.outcome == k) / len(chunk), 3)})
    return {"n": len(rows), "bins": out}


def select_elo(matches: Sequence[BTMatch], score_from: datetime, validation_end: datetime,
               ks=(20.0, 30.0, 40.0, 60.0, 80.0), home_advs=(40.0, 65.0, 90.0)) -> dict:
    """Elo K and home advantage chosen on the validation window only (the walk stops at ``validation_end``)."""
    grid = []
    for k in ks:
        for h in home_advs:
            preds = walk_forward(matches, StrengthParams(), score_from=score_from, until=validation_end, elo_k=k,
                                 elo_home_adv=h, with_strength=False)
            grid.append({"k_factor": k, "home_adv": h, "n": len(preds),
                         "validation_log_loss": round(log_loss([(p.p1x2["elo"], p.match.outcome) for p in preds]), 5)})
    grid.sort(key=lambda r: r["validation_log_loss"])
    return {"grid": grid, "best": grid[0]}


def select_strength(matches: Sequence[BTMatch], score_from: datetime, validation_end: datetime,
                    half_lives=(90.0, 180.0, 270.0, 365.0, 730.0), priors=(0.5, 1.0, 2.0, 6.0, 12.0), block_days: int = 14) -> dict:
    """Recency half-life and prior strength chosen on the validation window only, by 1X2 log-loss. The validation
    Over 2.5 / BTTS losses are recorded beside it as a diagnostic and play no part in the choice."""
    grid = []
    for hl in half_lives:
        for k in priors:
            preds = walk_forward(matches, StrengthParams(half_life_days=hl, prior_strength=k), block_days,
                                 score_from=score_from, until=validation_end)
            grid.append({"half_life_days": hl, "prior_strength": k, "n": len(preds),
                         "validation_log_loss": round(log_loss([(p.p1x2["poisson"], p.match.outcome) for p in preds]), 5),
                         "diagnostic_over25_log_loss": round(binary_log_loss([(p.p_over25["poisson"], 1 if p.match.hg + p.match.ag > 2 else 0) for p in preds]), 5),
                         "diagnostic_btts_log_loss": round(binary_log_loss([(p.p_btts["poisson"], 1 if p.match.hg > 0 and p.match.ag > 0 else 0) for p in preds]), 5)})
    grid.sort(key=lambda r: r["validation_log_loss"])
    return {"grid": grid, "best": grid[0]}


def run_dated_backtest(matches: Sequence[BTMatch], validation_start: datetime, test_start: datetime,
                       params: Optional[StrengthParams] = None, elo: Optional[Tuple[float, float]] = None,
                       block_days: int = 14, select: bool = True, keep: Optional[dict] = None) -> dict:
    """Training: everything before ``validation_start`` (learned, never scored). Validation: [validation_start,
    test_start): hyper-parameters and blend weights are chosen here. Test: ``test_start`` onward, scored once.

    Selection walks stop at ``test_start``: test results are never read while choosing anything."""
    matches = sorted(matches, key=lambda m: m.kickoff)
    selection = {}
    if select:
        selection["elo"] = select_elo(matches, validation_start, test_start)
        selection["strength"] = select_strength(matches, validation_start, test_start, block_days=block_days)
        elo = (selection["elo"]["best"]["k_factor"], selection["elo"]["best"]["home_adv"])
        b = selection["strength"]["best"]
        params = StrengthParams(half_life_days=b["half_life_days"], prior_strength=b["prior_strength"])
    params = params or StrengthParams()
    skipped: list = []
    preds = walk_forward(matches, params, block_days, score_from=validation_start, elo_k=elo[0] if elo else None,
                         elo_home_adv=elo[1] if elo else None, skipped=skipped)
    validation = [p for p in preds if p.match.kickoff < test_start]
    test = [p for p in preds if p.match.kickoff >= test_start]
    if len(validation) < 50 or len(test) < 50:
        return {"error": "need at least 50 validation and 50 test predictions", "validation": len(validation), "test": len(test)}
    chosen = add_blends(preds, validation)
    if keep is not None:
        keep["validation"], keep["test"] = validation, test
    models = ["market", "climatology", "elo", "poisson", "poisson+elo", "poisson+market", "elo+market", "poisson+elo+market"]
    one_x_two = [n for n in models if n != "market" and not n.endswith("market")]
    with_market = [p for p in test if "market" in p.p1x2]
    in_test = [m for m in matches if m.kickoff >= test_start]
    skipped_test = Counter(r for m, r in skipped if m.kickoff >= test_start)

    def period(p):
        return f"{p.match.kickoff.year}-{'H1' if p.match.kickoff.month < 7 else 'H2'}"

    pairs = ["climatology vs poisson", "climatology vs elo", "elo vs poisson", "elo vs poisson+elo", "poisson vs poisson+elo"]
    if with_market:
        pairs += ["market vs poisson", "market vs poisson+elo", "market vs poisson+market", "market vs poisson+elo+market",
                  "market vs elo+market"]
    with_ou = [p for p in test if "market" in p.p_over25]
    market_models = [n for n in models if n != "market"] + ["market"]
    by_comp_paired = {}
    for comp in sorted({p.match.competition for p in with_market}):
        sub = [p for p in with_market if p.match.competition == comp]
        by_comp_paired[comp] = {k: paired_difference(sub, *k.split(" vs ")) for k in ("market vs poisson", "market vs poisson+market")}
    return {
        "boundaries": {"training": [matches[0].kickoff.isoformat(), validation_start.isoformat()],
                       "validation": [validation[0].match.kickoff.isoformat(), validation[-1].match.kickoff.isoformat()],
                       "test": [test[0].match.kickoff.isoformat(), test[-1].match.kickoff.isoformat()]},
        "counts": {"matches_total": len(matches), "training_matches": sum(1 for m in matches if m.kickoff < validation_start),
                   "validation_predicted": len(validation), "test_predicted": len(test), "test_matches_in_window": len(in_test),
                   "test_skipped": dict(skipped_test), "test_with_market": len(with_market)},
        "selected": {"strength": {"half_life_days": params.half_life_days, "prior_strength": params.prior_strength},
                     "elo": {"k_factor": elo[0], "home_adv": elo[1]} if elo else "defaults"},
        "selection_grids": selection,
        "blend_weights_chosen_on_validation": chosen,
        "validation_1x2": score_models(validation, one_x_two),
        "test_1x2": score_models(test, one_x_two),
        "test_1x2_with_market": score_models(with_market, models) if with_market else "no historical odds in the store",
        "test_binary_markets": binary_markets(test, ["climatology", "poisson"]),
        "test_by_competition": score_by(test, one_x_two, lambda p: p.match.competition),
        "test_by_period": score_by(test, one_x_two, period),
        "paired_vs_baselines": {k: paired_difference(test, *k.split(" vs ")) for k in pairs},
        "disagreement_poisson_vs_market": disagreement(with_market) if with_market else "no historical odds in the store",
        "disagreement_bins": disagreement_bins(with_market) if with_market else "no historical odds in the store",
        "test_by_competition_with_market": score_by(with_market, market_models, lambda p: p.match.competition) if with_market else {},
        "test_by_competition_paired_vs_market": by_comp_paired,
        "test_over25_with_market": binary_markets(with_ou, ["climatology", "poisson", "market"]) if with_ou else "no historical O/U 2.5 odds in the store",
        "test_over25_with_market_n": len(with_ou),
    }


# ------------------------------------------------------------------------------------------------ disagreement
GAP_EDGES = (0.0, 0.02, 0.05, 0.10, 0.15, 1.01)


def disagreement_bins(preds: Sequence[Prediction], a: str = "poisson", b: str = "market", edges=GAP_EDGES,
                      boot: int = 300, seed: int = 7) -> dict:
    """Is ``delta = p_a - p_b`` information or model error?

    Every (match, outcome) pair is one instance with delta, both probabilities and the realised 0/1 outcome. Instances
    are binned by |delta| (default <2, 2-5, 5-10, 10-15, >=15 percentage points) and split by direction (a more bullish
    or more bearish than b). Per cell: sample size, mean probabilities, observed frequency, binary Brier and log-loss of
    each source, and which source was closer. ``slope`` regresses (outcome - p_b) on delta through the origin:
    0 means the gap is noise (the market was right), 1 means model ``a`` was right; a bootstrap over MATCHES (outcomes
    of one match are not independent) gives the 95% interval. Evidence only - nothing here selects or sizes a bet."""
    rows = [p for p in preds if a in p.p1x2 and b in p.p1x2]
    if not rows:
        return {"n_matches": 0}
    inst = []
    for i, p in enumerate(rows):
        for k in range(3):
            inst.append((i, p.p1x2[a][k], p.p1x2[b][k], 1 if p.match.outcome == k else 0))

    def cell(items):
        n = len(items)
        pa, pb = sum(x[1] for x in items) / n, sum(x[2] for x in items) / n
        obs = sum(x[3] for x in items) / n
        ba = sum((x[1] - x[3]) ** 2 for x in items) / n
        bb = sum((x[2] - x[3]) ** 2 for x in items) / n
        la = -sum(math.log(max(x[1] if x[3] else 1 - x[1], EPS)) for x in items) / n
        lb = -sum(math.log(max(x[2] if x[3] else 1 - x[2], EPS)) for x in items) / n
        return {"n": n, f"mean_p_{a}": round(pa, 4), f"mean_p_{b}": round(pb, 4), "observed": round(obs, 4),
                f"brier_{a}": round(ba, 5), f"brier_{b}": round(bb, 5), f"logloss_{a}": round(la, 5), f"logloss_{b}": round(lb, 5),
                "closer_by_brier": a if ba < bb else b if bb < ba else "tie",
                "closer_by_calibration_gap": a if abs(pa - obs) < abs(pb - obs) else b}

    cells = []
    for lo, hi in zip(edges, edges[1:]):
        for name, ok in (("bullish", lambda d: d > 0), ("bearish", lambda d: d < 0)):
            items = [x for x in inst if lo <= abs(x[1] - x[2]) < hi and ok(x[1] - x[2])]
            if items:
                cells.append({"gap_pp": f"{lo * 100:.0f}-{min(hi, 1) * 100:.0f}", "direction": name, **cell(items)})
    bins = []
    for lo, hi in zip(edges, edges[1:]):
        items = [x for x in inst if lo <= abs(x[1] - x[2]) < hi]
        if items:
            bins.append({"gap_pp": f"{lo * 100:.0f}-{min(hi, 1) * 100:.0f}", **cell(items)})

    def slope(items):
        num = sum((x[1] - x[2]) * (x[3] - x[2]) for x in items)
        den = sum((x[1] - x[2]) ** 2 for x in items)
        return num / den if den else 0.0

    by_match: Dict[int, list] = defaultdict(list)
    for x in inst:
        by_match[x[0]].append(x)
    groups = list(by_match.values())
    point = slope(inst)
    rng = random.Random(seed)
    draws = sorted(slope([x for g in (groups[rng.randrange(len(groups))] for _ in groups) for x in g]) for _ in range(boot))
    return {"n_matches": len(rows), "n_instances": len(inst), "bins": bins, "cells": cells,
            "slope_outcome_minus_market_on_delta": {"slope": round(point, 4), "ci95": [round(draws[int(0.025 * boot)], 4), round(draws[int(0.975 * boot)], 4)],
                                                    "meaning": "0 = disagreement is noise (market right); 1 = the model is right"}}
