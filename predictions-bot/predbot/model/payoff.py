"""Settlement distributions and unit-stake returns, shared by all price models.

Quoted two-way probabilities describe the winning share of decisive stake,
not the probability of a full win. Keep those quantities separate.
"""
from dataclasses import asdict, dataclass
import math

from predbot.markets.settlement import _cmp_line


@dataclass(frozen=True)
class Payoff:
    won: float = 0.0
    lost: float = 0.0
    push: float = 0.0
    half_won: float = 0.0
    half_lost: float = 0.0
    void: float = 0.0

    def __post_init__(self):
        values = asdict(self).values()
        if any(not math.isfinite(v) or v < -1e-12 for v in values) or abs(sum(values) - 1) > 1e-8:
            raise ValueError('invalid settlement distribution')

    @classmethod
    def binary(cls, p):
        return cls(won=p, lost=1-p)

    @property
    def win_stake(self):
        return self.won + .5 * self.half_won

    @property
    def loss_stake(self):
        return self.lost + .5 * self.half_lost

    @property
    def fair_probability(self):
        decisive = self.win_stake + self.loss_stake
        return self.win_stake / decisive if decisive > 0 else None

    def ev(self, price):
        return self.win_stake * (price - 1) - self.loss_stake

    def variance(self, price):
        mean = self.ev(price)
        return sum(p * (v - mean)**2 for p, v in (
            (self.won, price-1), (self.lost, -1), (self.push+self.void, 0),
            (self.half_won, (price-1)/2), (self.half_lost, -.5)))

    def reprice(self, q, fixed=None):
        """Update decisive price information, retaining modelled void mass and
        the relative full/half outcome shapes. No push rate is inferred from odds.
        """
        if fixed is not None and fixed[0] not in ('push', 'void'):
            category, probability = fixed
            masses = asdict(self)
            if probability < 0 or probability > masses[category]+1e-10:
                raise ValueError('invalid fixed settlement mass')
            masses[category] = max(0., masses[category]-probability)
            positive, negative = masses['won']+masses['half_won'], masses['lost']+masses['half_lost']
            a = (masses['won']+.5*masses['half_won'])/positive if positive else 0.
            b = (masses['lost']+.5*masses['half_lost'])/negative if negative else 0.
            fw = probability * {'won':1., 'half_won':.5}.get(category, 0.)
            fl = probability * {'lost':1., 'half_lost':.5}.get(category, 0.)
            mass = positive+negative
            denominator = a*(1-q)+q*b
            if denominator <= 0:
                if self.fair_probability is not None and abs(q-self.fair_probability)<1e-10:
                    return self
                raise ValueError('unidentifiable fixed-mass reprice')
            pos = (q*b*mass+q*fl-(1-q)*fw)/denominator
            if pos < -1e-10 or pos > mass+1e-10:
                raise ValueError('quote inconsistent with fixed settlement mass')
            pos = min(mass, max(0., pos))
            for name in ('won', 'half_won'):
                masses[name] *= pos/positive if positive else 0.
            for name in ('lost', 'half_lost'):
                masses[name] *= (mass-pos)/negative if negative else 0.
            masses[category] += probability
            return Payoff(**masses)
        positive, negative = self.won+self.half_won, self.lost+self.half_lost
        if positive <= 0 or negative <= 0:
            if self.fair_probability is not None and abs(q-self.fair_probability) < 1e-10:
                return self
            raise ValueError('cannot reprice a one-sided settlement distribution')
        a, b = self.win_stake/positive, self.loss_stake/negative
        mass = positive+negative
        pos = q*b*mass / (a*(1-q)+q*b)
        return Payoff(self.won*pos/positive, self.lost*(mass-pos)/negative,
                      self.push, self.half_won*pos/positive,
                      self.half_lost*(mass-pos)/negative, self.void)

    def notes(self, price):
        return {**asdict(self), 'strict_win': self.won,
                'positive_return': self.won+self.half_won,
                'no_loss': self.won+self.half_won+self.push+self.void,
                'fair_probability': self.fair_probability,
                'ev': self.ev(price), 'variance': self.variance(price),
                'version': 2}


def line_payoff(dist, line, side):
    """Preserve mutually exclusive full/partial outcomes rather than averaging
    two quarter-line win probabilities and losing their joint settlement.
    """
    masses = {k: 0.0 for k in ('won', 'lost', 'push', 'half_won', 'half_lost', 'void')}
    for value, probability in dist.items():
        masses[_cmp_line(value, line, side)] += probability
    return Payoff(**masses)


def requires_distribution(ml, sport):
    if ml.market == 'dnb':
        return True
    if ml.market == 'result2' and (sport in ('football', 'american_football') or ml.period not in ('full', '', None)):
        return True
    return (ml.market in ('total', 'handicap', 'result_total') and ml.line is not None
            and round(ml.line % 1, 2) in (0., .25, .75))


def nfl_tie_sensitive(market, period='full', line=None):
    """Full-game bets whose push/partial outcome specifically depends on a tie."""
    return period in ('full', '', None) and (market in ('result2', 'dnb') or
            market == 'handicap' and line is not None and abs(float(line)) < .5)


def nfl_contract_valid(row, notes):
    """Eligibility/calibration gate only; never change an issued forecast's meaning."""
    row = dict(row)
    if row.get('sport') != 'american_football' or not nfl_tie_sensitive(
            row.get('market'), row.get('period', 'full'), row.get('line')):
        return True
    evidence = notes.get('nfl_tie_evidence') or {}
    valid = (notes.get('probability_basis') == 'strict_win_v2' and
            notes.get('nfl_settlement_contract') == 'final_margin_v1' and
            evidence.get('source') == 'matched_book_three_way' and evidence.get('period') == 'full' and
            bool(evidence.get('books')) and isinstance(evidence.get('probability'), (int, float)) and
            math.isfinite(evidence['probability']) and 0 <= evidence['probability'] < 1)
    if not valid:
        return False
    try:
        settlement = notes['settlement']
        payoff = Payoff(**{name: settlement.get(name, 0.) for name in
                          ('won','lost','push','half_won','half_lost','void')})
        category = 'push' if row['market'] in ('result2','dnb') else _cmp_line(0, -float(row['line']), 'over')
        if category in ('push','half_won','half_lost') and not math.isclose(
                getattr(payoff,category),evidence['probability'],abs_tol=1e-8):
            return False
        if 'prob' in row and not math.isclose(payoff.won,row['prob'],abs_tol=1e-8):
            return False
        if 'ev' in row and not math.isclose(payoff.ev(row['price']),row['ev'],abs_tol=1e-8):
            return False
    except (KeyError, TypeError, ValueError):
        return False
    return True
