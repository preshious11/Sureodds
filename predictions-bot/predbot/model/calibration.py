"""Platt scaling: p' = sigmoid(a * logit(p) + b), fitted on settled picks.

Fitted by plain gradient descent (no numpy) once >= MIN_SAMPLES results exist;
until then the identity (a=1, b=0) is used. The effect is bounded so a bad
batch cannot swing probabilities wildly."""
from __future__ import annotations

import json
import logging
import math
from typing import List, Optional, Tuple

from predbot.model.devig import logit, sigmoid
from predbot.storage.db import Database
from predbot.model.payoff import nfl_contract_valid

log = logging.getLogger(__name__)
MIN_SAMPLES = 150


def _raw_probability(row) -> float:
    """The probability the model produced BEFORE calibration.

    Training the calibrator on already-calibrated ``prob`` would fit it to its own
    output; ``prob_raw`` is stored with every pick since that column was added, and
    older rows (NULL) fall back to ``prob``."""
    try:
        raw = row["prob_raw"]
    except (KeyError, IndexError):
        raw = None
    return float(raw if raw is not None else row["prob"])


def fit(samples: List[Tuple[float, float]], epochs: int = 400, lr: float = 0.05) -> Tuple[float, float]:
    """samples: (prob, outcome in {0, 0.5, 1})."""
    a, b = 1.0, 0.0
    n = len(samples)
    xs = [(logit(row[0]), row[1], row[2] if len(row) > 2 else 1.0) for row in samples]
    n = sum(w for _, _, w in xs)
    for _ in range(epochs):
        ga = gb = 0.0
        for x, y, weight in xs:
            pr = sigmoid(a * x + b)
            err = (pr - y) * weight
            ga += err * x
            gb += err
        a -= lr * ga / n
        b -= lr * gb / n
    a = max(0.5, min(1.5, a))
    b = max(-0.6, min(0.6, b))
    return a, b


class Calibrator:
    def __init__(self, db: Database, scope="global"):
        self.db, self.scope = db, scope
        cal = db.get_calibration(scope)
        self.a, self.b, self.n = cal if cal else (1.0, 0.0, 0)

    def apply(self, p: float) -> float:
        if self.n < MIN_SAMPLES:
            return p
        return sigmoid(self.a * logit(p) + self.b)

    def refit(self) -> Optional[Tuple[float, float, int]]:
        rows = self.db.all_settled_for_calibration()
        samples = []
        for r in rows:
            y = {"won": 1.0, "lost": 0.0, "half_won": 0.75, "half_lost": 0.25}.get(r["status"])
            if y is None:
                continue
            if self.scope == 'settlement_v2':
                notes = (json.loads(r['sources'] or '{}').get('_notes') or {})
                if not nfl_contract_valid(r, notes):
                    continue
                if notes.get('probability_basis') != 'strict_win_v2':
                    continue
                q = (notes.get('settlement') or {}).get('quote_raw')
                if q is None:
                    continue
                weight = .5 if r['status'] in ('half_won', 'half_lost') else 1.0
                samples.append((q, 1.0 if r['status'] in ('won', 'half_won') else 0.0, weight))
            else:
                samples.append((_raw_probability(r), y))
        effective_n = sum(row[2] if len(row) > 2 else 1 for row in samples)
        if effective_n < MIN_SAMPLES:
            return None
        a, b = fit(samples)
        self.a, self.b, self.n = a, b, int(effective_n)
        self.db.set_calibration(self.scope, a, b, int(effective_n))
        log.info("calibration refit on %d samples: a=%.3f b=%.3f", len(samples), a, b)
        return a, b, len(samples)

    def brier(self) -> Optional[float]:
        rows = self.db.all_settled_for_calibration()
        vals = []
        for r in rows:
            if self.scope == 'settlement_v2':
                meta = (json.loads(r['sources'] or '{}').get('_notes') or {})
                if not nfl_contract_valid(r, meta):
                    continue
                if meta.get('probability_basis') != 'strict_win_v2':
                    continue
                y = 1.0 if r['status'] == 'won' else 0.0
            else:
                y = {"won": 1.0, "lost": 0.0, "half_won": .75, "half_lost": .25}.get(r['status'])
            if y is not None:
                vals.append((_raw_probability(r) - y) ** 2)
        return sum(vals) / len(vals) if vals else None
