"""Turn scored market lines into the day's Top Picks and Daily Picks.

Sizing: the daily post is ONE ticket, so combined odds - not pick counts -
decide how long the lists are.

Rules (from the brief):
  * top picks are the most solid selections: main markets only, high probability,
    well covered by books (and not worse value than a sharp reference where one
    exists), searched for the best 1-3 leg ticket worth ~TOP_COMBINED_ODDS (2.00)
  * daily picks are sized on their OWN: the Daily ticket alone aims for
    DAILY_COMBINED_MIN_ODDS (10) and never exceeds DAILY_COMBINED_MAX_ODDS (12). The builder takes the
    best-evidenced legs first, STOPS as soon as the aim is reached, and never pads: if the qualifying
    legs only make 6.4, 6.4 is posted. The Top legs are consulted for duplicates but never counted
  * candidates are ranked on evidence (selection/evidence.py): the lower confidence bound of the
    probability plus the edge over a sharp reference - never on raw probability alone - and the
    restricted market class (first half, corners, far alternates, exotics) is only offered after the rest
  * an outcome that is a top pick is never repeated in the daily list; the same
    match may appear with a *different* outcome, but never a contradictory one
  * the bot must not lean on one outcome type: caps on home/away/over/under
    shares, one market type, player props, exotics (no favourites cap: with a 10-12 odds target
    the highest-probability legs ARE the short-priced ones, and capping them would trade hit rate for variety)
"""
from __future__ import annotations

import logging
import math
import time
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from collections import Counter
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from predbot.config import Config
from predbot.markets.catalog import contradicts, is_equivalent, selection_family, spec
from predbot.markets.settlement import unsettleable_reason
from predbot.selection import cross_tier
from predbot.selection import evidence as evmod
from predbot.selection.families import family_counts, order_by_family_z
from predbot.model.ensemble import Scored
from predbot.util.fixtures import competition_id, fixture_key, fixture_orientation
from predbot.util.names import canonical_team

log = logging.getLogger(__name__)


@dataclass
class PickSet:
    top: List[Scored] = field(default_factory=list)
    daily: List[Scored] = field(default_factory=list)
    pool_size: int = 0
    eligible: int = 0
    diagnostics: Dict = field(default_factory=dict)
    complete: bool = True

    def all(self) -> List[Scored]:
        return self.top + self.daily


def _settleable(s: Scored) -> bool:
    ml = s.line
    if spec(ml.market).score_settled:
        return True
    # provider-settled props: only SportsGameOdds gives us the settled value
    return ml.market == "prop" and "sportsgameodds" in s.event.sources and bool(ml.provider_odd_id)


#: Signals that are genuinely INDEPENDENT of the soft market. score_model is fitted
#: to the market's own main lines (Poisson/Gaussian derived from devigged
#: prices), so its "EV" is just the bookmaker margin - treating it as an
#: independent signal rejected every basketball candidate at once. Only these
#: can say the market is wrong: a sharp book's de-vigged price, and Elo (once it has data).
INDEPENDENT_SIGNALS = {"elo", "sharp"}


def has_independent_signal(s: "Scored") -> bool:
    return bool(INDEPENDENT_SIGNALS & set(s.sources))


def _conf(s: Scored) -> float:
    """The ranking value of a candidate: its evidence-adjusted ``rank_score`` once ``select`` has assessed it
    (lower confidence bound + sharp edge, selection/evidence.py), else - a bare Scored never assessed - its
    probability. A ranking score, NOT a probability: it is never shown or stored as ``prob``."""
    return s.rank_score if s.p_lcb is not None else s.p_model


def _top_score(s: Scored) -> float:
    return _conf(s)


def _daily_score(s: Scored) -> float:
    return _conf(s)


def _conf_q(s: Scored) -> float:
    """``_conf`` kept strictly inside (0, 1) so it can be multiplied along a ticket."""
    return min(0.999, max(0.01, _conf(s)))


def _cap(frac: float, target: int) -> int:
    return max(1, int(math.ceil(frac * target)))


def same_fixture(a, b) -> bool:
    """True when two Scored candidates' events are the same real-world match,
    even if they came from different providers as two distinct Event objects
    (event.dedupe_key()/provider ids can differ across providers, so identity
    and dedupe_key alone are not enough to catch a Top/Daily duplicate)."""
    return fixture_orientation(a, b) is not None


def _pre(event) -> tuple:
    comp = competition_id(event)
    return event.dedupe_key(), comp, fixture_key(event, comp)


def _pre_cached(cache: Dict[int, tuple], event) -> tuple:
    got = cache.get(id(event))
    if got is None:
        got = cache[id(event)] = _pre(event)
    return got


def fixture_relation(a, b, pre_a: Optional[tuple] = None, pre_b: Optional[tuple] = None) -> Optional[bool]:
    """``None`` when ``a`` and ``b`` are different matches; otherwise whether the two providers list the sides
    the other way round (``True`` = home/away swapped). One match is recognised by an identical dedupe_key,
    by ``fixture_orientation`` (fuzzy names, kickoff, competition), or by an identical ``fixture_key``.
    ``pre_a``/``pre_b`` are optional precomputed (dedupe_key, competition id, fixture_key) triples."""
    pre_a, pre_b = pre_a or _pre(a), pre_b or _pre(b)
    if a is b or pre_a[0] == pre_b[0]:
        return False
    orientation = fixture_orientation(a, b, pre_a[1], pre_b[1])
    if orientation is not None:
        return orientation == "swapped"
    if pre_a[2] == pre_b[2]:
        ha, hb = canonical_team(a.home, a.sport), canonical_team(b.home, b.sport)
        return ha != hb and ha == canonical_team(b.away, b.sport)
    return None


#: Hard ceiling on complete tickets evaluated by choose_top(). Real pools have
#: continuous probabilities, so pruning finishes after a handful (4 on a
#: 1,255-leg pool); this only matters for pathological ties (hundreds of legs
#: with identical p_model), where the best ticket found so far is returned.
MAX_TOP_TICKETS_EVALUATED = 50_000


class _SearchBudgetSpent(Exception):
    pass


def link_events(events) -> Tuple[List[set], set]:
    """``fixture_conflicts`` plus the set of (i, j) index pairs (i < j) whose two providers list home/away
    the other way round."""
    n = len(events)
    pre = [_pre(e) for e in events]
    conflicts: List[set] = [set() for _ in range(n)]
    swapped: set = set()
    for i in range(n):
        for j in range(i + 1, n):
            rel = fixture_relation(events[i], events[j], pre[i], pre[j])
            if rel is None:
                continue
            conflicts[i].add(j)
            conflicts[j].add(i)
            if rel:
                swapped.add((i, j))
    return conflicts, swapped


def fixture_conflicts(events) -> List[set]:
    """For each event, the set of OTHER indices that are the same real-world
    match: an identical dedupe_key(), or the fuzzy same_fixture() match (one
    match seen through two providers with slightly different names/kickoffs).

    Computed ONCE for a set of legs, so a combination search tests "different
    fixtures" by set membership instead of re-running the fuzzy matcher for
    every combination. Deliberately pairwise, not clustered: fuzzy matching is
    not transitive (A~B and B~C does not make A~C), and merging chains of
    near-matches would collapse genuinely different fixtures into one."""
    return link_events(events)[0]


def _cross_tier_verdict(s: Scored, top: List[Scored], mode: str, pre: Optional[Dict[int, tuple]] = None) -> Optional[Tuple[str, str]]:
    """(rejection name, why) when Daily candidate ``s`` may not be offered because of a Top leg on the same
    match; ``None`` when it is allowed. ``forbid``: any Top fixture is off-limits. ``different_outcome``: only
    the same tip (or one nested in it) is refused; contradictions are left to the builder's conflicts()."""
    for t in top:
        if pre is None:
            swapped = fixture_relation(t.event, s.event)
        else:                                  # per-event identity computed once for the whole selection
            swapped = fixture_relation(t.event, s.event, _pre_cached(pre, t.event), _pre_cached(pre, s.event))
        if swapped is None:
            continue
        if mode == cross_tier.FORBID:
            return "CROSS_TIER_DUPLICATE", "match is in the Top ticket (CROSS_TIER_SAME_FIXTURE=forbid)"
        rel = cross_tier.relation(t.line, s.line, swapped)
        if rel == cross_tier.DUPLICATE or (not swapped and s.label == t.label):
            return "CROSS_TIER_DUPLICATE", f"same tip as Top leg {t.label}"
        if rel == cross_tier.NESTED:
            return "CROSS_TIER_NESTED", f"nested in Top leg {t.label}"
    return None


def choose_top(pool: List[Scored], cfg: Config, target: Optional[float] = None, ceiling: Optional[float] = None,
               min_legs: Optional[int] = None, max_legs: Optional[int] = None):
    """Exact, band-aware search for the Top accumulator: the 1..3-leg combination
    of DIFFERENT fixtures whose combined odds sit in [TOP_COMBINED_ODDS,
    TOP_ODDS_MAX] with the highest joint confidence (product of each leg's
    evidence-adjusted confidence ``_conf``, which is p_model for an unassessed leg), ties
    broken by closeness to the target, outcome-family variety, then fewer legs.

    No shortlist: a pre-trim by score/price silently discarded every leg priced
    high enough to reach the band (confident legs are short-priced, so the 60
    "best" legs could never multiply to 2.00 - Top came back empty with 1,255
    qualifying legs). Instead the legs are sorted by price and a depth-first
    search only ever visits partners inside the band's interval - a leg at odds
    o needs the rest of the ticket in [target/o, ceiling/o] - found by bisect,
    and a branch is cut as soon as its best possible joint confidence cannot
    beat the best ticket found so far. Sizes are searched smallest first, so a
    2-leg answer seeds the pruning of the 3-leg search.

    Returns (legs, combinations_checked) where combinations_checked counts the
    complete tickets actually evaluated (bounded by MAX_TOP_TICKETS_EVALUATED)."""
    target = cfg.top_odds_target if target is None else target
    ceiling = cfg.top_odds_max if ceiling is None else ceiling
    min_size = max(1, cfg.top_legs_min if min_legs is None else min_legs)
    max_size = max(min_size, cfg.top_legs_max if max_legs is None else max_legs)

    # Identical fixture + price collapses to the most confident leg.
    ranked = sorted(pool, key=lambda s: (-_conf_q(s), -s.p_model, s.event.dedupe_key(), repr(s.line.key())))
    unique = {}
    for s in ranked:
        unique.setdefault((s.event.dedupe_key(), round(s.price, 4)), s)
    options = sorted(unique.values(),
                     key=lambda s: (s.price, -_conf_q(s), s.event.dedupe_key(), repr(s.line.key())))
    n = len(options)
    if n < min_size:
        return [], 0
    prices = [s.price for s in options]
    qs = [_conf_q(s) for s in options]
    families = [selection_family(s.line) for s in options]
    pmax, qmax = prices[-1], max(qs)

    # Which legs are the same real fixture: computed once, at EVENT level (a
    # match has many legs but is one event), never per combination.
    event_pos: Dict[str, int] = {}
    events = []
    leg_event = []
    for s in options:
        key = s.event.dedupe_key()
        if key not in event_pos:
            event_pos[key] = len(events)
            events.append(s.event)
        leg_event.append(event_pos[key])
    conflicts = fixture_conflicts(events)

    def clash(a: int, b: int) -> bool:
        ea, eb = leg_event[a], leg_event[b]
        return ea == eb or eb in conflicts[ea]

    # Sparse table over p_model: the most confident leg in any price window in
    # O(1), so a whole window is discarded when even it cannot beat the best.
    table = [qs]
    span = 1
    while span * 2 <= n:
        prev = table[-1]
        table.append([max(prev[i], prev[i + span]) for i in range(n - 2 * span + 1)])
        span *= 2

    def window_max(lo: int, hi: int) -> float:
        k = (hi - lo).bit_length() - 1
        row = table[k]
        return max(row[lo], row[hi - (1 << k)])

    EPS = 1e-9
    best: List[int] = []
    best_key = None
    checked = 0
    chosen: List[int] = []

    def rec(size: int, start: int, price: float, conf: float) -> None:
        nonlocal best, best_key, checked
        rem = size - len(chosen)
        if rem == 1:
            lo = bisect_left(prices, target / price - EPS, start)
            hi = bisect_right(prices, ceiling / price + EPS, start)
        else:
            # Legs are taken in ascending price: this one is the cheapest of the
            # remaining `rem`, so its price is at most (ceiling/price)^(1/rem),
            # and at least what still lets the rest (each <= pmax) reach target.
            lo = bisect_left(prices, target / (price * pmax ** (rem - 1)) - EPS, start)
            hi = bisect_right(prices, (ceiling / price) ** (1.0 / rem) + EPS, start)
        if lo >= hi:
            return
        rest_bound = qmax ** (rem - 1)
        if best_key is not None and conf * window_max(lo, hi) * rest_bound < -best_key[0] - 1e-12:
            return                            # nothing in this price window can beat the best
        for j in range(lo, hi):
            if best_key is not None and conf * qs[j] * rest_bound < -best_key[0] - 1e-12:
                continue                      # cannot beat the best ticket so far
            if any(clash(j, c) for c in chosen):
                continue
            chosen.append(j)
            if rem == 1:
                checked += 1
                if checked > MAX_TOP_TICKETS_EVALUATED:
                    chosen.pop()
                    raise _SearchBudgetSpent
                total = price * prices[j]
                # Bisect already allows floating-point error; the final check
                # must use the same tolerance (1.27 * (2/1.27) is 1.9999999999999998).
                if target - EPS <= total <= ceiling + EPS:
                    key = (-(conf * qs[j]), abs(total - target),
                           -len({families[i] for i in chosen}), size)
                    if best_key is None or key < best_key:
                        best_key, best = key, list(chosen)
            else:
                rec(size, j + 1, price * prices[j], conf * qs[j])
            chosen.pop()

    try:
        for size in range(min_size, max_size + 1):
            rec(size, 0, 1.0, 1.0)
    except _SearchBudgetSpent:
        log.warning("choose_top: evaluation budget (%d tickets) spent on a pool of %d legs - "
                    "returning the best ticket found so far", MAX_TOP_TICKETS_EVALUATED, n)
    legs = [options[i] for i in best]
    legs.sort(key=lambda s: -_top_score(s))
    return legs, checked


def select(scored: List[Scored], cfg: Config, now: datetime,
           top_priority_source: Optional[str] = None, trace: Optional[Dict] = None,
           results_gate=None, _exclude_daily: Optional[set] = None, _depth: int = 0,
           top_trace: Optional[Dict] = None) -> PickSet:
    """``trace`` (optional, diagnostics only) collects the first rule that
    blocked each candidate: id(Scored) -> reason string."""
    base_reasons: Dict[int, str] = {}

    def _note(s: Scored, rule: str) -> None:
        """Record the FIRST rule that stopped a candidate. daily:/cap:/loop:
        reasons override earlier base:/top: ones: a candidate can fail the Top
        bar and still be a Daily candidate, so the later rule is the one that
        actually decided its fate."""
        if rule.startswith("base:"):
            base_reasons[id(s)] = rule.split()[1]
        if trace is None:
            return
        if rule.startswith(("daily:", "cap:", "loop")):
            trace[id(s)] = rule
        else:
            trace.setdefault(id(s), rule)

    marks = [(time.perf_counter(), time.process_time())]
    ps = PickSet(pool_size=len(scored))
    lead = now + timedelta(minutes=cfg.min_lead_minutes)
    base: List[Scored] = []
    rejection_counts: Counter = Counter()
    gate_paths: Dict[int, Optional[str]] = {}
    no_path_by_league: Counter = Counter()      # distinct events per league
    for s in scored:
        if not s.event.start:
            rejection_counts["INVALID_KICKOFF"] += 1
            _note(s, "base: INVALID_KICKOFF")
            continue
        if s.event.start < lead:
            rejection_counts["WRONG_DATE"] += 1
            _note(s, "base: WRONG_DATE (before now+MIN_LEAD)")
            continue
        if s.line.market in cfg.disabled_markets:
            rejection_counts["DISABLED_MARKET"] += 1
            _note(s, "base: DISABLED_MARKET")
            continue
        if not _settleable(s):
            rejection_counts["UNSUPPORTED_MARKET"] += 1
            _note(s, "base: UNSUPPORTED_MARKET")
            continue
        if results_gate is not None:
            path = gate_paths.get(id(s.event), False)
            if path is False:
                path = gate_paths[id(s.event)] = results_gate(s.event)
                if path is None:
                    no_path_by_league[str(s.event.league_id or s.event.league or "?")] += 1
            if not path:
                rejection_counts["NO_RESULTS_PATH"] += 1
                _note(s, "base: NO_RESULTS_PATH")
                continue
            why = unsettleable_reason(path if isinstance(path, (list, tuple)) else [path], s.line.period, s.line.stat)
            if why:
                # a path exists for the event, but none can grade THIS market's period / stat
                rejection_counts["PERIOD_UNSETTLEABLE" if why == "period" else "STAT_UNSETTLEABLE"] += 1
                _note(s, f"base: {why.upper()}_UNSETTLEABLE")
                continue
        if not math.isfinite(s.price):
            rejection_counts["INVALID_PRICE"] += 1
            _note(s, "base: INVALID_PRICE (non-finite)")
            continue
        if s.price < 1.15:
            rejection_counts["INVALID_PRICE"] += 1
            _note(s, "base: INVALID_PRICE (<1.15 sanity floor)")
            continue
        base.append(s)
    ps.eligible = len(base)
    marks.append((time.perf_counter(), time.process_time()))
    # Evidence assessment, once per candidate: market-quality tier, uncertainty, lower confidence bound, sharp edge.
    # The ranking score (``rank_score``) is derived from it and is kept apart from ``p_model``.
    tables: Dict[int, dict] = {}
    assessed: Dict[int, evmod.Evidence] = {}
    for s in base:
        e = assessed[id(s)] = evmod.assess(s, cfg, tables)
        s.quality, s.se, s.p_lcb, s.rank_score = e.quality, e.se, e.p_lcb, e.rank
        s.notes["evidence"] = e.as_notes()
    ps.diagnostics["base_by_quality"] = dict(Counter(assessed[id(s)].quality for s in base))
    ps.diagnostics["base_with_sharp_reference"] = sum(1 for s in base if assessed[id(s)].has_sharp)

    def _shared_rejections(s: Scored) -> List[Tuple[str, str]]:
        """Evidence gates both tiers share. A sharp reference is only ever used when it exists."""
        e = assessed[id(s)]
        reasons = []
        if e.dispersion is not None and e.dispersion > cfg.max_book_dispersion:
            reasons.append(("OUTLIER_BOOKS", f"books disagree by {e.dispersion:.3f} > MAX_BOOK_DISPERSION"))
        if e.has_sharp and s.edge_ev < cfg.min_edge_vs_sharp:
            reasons.append(("NEGATIVE_EDGE_VS_SHARP", f"price is {s.edge_ev:+.3f} EV vs the sharp fair probability"))
        if not s.independent and s.edge > cfg.max_unsupported_edge:
            reasons.append(("UNSUPPORTED_EDGE", f"model claims {s.edge:+.3f} over the market with no independent signal"))
        return reasons

    def _shared_reject(s: Scored) -> Optional[Tuple[str, str]]:
        reasons = _shared_rejections(s)
        return reasons[0] if reasons else None

    # ------------------------------------------------------------ top picks
    # Sized by odds: take the best candidates until the ticket is worth ~2.00.
    top_pool: List[Scored] = []
    relaxed_extra: List[Scored] = []      # pass every Top gate except a per-leg p_model within TOP_RELAX_PROB of the floor
    relax_floor = cfg.top_min_prob - getattr(cfg, "top_relax_prob", 0.0)
    top_rejected: Counter = Counter()

    def _top_evidence_reject(s: Scored) -> Optional[Tuple[str, str]]:
        e = assessed[id(s)]
        if e.quality != evmod.MAIN:
            return "MARKET_QUALITY", f"top: {e.quality} market (Top is main markets only)"
        if e.books < cfg.top_min_books:
            return "INSUFFICIENT_BOOKS", f"top: books {e.books} < TOP_MIN_BOOKS ({cfg.top_min_books})"
        got = _shared_reject(s)
        return (got[0], f"top: {got[1]}") if got else None

    def _passes_rest(s: Scored) -> bool:
        if has_independent_signal(s) and s.ev < cfg.top_max_neg_ev:
            return False
        if _top_evidence_reject(s):
            return False
        return s.agreement >= cfg.top_min_agreement and len(s.sources) >= 2

    # Independent Top audit: the general trace is overwritten by Daily decisions.
    # Keep every failed gate, including failures hidden behind the first one.
    def top_reasons(s: Scored) -> List[str]:
        e = assessed[id(s)]
        reasons = []
        if e.quality != evmod.MAIN:
            reasons.append("MARKET_QUALITY")
        if s.price < cfg.top_leg_odds_min:
            reasons.append("LEG_ODDS_MIN")
        if not s.p_model >= cfg.top_min_prob:
            reasons.append("PROBABILITY_FLOOR")
        if e.books < cfg.top_min_books:
            reasons.append("INSUFFICIENT_BOOKS")
        if not s.agreement >= cfg.top_min_agreement:
            reasons.append("MODEL_AGREEMENT")
        if len(s.sources) < 2:
            reasons.append("INSUFFICIENT_MODEL_SIGNALS")
        if has_independent_signal(s) and s.ev < cfg.top_max_neg_ev:
            reasons.append("MODEL_MARKET_DISAGREEMENT")
        reasons.extend(name for name, _ in _shared_rejections(s))
        return reasons

    audit = {id(s): top_reasons(s) for s in base}
    if top_trace is not None:
        top_trace.update({id(s): audit.get(id(s), [base_reasons.get(id(s), "BASE_REJECTED")]) for s in scored})

    for s in base:
        if s.line.market == "prop":
            top_rejected["PLAYER_PROP"] += 1
            _note(s, "top: PLAYER_PROP")
            continue
        if not (s.price >= cfg.top_leg_odds_min):     # data-sanity floor only
            top_rejected["BELOW_THRESHOLD"] += 1
            _note(s, "top: price < TOP_LEG_ODDS_MIN")
            continue
        if not (s.p_model >= cfg.top_min_prob):
            top_rejected["BELOW_THRESHOLD"] += 1
            _note(s, f"top: p_model < TOP_MIN_PROB ({cfg.top_min_prob})")
            if s.p_model >= relax_floor and _passes_rest(s):
                relaxed_extra.append(s)
            continue
        # Confidence product, not an edge product: market-only candidates skip
        # the EV gate (their EV is negative by construction); candidates with
        # an independent signal are rejected only when that signal says the
        # market is materially overpricing the selection.
        if has_independent_signal(s) and s.ev < cfg.top_max_neg_ev:
            top_rejected["BELOW_THRESHOLD"] += 1
            _note(s, f"top: disagreement floor (ev < {cfg.top_max_neg_ev})")
            continue
        if not (s.agreement >= cfg.top_min_agreement):
            top_rejected["BELOW_THRESHOLD"] += 1
            _note(s, f"top: agreement < TOP_MIN_AGREEMENT ({cfg.top_min_agreement})")
            continue
        if not (len(s.sources) >= 2):
            # coherence check only: the score model fitted to this match's own lines must exist (it is NOT an
            # independent confirmation - real support is the book / sharp evidence below)
            top_rejected["INSUFFICIENT_SOURCES"] += 1
            _note(s, "top: INSUFFICIENT_SOURCES (<2)")
            continue
        rejected = _top_evidence_reject(s)
        if rejected:
            top_rejected[rejected[0]] += 1
            _note(s, rejected[1])
            continue
        top_pool.append(s)
    # Odds API is tried first for the Top ticket: only fall back to the full
    # pool (the other providers) when it can't reach the combined odds target.
    def find_top(pool: List[Scored], target: Optional[float] = None, ceiling: Optional[float] = None, **legs):
        """(legs, tickets checked, priority pool used): the priority source first, then the whole pool."""
        goal = cfg.top_odds_target if target is None else target
        if top_priority_source:
            priority_pool = [s for s in pool if top_priority_source in s.event.sources]
            found, n = choose_top(priority_pool, cfg, target, ceiling, **legs)
            if found and math.prod(s.price for s in found) >= goal - 1e-9:
                return found, n, True
            return (*choose_top(pool, cfg, target, ceiling, **legs), False)
        return (*choose_top(pool, cfg, target, ceiling, **legs), False)

    top, checked, used_priority_pool = find_top(top_pool)
    top_relaxed: Optional[str] = None
    if not top and cfg.top_legs_max >= 2 and getattr(cfg, "top_relaxed_fallback", False):
        # Only when the strict search found nothing: widen the band, then lower the probability floor. Every other
        # Top gate (>= 2 sources, agreement, disagreement floor) is unchanged; never over 3.
        band = cfg.top_relax_band
        wide = dict(target=cfg.top_odds_target - band, ceiling=cfg.top_odds_max + band,
                    min_legs=max(2, cfg.top_legs_min), max_legs=cfg.top_legs_max)
        top, checked2, used_priority_pool = find_top(top_pool, **wide)
        checked += checked2
        if top:
            top_relaxed = "band"
        elif relaxed_extra:
            top, checked2, used_priority_pool = find_top(top_pool + relaxed_extra, **wide)
            checked += checked2
            top_relaxed = "prob" if top else None
        if top:
            log.warning("picker: strict Top search found no ticket - relaxed fallback '%s' gave %d legs at %.2f",
                        top_relaxed, len(top), math.prod(s.price for s in top))
    top_ids = {id(s) for s in top}
    top_odds = math.prod(s.price for s in top)
    for s in top:
        s.tier = "top"
        if trace is not None:
            # A pick that makes the slate was not, in the end, blocked.
            trace.pop(id(s), None)
    ps.top = top
    ps.diagnostics = {"base_by_sport": dict(Counter(s.event.sport for s in base)),
                      "unique_candidates_by_sport": dict(Counter(sport for sport, _, _ in
                          {(s.event.sport, s.event.dedupe_key(), s.line.key()) for s in base})),
                      "gte50_by_sport": dict(Counter(s.event.sport for s in base if s.p_model >= .5)),
                      "top_eligible_by_sport": dict(Counter(s.event.sport for s in top_pool)),
                      "selection_rejections": dict(rejection_counts),
                      "no_results_path_by_league": dict(no_path_by_league),
                      "top_pool_rejections": dict(top_rejected),
                      "combinations_checked": checked, "top_odds": top_odds if top else None,
                      "top_priority_source": top_priority_source,
                      "top_priority_used": used_priority_pool, "top_relaxed": top_relaxed}

    funnel = {"raw_candidates": len(scored), "base_valid": len(base)}
    survivors = list(base)
    stages = [
        ("main_market", {"MARKET_QUALITY"}),
        ("probability_qualified", {"LEG_ODDS_MIN", "PROBABILITY_FLOOR"}),
        ("book_qualified", {"INSUFFICIENT_BOOKS"}),
        ("evidence_qualified", {"MODEL_AGREEMENT", "INSUFFICIENT_MODEL_SIGNALS",
                                "MODEL_MARKET_DISAGREEMENT", "OUTLIER_BOOKS", "UNSUPPORTED_EDGE"}),
        ("edge_qualified", {"NEGATIVE_EDGE_VS_SHARP"}),
    ]
    for name, rules in stages:
        survivors = [s for s in survivors if not rules.intersection(audit[id(s)])]
        funnel[name] = len(survivors)
    funnel["deduped_candidates"] = len({(s.event.dedupe_key(), round(s.price, 4)) for s in top_pool})
    funnel["eligible_fixtures"] = len({s.event.dedupe_key() for s in top_pool})
    funnel["final_candidates"] = len(top_pool)
    funnel["ticket_selections"] = len(top)
    ps.diagnostics["top_funnel"] = funnel
    ps.diagnostics["top_rejection_reasons"] = dict(Counter(
        [base_reasons[id(s)] for s in scored if id(s) in base_reasons] +
        [audit[id(s)][0] for s in base if audit[id(s)]]))
    ps.diagnostics["top_all_gate_failures"] = dict(Counter(r for rs in audit.values() for r in rs))
    quality_without_books = [s for s in base if not set(audit[id(s)]) - {"INSUFFICIENT_BOOKS"}]
    ps.diagnostics["top_quality_book_coverage"] = dict(Counter(
        str(assessed[id(s)].books) if assessed[id(s)].books < 4 else "4+" for s in quality_without_books))
    ps.diagnostics["base_by_quality"] = dict(Counter(assessed[id(s)].quality for s in base))
    ps.diagnostics["base_with_sharp_reference"] = sum(assessed[id(s)].has_sharp for s in base)
    from predbot.selection.top_diagnostics import effective_config
    ps.diagnostics["top_config"] = effective_config(cfg)
    ps.diagnostics["top_ticket_status"] = ("SELECTED" if top else "NO_QUALITY_CANDIDATES" if not top_pool
                                             else "NO_ACCEPTABLE_COMBINATION")

    marks.append((time.perf_counter(), time.process_time()))
    # ---------------------------------------------------------- daily picks
    # The Daily is its own ticket: keep filling while ITS product is short of the floor, stop before a pick would
    # break the ceiling or the pick cap. (It used to start from the Top's odds, so the Daily alone could stop below 10.)
    cap = cfg.daily_target
    cross_mode = getattr(cfg, "cross_tier_same_fixture", cross_tier.DIFFERENT_OUTCOME)
    pre_cache: Dict[int, tuple] = {}
    daily_pool: List[Scored] = []
    daily_rejected: Counter = Counter()
    for s in base:
        if id(s) in top_ids:
            _note(s, "daily: already a Top leg")
            continue
        if _exclude_daily and id(s) in _exclude_daily:
            rejection_counts["CROSS_TIER_DUPLICATE_REMOVED"] += 1
            _note(s, "daily: CROSS_TIER_DUPLICATE_REMOVED")
            continue
        verdict = _cross_tier_verdict(s, top, cross_mode, pre_cache)
        if verdict:
            name, why = verdict
            rejection_counts[name] += 1
            daily_rejected["DUPLICATE_OF_TOP" if name == "CROSS_TIER_DUPLICATE" else name] += 1
            s.notes["cross_tier"] = f"{name}: {why}"
            _note(s, f"daily: {name}")
            continue
        if not (cfg.daily_min_odds <= s.price <= cfg.daily_max_odds):
            daily_rejected["BELOW_THRESHOLD"] += 1
            _note(s, f"daily: price outside DAILY_MIN_ODDS..DAILY_MAX_ODDS ({s.price:.2f})")
            continue
        if not (s.p_model >= cfg.daily_min_prob):
            daily_rejected["BELOW_THRESHOLD"] += 1
            _note(s, f"daily: p_model < DAILY_MIN_PROB ({s.p_model:.3f} < {cfg.daily_min_prob})")
            continue
        ev_s = assessed[id(s)]
        need = evmod.min_books(ev_s.quality, cfg)
        if ev_s.books < need:
            daily_rejected["INSUFFICIENT_BOOKS"] += 1
            _note(s, f"daily: {ev_s.quality} market backed by {ev_s.books} book(s), needs {need}")
            continue
        shared = _shared_reject(s)
        if shared:
            daily_rejected[shared[0]] += 1
            _note(s, f"daily: {shared[1]}")
            continue
        if ev_s.p_lcb < cfg.daily_min_lcb:
            daily_rejected["LOW_CONFIDENCE_BOUND"] += 1
            _note(s, f"daily: lower confidence bound {ev_s.p_lcb:.3f} < DAILY_MIN_LCB ({cfg.daily_min_lcb})")
            continue
        if has_independent_signal(s) and s.ev < cfg.daily_max_neg_ev:
            # Confidence product: market-only candidates (EV negative by
            # construction) skip the EV gate entirely; candidates with an
            # independent signal are rejected only when that signal says the
            # market is materially overpricing the selection.
            daily_rejected["BELOW_THRESHOLD"] += 1
            _note(s, f"daily: disagreement floor (ev={s.ev:.3f})")
            continue
        if not (s.agreement >= 0.30):
            daily_rejected["BELOW_THRESHOLD"] += 1
            _note(s, f"daily: agreement < 0.30 ({s.agreement:.2f})")
            continue
        daily_pool.append(s)
    ps.diagnostics["daily_eligible_by_sport"] = dict(Counter(s.event.sport for s in daily_pool))
    ps.diagnostics["daily_pool_rejections"] = dict(daily_rejected)
    # Order only: every gate above and every cap below is untouched. ``raw`` ranks on rank_score alone;
    # family_z ranks each market family against its own peers. Either way the RESTRICTED market class comes LAST
    # (stable sort): a first-half / corners / far-alternate line is only offered once nothing better is left.
    if getattr(cfg, "daily_ranking", "family_z") == "raw":
        daily_pool.sort(key=lambda s: s.rank_score, reverse=True)
        ps.diagnostics["daily_family_pool"] = family_counts(daily_pool)
    else:
        daily_pool, ps.diagnostics["daily_family_pool"] = order_by_family_z(daily_pool, lambda s: s.rank_score)
    daily_pool.sort(key=lambda s: evmod.OFFER_ORDER[assessed[id(s)].quality])
    ps.diagnostics["daily_ranking"] = getattr(cfg, "daily_ranking", "family_z")

    # One real fixture = one event position, however many providers/legs describe
    # it. fixture_conflicts() links the same match seen under different names or
    # kickoffs, so MAX_PICKS_PER_EVENT cannot be sidestepped by provider naming,
    # and Top legs count too: a Daily pick never repeats a Top fixture.
    slate_events: List = []
    slate_pos: Dict[str, int] = {}

    def event_pos(e) -> int:
        key = e.dedupe_key()
        if key not in slate_pos:
            slate_pos[key] = len(slate_events)
            slate_events.append(e)
        return slate_pos[key]

    for t in top:
        event_pos(t.event)
    for s in daily_pool:
        event_pos(s.event)
    fixture_links, swapped_pairs = link_events(slate_events)

    def same_slate_fixture(a, b) -> bool:
        pa, pb = event_pos(a), event_pos(b)
        return pa == pb or pb in fixture_links[pa]

    def slate_swapped(a, b) -> bool:
        pa, pb = event_pos(a), event_pos(b)
        return (min(pa, pb), max(pa, pb)) in swapped_pairs

    per_event: Dict[int, int] = {}
    if cross_mode == cross_tier.FORBID:                 # Top legs count against the per-match cap...
        for t in top:
            per_event[event_pos(t.event)] = per_event.get(event_pos(t.event), 0) + 1
    # ...whereas under different_outcome MAX_PICKS_PER_EVENT counts per tier (the Top's leg is not a Daily pick)

    def picks_on_fixture(s: Scored) -> int:
        pos = event_pos(s.event)
        return per_event.get(pos, 0) + sum(per_event.get(o, 0) for o in fixture_links[pos])
    family_count: Dict[str, int] = {}
    # Keyed by (market, stat): corners totals and goals totals are both
    # market="total", and a share cap that can't tell them apart is exactly
    # how one of them ends up filling the whole list.
    market_count: Dict[Tuple[str, str], int] = {}
    stat_count: Dict[str, int] = {}
    period_count: Dict[str, int] = {}
    sport_count: Dict[str, int] = {}
    league_count: Dict[str, int] = {}
    props = exotics = 0
    sports_available = {s.event.sport for s in daily_pool}
    caps = {"family": _cap(0.45, cap), "exotic": _cap(0.25, cap),
            "league": cfg.daily_max_per_league}
    # The list is sized by combined odds, not by a count, so a 25% market
    # share has to be measured against the list as it is actually being
    # built - a share of daily_target (20) would still let one market take
    # 5 of a 6-pick slate.
    market_share = cfg.daily_max_market_share
    # Same measurement basis for the stat share: corners reach the list through
    # several markets (totals AND 1X2), so the (market, stat) cap alone still
    # lets "corners, corners, corners" happen.
    stat_share = cfg.daily_max_stat_share
    sport_share = cfg.daily_max_sport_share if len(sports_available) > 1 else 1.0
    relaxed_hits: Counter = Counter()

    def share_exceeded(count: int, share: float) -> bool:
        n_after = len(chosen) + 1
        return count + 1 > max(1, math.ceil(share * n_after))

    chosen: List[Scored] = []
    chosen_ids: set = set()

    def conflicts(s: Scored) -> bool:
        for other in top + chosen:
            if not same_slate_fixture(other.event, s.event):
                continue
            line = cross_tier.mirror(s.line) if slate_swapped(other.event, s.event) else s.line
            if is_equivalent(other.line, line) or contradicts(other.line, line):
                return True
        return False

    def try_add(s: Scored, relaxed: bool = False) -> bool:
        """The strict pass enforces every quota. The relaxed pass - run only when the strict ticket is short of
        DAILY_COMBINED_MIN_ODDS - waives the market / sport / league DIVERSITY shares (never the per-event,
        conflict, family, stat, period, prop or exotic limits) and records which quota each admitted pick would
        otherwise have broken. It only ever sees candidates that already passed every QUALITY gate (probability
        floor, book coverage, dispersion, sharp edge, lower bound): diversity is a preference, quality is not."""
        nonlocal props, exotics
        ek = event_pos(s.event)
        fam = selection_family(s.line)
        mk = s.line.market
        mkey = (mk, s.line.stat)
        grp = spec(mk).group
        if picks_on_fixture(s) >= cfg.max_picks_per_event:
            _note(s, f"cap: per-event (MAX_PICKS_PER_EVENT={cfg.max_picks_per_event})")
            return False
        if conflicts(s):
            _note(s, "cap: conflict with chosen")
            return False
        if family_count.get(fam, 0) >= caps["family"] and fam != "other":
            _note(s, f"cap: outcome family '{fam}' ({caps['family']})")
            return False
        if mk == "prop" and props >= cfg.max_player_props:
            _note(s, "cap: player props")
            return False
        if grp == "exotic" and exotics >= caps["exotic"]:
            _note(s, "cap: exotics")
            return False
        over = {"period": (s.line.period not in ("full", "reg", "", None)
                           and share_exceeded(period_count.get(s.line.period, 0), cfg.daily_max_period_share)),
                "market": share_exceeded(market_count.get(mkey, 0), market_share),
                # "points" is the DEFAULT stat every main market carries (match
                # result, totals, handicaps); it is not a derived stat like
                # corners/cards. Capping it here would cap all main-line bets
                # combined - it silently blocked every basketball candidate
                # (basketball has no non-points markets), duplicating the
                # (market, stat) cap above. The stat cap exists to stop
                # "corners, corners, corners", so it applies to DERIVED stats.
                "stat": (bool(s.line.stat) and s.line.stat != "points"
                         and share_exceeded(stat_count.get(s.line.stat, 0), stat_share)),
                "sport": share_exceeded(sport_count.get(s.event.sport, 0), sport_share),
                "league": bool(s.event.league) and league_count.get(s.event.league, 0) >= caps["league"]}
        if any(over.values()):
            # the relaxed pass waives only the pure-variety shares; the stat and period shares keep corners / cards /
            # first-half legs from taking the ticket (thin markets: that is a quality limit, not a preference)
            blocked = [name for name, hit in over.items() if hit and (not relaxed or name in ("stat", "period"))]
            if blocked:
                for name in blocked:
                    _note(s, f"cap: share '{name}' ({name} share exceeded)")
                return False
            for name, hit in over.items():
                if hit:
                    relaxed_hits[name] += 1
        chosen.append(s)
        if trace is not None:
            # A pick that makes the slate was not, in the end, blocked.
            trace.pop(id(s), None)
        chosen_ids.add(id(s))
        per_event[ek] = per_event.get(ek, 0) + 1
        family_count[fam] = family_count.get(fam, 0) + 1
        market_count[mkey] = market_count.get(mkey, 0) + 1
        if s.line.stat and s.line.stat != "points":
            stat_count[s.line.stat] = stat_count.get(s.line.stat, 0) + 1
        if s.line.period not in ("full", "reg", "", None):
            period_count[s.line.period] = period_count.get(s.line.period, 0) + 1
        sport_count[s.event.sport] = sport_count.get(s.event.sport, 0) + 1
        league_count[s.event.league] = league_count.get(s.event.league, 0) + 1
        props += 1 if mk == "prop" else 0
        exotics += 1 if grp == "exotic" else 0
        return True

    ticket = 1.0                                # the Daily's own product - the Top's odds are not part of it

    def under_cap() -> bool:
        return len(top) + len(chosen) < cap        # "up to DAILY_PICKS_TARGET picks" in one list

    def fits(price: float) -> bool:
        """Never accept a pick that would push the ticket past the ceiling, floor or not."""
        if ticket >= cfg.daily_odds_max:
            return False
        return ticket * price <= cfg.daily_odds_max

    # Best-evidenced first. Stop the moment the ticket is worth DAILY_COMBINED_MIN_ODDS (fewest legs that do the
    # job); skip a leg that would push it past DAILY_COMBINED_MAX_ODDS and try the next. Never reach for odds.
    def sweep(relaxed: bool) -> None:
        """Offer the ordered pool repeatedly until a whole sweep adds nothing: a leg a share quota turned away
        early is reconsidered once the ticket has grown (market shares are measured on the ticket as built)."""
        nonlocal ticket
        added = True
        while added:
            added = False
            for s in daily_pool:
                if id(s) in chosen_ids:
                    continue
                if ticket >= cfg.daily_odds_min or not under_cap():
                    return
                if not fits(s.price):
                    _note(s, f"loop{'(relaxed)' if relaxed else ''}: ticket x {s.price:.2f} would exceed "
                             "DAILY_COMBINED_MAX_ODDS")
                    continue
                if try_add(s, relaxed):
                    ticket *= s.price
                    added = True

    sweep(False)
    strict_ticket, strict_picks = ticket, len(chosen)
    if ticket < cfg.daily_odds_min and under_cap():
        sweep(True)
        if relaxed_hits:
            ps.diagnostics["daily_caps_relaxed"] = {
                "reason": "strict pass could not reach DAILY_COMBINED_MIN_ODDS with qualifying legs",
                "strict_ticket": round(strict_ticket, 2), "strict_picks": strict_picks,
                "daily_pool": len(daily_pool), "exceeded": dict(relaxed_hits)}
            log.warning("picker: diversity quotas relaxed %s - strict ticket %.2f from %d picks was "
                        "below the %.2f target with a qualifying pool of %d (no quality gate was relaxed)",
                        dict(relaxed_hits), strict_ticket, strict_picks, cfg.daily_odds_min, len(daily_pool))
    stopped = ("target odds reached" if ticket >= cfg.daily_odds_min
               else "pick cap" if not under_cap() else "pool exhausted")
    if trace is not None and stopped != "pool exhausted":
        for s in daily_pool:
            if id(s) not in chosen_ids and id(s) not in trace:
                _note(s, f"loop: {stopped} (never attempted)")
    ps.diagnostics["daily_stopped"] = stopped
    ps.diagnostics["daily_target_reached"] = bool(chosen) and ticket >= cfg.daily_odds_min

    for s in chosen:
        s.tier = "daily"
    ps.daily = chosen
    ps.diagnostics["daily_own_odds"] = round(ticket, 2) if chosen else None
    ps.diagnostics["daily_legs"] = len(chosen)
    ps.diagnostics["daily_odds_with_top"] = round(top_odds * ticket, 2) if chosen else None    # one release only: to compare with the old sizing
    log.info("picker: pool=%d eligible=%d top_pool=%d daily_pool=%d -> top=%d daily=%d ticket=%.2f top_relaxed=%s",
             ps.pool_size, ps.eligible, len(top_pool), len(daily_pool), len(top), len(chosen), ticket, top_relaxed)
    marks.append((time.perf_counter(), time.process_time()))
    ps.diagnostics["select_timings"] = {
        name: {"wall": round(b[0] - a[0], 4), "cpu": round(b[1] - a[1], 4)}
        for name, a, b in zip(("select_base", "select_top", "select_daily"), marks, marks[1:])}
    bad = cross_tier.violations(ps.top, ps.daily, fixture_relation, cross_mode)
    if bad and _depth < 5:
        for d, t, why in bad:
            log.warning("cross-tier invariant caught a %s the selection layer missed: Daily '%s' (%s %s v %s) vs Top '%s' "
                        "(%s %s v %s) - removing the Daily leg and refilling", why, d.label, d.event.provider, d.event.home,
                        d.event.away, t.label, t.event.provider, t.event.home, t.event.away)
        removed = (_exclude_daily or set()) | {id(d) for d, _, _ in bad}
        again = select(scored, cfg, now, top_priority_source=top_priority_source, trace=trace,
                       results_gate=results_gate, _exclude_daily=removed, _depth=_depth + 1,
                       top_trace=top_trace)
        again.diagnostics["cross_tier_removed"] = len(removed)
        return again
    ps.diagnostics["selection_rejections"] = dict(rejection_counts)
    ps.diagnostics["cross_tier_mode"] = cross_mode
    ps.diagnostics["daily_family_picks"] = family_counts(ps.daily)
    ps.diagnostics["daily_ticket_by_sport"] = dict(Counter(s.event.sport for s in ps.all()))
    ps.diagnostics["daily_by_market"] = dict(Counter(f"{s.line.market}/{s.line.stat}" for s in ps.daily))
    ps.diagnostics["daily_by_league"] = dict(Counter(s.event.league for s in ps.daily))
    ps.diagnostics["top_selected_by_sport"] = dict(Counter(s.event.sport for s in ps.top))
    log.info("selection diagnostics: %s", ps.diagnostics)
    return ps
